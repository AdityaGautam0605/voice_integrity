"""Calibration, fusion and temporal accumulation (L4).

The arithmetic, and why each step is the way it is.

    llr_branch   = a * score + b               Platt, fitted on dev ONLY
    running_llr += clamp(llr_branch, -4, +4)   evidence accumulates by SUM
    posterior    = running_llr + prior_llr     metadata enters as log-odds
    risk         = 100 * sigmoid(posterior / scale)
    ci           = 100 / sqrt(n_windows)       tightens as evidence arrives

**Why sum, not average.**  Ten witnesses each 60% sure are not one witness
60% sure.  Averaging per-window scores discards everything the passage of time
provides; summing calibrated log-likelihood ratios is what makes the
confidence interval visibly narrow during a call.  This is the machinery of a
sequential probability ratio test, so the stopping rule is principled rather
than hand-tuned.

**Why calibrate first.**  Uncalibrated scores from different branches have no
common unit.  Summing them is adding apples to oranges, and the result looks
plausible while meaning nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from vif.common.config import FusionConfig, PolicyConfig
from vif.common.logging import get_logger
from vif.common.types import BranchScores
from vif.eval.calibration import Calibrator

log = get_logger(__name__)


@dataclass
class FusionState:
    """Accumulated evidence for one call."""

    running_llr: float = 0.0
    n_windows: int = 0
    prior_llr: float = 0.0
    per_branch_llr: dict[str, float] = field(default_factory=dict)
    liveness_llr: float | None = None

    @property
    def posterior(self) -> float:
        live = self.liveness_llr or 0.0
        return self.running_llr + self.prior_llr + live


def metadata_prior_llr(metadata: dict, policy: PolicyConfig) -> float:
    """Start the meter somewhere sensible before any audio arrives.

    An unknown international number at 11pm asking for a large transfer should
    not begin from the same place as a known contact in business hours.  This
    is what makes an early, weak acoustic observation actionable in a
    high-risk context without lowering the threshold globally.

    Entered as a log-odds offset, never as a post-hoc multiplier (FR-FU-05).
    """
    weights = policy.metadata_prior
    if not weights:
        return 0.0

    total = 0.0
    if metadata.get("first_contact"):
        total += weights.get("first_contact", 0.0)
    if metadata.get("outside_business_hours"):
        total += weights.get("outside_business_hours", 0.0)
    if metadata.get("above_tier_threshold"):
        total += weights.get("above_tier_threshold", 0.0)
    if metadata.get("known_contact"):
        total += weights.get("known_contact", 0.0)
    if metadata.get("prior_verified_calls"):
        total += weights.get("prior_verified_calls", 0.0)

    return float(np.clip(total, -3.0, 3.0))


class FusionEngine:
    """Turns per-window branch scores into a risk score with an interval."""

    def __init__(
        self,
        config: FusionConfig | None = None,
        calibrator: Calibrator | None = None,
        prior_llr: float = 0.0,
    ):
        self.config = config or FusionConfig()
        self.calibrator = calibrator or Calibrator({})
        self.state = FusionState(prior_llr=prior_llr)

    # -- per-window --------------------------------------------------------

    def update(self, scores: BranchScores) -> FusionState:
        """Fold one window's branch outputs into the running evidence.

        Branches reporting None abstained (no enrolment, disabled, not enough
        turns).  Abstention is not the same as a score of zero and must not
        move the posterior.
        """
        window_llr = 0.0
        contributed = False

        for branch in ("spoof", "speaker", "prosody"):
            raw = getattr(scores, branch)
            if raw is None:
                continue
            weight = self.config.weights.get(branch, 0.0)
            if weight == 0.0:
                continue
            llr = self.calibrator.to_llr(branch, raw)
            llr = float(np.clip(llr, -self.config.llr_clamp, self.config.llr_clamp))
            self.state.per_branch_llr[branch] = llr
            window_llr += weight * llr
            contributed = True

        if contributed:
            # Clamp the combined per-window contribution too, so one
            # pathological window cannot pin the whole call.
            window_llr = float(np.clip(window_llr, -self.config.llr_clamp, self.config.llr_clamp))
            self.state.running_llr += window_llr
            self.state.n_windows += 1

        return self.state

    def set_liveness(self, llr: float | None) -> None:
        """Liveness accrues per turn transition, not per window.

        Kept out of `running_llr` deliberately: it is on a different clock, so
        adding it once per window would count the same evidence repeatedly.
        """
        if llr is None:
            self.state.liveness_llr = None
            return
        weight = self.config.weights.get("liveness", 0.0)
        self.state.liveness_llr = float(
            np.clip(weight * llr, -self.config.llr_clamp, self.config.llr_clamp)
        )

    # -- readout -----------------------------------------------------------

    @property
    def risk(self) -> float:
        """0-100.  Higher means more likely synthetic."""
        posterior = self.state.posterior
        return float(100.0 / (1.0 + np.exp(-posterior / self.config.risk_scale)))

    @property
    def confidence_interval(self) -> float:
        """Half-width of the reported interval.

        Shrinks as 1/sqrt(n).  Reported rather than hidden: a score with two
        windows behind it should not look like a score with forty.
        """
        n = max(self.state.n_windows, 1)
        return float(min(50.0, 100.0 / np.sqrt(n)))

    def reset(self) -> None:
        prior = self.state.prior_llr
        self.state = FusionState(prior_llr=prior)
