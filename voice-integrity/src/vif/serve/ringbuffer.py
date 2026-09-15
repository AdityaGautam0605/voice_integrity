"""Bounded speech buffer and window assembly (L0/L1).

The privacy guarantee of this system is a data structure, not a policy
document: the buffer is fixed length, lives only in memory, and when it wraps
the oldest audio is gone.  There is no code path that writes audio to disk.

Only speech enters, so `window_samples` is always that many samples of actual
speech.  Windows are cut at exactly the crop length the detection head was
trained on - matching inference geometry to training geometry is free
accuracy, and mismatching it is a silent regression (FR-CO-02, FR-CO-03).
"""

from __future__ import annotations

import numpy as np

from vif.common.logging import get_logger

log = get_logger(__name__)


class SpeechRingBuffer:
    """Fixed-capacity float32 buffer of speech-only audio.

    Capacity defaults to one window: nothing older than the current analysis
    window is retained, because nothing older is ever used.
    """

    def __init__(
        self, window_samples: int = 64600, hop_samples: int = 16000, capacity: int | None = None
    ):
        if hop_samples > window_samples:
            raise ValueError("hop_samples must not exceed window_samples")
        self.window_samples = window_samples
        self.hop_samples = hop_samples
        self.capacity = capacity or window_samples
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._filled = 0
        self._since_last_window = 0
        self.total_speech_samples = 0

    # -- ingest ------------------------------------------------------------

    def append(self, chunk: np.ndarray) -> None:
        """Add speech-classified audio.  Silence must never reach this."""
        chunk = np.asarray(chunk, dtype=np.float32).ravel()
        if chunk.size == 0:
            return
        self.total_speech_samples += chunk.size
        self._since_last_window += chunk.size

        if chunk.size >= self.capacity:
            self._buf[:] = chunk[-self.capacity :]
            self._filled = self.capacity
            return

        # Shift left by the incoming size, then write at the tail.
        overflow = self._filled + chunk.size - self.capacity
        if overflow > 0:
            self._buf[:-overflow] = self._buf[overflow:]
            self._filled -= overflow
        self._buf[self._filled : self._filled + chunk.size] = chunk
        self._filled += chunk.size

    # -- window assembly ---------------------------------------------------

    @property
    def ready(self) -> bool:
        """True when a full window exists and a whole hop has passed."""
        return self._filled >= self.window_samples and self._since_last_window >= self.hop_samples

    def take_window(self) -> np.ndarray | None:
        """Return the most recent window, or None if not ready.

        Returns a copy: the caller hands it to a worker thread, and the buffer
        keeps mutating underneath.
        """
        if not self.ready:
            return None
        window = self._buf[self._filled - self.window_samples : self._filled].copy()
        self._since_last_window = 0
        return window

    def windows(self):
        """Drain every window currently available."""
        while True:
            window = self.take_window()
            if window is None:
                return
            yield window

    # -- lifecycle ---------------------------------------------------------

    def speech_seconds(self, sample_rate: int = 16000) -> float:
        """Accumulated speech, distinct from wall-clock call duration."""
        return self.total_speech_samples / sample_rate

    def zeroise(self) -> None:
        """Overwrite the buffer at teardown.

        Best effort only.  A managed runtime cannot guarantee erasure - the
        garbage collector relocates objects and freed pages carry no promise -
        so this is documented as a mitigation, not a control.  See SEC-18.
        """
        self._buf[:] = 0.0
        self._filled = 0
        self._since_last_window = 0

    def __len__(self) -> int:
        return self._filled
