"""Scoring and risk bands (L4/L5).

Replaces cross-branch log-likelihood-ratio fusion with **independent branch
outputs and deterministic thresholds**.

Why that is the right call here.  With one primary detector and one optional
speaker verifier, combining them into a single fused number produces the
appearance of mathematical rigour without the evidence to support it - and it
hides which branch actually fired, which is exactly what an operator and a
judge both need to see.  Branches are reported side by side and each carries
its own status.

What is retained, and why it is not the same thing: **temporal smoothing
within the spoof branch**.  A raw per-window probability jitters, because 4
seconds of speech is a small sample.  Averaging in the log-odds domain across
recent windows is ordinary smoothing of one estimator over time, not fusion of
two different ones.  It is what makes the score settle during a call instead
of flickering, which is the single most visible behaviour in a live demo.  Set
``smoothing_windows: 1`` in config to disable it and report the raw window.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from vif.common.config import ScoringConfig
from vif.common.logging import get_logger
from vif.common.types import Risk, SpeakerStatus

log = get_logger(__name__)


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


@dataclass
class ScoringState:
    """Per-session scoring state."""

    windows: int = 0
    last_probability: float = 0.5
    smoothed_probability: float = 0.5
    history: deque[float] = field(default_factory=lambda: deque(maxlen=8))
    speaker_similarity: float | None = None
    liveness_score: float | None = None


class Scorer:
    """Turns a raw detector score into a probability and a risk band.

    The detector emits an unbounded logit oriented so that higher means more
    synthetic.  Two paths convert it to a probability:

    * **calibrated** - Platt parameters fitted on the development split, which
      makes 0.87 actually mean 87%.  Preferred when they exist.
    * **uncalibrated** - a plain sigmoid.  Monotone and therefore fine for
      ranking and thresholds, but the number is not a real probability, and
      the API says so via ``calibrated``.
    """

    def __init__(self, config: ScoringConfig | None = None, calibrator=None):
        self.config = config or ScoringConfig()
        self.calibrator = calibrator
        self.state = ScoringState()
        self.calibrated = bool(calibrator is not None and calibrator.has("spoof"))
        if not self.calibrated:
            log.warning(
                "no calibration for the spoof branch - probabilities are monotone "
                "but not calibrated.  Fit on the dev split before quoting them."
            )

    # -- spoof branch ------------------------------------------------------

    def update_spoof(self, raw_score: float) -> float:
        """Fold one window's detector output in, return the current probability."""
        if self.calibrated:
            value = self.calibrator.to_llr("spoof", raw_score)
        else:
            value = raw_score / self.config.logit_scale

        probability = sigmoid(value)
        self.state.last_probability = probability
        self.state.history.append(value)
        self.state.windows += 1

        # Smooth in the log-odds domain: averaging probabilities directly
        # compresses toward 0.5 and understates confident windows.
        n = min(self.config.smoothing_windows, len(self.state.history))
        recent = list(self.state.history)[-n:] if n > 0 else [value]
        self.state.smoothed_probability = sigmoid(sum(recent) / len(recent))
        return self.state.smoothed_probability

    @property
    def spoof_probability(self) -> float:
        return self.state.smoothed_probability

    # -- speaker branch ----------------------------------------------------

    def update_speaker(self, similarity: float | None) -> SpeakerStatus:
        """Independent status, never folded into the spoof probability.

        None means the branch abstained - no enrolment exists - which is not
        the same as a low similarity and must not read as one.
        """
        self.state.speaker_similarity = similarity
        if similarity is None:
            return SpeakerStatus.NOT_ENROLLED
        if similarity < self.config.speaker_threshold:
            return SpeakerStatus.MISMATCH
        return SpeakerStatus.MATCH

    @property
    def speaker_status(self) -> SpeakerStatus:
        similarity = self.state.speaker_similarity
        if similarity is None:
            return SpeakerStatus.NOT_ENROLLED
        return (
            SpeakerStatus.MISMATCH
            if similarity < self.config.speaker_threshold
            else SpeakerStatus.MATCH
        )

    # -- liveness branch ---------------------------------------------------

    def update_liveness(self, score: float | None) -> None:
        """Optional, reported separately, never folded in."""
        self.state.liveness_score = score

    # -- readout -----------------------------------------------------------

    def risk(self) -> Risk:
        """Deterministic band from the spoof probability.

        Two thresholds, both in config, both printable on a slide.  A judge
        asking "why is this red?" gets a number and a threshold, not a chain
        of reasoning through a fused posterior.
        """
        p = self.spoof_probability
        if p >= self.config.red_threshold:
            return Risk.RED
        if p >= self.config.amber_threshold:
            return Risk.AMBER
        return Risk.GREEN

    def reset(self) -> None:
        self.state = ScoringState()


def risk_from_probability(probability: float, config: ScoringConfig) -> Risk:
    """Stateless band lookup, for offline scoring and tests."""
    if probability >= config.red_threshold:
        return Risk.RED
    if probability >= config.amber_threshold:
        return Risk.AMBER
    return Risk.GREEN
