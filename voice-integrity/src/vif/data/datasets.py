"""Torch datasets over manifests.

Two shapes, matching the two halves of the training workflow:

    AudioDataset    reads audio, optionally degrades it.  Used by the one-time
                    feature extraction pass and by the final fine-tune run.
    FeatureDataset  reads cached .npy features.  Used for head training, where
                    an experiment must cost minutes rather than a day.

The second is the reason the whole project fits in a free GPU budget.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from vif.common.logging import get_logger
from vif.data.augment import CodecAugmenter
from vif.data.manifests import Item

log = get_logger(__name__)

try:  # torch is optional at import time so the data layer stays testable
    import torch
    from torch.utils.data import Dataset

    _TORCH = True
except ImportError:  # pragma: no cover
    _TORCH = False

    class Dataset:  # type: ignore[no-redef]
        pass


def load_audio(path: str | Path, sample_rate: int = 16000) -> np.ndarray:
    """Read any supported file to mono float32 at the target rate."""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != sample_rate:
        import librosa

        data = librosa.resample(data, orig_sr=sr, target_sr=sample_rate)
    return np.ascontiguousarray(data, dtype=np.float32)


def crop_or_pad(wav: np.ndarray, length: int, rng: np.random.Generator | None = None) -> np.ndarray:
    """Force an utterance to exactly `length` samples.

    Short audio is tiled rather than zero-padded: a long silent tail would be
    dropped by the VAD gate at inference time, so training on it teaches the
    model something it will never see.
    """
    if len(wav) == 0:
        # An empty or fully corrupt file.  Tiling nothing yields nothing, and a
        # short array would break batch collation mid-epoch.
        return np.zeros(length, dtype=np.float32)
    if len(wav) == length:
        return wav
    if len(wav) > length:
        if rng is None:
            start = (len(wav) - length) // 2
        else:
            start = int(rng.integers(0, len(wav) - length + 1))
        return wav[start : start + length]
    reps = int(np.ceil(length / len(wav)))
    return np.tile(wav, reps)[:length]


class AudioDataset(Dataset):
    """Raw audio, with optional on-the-fly telephony degradation.

    `augmenter` is the single switch between the baseline model and the
    codec-robust one.  Everything else about the two training runs is
    identical, which is what makes the comparison meaningful.
    """

    def __init__(
        self,
        items: list[Item],
        window_samples: int = 64600,
        sample_rate: int = 16000,
        augmenter: CodecAugmenter | None = None,
        random_crop: bool = True,
        seed: int = 0,
    ):
        self.items = items
        self.window_samples = window_samples
        self.sample_rate = sample_rate
        self.augmenter = augmenter
        self.random_crop = random_crop
        self.rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int):
        item = self.items[index]
        wav = load_audio(item.path, self.sample_rate)
        if self.augmenter is not None:
            wav, _condition = self.augmenter(wav)
        wav = crop_or_pad(wav, self.window_samples, self.rng if self.random_crop else None)
        if _TORCH:
            return torch.from_numpy(wav), item.target, index
        return wav, item.target, index


class FeatureDataset(Dataset):
    """Cached self-supervised features.

    Layout written by the extraction pass:

        features/<manifest-stem>/<index>.npy   shape (T, hidden_dim)

    Loading these instead of re-running a 300M-parameter encoder is the
    difference between five experiments and several hundred.
    """

    def __init__(self, feature_dir: str | Path, items: list[Item], max_frames: int | None = None):
        self.feature_dir = Path(feature_dir)
        self.items = items
        self.max_frames = max_frames
        if not self.feature_dir.exists():
            raise FileNotFoundError(
                f"feature cache not found: {self.feature_dir}. Run the extraction pass first."
            )
        # Items whose audio was unreadable at extraction have no feature file.
        # Skip them here rather than crash mid-epoch, and keep the original
        # manifest index so scores still line up with the manifest.
        self.indices = [i for i in range(len(items)) if (self.feature_dir / f"{i}.npy").exists()]
        missing = len(items) - len(self.indices)
        if missing:
            log.warning(
                "%d of %d items have no cached features in %s - skipping them",
                missing,
                len(items),
                self.feature_dir,
            )

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, position: int):
        index = self.indices[position]
        feats = np.load(self.feature_dir / f"{index}.npy")
        if self.max_frames is not None and feats.shape[0] > self.max_frames:
            feats = feats[: self.max_frames]
        target = self.items[index].target
        if _TORCH:
            return torch.from_numpy(feats.astype(np.float32)), target, index
        return feats.astype(np.float32), target, index


def collate_features(batch):
    """Pad a batch of variable-length feature sequences to the longest member."""
    if not _TORCH:  # pragma: no cover
        raise RuntimeError("torch is required for collate_features")
    feats, targets, indices = zip(*batch, strict=True)
    max_len = max(f.shape[0] for f in feats)
    dim = feats[0].shape[1]
    padded = torch.zeros(len(feats), max_len, dim, dtype=torch.float32)
    mask = torch.zeros(len(feats), max_len, dtype=torch.bool)
    for i, f in enumerate(feats):
        padded[i, : f.shape[0]] = f
        mask[i, : f.shape[0]] = True
    return padded, mask, torch.tensor(targets, dtype=torch.long), torch.tensor(indices)
