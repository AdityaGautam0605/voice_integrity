"""Per-session state: the spine.

One `CallSession` per analysis session, born when the session is created and
destroyed at teardown.  Everything global is read-only and shared (models,
config); everything per-session lives here, which is what lets one process
serve several concurrent sessions without them contaminating each other.

Three things this file gets deliberately right:

**Inference runs off the event loop.**  `run_in_executor` is not an
optimisation, it is a correctness requirement.  Calling torch inline would
stall frame reception and drop audio.

**Teardown runs on every exit path.**  The privacy claim of this system is
implemented in a `finally` block, not in a policy document.  When someone asks
what happens to the audio, the answer is a function you can point at.

**Backpressure drops, never queues.**  If inference is slower than the hop,
unbounded queueing makes the reported score fall progressively further behind
the live session while everything looks healthy.  Stale windows are discarded
and the drop rate is surfaced as a metric.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from vif.common.config import AppConfig
from vif.common.logging import get_logger
from vif.common.types import (
    Side,
    StreamMessage,
    TurnEvent,
    TurnEventKind,
    VerdictPayload,
)
from vif.serve.adapters.base import AudioFrame, IngestAdapter
from vif.serve.challenge import Challenge, generate_challenge
from vif.serve.detector import BaseDetector
from vif.serve.policy import Decision, PolicyEngine
from vif.serve.ringbuffer import SpeechRingBuffer
from vif.serve.scoring import Scorer
from vif.serve.vad import BaseVAD

log = get_logger(__name__)

StreamCallback = Callable[[dict], Awaitable[None]]


@dataclass
class SessionStats:
    windows_scored: int = 0
    windows_dropped: int = 0
    frames_ingested: int = 0
    inference_ms_total: float = 0.0
    last_inference_ms: float = 0.0

    @property
    def mean_inference_ms(self) -> float:
        return self.inference_ms_total / max(self.windows_scored, 1)

    def as_dict(self) -> dict:
        return {
            "windows_scored": self.windows_scored,
            "windows_dropped": self.windows_dropped,
            "frames_ingested": self.frames_ingested,
            "mean_inference_ms": round(self.mean_inference_ms, 2),
            "last_inference_ms": round(self.last_inference_ms, 2),
        }


class CallSession:
    """Owns all per-session state for the lifetime of one session."""

    def __init__(
        self,
        session_id: str,
        config: AppConfig,
        detector: BaseDetector,
        vad: BaseVAD,
        on_message: StreamCallback | None = None,
        calibrator=None,
        speaker_id: str | None = None,
        vault=None,
    ):
        self.session_id = session_id
        self.config = config
        self.detector = detector
        self.vad = vad
        self.on_message = on_message
        self.speaker_id = speaker_id
        self.vault = vault

        audio = config.model.audio
        self.buffer = SpeechRingBuffer(audio.window_samples, audio.hop_samples)
        self.scorer = Scorer(config.model.scoring, calibrator)
        self.policy = PolicyEngine(config.policy)

        self.liveness = None
        if config.model.liveness.enabled:
            from vif.serve.liveness import LivenessBranch

            self.liveness = LivenessBranch(config.model.liveness)

        self._in_speech: dict[Side, bool] = {Side.CALLER: False, Side.AGENT: False}
        # Transport frames (20 ms = 320 samples at 16 kHz) are smaller than a
        # VAD frame (512), so audio is staged per side until a whole VAD frame
        # is available.  `_stage_ts` tracks the arrival time of sample 0 of the
        # staged buffer, which keeps turn timestamps accurate across the
        # boundary between two transport frames.
        self._stage: dict[Side, np.ndarray] = {
            Side.CALLER: np.zeros(0, dtype=np.float32),
            Side.AGENT: np.zeros(0, dtype=np.float32),
        }
        self._stage_ts: dict[Side, int] = {Side.CALLER: 0, Side.AGENT: 0}

        self.stats = SessionStats()
        self.sequence = 0
        self.started_ns = time.monotonic_ns()
        self.challenge: Challenge | None = None
        self.last_decision: Decision | None = None
        self._inference_busy = False
        self._closed = False

        log.info("session %s opened", session_id)

    # -- ingest ------------------------------------------------------------

    async def consume(self, adapter: IngestAdapter, side: Side = Side.CALLER) -> None:
        """Drain one direction until it ends."""
        async for frame in adapter.frames(side):
            if self._closed:
                return
            await self.handle_frame(frame)

    async def handle_frame(self, frame: AudioFrame) -> None:
        """Gate, buffer, and score when a window is ready."""
        self.stats.frames_ingested += 1
        side = frame.side
        audio_cfg = self.config.model.audio
        vad_frame = audio_cfg.vad_frame
        pcm = np.asarray(frame.pcm, dtype=np.float32).ravel()

        if self._stage[side].size == 0:
            self._stage_ts[side] = frame.timestamp_ns
        self._stage[side] = np.concatenate([self._stage[side], pcm])

        chunk_ns = int(vad_frame / audio_cfg.sample_rate * 1e9)

        while self._stage[side].size >= vad_frame:
            chunk = self._stage[side][:vad_frame]
            chunk_ts = self._stage_ts[side]
            self._stage[side] = self._stage[side][vad_frame:]
            self._stage_ts[side] = chunk_ts + chunk_ns

            speaking = self.vad.is_speech(chunk, audio_cfg.vad_threshold)

            if speaking != self._in_speech[side]:
                self._in_speech[side] = speaking
                if self.liveness is not None:
                    self.liveness.record(
                        TurnEvent(
                            side=side,
                            kind=TurnEventKind.START if speaking else TurnEventKind.END,
                            monotonic_ns=chunk_ts,
                        )
                    )

            # Only the caller feeds the detector.  The agent side, when
            # present, exists solely as the liveness reference.
            if speaking and side == Side.CALLER:
                self.buffer.append(chunk)

        await self._maybe_score()

    async def _maybe_score(self) -> None:
        """Score every ready window, dropping stale ones under backpressure."""
        while self.buffer.ready:
            window = self.buffer.take_window()
            if window is None:
                return
            if self._inference_busy:
                self.stats.windows_dropped += 1
                continue
            await self._score_window(window)

    async def _score_window(self, window: np.ndarray) -> None:
        loop = asyncio.get_running_loop()
        self._inference_busy = True
        started = time.perf_counter()
        try:
            # THE critical line: inference must not run on the event loop.
            raw = await loop.run_in_executor(None, self.detector.score_window, window)
        except Exception as exc:  # noqa: BLE001
            log.error("inference failed on session %s: %s", self.session_id, exc)
            return
        finally:
            self._inference_busy = False

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.stats.inference_ms_total += elapsed_ms
        self.stats.last_inference_ms = elapsed_ms
        self.stats.windows_scored += 1

        self.scorer.update_spoof(raw)
        self.scorer.update_speaker(await self._speaker_similarity(window, loop))

        if self.liveness is not None:
            _features, score = self.liveness.snapshot(self._now_s())
            self.scorer.update_liveness(score)

        await self._emit()

    async def _speaker_similarity(self, window: np.ndarray, loop) -> float | None:
        """Independent branch.  Abstains when there is no enrolment."""
        if self.vault is None or self.speaker_id is None:
            return None
        if not self.vault.is_enrolled(self.speaker_id):
            return None
        embedding = await loop.run_in_executor(None, self.detector.embed_speaker, window)
        if embedding is None:
            return None
        return self.vault.match(self.speaker_id, embedding)

    # -- output ------------------------------------------------------------

    async def _emit(self) -> None:
        probability = self.scorer.spoof_probability
        risk = self.scorer.risk()
        speaker_status = self.scorer.speaker_status

        decision = self.policy.evaluate(risk, probability, speaker_status)
        self.last_decision = decision

        if (
            self.config.policy.challenge_on_amber
            and decision.action.value == "CHALLENGE"
            and self.challenge is None
        ):
            self.challenge = generate_challenge()
            log.info("session %s crossed amber - challenge issued", self.session_id)

        message = StreamMessage(
            session_id=self.session_id,
            sequence=self.sequence,
            speech_seconds=round(
                self.buffer.speech_seconds(self.config.model.audio.sample_rate), 2
            ),
            spoof_probability=round(probability, 4),
            risk=risk,
            speaker_similarity=(
                round(self.scorer.state.speaker_similarity, 4)
                if self.scorer.state.speaker_similarity is not None
                else None
            ),
            speaker_status=speaker_status,
            inference_ms=round(self.stats.last_inference_ms, 1),
            model_version=self.detector.model_version,
            liveness_score=self.scorer.state.liveness_score,
        )
        self.sequence += 1

        if self.on_message is not None:
            payload = message.model_dump(mode="json")
            payload["decision"] = decision.as_dict()
            payload["calibrated"] = self.scorer.calibrated
            if self.challenge is not None:
                payload["challenge"] = self.challenge.as_dict()
            await self.on_message(payload)

    # -- teardown ----------------------------------------------------------

    def finalise(self) -> VerdictPayload:
        """Close the session and build the verdict payload."""
        liveness_features = None
        if self.liveness is not None:
            liveness_features, score = self.liveness.finalise(self._now_s())
            self.scorer.update_liveness(score)

        probability = self.scorer.spoof_probability
        risk = self.scorer.risk()
        speaker_status = self.scorer.speaker_status
        decision = self.policy.evaluate(risk, probability, speaker_status)
        self.last_decision = decision

        return VerdictPayload(
            session_id=self.session_id,
            spoof_probability=round(probability, 4),
            risk=risk,
            speaker_status=speaker_status,
            speaker_similarity=self.scorer.state.speaker_similarity,
            action=decision.action,
            speech_seconds=round(
                self.buffer.speech_seconds(self.config.model.audio.sample_rate), 2
            ),
            windows_scored=self.stats.windows_scored,
            liveness=liveness_features,
            model_version=self.detector.model_version,
            model_checksum=getattr(self.detector, "model_checksum", ""),
            policy_version=self.config.policy.version,
            nonce=secrets.token_hex(16),
        )

    def close(self) -> None:
        """Release everything sensitive.

        Best-effort zeroing: a managed runtime cannot guarantee erasure, since
        the collector relocates objects and freed pages carry no promise.
        Documented as a mitigation rather than a control.
        """
        if self._closed:
            return
        self._closed = True
        self.buffer.zeroise()
        self._stage = {
            Side.CALLER: np.zeros(0, dtype=np.float32),
            Side.AGENT: np.zeros(0, dtype=np.float32),
        }
        self.vad.reset()
        log.info("session %s closed - %s", self.session_id, self.stats.as_dict())

    def _now_s(self) -> float:
        return time.monotonic_ns() / 1e9

    def __enter__(self) -> CallSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
