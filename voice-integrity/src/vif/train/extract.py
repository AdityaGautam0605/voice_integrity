"""One-time feature extraction.

The single most consequential engineering decision in the project: run the
300M-parameter front end forward-only, once, and cache the result.  After
that, training the head costs minutes per experiment instead of a day.  Over a
prep period that is the difference between five experiments and several
hundred, and it is why the whole project fits inside a free GPU budget.

Roughly 30 minutes on a free T4 for the ASVspoof LA training split.

Note what is *not* cached: augmented audio.  Codec degradation is applied
before the front end, so the baseline and codec-robust models need separate
caches.  That is deliberate - conflating them would silently compare a model
against itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from tqdm import tqdm

from vif.common.config import AppConfig
from vif.common.logging import get_logger
from vif.data.augment import CodecAugmenter
from vif.data.datasets import crop_or_pad, load_audio
from vif.data.manifests import Item, write_manifest

log = get_logger(__name__)


def extract_features(
    items: list[Item],
    config: AppConfig,
    out_dir: str | Path,
    device: str = "cuda",
    augment: bool = False,
    batch_size: int = 8,
    overwrite: bool = False,
    seed: int = 0,
) -> Path:
    """Run the front end over a manifest and cache one .npy per item.

    Layout:  <out_dir>/<index>.npy  with shape (T, hidden_dim)

    The index is the position in the manifest, so `FeatureDataset` can pair
    features with labels without storing paths twice.
    """
    import torch

    from vif.models.frontend import SSLFrontend

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frontend = SSLFrontend(config.model.frontend).to(device).eval()
    augmenter = (
        CodecAugmenter(config.augment, config.model.audio.sample_rate, seed=seed)
        if augment
        else None
    )

    window = config.model.audio.window_samples
    sample_rate = config.model.audio.sample_rate
    rng = np.random.default_rng(seed)
    written = 0

    # The augmenter is stochastic, so the codec actually applied to each item
    # is logged as each batch lands.  A run killed part-way - a Colab
    # disconnect - then resumes with its codec labels intact, instead of
    # relabelling everything already extracted as the manifest default.
    conditions_log = out_dir / "conditions.jsonl"
    if overwrite and conditions_log.exists():
        conditions_log.unlink()
    realised: dict[int, str] = {}
    if conditions_log.exists():
        with conditions_log.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    realised[int(row["index"])] = row["condition"]

    for start in tqdm(range(0, len(items), batch_size), desc="extracting", unit="batch"):
        batch_items = items[start : start + batch_size]
        batch_wavs, batch_indices = [], []

        for offset, item in enumerate(batch_items):
            index = start + offset
            if (out_dir / f"{index}.npy").exists() and not overwrite:
                continue
            try:
                wav = load_audio(item.path, sample_rate)
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping %s: %s", item.path, exc)
                continue

            condition = item.condition
            if augmenter is not None:
                wav, condition = augmenter(wav)
            realised[index] = condition

            batch_wavs.append(crop_or_pad(wav, window, rng))
            batch_indices.append(index)

        if not batch_wavs:
            continue

        with torch.inference_mode():
            tensor = torch.from_numpy(np.stack(batch_wavs)).float().to(device)
            feats = frontend(tensor).cpu().numpy().astype(np.float16)

        # Log first, then save.  A killed process (a Colab disconnect) loses
        # unflushed writes, and a feature file without its log line would
        # resume under the manifest's default label.  A log line without its
        # feature file is harmless: the item is re-extracted and re-logged.
        with conditions_log.open("a", encoding="utf-8") as fh:
            for index in batch_indices:
                fh.write(json.dumps({"index": index, "condition": realised[index]}) + "\n")
        for index, feat in zip(batch_indices, feats, strict=True):
            np.save(out_dir / f"{index}.npy", feat)
            written += 1

    for index, item in enumerate(items):
        if index in realised:
            item.condition = realised[index]
    write_manifest(items, out_dir / "manifest.jsonl")

    log.info("wrote %d feature files to %s", written, out_dir)
    return out_dir


def verify_cache(out_dir: str | Path, n_items: int, hidden_dim: int = 1024) -> tuple[bool, str]:
    """Check a cache before training against it.

    Catches the two failures that otherwise surface as a confusing loss curve:
    a partially written cache, and a dimension mismatch from a changed front
    end.
    """
    out_dir = Path(out_dir)
    missing = [i for i in range(n_items) if not (out_dir / f"{i}.npy").exists()]
    if missing:
        return False, f"{len(missing)} of {n_items} feature files missing (first: {missing[:5]})"

    sample = np.load(out_dir / "0.npy")
    if sample.ndim != 2:
        return False, f"expected 2-D features, got shape {sample.shape}"
    if sample.shape[1] != hidden_dim:
        return False, (
            f"feature dim {sample.shape[1]} does not match the configured "
            f"front end ({hidden_dim}) - the cache is from a different model"
        )
    return True, f"{n_items} files, shape (T, {sample.shape[1]}), dtype {sample.dtype}"
