"""Detection heads and the composed detector.

`LightHead` exists for two reasons: it is the day-one sanity check that the
whole pipeline works before AASIST is wired in, and it is a candidate for the
on-device cascade tier where the SSL front end cannot run in real time.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn

from vif.common.config import HeadConfig
from vif.common.logging import get_logger
from vif.models.aasist import AASIST, count_parameters

log = get_logger(__name__)


class AttentiveStatsPool(nn.Module):
    """Attention-weighted mean and standard deviation over time."""

    def __init__(self, in_dim: int, bottleneck: int = 128):
        super().__init__()
        self.attention = nn.Sequential(
            nn.Conv1d(in_dim, bottleneck, kernel_size=1),
            nn.ReLU(),
            nn.BatchNorm1d(bottleneck),
            nn.Tanh(),
            nn.Conv1d(bottleneck, in_dim, kernel_size=1),
        )

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        # x: (B, T, C)
        x = x.transpose(1, 2)  # (B, C, T)
        weights = self.attention(x)
        if mask is not None:
            weights = weights.masked_fill(~mask.unsqueeze(1), float("-inf"))
        weights = torch.softmax(weights, dim=-1)
        mean = torch.sum(x * weights, dim=-1)
        variance = torch.sum((x**2) * weights, dim=-1) - mean**2
        std = torch.sqrt(variance.clamp(min=1e-8))
        return torch.cat([mean, std], dim=-1)


class LightHead(nn.Module):
    """Attentive pooling plus a small MLP.

    Not a throwaway: this is the sanity check for P0/P1 and the candidate
    architecture for the edge tier.
    """

    def __init__(self, feat_dim: int = 1024, hidden: int = 256, dropout: float = 0.3):
        super().__init__()
        self.pool = AttentiveStatsPool(feat_dim)
        self.net = nn.Sequential(
            nn.Linear(feat_dim * 2, hidden),
            nn.BatchNorm1d(hidden),
            nn.SELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, 2),
        )

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        return self.net(self.pool(x, mask))

    @staticmethod
    def score_from_logits(logits: torch.Tensor) -> torch.Tensor:
        return logits[:, 1] - logits[:, 0]


def build_head(config: HeadConfig, feat_dim: int = 1024) -> nn.Module:
    if config.arch == "aasist":
        head = AASIST(
            feat_dim=feat_dim,
            gat_dims=tuple(config.gat_dims),
            pool_ratios=tuple(config.pool_ratios),
            temperatures=tuple(config.temperatures),
        )
    elif config.arch == "light":
        head = LightHead(feat_dim=feat_dim)
    else:
        raise ValueError(f"unknown head arch: {config.arch}")
    log.info("built %s head with %d trainable parameters", config.arch, count_parameters(head))
    return head


def save_checkpoint(
    head: nn.Module,
    path: str | Path,
    *,
    arch: str,
    feat_dim: int,
    frontend_id: str,
    window_samples: int,
    condition: str,
    epoch: int = 0,
    metrics: dict | None = None,
) -> None:
    """Persist a head together with everything needed to reject a mismatch.

    `window_samples` and `frontend_id` are stored so that loading a checkpoint
    against the wrong geometry or a different front end fails loudly instead of
    degrading accuracy silently.  See FR-CO-03 and FR-DE-01.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": head.state_dict(),
            "arch": arch,
            "feat_dim": feat_dim,
            "frontend_id": frontend_id,
            "window_samples": window_samples,
            "condition": condition,
            "epoch": epoch,
            "metrics": metrics or {},
        },
        path,
    )
    log.info("saved %s checkpoint (%s) to %s", arch, condition, path)


def load_checkpoint(
    path: str | Path,
    config: HeadConfig,
    feat_dim: int = 1024,
    *,
    expect_window: int | None = None,
    expect_frontend: str | None = None,
    strict: bool = True,
) -> tuple[nn.Module, dict]:
    """Load a head and verify it matches the running configuration.

    A window or front-end mismatch raises rather than warns.  This is the
    startup guard the SRS calls for: the failure mode of a silent mismatch is
    quietly worse accuracy that nobody notices until the demo.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"checkpoint not found: {path}")

    blob = torch.load(path, map_location="cpu", weights_only=False)
    meta = {k: v for k, v in blob.items() if k != "state_dict"}

    if strict:
        if expect_window is not None and meta.get("window_samples") not in (None, expect_window):
            raise ValueError(
                f"window mismatch: checkpoint trained at {meta['window_samples']} samples, "
                f"runtime configured for {expect_window}. Refusing to start."
            )
        if expect_frontend is not None and meta.get("frontend_id") not in (None, expect_frontend):
            raise ValueError(
                f"front-end mismatch: checkpoint used {meta['frontend_id']}, "
                f"runtime configured for {expect_frontend}. Refusing to start."
            )

    head = build_head(config, feat_dim=meta.get("feat_dim", feat_dim))
    head.load_state_dict(blob["state_dict"])
    head.eval()
    log.info("loaded %s checkpoint (%s) from %s", meta.get("arch"), meta.get("condition"), path)
    return head, meta
