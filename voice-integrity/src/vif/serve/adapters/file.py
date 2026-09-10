"""File and synthetic ingest.

Two adapters that need no network and no media stack:

    FileAdapter       replays audio files as if they were a live call
    SyntheticAdapter  generates a scripted conversation with known turn timing

The second is how the whole pipeline is smoke-tested without a single
download, and how liveness features are validated against ground truth that we
actually control - including a configurable pipeline floor, which is the thing
the branch is supposed to detect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from vif.common.logging import get_logger
from vif.common.types import Side
from vif.serve.adapters.base import AudioFrame, IngestAdapter, now_ns, to_mono_float32

log = get_logger(__name__)


class FileAdapter(IngestAdapter):
    """Replay audio files at wall-clock speed, or as fast as possible."""

    def __init__(
        self,
        caller_path: str | Path,
        agent_path: str | Path | None = None,
        sample_rate: int = 16000,
        frame_ms: float = 20.0,
        realtime: bool = True,
    ):
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000.0)
        self.realtime = realtime
        self._audio: dict[Side, np.ndarray] = {Side.CALLER: self._load(caller_path)}
        if agent_path is not None:
            self._audio[Side.AGENT] = self._load(agent_path)

    def _load(self, path: str | Path) -> np.ndarray:
        from vif.data.datasets import load_audio

        return load_audio(path, self.sample_rate)

    async def frames(self, side: Side) -> AsyncIterator[AudioFrame]:
        audio = self._audio.get(side)
        if audio is None:
            return
        frame_s = self.frame_samples / self.sample_rate
        base_ns = now_ns()
        for start in range(0, len(audio) - self.frame_samples + 1, self.frame_samples):
            chunk = audio[start : start + self.frame_samples]
            # Media clock, not wall clock.  Replaying a file faster than real
            # time must not compress the apparent conversation - otherwise the
            # liveness branch measures our replay speed instead of the call.
            yield AudioFrame(
                side=side,
                pcm=chunk,
                timestamp_ns=base_ns + int(start / self.sample_rate * 1e9),
            )
            await asyncio.sleep(frame_s if self.realtime else 0)

    async def close(self) -> None:
        self._audio.clear()


class SyntheticAdapter(IngestAdapter):
    """A scripted two-party conversation with controllable turn timing.

    `pipeline_floor_ms` is the point of this class.  Set it above zero and the
    caller side can never respond faster than that, simulating the buffering
    delay of a real-time voice converter - which is exactly the hard floor the
    liveness branch looks for.  Set it to zero and the caller behaves like a
    human, including occasional sub-100ms backchannels and genuine overlap.
    """

    def __init__(
        self,
        n_turns: int = 12,
        sample_rate: int = 16000,
        frame_ms: float = 20.0,
        pipeline_floor_ms: float = 0.0,
        overlap_probability: float = 0.18,
        seed: int = 0,
        realtime: bool = False,
    ):
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000.0)
        self.realtime = realtime
        self.rng = np.random.default_rng(seed)
        self.pipeline_floor_ms = pipeline_floor_ms
        self.overlap_probability = overlap_probability
        self.n_turns = n_turns
        self._timeline = self._build_timeline()
        self._base_ns = now_ns()

    def _gap_ms(self, to_side: Side) -> float:
        """Sample a response gap.

        The agent always behaves like a human.  The caller is subject to the
        configured floor, which is what makes the two sides differ in a way
        the within-call comparison can see.
        """
        if to_side == Side.AGENT or self.pipeline_floor_ms <= 0:
            if self.rng.random() < self.overlap_probability:
                return float(self.rng.uniform(-250.0, -10.0))  # genuine overlap
            if self.rng.random() < 0.2:
                return float(self.rng.uniform(20.0, 140.0))  # fast backchannel
            return float(self.rng.gamma(shape=2.0, scale=140.0))
        # Machine in the loop: a hard floor, and compressed variance.
        return float(self.pipeline_floor_ms + abs(self.rng.normal(60.0, 45.0)))

    def _build_timeline(self) -> list[tuple[Side, float, float]]:
        """(side, start_s, end_s) for every utterance in the call."""
        timeline: list[tuple[Side, float, float]] = []
        clock = 0.5
        side = Side.AGENT
        for _ in range(self.n_turns):
            duration = float(self.rng.uniform(1.2, 3.0))
            timeline.append((side, clock, clock + duration))
            end = clock + duration
            side = Side.CALLER if side == Side.AGENT else Side.AGENT
            clock = end + self._gap_ms(side) / 1000.0
            clock = max(clock, 0.0)
        return timeline

    def _render(self, side: Side) -> np.ndarray:
        """Band-limited noise bursts during this side's utterances."""
        total_s = max(end for _, _, end in self._timeline) + 1.0
        audio = np.zeros(int(total_s * self.sample_rate), dtype=np.float32)
        for utt_side, start, end in self._timeline:
            if utt_side != side:
                continue
            a, b = int(start * self.sample_rate), int(end * self.sample_rate)
            b = min(b, len(audio))
            if b <= a:
                continue
            n = b - a
            t = np.arange(n) / self.sample_rate
            # A couple of formant-ish tones plus noise: enough to trip a VAD.
            signal = (
                0.35 * np.sin(2 * np.pi * 220.0 * t)
                + 0.25 * np.sin(2 * np.pi * 720.0 * t)
                + 0.12 * self.rng.normal(size=n)
            )
            envelope = np.hanning(n) ** 0.3
            audio[a:b] += (signal * envelope).astype(np.float32)
        return np.clip(audio, -1.0, 1.0)

    async def frames(self, side: Side) -> AsyncIterator[AudioFrame]:
        audio = self._render(side)
        frame_s = self.frame_samples / self.sample_rate
        # One shared base for both sides, so the two directions stay on a
        # common clock.  Without that, the gap between an agent utterance and
        # the caller's reply would be measured against two unrelated origins.
        base_ns = self._base_ns
        for start in range(0, len(audio) - self.frame_samples + 1, self.frame_samples):
            yield AudioFrame(
                side=side,
                pcm=audio[start : start + self.frame_samples],
                timestamp_ns=base_ns + int(start / self.sample_rate * 1e9),
            )
            await asyncio.sleep(frame_s if self.realtime else 0)

    async def close(self) -> None:
        self._timeline.clear()

    async def rtt_ms(self) -> float | None:
        return 45.0

    def ground_truth(self) -> list[tuple[str, float, float]]:
        """Utterance timing, for validating the turn tracker against truth."""
        return [(s.value, a, b) for s, a, b in self._timeline]


def to_frames(
    audio: np.ndarray,
    side: Side,
    frame_samples: int = 320,
) -> list[AudioFrame]:
    """Chop a whole array into frames.  Used by tests and offline scoring."""
    audio = to_mono_float32(audio)
    out = []
    for start in range(0, len(audio) - frame_samples + 1, frame_samples):
        out.append(
            AudioFrame(side=side, pcm=audio[start : start + frame_samples], timestamp_ns=now_ns())
        )
    return out
