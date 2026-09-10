"""Per-call session: the spine.

One `CallSession` per call, born when the offer arrives and destroyed at
teardown.  Everything global is read-only and shared (models, config);
everything per-call lives here, which is what lets one process serve many
concurrent calls without them contaminating each other.

Three things this file gets deliberately right:

**Inference runs off the event loop.**  `run_in_executor` is not an
optimisation, it is a correctness requirement.  Calling torch inline would
stall frame reception, drop audio, and smear the timestamps the liveness
branch depends on.

**Teardown runs on every exit path.**  The privacy claim of this system is
implemented in a `finally` block, not in a policy document.  When someone asks
what happens to the audio, the answer is a function you can point at.

**Backpressure drops, never queues.**  If inference is slower than the hop,
unbounded queueing makes the reported score fall progressively further behind
the live call while everything looks healthy.  Stale windows are discarded and
the drop rate is surfaced as a metric (NFR-12).
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
    Action,
    BranchScores,
    ScoreFrame,
    Side,
    TurnEvent,
    TurnEventKind,
    VerdictPayload,
)
from vif.eval.calibration import Calibrator
from vif.serve.adapters.base import AudioFrame, IngestAdapter
from vif.serve.challenge import Challenge, generate_challenge
from vif.serve.detector import BaseDetector
from vif.serve.fusion import FusionEngine, metadata_prior_llr
from vif.serve.liveness import LivenessBranch
from vif.serve.policy import Decision, PolicyEngine
from vif.serve.ringbuffer import SpeechRingBuffer
from vif.serve.vad import BaseVAD

log = get_logger(__name__)

ScoreCallback = Callable[[dict], Awaitable[None]]


@dataclass
class SessionStats:
    windows_scored: int = 0
    windows_dropped: int = 0
    frames_ingested: int = 0
    inference_ms_total: float = 0.0

    @property
    def mean_inference_ms(self) -> float:
        return self.inference_ms_total / max(self.windows_scored, 1)

    def as_dict(self) -> dict:
        return {
            "windows_scored": self.windows_scored,
            "windows_dropped": self.windows_dropped,
            "frames_ingested": self.frames_ingested,
            "mean_inference_ms": round(self.mean_inference_ms, 2),
        }


class CallSession:
    """Owns all per-call state for the lifetime of one call."""

    def __init__(
        self,
        call_id: str,
        config: AppConfig,
        detector: BaseDetector,
        vad: BaseVAD,
        on_score: ScoreCallback | None = None,
        calibrator: Calibrator | None = None,
        metadata: dict | None = None,
        speaker_id: str | None = None,
        vault=None,
    ):
        self.call_id = call_id
        self.config = config
        self.detector = detector
        self.vad = vad
        self.on_score = on_score
        self.metadata = metadata or {}
        self.speaker_id = speaker_id
        self.vault = vault

        audio = config.model.audio
        self.buffer = SpeechRingBuffer(audio.window_samples, audio.hop_samples)

        prior = metadata_prior_llr(self.metadata, config.policy)
        self.fusion = FusionEngine(config.model.fusion, calibrator, prior_llr=prior)
        self.policy = PolicyEngine(config.policy)
        self.liveness = LivenessBranch(config.model.liveness)

        self._in_speech: dict[Side, bool] = {Side.CALLER: False, Side.AGENT: False}
        self._speech_frames: dict[Side, int] = {Side.CALLER: 0, Side.AGENT: 0}
        # Transport frames (20 ms = 320 samples at 16 kHz) are smaller than a
        # VAD frame (512), so audio is staged per side until a whole VAD frame
        # is available.  `_stage_ts` tracks the arrival time of sample 0 of the
        # staged buffer, which is what keeps turn timestamps accurate across
        # the boundary between two transport frames.
        self._stage: dict[Side, np.ndarray] = {
            Side.CALLER: np.zeros(0, dtype=np.float32),
            Side.AGENT: np.zeros(0, dtype=np.float32),
        }
        self._stage_ts: dict[Side, int] = {Side.CALLER: 0, Side.AGENT: 0}
        self.stats = SessionStats()
        self.seq = 0
        self.started_ns = time.monotonic_ns()
        self.challenge: Challenge | None = None
        self.last_decision: Decision | None = None
        self._inference_busy = False
        self._closed = False

        log.info("session %s opened (prior_llr=%.2f)", call_id, prior)

    # -- ingest ------------------------------------------------------------

    async def consume(self, adapter: IngestAdapter, side: Side) -> None:
        """Drain one direction of the call until it ends.

        Launched once per side, so both directions are ingested concurrently.
        The liveness branch needs both.
        """
        async for frame in adapter.frames(side):
            if self._closed:
                return
            await self.handle_frame(frame)

    async def handle_frame(self, frame: AudioFrame) -> None:
        """Gate, buffer, and score when a window is ready."""
        self.stats.frames_ingested += 1
        side = frame.side
        vad_frame = self.config.model.audio.vad_frame
        threshold = self.config.model.audio.vad_threshold
        sample_rate = self.config.model.audio.sample_rate
        pcm = np.asarray(frame.pcm, dtype=np.float32).ravel()

        # Stage incoming audio until a whole VAD frame is available.
        if self._stage[side].size == 0:
            self._stage_ts[side] = frame.timestamp_ns
        self._stage[side] = np.concatenate([self._stage[side], pcm])

        chunk_ns = int(vad_frame / sample_rate * 1e9)

        while self._stage[side].size >= vad_frame:
            chunk = self._stage[side][:vad_frame]
            chunk_ts = self._stage_ts[side]
            self._stage[side] = self._stage[side][vad_frame:]
            self._stage_ts[side] = chunk_ts + chunk_ns

            speaking = self.vad.is_speech(chunk, threshold)

            # L1 -> Branch D: record the boundary, discard the audio.
            if speaking != self._in_speech[side]:
                self._in_speech[side] = speaking
                self.liveness.record(
                    TurnEvent(
                        side=side,
                        kind=TurnEventKind.START if speaking else TurnEventKind.END,
                        monotonic_ns=chunk_ts,
                    )
                )

            # Only the caller feeds the acoustic model.  The agent side exists
            # to give the liveness branch a known-human reference.
            if speaking and side == Side.CALLER:
                self._speech_frames[side] += 1
                self.buffer.append(chunk)

        await self._maybe_score()

    async def _maybe_score(self) -> None:
        """Score every ready window, dropping stale ones under backpressure."""
        while self.buffer.ready:
            window = self.buffer.take_window()
            if window is None:
                return
            if self._inference_busy:
                # Drop rather than queue.  A growing backlog makes the score
                # silently lag the live call while looking healthy.
                self.stats.windows_dropped += 1
                continue
            await self._score_window(window)

    async def _score_window(self, window: np.ndarray) -> None:
        loop = asyncio.get_running_loop()
        self._inference_busy = True
        started = time.perf_counter()
        try:
            # THE critical line: inference must not run on the event loop.
            spoof = await loop.run_in_executor(None, self.detector.score_window, window)
        except Exception as exc:  # noqa: BLE001
            log.error("inference failed on call %s: %s", self.call_id, exc)
            return
        finally:
            self._inference_busy = False

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.stats.inference_ms_total += elapsed_ms
        self.stats.windows_scored += 1

        speaker = await self._speaker_score(window, loop)
        prosody = self._prosody_score(window)

        scores = BranchScores(spoof=spoof, speaker=speaker, prosody=prosody)
        self.fusion.update(scores)

        # Liveness accrues per turn, not per window; refresh the snapshot.
        _features, liveness_llr = self.liveness.snapshot(self._now_s())
        self.fusion.set_liveness(liveness_llr)
        scores.liveness = liveness_llr

        await self._emit(scores)

    async def _speaker_score(self, window: np.ndarray, loop) -> float | None:
        """Branch B abstains when there is no enrolment (FR-DE-03)."""
        if self.vault is None or self.speaker_id is None:
            return None
        if not self.vault.is_enrolled(self.speaker_id):
            return None
        embedding = await loop.run_in_executor(None, self.detector.embed_speaker, window)
        if embedding is None:
            return None
        similarity = self.vault.match(self.speaker_id, embedding)
        if similarity is None:
            return None
        # Cosine similarity is high for a match; the score must be oriented so
        # that higher means more suspicious.
        return float(-similarity)

    def _prosody_score(self, window: np.ndarray) -> float | None:
        """Thin prosody signal, deliberately never decisive.

        Language-dependent by nature, so its fusion weight is capped in config
        and it cannot move the policy band on its own (FR-DE-04).
        """
        if not self.config.model.prosody.enabled:
            return None
        from vif.serve.prosody import prosody_score

        return prosody_score(window, self.config.model.audio.sample_rate)

    # -- output ------------------------------------------------------------

    async def _emit(self, scores: BranchScores) -> None:
        risk = self.fusion.risk
        decision = self.policy.evaluate(risk, self.metadata.get("transaction_value"))
        self.last_decision = decision

        if decision.action == Action.CHALLENGE and self.challenge is None:
            self.challenge = generate_challenge()
            log.info("call %s crossed amber - challenge issued", self.call_id)

        frame = ScoreFrame(
            call_id=self.call_id,
            seq=self.seq,
            risk=round(risk, 2),
            ci=round(self.fusion.confidence_interval, 2),
            band=decision.band,
            branches=scores,
            speech_s=round(self.buffer.speech_seconds(self.config.model.audio.sample_rate), 2),
            model_version=self.detector.model_version,
        )
        self.seq += 1

        if self.on_score is not None:
            payload = frame.model_dump(mode="json")
            payload["decision"] = decision.as_dict()
            if self.challenge is not None:
                payload["challenge"] = self.challenge.as_dict()
            await self.on_score(payload)

    # -- teardown ----------------------------------------------------------

    def finalise(self) -> VerdictPayload:
        """Close the call and build the verdict payload.

        Liveness runs once here over the collected timestamps, which is why it
        costs nothing during the call - and why it can produce a result even
        on a call where the acoustic branch never got enough speech.
        """
        features, liveness_llr = self.liveness.finalise(self._now_s())
        self.fusion.set_liveness(liveness_llr)

        risk = self.fusion.risk
        decision = self.policy.evaluate(risk, self.metadata.get("transaction_value"))
        self.last_decision = decision

        return VerdictPayload(
            call_id=self.call_id,
            risk=round(risk, 2),
            band=decision.band,
            branches=BranchScores(
                spoof=self.fusion.state.per_branch_llr.get("spoof"),
                speaker=self.fusion.state.per_branch_llr.get("speaker"),
                prosody=self.fusion.state.per_branch_llr.get("prosody"),
                liveness=liveness_llr,
            ),
            liveness=features,
            speech_s=round(self.buffer.speech_seconds(self.config.model.audio.sample_rate), 2),
            n_windows=self.fusion.state.n_windows,
            model_version=self.detector.model_version,
            policy_version=self.config.policy.version,
            action=decision.action,
            nonce=secrets.token_hex(16),
        )

    def close(self) -> None:
        """Release everything sensitive.

        Best-effort zeroing: a managed runtime cannot guarantee erasure, since
        the collector relocates objects and freed pages carry no promise.
        Documented as a mitigation rather than a control (SEC-18).
        """
        if self._closed:
            return
        self._closed = True
        self.buffer.zeroise()
        self.vad.reset()
        log.info("session %s closed - %s", self.call_id, self.stats.as_dict())

    def _now_s(self) -> float:
        return time.monotonic_ns() / 1e9

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> CallSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
