"""Voice activity gate (L1).

Two jobs, and the second is the one people forget.

1. Keep silence out of the analysis buffer, so a window always contains 4.04
   seconds of *speech* rather than 4.04 seconds of wall clock.  A hesitant
   caller therefore takes longer to reach a score, which is correct behaviour.
2. Emit speech boundaries for the liveness branch.  Those timestamps are that
   branch's entire feature set.

`EnergyVAD` is a dependency-free fallback so the pipeline, its tests and the
synthetic smoke path all run without downloading a model.  Silero is the
default in production.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from vif.common.logging import get_logger

log = get_logger(__name__)


class BaseVAD(ABC):
    """Operates on fixed-size frames and returns a speech probability."""

    frame_size: int = 512
    sample_rate: int = 16000

    @abstractmethod
    def probability(self, frame: np.ndarray) -> float: ...

    def is_speech(self, frame: np.ndarray, threshold: float = 0.5) -> bool:
        return self.probability(frame) >= threshold

    def reset(self) -> None:
        """Clear per-call state.  Called at session teardown."""


class EnergyVAD(BaseVAD):
    """Adaptive-threshold energy gate.

    Tracks a running noise floor and calls a frame speech when it sits far
    enough above it.  Crude next to a neural VAD, but deterministic, instant,
    and good enough to keep tests honest.
    """

    def __init__(
        self,
        frame_size: int = 512,
        sample_rate: int = 16000,
        margin_db: float = 12.0,
        floor_alpha: float = 0.02,
    ):
        self.frame_size = frame_size
        self.sample_rate = sample_rate
        self.margin_db = margin_db
        self.floor_alpha = floor_alpha
        self.noise_floor_db = -60.0
        self._initialised = False

    def reset(self) -> None:
        self.noise_floor_db = -60.0
        self._initialised = False

    def probability(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(frame)) + 1e-12))
        level_db = 20.0 * np.log10(rms + 1e-12)

        if not self._initialised:
            self.noise_floor_db = level_db
            self._initialised = True

        above = level_db - self.noise_floor_db
        speech = above > self.margin_db

        # Only adapt the floor on non-speech, or a loud talker walks it upward.
        if not speech:
            self.noise_floor_db = (
                1 - self.floor_alpha
            ) * self.noise_floor_db + self.floor_alpha * level_db

        # Soft probability so a threshold change behaves sensibly.
        return float(np.clip(above / (2.0 * self.margin_db), 0.0, 1.0))


class SileroVAD(BaseVAD):
    """Neural VAD.  Expects exactly 512 samples per frame at 16 kHz."""

    def __init__(self, frame_size: int = 512, sample_rate: int = 16000):
        self.frame_size = frame_size
        self.sample_rate = sample_rate
        import torch

        self._torch = torch
        try:
            from silero_vad import load_silero_vad

            self.model = load_silero_vad()
        except ImportError:  # pragma: no cover - torch.hub fallback
            self.model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad", model="silero_vad", trust_repo=True
            )
        self.model.eval()

    def reset(self) -> None:
        if hasattr(self.model, "reset_states"):
            self.model.reset_states()

    def probability(self, frame: np.ndarray) -> float:
        if len(frame) != self.frame_size:
            frame = _fit(frame, self.frame_size)
        with self._torch.inference_mode():
            tensor = self._torch.from_numpy(np.asarray(frame, dtype=np.float32))
            return float(self.model(tensor, self.sample_rate).item())


def build_vad(kind: str = "auto", frame_size: int = 512, sample_rate: int = 16000) -> BaseVAD:
    """Prefer Silero, fall back to energy so nothing hard-fails offline."""
    if kind == "energy":
        return EnergyVAD(frame_size, sample_rate)
    if kind in ("silero", "auto"):
        try:
            return SileroVAD(frame_size, sample_rate)
        except Exception as exc:  # noqa: BLE001 - any import/download failure
            if kind == "silero":
                raise
            log.warning("Silero VAD unavailable (%s) - falling back to energy VAD", exc)
            return EnergyVAD(frame_size, sample_rate)
    raise ValueError(f"unknown VAD kind: {kind}")


def _fit(frame: np.ndarray, size: int) -> np.ndarray:
    if len(frame) >= size:
        return frame[:size]
    return np.pad(frame, (0, size - len(frame)))
