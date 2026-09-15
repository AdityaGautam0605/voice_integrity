"""Evaluation across all conditions of record.

The protocol, from the SRS: never quote the in-domain number alone.  Report
in-domain, out-of-domain, codec-degraded and leave-one-attack-out together.
A team that shows where its model fails, and can explain why, reads as the
team that understood the problem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from vif.common.logging import get_logger
from vif.data.manifests import Item
from vif.eval.metrics import MetricResult, evaluate

log = get_logger(__name__)


@dataclass
class ConditionResult:
    name: str
    model: str
    metrics: MetricResult
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "condition": self.name,
            "model": self.model,
            "note": self.note,
            **self.metrics.as_dict(),
        }


@dataclass
class EvalReport:
    """A whole evaluation run.  Serialisable, so charts regenerate from it."""

    results: list[ConditionResult] = field(default_factory=list)

    def add(self, name: str, model: str, labels, scores, note: str = "") -> ConditionResult:
        result = ConditionResult(
            name=name, model=model, metrics=evaluate(labels, scores), note=note
        )
        self.results.append(result)
        log.info("%-28s %-14s %s", name, model, result.metrics.summary())
        return result

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump([r.as_dict() for r in self.results], fh, indent=2)
        log.info("wrote %d condition results to %s", len(self.results), path)

    def table(self) -> str:
        """Plain-text table.  What goes in the report and on the slide."""
        header = f"{'condition':<28} {'model':<14} {'EER':>8} {'TPR@1%FPR':>11} {'minTDCF':>9}  n"
        lines = [header, "-" * len(header)]
        for r in self.results:
            m = r.metrics
            tdcf = f"{m.min_tdcf:9.4f}" if m.min_tdcf is not None else f"{'-':>9}"
            lines.append(
                f"{r.name:<28} {r.model:<14} {m.eer * 100:7.2f}% "
                f"{m.tpr_at_1pct_fpr * 100:10.2f}% {tdcf}  "
                f"{m.n_bonafide}+{m.n_spoof}"
            )
        return "\n".join(lines)

    def gap(self, condition_a: str, condition_b: str, model: str) -> dict | None:
        """The difference between two conditions for one model.

        The codec-degraded minus clean gap for the baseline versus the
        augmented model is the entire contribution of this project, so it gets
        a first-class accessor rather than being recomputed in a notebook.
        """
        a = next((r for r in self.results if r.name == condition_a and r.model == model), None)
        b = next((r for r in self.results if r.name == condition_b and r.model == model), None)
        if a is None or b is None:
            return None
        return {
            "model": model,
            "from": condition_a,
            "to": condition_b,
            "eer_delta": round(b.metrics.eer - a.metrics.eer, 5),
            "tpr_delta": round(b.metrics.tpr_at_1pct_fpr - a.metrics.tpr_at_1pct_fpr, 5),
        }


def score_arrays(
    items: list[Item], scores_by_index: dict[int, float]
) -> tuple[np.ndarray, np.ndarray]:
    """Align manifest labels with a sparse score dictionary."""
    labels, scores = [], []
    for i, item in enumerate(items):
        if i in scores_by_index:
            labels.append(item.target)
            scores.append(scores_by_index[i])
    return np.array(labels), np.array(scores)


def leave_one_attack_out(
    items: list[Item],
    scores_by_index: dict[int, float],
    attacks: list[str] | None = None,
) -> dict[str, MetricResult]:
    """Per-attack performance against bonafide.

    Evaluated one generator at a time, each against the full bonafide set.
    This is the number that reflects deployment against a synthesizer the
    model has never seen - and the one almost nobody reports.
    """
    attacks = attacks or sorted({i.attack for i in items if i.label == "spoof" and i.attack != "-"})
    bonafide_idx = [i for i, item in enumerate(items) if item.label == "bonafide"]

    out: dict[str, MetricResult] = {}
    for attack in attacks:
        spoof_idx = [
            i for i, item in enumerate(items) if item.label == "spoof" and item.attack == attack
        ]
        selected = bonafide_idx + spoof_idx
        labels = [items[i].target for i in selected if i in scores_by_index]
        scores = [scores_by_index[i] for i in selected if i in scores_by_index]
        if len(set(labels)) < 2:
            continue
        out[attack] = evaluate(labels, scores)
        log.info("attack %-8s %s", attack, out[attack].summary())
    return out


def per_condition(
    items: list[Item],
    scores_by_index: dict[int, float],
) -> dict[str, MetricResult]:
    """Break results down by the `condition` field of the manifest.

    With the codec chain writing its condition label into the manifest, this
    gives a per-codec breakdown for free - the evidence that the augmented
    model holds where the baseline collapses.
    """
    conditions = sorted({i.condition for i in items})
    out: dict[str, MetricResult] = {}
    for condition in conditions:
        subset = [(i, item) for i, item in enumerate(items) if item.condition == condition]
        labels = [item.target for i, item in subset if i in scores_by_index]
        scores = [scores_by_index[i] for i, _ in subset if i in scores_by_index]
        if len(set(labels)) < 2:
            continue
        out[condition] = evaluate(labels, scores)
        log.info("condition %-16s %s", condition, out[condition].summary())
    return out


def per_language(
    items: list[Item],
    scores_by_index: dict[int, float],
) -> dict[str, MetricResult]:
    """Break results down by language.

    Validates the language-agnostic claim: if artifact detection really is
    physics rather than linguistics, these numbers should be comparable.
    """
    languages = sorted({i.lang for i in items})
    out: dict[str, MetricResult] = {}
    for lang in languages:
        subset = [(i, item) for i, item in enumerate(items) if item.lang == lang]
        labels = [item.target for i, item in subset if i in scores_by_index]
        scores = [scores_by_index[i] for i, _ in subset if i in scores_by_index]
        if len(set(labels)) < 2:
            continue
        out[lang] = evaluate(labels, scores)
        log.info("language %-10s %s", lang, out[lang].summary())
    return out


def compare_models(
    report: EvalReport,
    condition: str,
    baseline: str = "baseline",
    candidate: str = "codec_robust",
) -> str:
    """The money-shot comparison, as text."""
    a = next((r for r in report.results if r.name == condition and r.model == baseline), None)
    b = next((r for r in report.results if r.name == condition and r.model == candidate), None)
    if a is None or b is None:
        return f"cannot compare on '{condition}': missing results"
    return (
        f"{condition}\n"
        f"  {baseline:<14} EER {a.metrics.eer * 100:6.2f}%  "
        f"TPR@1%FPR {a.metrics.tpr_at_1pct_fpr * 100:6.2f}%\n"
        f"  {candidate:<14} EER {b.metrics.eer * 100:6.2f}%  "
        f"TPR@1%FPR {b.metrics.tpr_at_1pct_fpr * 100:6.2f}%\n"
        f"  delta          EER {(b.metrics.eer - a.metrics.eer) * 100:+6.2f}pp "
        f"TPR {(b.metrics.tpr_at_1pct_fpr - a.metrics.tpr_at_1pct_fpr) * 100:+6.2f}pp"
    )
