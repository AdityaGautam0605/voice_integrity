"""Head training over cached features.

Minutes per run, which is the whole point of the extraction pass.  The two
models the project compares - baseline and codec-robust - are trained by this
same function with identical hyperparameters; the only thing that differs is
which feature cache they read.  Keeping that difference to a single argument
is what makes the comparison meaningful.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from vif.common.logging import get_logger
from vif.data.manifests import Item
from vif.eval.metrics import evaluate

log = get_logger(__name__)


@dataclass
class TrainConfig:
    epochs: int = 25
    batch_size: int = 32
    learning_rate: float = 1e-4
    weight_decay: float = 1e-4
    max_frames: int = 208
    class_weighting: bool = True
    grad_clip: float = 5.0
    early_stop_patience: int = 6
    seed: int = 1234


@dataclass
class TrainHistory:
    train_loss: list[float] = field(default_factory=list)
    dev_eer: list[float] = field(default_factory=list)
    dev_tpr: list[float] = field(default_factory=list)
    best_epoch: int = 0
    best_eer: float = float("inf")  # EER is in [0, 1], so epoch one always saves

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def train_head(
    head,
    train_items: list[Item],
    dev_items: list[Item],
    train_features: str | Path,
    dev_features: str | Path,
    train_config: TrainConfig | None = None,
    device: str = "cuda",
    checkpoint_path: str | Path | None = None,
    checkpoint_meta: dict | None = None,
) -> TrainHistory:
    """Train a head, selecting on dev EER rather than on loss.

    Model selection on EER matters here: the training corpus carries roughly
    nine spoofed utterances per bonafide one, so loss and accuracy both track
    the majority class and would happily select a useless model.
    """
    import torch
    from torch.utils.data import DataLoader

    from vif.data.datasets import FeatureDataset, collate_features
    from vif.models.heads import save_checkpoint

    if checkpoint_path is not None:
        required = {"arch", "feat_dim", "frontend_id", "window_samples", "condition"}
        missing = required - set(checkpoint_meta or {})
        if missing:
            # Fail before training rather than after the first epoch has run.
            raise ValueError(f"checkpoint_meta is missing {sorted(missing)}")

    cfg = train_config or TrainConfig()
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    train_ds = FeatureDataset(train_features, train_items, max_frames=cfg.max_frames)
    dev_ds = FeatureDataset(dev_features, dev_items, max_frames=cfg.max_frames)

    train_loader = DataLoader(
        train_ds,
        batch_size=cfg.batch_size,
        shuffle=True,
        collate_fn=collate_features,
        drop_last=True,
    )
    dev_loader = DataLoader(
        dev_ds, batch_size=cfg.batch_size, shuffle=False, collate_fn=collate_features
    )

    head = head.to(device)
    optimiser = torch.optim.AdamW(
        head.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=cfg.epochs)

    # Class weights counteract the 9:1 imbalance so the minority (bonafide)
    # class is not simply ignored.
    if cfg.class_weighting:
        n_bona = sum(1 for i in train_items if i.target == 1)
        n_spoof = len(train_items) - n_bona
        weights = torch.tensor(
            [len(train_items) / (2 * max(n_spoof, 1)), len(train_items) / (2 * max(n_bona, 1))],
            dtype=torch.float32,
            device=device,
        )
        log.info("class weights: spoof=%.3f bonafide=%.3f", weights[0].item(), weights[1].item())
    else:
        weights = None

    criterion = torch.nn.CrossEntropyLoss(weight=weights)
    history = TrainHistory()
    patience = 0
    best_state = None

    for epoch in range(cfg.epochs):
        head.train()
        started = time.time()
        losses = []

        for feats, _mask, targets, _ in train_loader:
            feats, targets = feats.to(device), targets.to(device)
            optimiser.zero_grad(set_to_none=True)
            logits = head(feats)
            loss = criterion(logits, targets)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(head.parameters(), cfg.grad_clip)
            optimiser.step()
            losses.append(float(loss.item()))

        scheduler.step()
        mean_loss = float(np.mean(losses)) if losses else float("nan")

        labels, scores = predict(head, dev_loader, device)
        metrics = evaluate(labels, scores)

        history.train_loss.append(mean_loss)
        history.dev_eer.append(metrics.eer)
        history.dev_tpr.append(metrics.tpr_at_1pct_fpr)

        log.info(
            "epoch %2d/%d  loss %.4f  %s  (%.0fs)",
            epoch + 1,
            cfg.epochs,
            mean_loss,
            metrics.summary(),
            time.time() - started,
        )

        if metrics.eer < history.best_eer:
            history.best_eer = metrics.eer
            history.best_epoch = epoch
            patience = 0
            best_state = {k: v.detach().clone() for k, v in head.state_dict().items()}
            if checkpoint_path is not None:
                save_checkpoint(
                    head,
                    checkpoint_path,
                    epoch=epoch,
                    metrics=metrics.as_dict(),
                    **(checkpoint_meta or {}),
                )
        else:
            patience += 1
            if patience >= cfg.early_stop_patience:
                log.info(
                    "early stop at epoch %d (best epoch %d)", epoch + 1, history.best_epoch + 1
                )
                break

    if best_state is not None:
        # Training runs up to `early_stop_patience` epochs past the best, and the
        # notebooks calibrate on the returned head - so it must be the saved one.
        head.load_state_dict(best_state)

    history.save(
        Path(checkpoint_path).with_suffix(".history.json") if checkpoint_path else "history.json"
    )
    return history


def predict(head, loader, device: str = "cuda") -> tuple[np.ndarray, np.ndarray]:
    """Score a loader.  Returns (labels, scores) with higher = more synthetic."""
    import torch

    head.eval()
    all_labels, all_scores = [], []
    with torch.inference_mode():
        for feats, _mask, targets, _ in loader:
            logits = head(feats.to(device))
            scores = head.score_from_logits(logits)
            all_scores.append(scores.cpu().numpy())
            all_labels.append(targets.numpy())
    return np.concatenate(all_labels), np.concatenate(all_scores)


def score_manifest(
    head,
    items: list[Item],
    feature_dir: str | Path,
    device: str = "cuda",
    batch_size: int = 32,
    max_frames: int = 208,
) -> dict[int, float]:
    """Score every item, keyed by manifest index.

    The index keying is what lets the eval runner slice by attack, condition
    or language without rescoring.
    """
    import torch
    from torch.utils.data import DataLoader

    from vif.data.datasets import FeatureDataset, collate_features

    ds = FeatureDataset(feature_dir, items, max_frames=max_frames)
    loader = DataLoader(ds, batch_size=batch_size, shuffle=False, collate_fn=collate_features)

    head = head.to(device).eval()
    scores: dict[int, float] = {}
    with torch.inference_mode():
        for feats, _mask, _targets, indices in loader:
            logits = head(feats.to(device))
            batch_scores = head.score_from_logits(logits).cpu().numpy()
            for index, score in zip(indices.numpy(), batch_scores, strict=True):
                scores[int(index)] = float(score)
    return scores
