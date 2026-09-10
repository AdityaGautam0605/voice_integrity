"""Ingest adapter interface (L0).

Every media source is reduced to the same thing: labelled, resampled frames
with an RTP-layer timestamp.  Swapping WebRTC for SIPREC or a hosted media
stream should change nothing above this line (FR-IN-07).

Two properties every adapter must preserve:

**Bidirectional.**  Both call directions arrive as separately labelled
streams, because the liveness branch needs a known-human reference in the same
call.

**RTP-layer timestamps, not playout.**  Timestamping at playout measures their
processing plus the network plus *our own adaptive jitter buffer*, which
drifts with network conditions - reintroducing exactly the confound the
liveness branch was designed to eliminate (FR-IN-05).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np

from vif.common.types import Side


@dataclass
class AudioFrame:
    """One decoded chunk from one side of the call."""

    side: Side
    pcm: np.ndarray  # float32 mono at the internal rate
    timestamp_ns: int  # monotonic, taken at RTP arrival

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / 16000.0


class IngestAdapter(ABC):
    """Yields frames until the call ends."""

    sample_rate: int = 16000

    @abstractmethod
    def frames(self, side: Side) -> AsyncIterator[AudioFrame]:
        """Async iterator over one direction of the call."""

    @abstractmethod
    async def close(self) -> None: ...

    async def rtt_ms(self) -> float | None:
        """Transport round-trip time.

        Taken from RTCP or the equivalent, never estimated acoustically: an
        echo probe would be fighting a well-tuned adaptive echo canceller for
        a number the stack already reports (FR-IN-06).
        """
        return None


def now_ns() -> int:
    """Monotonic clock for all timestamping.

    `monotonic_ns` rather than wall clock: NTP adjustments mid-call would
    otherwise create phantom gaps and overlaps in the turn record.
    """
    return time.monotonic_ns()


def to_mono_float32(pcm: np.ndarray) -> np.ndarray:
    """Normalise any incoming array to mono float32 in [-1, 1]."""
    arr = np.asarray(pcm)
    if arr.ndim > 1:
        arr = arr.mean(axis=-1) if arr.shape[-1] <= 2 else arr.mean(axis=0)
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32768.0
    elif arr.dtype == np.int32:
        arr = arr.astype(np.float32) / 2147483648.0
    else:
        arr = arr.astype(np.float32)
    return np.ascontiguousarray(arr)
