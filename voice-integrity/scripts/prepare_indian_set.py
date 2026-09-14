#!/usr/bin/env python3
"""Build the Indian-language evaluation set.

No public Indian-language deepfake corpus exists, so we make one.  The recipe:

    Real   AI4Bharat Kathbath / IndicSUPERB    real speakers, ~12 languages
    Fake   clone those SAME speakers           XTTS-v2, OpenVoice v2, F5-TTS,
                                               AI4Bharat Indic Parler-TTS
    Both   through the codec chain             matched pairs, phone conditions

Cloning the same speakers is what makes it an evaluation set rather than two
unrelated piles of audio: the model cannot succeed by learning speaker
identity or recording conditions.

Using several generators matters too.  If every fake comes from one tool, the
model learns that tool instead of learning synthesis.

Around 500 clips is plenty.  This is evidence for one slide, not a research
corpus.

Cloning tools pull conflicting dependencies - install them in a SEPARATE
environment and run the cloning stage there.  This script handles selection,
manifest construction and codec degradation; `--clone-cmd` shells out to
whatever synthesiser you have.

    PYTHONPATH=src python scripts/prepare_indian_set.py \
        --real-root data/raw/kathbath --out data/raw/indian --per-language 20
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vif.common.config import load_config  # noqa: E402
from vif.common.logging import get_logger  # noqa: E402
from vif.data.augment import CodecAugmenter  # noqa: E402
from vif.data.datasets import load_audio  # noqa: E402
from vif.data.manifests import Item, summarise, write_manifest  # noqa: E402

log = get_logger("indian_set")

INDIC_LANGUAGES = (
    "hindi",
    "tamil",
    "telugu",
    "bengali",
    "marathi",
    "gujarati",
    "kannada",
    "malayalam",
    "odia",
    "punjabi",
    "urdu",
    "assamese",
)


def select_reference_clips(real_root: Path, per_language: int) -> dict[str, list[Path]]:
    """Pick a handful of clips per language, grouped by speaker directory."""
    selected: dict[str, list[Path]] = {}
    for language in INDIC_LANGUAGES:
        lang_dir = next(
            (d for d in real_root.rglob("*") if d.is_dir() and d.name.lower() == language),
            None,
        )
        if lang_dir is None:
            continue
        clips = sorted(lang_dir.rglob("*.wav")) + sorted(lang_dir.rglob("*.flac"))
        if clips:
            selected[language] = clips[:per_language]
            log.info("%-10s %d reference clips", language, len(selected[language]))
    return selected


def clone_with_command(
    reference: Path,
    text: str,
    out_path: Path,
    clone_cmd: str,
) -> bool:
    """Invoke an external synthesiser.

    The command template receives {reference}, {text} and {out}.  Example for
    Coqui XTTS-v2:

        --clone-cmd 'tts --model_name tts_models/multilingual/multi-dataset/xtts_v2 \
            --speaker_wav {reference} --text "{text}" --out_path {out}'
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = clone_cmd.format(reference=str(reference), text=text, out=str(out_path))
    try:
        subprocess.run(command, shell=True, check=True, capture_output=True, timeout=300)
        return out_path.exists()
    except subprocess.SubprocessError as exc:
        log.warning("cloning failed for %s: %s", reference.name, exc)
        return False


def degrade_directory(
    source_dir: Path,
    out_dir: Path,
    config,
    seed: int = 0,
) -> list[Path]:
    """Push a directory of audio through the telephony chain."""
    import soundfile as sf

    augmenter = CodecAugmenter(config.augment, config.model.audio.sample_rate, seed=seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for path in sorted(source_dir.rglob("*.wav")):
        try:
            wav = load_audio(path, config.model.audio.sample_rate)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping %s: %s", path, exc)
            continue
        degraded, condition = augmenter(wav)
        # Keep the <language>/ folder: it is the only record of the clip's language.
        target = out_dir / path.relative_to(source_dir).parent / f"{path.stem}__{condition}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(target), np.clip(degraded, -1.0, 1.0), config.model.audio.sample_rate)
        written.append(target)

    log.info("wrote %d degraded clips to %s", len(written), out_dir)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-root", required=True, help="root of the genuine Indic corpus")
    parser.add_argument("--out", default="data/raw/indian")
    parser.add_argument("--per-language", type=int, default=20)
    parser.add_argument("--clone-cmd", help="synthesiser command template")
    parser.add_argument("--text", default="Please approve the transfer immediately.")
    parser.add_argument("--degrade", action="store_true", help="apply the codec chain")
    parser.add_argument("--config", default="configs")
    args = parser.parse_args()

    config = load_config(args.config)
    out_root = Path(args.out)
    real_out, fake_out = out_root / "real", out_root / "fake"

    selected = select_reference_clips(Path(args.real_root), args.per_language)
    if not selected:
        log.error("no language directories found under %s", args.real_root)
        return 1

    items: list[Item] = []

    import soundfile as sf

    sample_rate = config.model.audio.sample_rate
    for language, clips in selected.items():
        for clip in clips:
            # The genuine half is written to <out>/real/<language>/ as 16 kHz wav.
            # --degrade and notebook 07 both read it from there; without the copy
            # only the fakes were ever degraded, so codec condition tracked the label.
            real_copy = real_out / language / f"{clip.stem}.wav"
            if not real_copy.exists():
                try:
                    wav = load_audio(clip, sample_rate)
                except Exception as exc:  # noqa: BLE001
                    log.warning("skipping %s: %s", clip, exc)
                    continue
                real_copy.parent.mkdir(parents=True, exist_ok=True)
                sf.write(str(real_copy), wav, sample_rate)
            items.append(
                Item(
                    path=str(real_copy),
                    label="bonafide",
                    split="eval",
                    corpus="indian",
                    speaker=clip.parent.name,
                    lang=language,
                    condition="clean",
                )
            )

            if args.clone_cmd:
                target = fake_out / language / f"{clip.stem}_xtts.wav"
                if clone_with_command(clip, args.text, target, args.clone_cmd):
                    items.append(
                        Item(
                            path=str(target),
                            label="spoof",
                            split="eval",
                            corpus="indian",
                            speaker=clip.parent.name,
                            attack="xtts_v2",
                            lang=language,
                            condition="clean",
                        )
                    )

    if args.degrade:
        for source, destination in (
            (real_out, out_root / "real_codec"),
            (fake_out, out_root / "fake_codec"),
        ):
            if source.exists():
                degrade_directory(source, destination, config)

    manifest = Path("data/manifests/indian_eval.jsonl")
    write_manifest(items, manifest)
    print(json.dumps(summarise(items), indent=2))
    print(f"\nwrote {manifest}")

    if not args.clone_cmd:
        print(
            "\nNo --clone-cmd given, so only genuine clips were catalogued.\n"
            "Run the cloning stage in a separate environment, then re-run with\n"
            "--clone-cmd to add the spoofed half."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
