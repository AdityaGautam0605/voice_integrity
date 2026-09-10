"""Corpus to manifest conversion.

Every corpus is reduced to one JSONL schema so that leave-one-attack-out,
per-codec breakdowns and per-language slices are one-line filters rather than
bespoke scripts.  One JSON object per line:

    {"path": "...", "label": "spoof", "speaker": "LA_0079", "attack": "A03",
     "lang": "en", "condition": "clean", "split": "train", "corpus": "asvspoof19la"}
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vif.common.logging import get_logger

log = get_logger(__name__)

BONAFIDE = "bonafide"
SPOOF = "spoof"


@dataclass
class Item:
    """One utterance.

    `attack` is the generator id where known.  It is what makes
    leave-one-attack-out evaluation possible, so never discard it.
    """

    path: str
    label: str
    split: str
    corpus: str
    speaker: str = "unknown"
    attack: str = "-"
    lang: str = "unknown"
    condition: str = "clean"
    extra: dict = field(default_factory=dict)

    @property
    def target(self) -> int:
        """1 = bonafide, 0 = spoof.

        Scores are oriented so that higher means *more synthetic*, so the
        detection target is inverted at metric time, not here.
        """
        return 1 if self.label == BONAFIDE else 0


def write_manifest(items: Iterable[Item], out_path: str | Path) -> int:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
            n += 1
    log.info("wrote %d items to %s", n, out_path)
    return n


def read_manifest(path: str | Path) -> list[Item]:
    items: list[Item] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(Item(**json.loads(line)))
    return items


def iter_manifest(path: str | Path) -> Iterator[Item]:
    """Stream a manifest without holding it all in memory."""
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Item(**json.loads(line))


def filter_items(
    items: Iterable[Item],
    *,
    exclude_attacks: set[str] | None = None,
    only_attacks: set[str] | None = None,
    condition: str | None = None,
    lang: str | None = None,
) -> list[Item]:
    """Slice a manifest.

    `exclude_attacks` is the leave-one-attack-out knob: hold a generator out of
    training, then evaluate on only that generator to get the number that
    actually reflects deployment against an unseen synthesizer.
    """
    out = []
    for item in items:
        if exclude_attacks and item.attack in exclude_attacks:
            continue
        if only_attacks and item.attack not in only_attacks:
            continue
        if condition and item.condition != condition:
            continue
        if lang and item.lang != lang:
            continue
        out.append(item)
    return out


def summarise(items: Iterable[Item]) -> dict:
    """Counts by label, attack and condition.

    Print this before training.  ASVspoof LA is roughly nine spoofed
    utterances per bonafide one, which is exactly why accuracy on it is
    meaningless and the harness reports EER and TPR@1%FPR instead.
    """
    items = list(items)
    by_label: dict[str, int] = {}
    by_attack: dict[str, int] = {}
    by_condition: dict[str, int] = {}
    for item in items:
        by_label[item.label] = by_label.get(item.label, 0) + 1
        by_attack[item.attack] = by_attack.get(item.attack, 0) + 1
        by_condition[item.condition] = by_condition.get(item.condition, 0) + 1
    n_bona = by_label.get(BONAFIDE, 0)
    n_spoof = by_label.get(SPOOF, 0)
    return {
        "total": len(items),
        "by_label": by_label,
        "by_attack": by_attack,
        "by_condition": by_condition,
        "spoof_per_bonafide": round(n_spoof / n_bona, 2) if n_bona else None,
    }


# --------------------------------------------------------------------------
# Corpus-specific builders
# --------------------------------------------------------------------------


def build_asvspoof19_la(root: str | Path, split: str) -> list[Item]:
    """ASVspoof 2019 Logical Access.

    Protocol lines look like:
        LA_0079 LA_T_1138215 - A03 spoof
        speaker utt-id       - attack label
    """
    root = Path(root)
    split_key = {"train": "trn", "dev": "dev", "eval": "eval"}[split]
    protocol = (
        root
        / "ASVspoof2019_LA_cm_protocols"
        / (
            f"ASVspoof2019.LA.cm.{split_key}.trl.txt"
            if split != "train"
            else "ASVspoof2019.LA.cm.train.trn.txt"
        )
    )
    audio_dir = root / f"ASVspoof2019_LA_{split}" / "flac"
    if not protocol.exists():
        raise FileNotFoundError(f"protocol not found: {protocol}")

    items: list[Item] = []
    with protocol.open("r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 5:
                continue
            speaker, utt_id, _, attack, label = parts[0], parts[1], parts[2], parts[3], parts[4]
            items.append(
                Item(
                    path=str(audio_dir / f"{utt_id}.flac"),
                    label=label,
                    split=split,
                    corpus="asvspoof19la",
                    speaker=speaker,
                    attack=attack,
                    lang="en",
                    condition="clean",
                )
            )
    return items


def build_in_the_wild(root: str | Path, meta_csv: str = "meta.csv") -> list[Item]:
    """In-the-Wild deepfakes.

    Out-of-domain by construction: different generators, different channels,
    different recording conditions.  This is the honesty number.
    """
    import csv

    root = Path(root)
    meta = root / meta_csv
    if not meta.exists():
        raise FileNotFoundError(f"metadata not found: {meta}")

    items: list[Item] = []
    with meta.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            raw_label = (row.get("label") or "").strip().lower()
            label = BONAFIDE if raw_label in ("bona-fide", "bonafide", "real") else SPOOF
            items.append(
                Item(
                    path=str(root / row["file"]),
                    label=label,
                    split="eval",
                    corpus="in_the_wild",
                    speaker=row.get("speaker", "unknown"),
                    attack="unknown",
                    lang="en",
                    condition="wild",
                )
            )
    return items


def build_from_directory(
    root: str | Path,
    label: str,
    split: str,
    corpus: str,
    lang: str = "unknown",
    attack: str = "-",
    condition: str = "clean",
    patterns: tuple[str, ...] = ("*.wav", "*.flac", "*.mp3"),
) -> list[Item]:
    """Generic builder for a flat directory of audio.

    Used for the Indian-language set we generate ourselves and for any corpus
    without a published protocol file.
    """
    root = Path(root)
    items: list[Item] = []
    for pattern in patterns:
        for path in sorted(root.rglob(pattern)):
            items.append(
                Item(
                    path=str(path),
                    label=label,
                    split=split,
                    corpus=corpus,
                    speaker=path.parent.name,
                    attack=attack,
                    lang=lang,
                    condition=condition,
                )
            )
    return items
