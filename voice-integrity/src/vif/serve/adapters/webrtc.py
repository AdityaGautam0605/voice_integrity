"""WebRTC ingest via aiortc (L0).

Terminating WebRTC server-side rather than in the browser is what gives us
RTP-level timestamps and decoded frames in the same process as the session
layer.  Browser-side encoded-frame access exists but support is uneven, and
the numbers that matter here are timing numbers.

Windows note: aiortc depends on PyAV, which needs ffmpeg development
libraries.  Build under WSL2 rather than native Windows.

Side assignment comes from transceiver order, which the client controls: the
first audio transceiver is the caller (the party under assessment), the second
is the agent (our known human, the liveness reference).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from vif.common.logging import get_logger
from vif.common.types import Side
from vif.serve.adapters.base import AudioFrame, IngestAdapter, now_ns

log = get_logger(__name__)


class WebRTCAdapter(IngestAdapter):
    """Wraps an aiortc peer connection.

    Tracks are registered as they arrive; `frames()` waits for the matching
    side rather than assuming both are present at construction, because ICE
    and track negotiation complete asynchronously.
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._tracks: dict[Side, object] = {}
        self._ready: dict[Side, asyncio.Event] = {
            Side.CALLER: asyncio.Event(),
            Side.AGENT: asyncio.Event(),
        }
        self._pc = None
        self._resamplers: dict[Side, object] = {}

    # -- wiring ------------------------------------------------------------

    def attach_peer_connection(self, pc) -> None:
        self._pc = pc

    def register_track(self, track, side: Side) -> None:
        self._tracks[side] = track
        self._ready[side].set()
        log.info("registered %s track (%s)", side.value, getattr(track, "kind", "?"))

    def _resampler(self, side: Side):
        """One resampler per side, reused across frames.

        av.AudioResampler is stateful; constructing a new one per frame both
        wastes work and produces discontinuities at frame boundaries.
        """
        if side not in self._resamplers:
            import av

            self._resamplers[side] = av.AudioResampler(
                format="s16", layout="mono", rate=self.sample_rate
            )
        return self._resamplers[side]

    # -- ingest ------------------------------------------------------------

    async def frames(self, side: Side, wait_timeout: float = 30.0) -> AsyncIterator[AudioFrame]:
        try:
            await asyncio.wait_for(self._ready[side].wait(), timeout=wait_timeout)
        except TimeoutError:
            log.warning("no %s track arrived within %.0fs", side.value, wait_timeout)
            return

        track = self._tracks[side]
        resampler = self._resampler(side)

        while True:
            try:
                frame = await track.recv()
            except Exception as exc:  # noqa: BLE001 - MediaStreamError and friends
                log.info("%s track ended (%s)", side.value, type(exc).__name__)
                return

            # Timestamp immediately on arrival, before any decoding work.
            timestamp = now_ns()
            pcm = self._decode(frame, resampler)
            if pcm.size:
                yield AudioFrame(side=side, pcm=pcm, timestamp_ns=timestamp)

    @staticmethod
    def _decode(frame, resampler) -> np.ndarray:
        """Opus at 48 kHz stereo to mono float32 at the internal rate."""
        resampled = resampler.resample(frame)
        frames = resampled if isinstance(resampled, list) else [resampled]
        chunks = []
        for f in frames:
            if f is None:
                continue
            arr = f.to_ndarray()
            chunks.append(arr.reshape(-1))
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
        return pcm

    # -- transport ---------------------------------------------------------

    async def rtt_ms(self) -> float | None:
        """Round-trip time from RTCP.

        The liveness branch subtracts this rather than trying to recover it
        acoustically.  Free, exact, and it sidesteps a fight with the far
        end's echo canceller.
        """
        if self._pc is None:
            return None
        try:
            stats = await self._pc.getStats()
        except Exception:  # noqa: BLE001
            return None
        for report in stats.values():
            rtt = getattr(report, "roundTripTime", None)
            if rtt is not None:
                return float(rtt) * 1000.0
        return None

    async def close(self) -> None:
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception:  # noqa: BLE001
                pass
        self._tracks.clear()
        self._resamplers.clear()


def side_for_transceiver(index: int) -> Side:
    """Transceiver order defines the sides.

    Documented rather than inferred, because getting this backwards silently
    swaps which party is under assessment and which is the human reference -
    and the system would keep running, producing inverted liveness features.
    """
    return Side.CALLER if index == 0 else Side.AGENT
