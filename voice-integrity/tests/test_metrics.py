"""Metric harness tests.

These run before any model exists.  A harness trusted only after models exist
has no independent authority to declare them wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from vif.eval.calibration import Calibrator, PlattParams, fit_platt
from vif.eval.metrics import (
    compute_eer,
    compute_min_tdcf,
    evaluate,
    expected_calibration_error,
    roc_auc,
    sanity_check_random,
    tpr_at_fpr,
)


def test_random_classifier_reports_chance():
    """The P0 gate.  If this fails, every later number is meaningless."""
    result = sanity_check_random(n=20000, seed=0)
    assert 0.45 < result.eer < 0.55
    assert 0.45 < result.auc < 0.55


def test_perfect_separation():
    labels = np.array([1] * 100 + [0] * 100)
    scores = np.array([-5.0] * 100 + [5.0] * 100)  # higher = more synthetic
    eer, _ = compute_eer(labels, scores)
    assert eer < 0.01
    assert tpr_at_fpr(labels, scores, 0.01) > 0.99
    assert roc_auc(labels, scores) > 0.99


def test_inverted_scores_are_worse_than_chance():
    """Guards the score orientation.

    Everything downstream assumes higher = more synthetic.  A sign flip would
    otherwise look like a merely bad model rather than a wiring bug.
    """
    labels = np.array([1] * 100 + [0] * 100)
    scores = np.array([5.0] * 100 + [-5.0] * 100)
    assert roc_auc(labels, scores) < 0.01


def test_accuracy_would_mislead_on_imbalanced_data():
    """Demonstrates why the project never quotes accuracy.

    Nine spoofed utterances per bonafide one, and a detector that always
    answers 'spoof' scores 90% accuracy while being useless.  EER exposes it.
    """
    rng = np.random.default_rng(0)
    labels = np.array([1] * 100 + [0] * 900)
    scores = np.full(1000, 5.0) + rng.normal(0, 1e-6, 1000)

    accuracy = float(((scores > 0).astype(int) == (labels == 0).astype(int)).mean())
    assert accuracy >= 0.89

    eer, _ = compute_eer(labels, scores)
    assert eer > 0.4  # the honest number


def test_tpr_at_fpr_respects_the_budget():
    rng = np.random.default_rng(1)
    labels = rng.integers(0, 2, 5000)
    scores = np.where(labels == 0, rng.normal(2.0, 1.0, 5000), rng.normal(0.0, 1.0, 5000))

    strict = tpr_at_fpr(labels, scores, 0.01)
    loose = tpr_at_fpr(labels, scores, 0.10)
    assert 0.0 <= strict <= loose <= 1.0


def test_min_tdcf_in_range():
    rng = np.random.default_rng(2)
    labels = rng.integers(0, 2, 2000)
    scores = np.where(labels == 0, rng.normal(1.5, 1.0, 2000), rng.normal(-1.5, 1.0, 2000))
    assert 0.0 <= compute_min_tdcf(labels, scores) <= 1.0


def test_ece_rewards_a_calibrated_model():
    rng = np.random.default_rng(3)
    n = 5000
    probabilities = rng.uniform(0, 1, n)
    labels = np.where(rng.uniform(0, 1, n) < probabilities, 0, 1)  # 0 = spoof
    assert expected_calibration_error(labels, probabilities) < 0.06


def test_ece_punishes_overconfidence():
    labels = np.array([1] * 500 + [0] * 500)
    probabilities = np.array([0.99] * 1000)  # always certain it is spoof
    assert expected_calibration_error(labels, probabilities) > 0.4


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        evaluate([1, 0, 1], [0.5, 0.2])


def test_single_class_raises():
    with pytest.raises(ValueError):
        compute_eer([1, 1, 1], [0.1, 0.2, 0.3])


class TestCalibration:
    def test_platt_produces_monotone_llr(self):
        rng = np.random.default_rng(4)
        labels = rng.integers(0, 2, 4000)
        scores = np.where(labels == 0, rng.normal(1.5, 1.0, 4000), rng.normal(-1.5, 1.0, 4000))
        params = fit_platt(labels, scores, split="dev")
        assert params.a > 0
        assert params.to_llr(3.0) > params.to_llr(-3.0)

    def test_platt_handles_separable_data(self):
        """Target smoothing keeps parameters finite when classes separate."""
        labels = np.array([1] * 200 + [0] * 200)
        scores = np.array([-10.0] * 200 + [10.0] * 200)
        params = fit_platt(labels, scores, split="dev")
        assert np.isfinite(params.a) and np.isfinite(params.b)

    def test_eval_fitted_calibration_is_rejected(self, tmp_path):
        """Contaminated calibration must fail loudly, not silently."""
        calibrator = Calibrator({"spoof": PlattParams(a=1.0, b=0.0, fitted_on="eval")})
        path = tmp_path / "calibration.json"
        calibrator.save(path)
        with pytest.raises(ValueError, match="eval split"):
            Calibrator.load(path, strict=True)

    def test_missing_calibration_passes_scores_through(self):
        calibrator = Calibrator({})
        assert calibrator.to_llr("spoof", 1.234) == pytest.approx(1.234)

    def test_roundtrip(self, tmp_path):
        original = Calibrator({"spoof": PlattParams(a=2.5, b=-0.3, fitted_on="dev")})
        path = tmp_path / "calibration.json"
        original.save(path)
        loaded = Calibrator.load(path)
        assert loaded.to_llr("spoof", 1.0) == pytest.approx(2.2)
