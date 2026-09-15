"""Score calibration.

A model that outputs 0.87 does not mean 87% probability.  Raw outputs are
systematically overconfident, and two different branches are overconfident in
different ways.  Calibration is the step that puts them on a common scale.

The useful consequence: after Platt scaling the log-likelihood ratio is just
an affine transform of the raw score,

    LLR = a * score + b

which is exactly why calibrating first is what makes evidence *additive*.
Summing uncalibrated scores adds quantities with no common unit.

Fit on the development split only.  Fitting on evaluation data produces
numbers that look excellent and mean nothing, so `fit_platt` records which
split it saw and the loader refuses anything labelled "eval".
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from vif.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class PlattParams:
    """a, b such that llr = a * score + b."""

    a: float
    b: float
    fitted_on: str = "dev"
    n_samples: int = 0
    branch: str = "spoof"
    prior_log_odds: float = 0.0  # fitting split's class prior, removed from b

    def to_llr(self, score: float | np.ndarray) -> float | np.ndarray:
        return self.a * np.asarray(score, dtype=float) + self.b

    def to_probability(self, score: float | np.ndarray) -> float | np.ndarray:
        return 1.0 / (1.0 + np.exp(-self.to_llr(score)))


class Calibrator:
    """Per-branch Platt parameters with a safe identity fallback.

    A branch with no fitted parameters passes its score through unchanged and
    logs once.  That is deliberate: a missing calibration should degrade the
    fusion, not crash a live call.
    """

    def __init__(self, params: dict[str, PlattParams] | None = None):
        self.params = params or {}
        self._warned: set[str] = set()

    def to_llr(self, branch: str, score: float) -> float:
        p = self.params.get(branch)
        if p is None:
            if branch not in self._warned:
                log.warning("no calibration for branch '%s' - passing score through raw", branch)
                self._warned.add(branch)
            return float(score)
        return float(p.to_llr(score))

    def has(self, branch: str) -> bool:
        return branch in self.params

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump({k: asdict(v) for k, v in self.params.items()}, fh, indent=2)
        log.info("wrote calibration for %d branch(es) to %s", len(self.params), path)

    @classmethod
    def load(cls, path: str | Path, strict: bool = True) -> Calibrator:
        path = Path(path)
        if not path.exists():
            log.warning("calibration file %s not found - scores will pass through raw", path)
            return cls({})
        with path.open("r", encoding="utf-8") as fh:
            blob = json.load(fh)
        params = {}
        for branch, values in blob.items():
            p = PlattParams(**values)
            if strict and p.fitted_on == "eval":
                raise ValueError(
                    f"branch '{branch}' was calibrated on the eval split. "
                    "This contaminates every number downstream. Refit on dev."
                )
            params[branch] = p
        return cls(params)


def fit_platt(
    labels,
    scores,
    branch: str = "spoof",
    split: str = "dev",
    max_iter: int = 200,
    tol: float = 1e-7,
) -> PlattParams:
    """Fit a * score + b by logistic regression on the spoof class.

    Uses Platt's target smoothing, which prevents the parameters running off
    to infinity when the two classes are perfectly separable on the fitting
    split - a real risk on in-domain data where the model is very strong.
    """
    labels = np.asarray(labels).astype(int).ravel()
    scores = np.asarray(scores, dtype=float).ravel()
    y = (labels == 0).astype(float)  # 1 = spoof, the thing we are detecting

    n_pos = float(y.sum())
    n_neg = float(len(y) - n_pos)
    if n_pos == 0 or n_neg == 0:
        raise ValueError("both classes must be present to calibrate")

    hi = (n_pos + 1.0) / (n_pos + 2.0)
    lo = 1.0 / (n_neg + 2.0)
    target = np.where(y > 0.5, hi, lo)

    a, b = 0.0, 0.0
    for _ in range(max_iter):
        z = a * scores + b
        p = 1.0 / (1.0 + np.exp(-z))
        grad_a = float(np.sum((p - target) * scores))
        grad_b = float(np.sum(p - target))
        w = p * (1.0 - p) + 1e-12
        h_aa = float(np.sum(w * scores * scores)) + 1e-9
        h_ab = float(np.sum(w * scores))
        h_bb = float(np.sum(w)) + 1e-9
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * grad_a - h_ab * grad_b) / det
        db = (h_aa * grad_b - h_ab * grad_a) / det
        a -= da
        b -= db
        if abs(da) < tol and abs(db) < tol:
            break

    # The logistic fit estimates a posterior, so its intercept absorbs the class
    # ratio of the fitting split - about nine spoofs per bonafide on ASVspoof dev.
    # Left in, a score carrying no evidence would read as p=0.9 and every genuine
    # caller would open at RED.  Removing the split's prior log-odds leaves the
    # likelihood ratio this module promises: llr = 0 means no evidence either way.
    prior_log_odds = float(np.log(n_pos / n_neg))
    b -= prior_log_odds

    log.info(
        "calibrated branch '%s' on %s split (n=%d, prior log-odds %.3f removed): "
        "llr = %.4f * score + %.4f",
        branch,
        split,
        len(scores),
        prior_log_odds,
        a,
        b,
    )
    return PlattParams(
        a=float(a),
        b=float(b),
        fitted_on=split,
        n_samples=len(scores),
        branch=branch,
        prior_log_odds=prior_log_odds,
    )


def reliability_bins(labels, probabilities, n_bins: int = 15) -> list[dict]:
    """Data for a reliability diagram: confidence against observed frequency."""
    labels = np.asarray(labels).astype(int).ravel()
    probabilities = np.asarray(probabilities, dtype=float).ravel()
    y = (labels == 0).astype(int)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (probabilities > lo) & (probabilities <= hi)
        if not mask.any():
            continue
        out.append(
            {
                "bin_lo": float(lo),
                "bin_hi": float(hi),
                "count": int(mask.sum()),
                "confidence": float(probabilities[mask].mean()),
                "accuracy": float(y[mask].mean()),
            }
        )
    return out
