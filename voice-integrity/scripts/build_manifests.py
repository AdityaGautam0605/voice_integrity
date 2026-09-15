#!/usr/bin/env python3
"""Build JSONL manifests from downloaded corpora.

Run this once the corpora are in data/raw/.  Prints a summary per split - read
the `spoof_per_bonafide` line, because that ratio is why accuracy is a
meaningless metric on this data.

    PYTHONPATH=src python scripts/build_manifests.py --asvspoof data/raw/LA
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vif.data.manifests import (  # noqa: E402
    build_asvspoof19_la,
    build_from_directory,
    build_in_the_wild,
    summarise,
    write_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asvspoof", type=str, help="root of the ASVspoof 2019 LA release")
    parser.add_argument("--in-the-wild", type=str, help="root of the In-the-Wild release")
    parser.add_argument(
        "--indian-real", type=str, help="directory of genuine Indian-language audio"
    )
    parser.add_argument("--indian-fake", type=str, help="directory of cloned Indian-language audio")
    parser.add_argument("--out", type=str, default="data/manifests")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    built = 0

    if args.asvspoof:
        for split in ("train", "dev", "eval"):
            try:
                items = build_asvspoof19_la(args.asvspoof, split)
            except FileNotFoundError as exc:
                print(f"  skip asvspoof {split}: {exc}")
                continue
            write_manifest(items, out_dir / f"asvspoof19la_{split}.jsonl")
            print(f"\nasvspoof19la_{split}")
            print(json.dumps(summarise(items), indent=2))
            built += 1

    if args.in_the_wild:
        items = build_in_the_wild(args.in_the_wild)
        write_manifest(items, out_dir / "in_the_wild_eval.jsonl")
        print("\nin_the_wild_eval")
        print(json.dumps(summarise(items), indent=2))
        built += 1

    if args.indian_real and args.indian_fake:
        real = build_from_directory(
            args.indian_real, label="bonafide", split="eval", corpus="indian", lang="multi"
        )
        fake = build_from_directory(
            args.indian_fake,
            label="spoof",
            split="eval",
            corpus="indian",
            lang="multi",
            attack="xtts_v2",
        )
        items = real + fake
        write_manifest(items, out_dir / "indian_eval.jsonl")
        print("\nindian_eval")
        print(json.dumps(summarise(items), indent=2))
        built += 1

    if built == 0:
        parser.print_help()
        return 1

    print(f"\nbuilt {built} manifest(s) in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
