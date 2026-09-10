"""Metrics of record.

Built before any model, and validated by confirming a random-guess classifier
reports EER near 50%.  A harness trusted only after models exist has no
independent authority to declare them wrong.

Scores throughout are oriented so **higher means more synthetic**.  Labels are
1 for bonafide, 0 for spoof.  The detection problem is therefore "detect the
spoof", so the positive class for TPR is the spoof class.

Why not accuracy: ASVspoof LA carries roughly nine spoofed utterances per
bonafide one, so a model that answers "spoof" unconditionally scores near 90%.
The operational number is the detection rate at a bounded false-positive rate,
because false alarms on genuine callers are what get a system switched off.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class MetricResult:
    eer: float
    eer_threshold: float
    tpr_at_1pct_fpr: float
    tpr_at_5pct_fpr: float
    auc: float
    min_tdcf: float | None = None
    ece: float | None = None
    n_bonafide: int = 0
    n_spoof: int = 0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "eer": round(self.eer, 5),
            "eer_threshold": round(self.eer_threshold, 5),
            "tpr_at_1pct_fpr": round(self.tpr_at_1pct_fpr, 5),
            "tpr_at_5pct_fpr": round(self.tpr_at_5pct_fpr, 5),
            "auc": round(self.auc, 5),
            "min_tdcf": round(self.min_tdcf, 5) if self.min_tdcf is not None else None,
            "ece": round(self.ece, 5) if self.ece is not None else None,
            "n_bonafide": self.n_bonafide,
            "n_spoof": self.n_spoof,
            **self.extra,
        }

    def summary(self) -> str:
        return (
            f"EER {self.eer * 100:6.2f}%   "
            f"TPR@1%FPR {self.tpr_at_1pct_fpr * 100:6.2f}%   "
            f"TPR@5%FPR {self.tpr_at_5pct_fpr * 100:6.2f}%   "
            f"AUC {self.auc:.4f}   "
            f"(n={self.n_bonafide}+{self.n_spoof})"
        )


def _as_arrays(labels, scores) -> tuple[np.ndarray, np.ndarray]:
    labels = np.asarray(labels).astype(int).ravel()
    scores = np.asarray(scores, dtype=float).ravel()
    if labels.shape != scores.shape:
        raise ValueError(f"labels {labels.shape} and scores {scores.shape} differ in length")
    if len(labels) == 0:
        raise ValueError("empty input")
    return labels, scores


def det_curve(labels, scores) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """False-alarm and miss rates over all thresholds.

    A false alarm is a bonafide caller flagged as synthetic - the error that
    actually gets the system disabled in production.
    """
    labels, scores = _as_arrays(labels, scores)
    bonafide = scores[labels == 1]
    spoof = scores[labels == 0]
    if len(bonafide) == 0 or len(spoof) == 0:
        raise ValueError("both classes must be present to compute a DET curve")

    thresholds = np.sort(np.unique(np.concatenate([bonafide, spoof])))
    # far: bonafide scored at or above threshold (wrongly called synthetic)
    far = np.array([(bonafide >= t).mean() for t in thresholds])
    # frr: spoof scored below threshold (missed)
    frr = np.array([(spoof < t).mean() for t in thresholds])
    return far, frr, thresholds


def compute_eer(labels, scores) -> tuple[float, float]:
    """Equal error rate and the threshold at which it occurs."""
    far, frr, thresholds = det_curve(labels, scores)
    idx = int(np.nanargmin(np.abs(far - frr)))
    eer = float((far[idx] + frr[idx]) / 2.0)
    return eer, float(thresholds[idx])


def tpr_at_fpr(labels, scores, target_fpr: float = 0.01) -> float:
    """Spoof detection rate while flagging at most `target_fpr` of genuine callers.

    This is the number to quote.  "At a 1% false-positive rate we catch X%"
    is a claim a bank can act on; "95% accurate" is not.
    """
    labels, scores = _as_arrays(labels, scores)
    bonafide = scores[labels == 1]
    spoof = scores[labels == 0]
    if len(bonafide) == 0 or len(spoof) == 0:
        return float("nan")
    # Threshold that admits at most target_fpr of bonafide above it.
    threshold = float(np.quantile(bonafide, 1.0 - target_fpr))
    return float((spoof >= threshold).mean())


def roc_auc(labels, scores) -> float:
    """AUC with the spoof class as positive, computed by rank statistics."""
    labels, scores = _as_arrays(labels, scores)
    y = (labels == 0).astype(int)  # spoof is the positive class
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    # Average ranks over ties so identical scores do not bias the estimate.
    _, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inverse, ranks)
    ranks = (sums / counts)[inverse]
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def compute_min_tdcf(
    labels,
    scores,
    p_spoof: float = 0.05,
    c_miss: float = 1.0,
    c_fa: float = 10.0,
) -> float:
    """Minimum normalised detection cost.

    Included for comparability with the anti-spoofing literature.  The default
    cost asymmetry encodes the operational reality of Trap 3: a false alarm on
    a genuine caller is far more expensive than a single miss, because false
    alarms are what train staff to ignore the system.
    """
    labels, scores = _as_arrays(labels, scores)
    far, frr, _ = det_curve(labels, scores)
    p_bonafide = 1.0 - p_spoof
    cost = c_miss * p_spoof * frr + c_fa * p_bonafide * far
    default_cost = min(c_miss * p_spoof, c_fa * p_bonafide)
    return float(np.min(cost) / default_cost)


def expected_calibration_error(labels, probabilities, n_bins: int = 15) -> float:
    """How wrong the model is about its own confidence.

    Fusion is invalid if branches are miscalibrated: summing log-likelihood
    ratios only means something when each one is on a common scale.  Check
    this before trusting any fused score.
    """
    labels, probabilities = _as_arrays(labels, probabilities)
    y = (labels == 0).astype(int)  # probability that the utterance is spoof
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(bins[:-1], bins[1:], strict=True):
        mask = (probabilities > lo) & (probabilities <= hi)
        if not mask.any():
            continue
        confidence = probabilities[mask].mean()
        accuracy = y[mask].mean()
        ece += (mask.mean()) * abs(accuracy - confidence)
    return float(ece)


def evaluate(
    labels,
    scores,
    probabilities=None,
    p_spoof: float = 0.05,
) -> MetricResult:
    """Everything at once.  This is what the runner reports per condition."""
    labels, scores = _as_arrays(labels, scores)
    eer, threshold = compute_eer(labels, scores)
    return MetricResult(
        eer=eer,
        eer_threshold=threshold,
        tpr_at_1pct_fpr=tpr_at_fpr(labels, scores, 0.01),
        tpr_at_5pct_fpr=tpr_at_fpr(labels, scores, 0.05),
        auc=roc_auc(labels, scores),
        min_tdcf=compute_min_tdcf(labels, scores, p_spoof=p_spoof),
        ece=(
            expected_calibration_error(labels, probabilities) if probabilities is not None else None
        ),
        n_bonafide=int((labels == 1).sum()),
        n_spoof=int((labels == 0).sum()),
    )


def sanity_check_random(n: int = 20000, seed: int = 0) -> MetricResult:
    """The P0 gate.

    A random-guess classifier must report EER near 50%.  If it does not, the
    harness is broken and every number produced afterwards is meaningless.
    Run this before training anything.
    """
    rng = np.random.default_rng(seed)
    labels = rng.integers(0, 2, size=n)
    scores = rng.normal(0.0, 1.0, size=n)
    return evaluate(labels, scores)
