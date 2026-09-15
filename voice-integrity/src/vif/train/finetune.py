"""Final fine-tuning run (P7).

One overnight pass on Kaggle: release the top N transformer layers of the
front end and train them jointly with the head at a low learning rate.

Why top-6 rather than a full unfreeze: it captures most of the gain, fits
comfortably in 16 GB with mixed precision and gradient accumulation, and will
not run out of memory at 2am the night before.  The P2 checkpoints are kept as
the fallback if this run disappoints.

This path reads audio rather than cached features, because the front end is
now being trained and its outputs change every step.  That is the reason it
costs a night instead of two minutes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vif.common.config import AppConfig
from vif.common.logging import get_logger
from vif.data.manifests import Item
from vif.eval.metrics import evaluate

log = get_logger(__name__)


@dataclass
class FinetuneConfig:
    epochs: int = 4
    batch_size: int = 4
    grad_accum_steps: int = 8  # effective batch 32
    frontend_lr: float = 1e-6  # deliberately tiny: this is a fine-tune
    head_lr: float = 5e-5
    weight_decay: float = 1e-4
    mixed_precision: bool = True
    grad_clip: float = 5.0
    gradient_checkpointing: bool = True
    seed: int = 1234


def finetune(
    config: AppConfig,
    train_items: list[Item],
    dev_items: list[Item],
    head,
    ft_config: FinetuneConfig | None = None,
    device: str = "cuda",
    checkpoint_path: str | Path = "models/checkpoints/finetuned.pt",
    augment: bool = True,
):
    """Joint fine-tune of the top front-end layers plus the head."""
    import torch
    from torch.utils.data import DataLoader

    from vif.data.augment import CodecAugmenter
    from vif.data.datasets import AudioDataset
    from vif.models.frontend import SSLFrontend
    from vif.models.heads import save_checkpoint

    cfg = ft_config or FinetuneConfig()
    torch.manual_seed(cfg.seed)

    # Release the top N layers.  Everything below stays frozen.
    frontend_cfg = config.model.frontend.model_copy(update={"frozen": False})
    frontend = SSLFrontend(frontend_cfg).to(device)
    if cfg.gradient_checkpointing:
        try:
            frontend.model.gradient_checkpointing_enable()
            log.info("gradient checkpointing enabled - slower per step, much less memory")
        except Exception:  # noqa: BLE001
            log.warning("gradient checkpointing unavailable for this model")

    head = head.to(device)

    augmenter = (
        CodecAugmenter(config.augment, config.model.audio.sample_rate, seed=cfg.seed)
        if augment
        else None
    )
    train_ds = AudioDataset(
        train_items,
        window_samples=config.model.audio.window_samples,
        sample_rate=config.model.audio.sample_rate,
        augmenter=augmenter,
        random_crop=True,
        seed=cfg.seed,
    )
    dev_ds = AudioDataset(
        dev_items,
        window_samples=config.model.audio.window_samples,
        sample_rate=config.model.audio.sample_rate,
        augmenter=None,
        random_crop=False,
    )
    train_loader = DataLoader(train_ds, batch_size=cfg.batch_size, shuffle=True, drop_last=True)
    dev_loader = DataLoader(dev_ds, batch_size=cfg.batch_size, shuffle=False)

    # Two parameter groups: the front end moves far more slowly than the head.
    trainable_frontend = [p for p in frontend.parameters() if p.requires_grad]
    optimiser = torch.optim.AdamW(
        [
            {"params": trainable_frontend, "lr": cfg.frontend_lr},
            {"params": head.parameters(), "lr": cfg.head_lr},
        ],
        weight_decay=cfg.weight_decay,
    )
    scaler = torch.amp.GradScaler("cuda", enabled=cfg.mixed_precision and device == "cuda")
    criterion = torch.nn.CrossEntropyLoss()

    best_eer = float("inf")
    for epoch in range(cfg.epochs):
        frontend.train()
        head.train()
        started = time.time()
        losses = []
        optimiser.zero_grad(set_to_none=True)

        for step, (wav, targets, _) in enumerate(train_loader):
            wav, targets = wav.to(device), targets.to(device)
            with torch.autocast("cuda", enabled=cfg.mixed_precision and device == "cuda"):
                feats = frontend(wav)
                logits = head(feats)
                loss = criterion(logits, targets) / cfg.grad_accum_steps

            scaler.scale(loss).backward()
            losses.append(float(loss.item()) * cfg.grad_accum_steps)

            if (step + 1) % cfg.grad_accum_steps == 0:
                scaler.unscale_(optimiser)
                torch.nn.utils.clip_grad_norm_(
                    list(head.parameters()) + trainable_frontend, cfg.grad_clip
                )
                scaler.step(optimiser)
                scaler.update()
                optimiser.zero_grad(set_to_none=True)

        labels, scores = _evaluate(frontend, head, dev_loader, device, cfg)
        metrics = evaluate(labels, scores)
        log.info(
            "epoch %d/%d  loss %.4f  %s  (%.0f min)",
            epoch + 1,
            cfg.epochs,
            float(np.mean(losses)),
            metrics.summary(),
            (time.time() - started) / 60,
        )

        if metrics.eer < best_eer:
            best_eer = metrics.eer
            save_checkpoint(
                head,
                checkpoint_path,
                arch=config.model.head.arch,
                feat_dim=frontend.hidden_dim,
                frontend_id=config.model.frontend.model_id,
                window_samples=config.model.audio.window_samples,
                condition="finetuned",
                epoch=epoch,
                metrics=metrics.as_dict(),
            )
            frontend_path = Path(checkpoint_path).with_name("frontend_top_layers.pt")
            torch.save(
                {
                    "state_dict": {
                        k: v for k, v in frontend.state_dict().items() if "encoder.layers" in k
                    },
                    "unfreeze_top_n": config.model.frontend.unfreeze_top_n,
                    "model_id": config.model.frontend.model_id,
                },
                frontend_path,
            )
            log.info("saved released front-end layers to %s", frontend_path)

    return frontend, head, best_eer


def _evaluate(frontend, head, loader, device: str, cfg: FinetuneConfig):
    import torch

    frontend.eval()
    head.eval()
    all_labels, all_scores = [], []
    with torch.inference_mode():
        for wav, targets, _ in loader:
            with torch.autocast("cuda", enabled=cfg.mixed_precision and device == "cuda"):
                logits = head(frontend(wav.to(device)))
            all_scores.append(head.score_from_logits(logits).float().cpu().numpy())
            all_labels.append(targets.numpy())
    return np.concatenate(all_labels), np.concatenate(all_scores)
