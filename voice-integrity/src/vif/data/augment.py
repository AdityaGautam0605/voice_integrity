"""Telephony degradation chain.

The point of this module: a detector trained on clean 16 kHz audio leans on
high-frequency detail that the phone network deletes before the model ever
sees it.  Published systems go from roughly 1% EER on clean audio to 15-25%
through codecs.  Training on the degraded distribution is the contribution;
this file is where it happens.

Verify AMR-NB encoding exists before relying on it:

    ffmpeg -encoders | grep amr

Stock builds often decode but cannot encode AMR-NB, in which case that codec
silently does nothing.  `check_ffmpeg_codecs()` turns that into a loud warning.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from vif.common.config import AugmentConfig, CodecSpec
from vif.common.logging import get_logger

log = get_logger(__name__)

_CODEC_ARGS: dict[str, dict] = {
    # G.711 mu-law: the landline path.  8 kHz, ~300-3400 Hz passband.
    "g711_ulaw": {"ext": "wav", "args": ["-acodec", "pcm_mulaw"]},
    "g711_alaw": {"ext": "wav", "args": ["-acodec", "pcm_alaw"]},
    # AMR narrowband: the mobile path.  Needs libopencore-amrnb to ENCODE.
    "amr_nb": {"ext": "amr", "args": ["-acodec", "libopencore_amrnb"]},
    # Opus at a low bitrate: VoIP and messaging apps.
    "opus": {"ext": "opus", "args": ["-acodec", "libopus"]},
    "gsm": {"ext": "gsm", "args": ["-acodec", "libgsm"]},
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def check_ffmpeg_codecs(codecs: list[CodecSpec]) -> dict[str, bool]:
    """Report which configured codecs this ffmpeg build can actually encode.

    Call this at the start of a training run.  A missing encoder is a silent
    failure otherwise: the augmentation runs, changes nothing, and the whole
    thesis of the project quietly evaporates.
    """
    if not ffmpeg_available():
        log.error("ffmpeg not found on PATH - codec augmentation is disabled")
        return {c.name: False for c in codecs}

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    listing = proc.stdout + proc.stderr
    status: dict[str, bool] = {}
    for spec in codecs:
        entry = _CODEC_ARGS.get(spec.name)
        if entry is None:
            status[spec.name] = False
            log.warning("unknown codec in config: %s", spec.name)
            continue
        encoder = entry["args"][1] if len(entry["args"]) > 1 else ""
        ok = encoder in listing or encoder.startswith("pcm_")
        status[spec.name] = ok
        if not ok:
            log.warning(
                "ffmpeg cannot encode %s (encoder %s missing) - "
                "install libopencore-amrnb or drop this codec from augment.yaml",
                spec.name,
                encoder,
            )
    return status


def apply_codec(
    wav: np.ndarray,
    sample_rate: int,
    spec: CodecSpec,
) -> np.ndarray:
    """Round-trip audio through a real codec and back to the model's rate.

    The resample back to 16 kHz does not restore the discarded band.  It only
    restores the geometry the model expects, which is the whole point: the
    model must learn to work with the information that survives.
    """
    entry = _CODEC_ARGS.get(spec.name)
    if entry is None or not ffmpeg_available():
        return wav

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "in.wav"
        mid = tmp_dir / f"mid.{entry['ext']}"
        dst = tmp_dir / "out.wav"

        _write_wav(src, wav, sample_rate)

        encode = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        encode += ["-ar", str(spec.rate), "-ac", "1"]
        encode += entry["args"]
        if spec.bitrate:
            encode += ["-b:a", str(spec.bitrate)]
        encode += [str(mid)]

        decode = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(mid),
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            str(dst),
        ]

        try:
            subprocess.run(encode, check=True, capture_output=True)
            subprocess.run(decode, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            log.debug("codec %s failed, passing audio through: %s", spec.name, exc)
            return wav

        return _read_wav(dst)


def drop_packets(
    wav: np.ndarray,
    sample_rate: int,
    span_ms: tuple[float, float],
    max_drops: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Zero short spans to imitate packet loss.

    Real loss is concealed by the jitter buffer rather than silenced, so this
    is a crude stand-in.  It is still worth doing: it stops the model relying
    on perfectly continuous framing.
    """
    out = wav.copy()
    n_drops = int(rng.integers(1, max_drops + 1))
    for _ in range(n_drops):
        span = rng.uniform(span_ms[0], span_ms[1]) / 1000.0
        length = int(span * sample_rate)
        if length >= len(out):
            continue
        start = int(rng.integers(0, len(out) - length))
        out[start : start + length] = 0.0
    return out


def add_noise(
    wav: np.ndarray,
    snr_db: float,
    rng: np.random.Generator,
) -> np.ndarray:
    signal_power = float(np.mean(wav**2))
    if signal_power <= 0:
        return wav
    noise_power = signal_power / (10 ** (snr_db / 10.0))
    noise = rng.normal(0.0, np.sqrt(noise_power), size=wav.shape).astype(wav.dtype)
    return wav + noise


class CodecAugmenter:
    """Stochastic degradation pipeline, applied per utterance at training time.

    Instantiate once and reuse: it caches the codec availability check so the
    ffmpeg probe does not run on every sample.
    """

    def __init__(self, config: AugmentConfig, sample_rate: int = 16000, seed: int | None = None):
        self.config = config
        self.sample_rate = sample_rate
        self.rng = np.random.default_rng(seed)
        self.available = check_ffmpeg_codecs(config.codecs) if config.codecs else {}
        self.usable = [c for c in config.codecs if self.available.get(c.name, False)]
        if config.codecs and not self.usable:
            log.error(
                "no configured codec is usable - augmentation will only add noise "
                "and packet loss.  The codec-robustness result depends on this."
            )

    def __call__(self, wav: np.ndarray) -> tuple[np.ndarray, str]:
        """Return degraded audio and the condition label describing it."""
        if self.rng.random() > self.config.apply_probability:
            return wav, "clean"

        condition_parts: list[str] = []
        out = wav

        if self.usable:
            weights = np.array([c.weight for c in self.usable], dtype=float)
            weights = weights / weights.sum()
            spec = self.usable[int(self.rng.choice(len(self.usable), p=weights))]
            out = apply_codec(out, self.sample_rate, spec)
            condition_parts.append(spec.name)

        pl = self.config.packet_loss
        if self.rng.random() < pl.probability:
            out = drop_packets(
                out, self.sample_rate, (pl.span_ms[0], pl.span_ms[1]), pl.max_drops, self.rng
            )
            condition_parts.append("loss")

        nz = self.config.noise
        if self.rng.random() < nz.probability:
            snr = float(self.rng.uniform(nz.snr_db[0], nz.snr_db[1]))
            out = add_noise(out, snr, self.rng)
            condition_parts.append("noise")

        # Length can drift by a few samples through the codec round trip.
        out = _match_length(out, len(wav))
        return out, "+".join(condition_parts) if condition_parts else "clean"


# --------------------------------------------------------------------------
# Small local wav helpers, kept here so this module has no torch dependency
# --------------------------------------------------------------------------


def _write_wav(path: Path, wav: np.ndarray, sample_rate: int) -> None:
    import soundfile as sf

    sf.write(str(path), np.clip(wav, -1.0, 1.0), sample_rate, subtype="PCM_16")


def _read_wav(path: Path) -> np.ndarray:
    import soundfile as sf

    data, _ = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32)


def _match_length(wav: np.ndarray, target: int) -> np.ndarray:
    if len(wav) == target:
        return wav
    if len(wav) > target:
        return wav[:target]
    return np.pad(wav, (0, target - len(wav)))
