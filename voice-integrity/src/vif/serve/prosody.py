"""Prosody branch (C).

Present for problem-statement coverage and dashboard richness, and capped so
it can never carry a decision on its own.  The reasoning is worth stating
plainly, because "we built it and then constrained it" reads very differently
from "we forgot about it":

1. **The SSL front end already encodes prosody.**  wav2vec2 representations
   carry pitch, rhythm and timing implicitly.  A hand-crafted prosody branch
   mostly re-derives, worse, what the model already has.

2. **It is language-dependent.**  Hindi, Tamil, Bengali and Indian English
   have very different rhythm and intonation, so an absolute prosody model
   trained on one flags natural speakers of another as abnormal.  That is a
   false-alarm factory across exactly the languages that matter here.

So: half a day of work, a small fusion weight, and an ablation in the report
showing it added language-dependence without adding discrimination.  Shipping
that ablation is stronger than shipping the branch.

Everything here is pure numpy, so it costs effectively nothing per window.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ProsodyFeatures:
    f0_mean: float
    f0_std: float
    f0_range: float
    voiced_ratio: float
    energy_std: float
    speaking_rate: float
    jitter: float

    def as_dict(self) -> dict:
        return {k: round(float(v), 5) for k, v in self.__dict__.items()}


def estimate_f0(
    wav: np.ndarray,
    sample_rate: int = 16000,
    frame_ms: float = 30.0,
    hop_ms: float = 10.0,
    fmin: float = 60.0,
    fmax: float = 400.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Autocorrelation pitch track.

    Returns (f0 per frame, voiced mask).  Deliberately simple: a full pYIN
    would be more accurate but this branch is capped anyway, and the extra
    cost would show up on every window of every call.
    """
    frame = int(sample_rate * frame_ms / 1000.0)
    hop = int(sample_rate * hop_ms / 1000.0)
    min_lag = int(sample_rate / fmax)
    max_lag = int(sample_rate / fmin)

    f0: list[float] = []
    voiced: list[bool] = []

    for start in range(0, max(len(wav) - frame + 1, 0), hop):
        segment = wav[start : start + frame]
        segment = segment - segment.mean()
        energy = float(np.sum(segment**2))
        if energy < 1e-6:
            f0.append(0.0)
            voiced.append(False)
            continue

        corr = np.correlate(segment, segment, mode="full")[frame - 1 :]
        if max_lag >= len(corr):
            f0.append(0.0)
            voiced.append(False)
            continue

        window = corr[min_lag:max_lag]
        if window.size == 0:
            f0.append(0.0)
            voiced.append(False)
            continue

        peak = int(np.argmax(window)) + min_lag
        strength = corr[peak] / (corr[0] + 1e-9)
        if strength > 0.3:
            f0.append(sample_rate / peak)
            voiced.append(True)
        else:
            f0.append(0.0)
            voiced.append(False)

    return np.array(f0), np.array(voiced)


def compute_features(wav: np.ndarray, sample_rate: int = 16000) -> ProsodyFeatures:
    wav = np.asarray(wav, dtype=np.float32)
    f0, voiced = estimate_f0(wav, sample_rate)
    voiced_f0 = f0[voiced] if voiced.any() else np.array([0.0])

    # Short-time energy contour, for the dynamics of the delivery.
    frame = int(sample_rate * 0.03)
    hop = int(sample_rate * 0.01)
    energies = np.array(
        [
            float(np.sqrt(np.mean(wav[i : i + frame] ** 2) + 1e-12))
            for i in range(0, max(len(wav) - frame + 1, 0), hop)
        ]
    )

    # Jitter: cycle-to-cycle pitch perturbation.  Natural voices carry more of
    # it than a vocoder, which is smooth in ways a larynx is not.
    if voiced_f0.size > 2:
        periods = 1.0 / np.clip(voiced_f0, 1e-3, None)
        jitter = float(np.mean(np.abs(np.diff(periods))) / (np.mean(periods) + 1e-9))
    else:
        jitter = 0.0

    return ProsodyFeatures(
        f0_mean=float(np.mean(voiced_f0)),
        f0_std=float(np.std(voiced_f0)),
        f0_range=float(np.ptp(voiced_f0)) if voiced_f0.size > 1 else 0.0,
        voiced_ratio=float(voiced.mean()) if voiced.size else 0.0,
        energy_std=float(np.std(energies)) if energies.size else 0.0,
        speaking_rate=_estimate_rate(energies, hop, sample_rate),
        jitter=jitter,
    )


def _estimate_rate(energies: np.ndarray, hop: int, sample_rate: int) -> float:
    """Syllable-ish rate from energy peaks per second."""
    if energies.size < 3:
        return 0.0
    threshold = float(np.mean(energies))
    peaks = int(
        np.sum(
            (energies[1:-1] > energies[:-2])
            & (energies[1:-1] > energies[2:])
            & (energies[1:-1] > threshold)
        )
    )
    duration_s = energies.size * hop / sample_rate
    return float(peaks / duration_s) if duration_s > 0 else 0.0


def prosody_score(wav: np.ndarray, sample_rate: int = 16000) -> float:
    """A single suspicion score, oriented so higher means more synthetic.

    The weights are a documented prior, not a fit.  Replace them with
    coefficients learned per language before quoting anything from this
    branch - and if the ablation says it adds nothing, cut it and say so.
    """
    features = compute_features(wav, sample_rate)
    score = 0.0

    # Neural TTS tends to produce unnaturally low jitter: a larynx is messy,
    # a vocoder is not.
    if features.jitter < 0.01 and features.voiced_ratio > 0.3:
        score += 0.6

    # Very low pitch variance suggests a flat, generated contour.  Threshold
    # is a global default and is exactly the part that needs per-language
    # tuning before it can be trusted.
    if 0 < features.f0_std < 8.0:
        score += 0.4

    # An implausibly steady energy contour.
    if features.energy_std < 0.01:
        score += 0.3

    return float(np.clip(score, -2.0, 2.0))
