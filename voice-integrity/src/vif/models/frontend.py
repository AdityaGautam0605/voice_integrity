"""Self-supervised front end.

`facebook/wav2vec2-xls-r-300m`: 300M parameters, pretrained on 436k hours
across 128 languages including Indic ones.  That pretraining is load-bearing
for the multilingual claim - it is the difference between answering "why would
this work in Tamil?" with a fact rather than a hope.

Two modes, and the split matters more than any other engineering decision here:

    frozen  (development)  run once, cache to disk, train the head in minutes
    partial (final run)    release the top N transformer layers, one overnight
                           pass, and those are the presentation numbers

Fine-tuning from the start turns a two-minute experiment into a day-long one,
which over a prep period is the difference between five experiments and
several hundred.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

from vif.common.config import FrontendConfig
from vif.common.logging import get_logger

log = get_logger(__name__)


class SSLFrontend(nn.Module):
    """Wraps a wav2vec2-family encoder and exposes hidden states.

    Deliberately thin.  The only logic here is freezing policy and layer
    selection, because everything else about this model must be byte-identical
    between training and inference (FR-DE-01).
    """

    def __init__(self, config: FrontendConfig | None = None):
        super().__init__()
        self.config = config or FrontendConfig()
        from transformers import Wav2Vec2Model

        self.model = Wav2Vec2Model.from_pretrained(self.config.model_id)
        self.hidden_dim = self.model.config.hidden_size
        if self.hidden_dim != self.config.hidden_dim:
            log.warning(
                "config declares hidden_dim=%d but %s provides %d - using the model's value",
                self.config.hidden_dim,
                self.config.model_id,
                self.hidden_dim,
            )
        self.apply_freezing()

    def apply_freezing(self) -> None:
        """Freeze everything, then optionally release the top N layers."""
        for param in self.model.parameters():
            param.requires_grad = False

        if self.config.frozen:
            self.model.eval()
            log.info("front end fully frozen (%s)", self.config.model_id)
            return

        n = self.config.unfreeze_top_n
        layers = self.model.encoder.layers
        for layer in layers[-n:]:
            for param in layer.parameters():
                param.requires_grad = True
        trainable = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        log.info(
            "released top %d of %d transformer layers (%.1fM trainable)",
            n,
            len(layers),
            trainable / 1e6,
        )

    def train(self, mode: bool = True):
        """Keep a fully frozen front end in eval mode regardless of the caller.

        Otherwise dropout and any remaining batch statistics would differ
        between the cached-feature pass and live inference, and the cache would
        no longer match what the model sees at serve time.
        """
        super().train(mode)
        if self.config.frozen:
            self.model.eval()
        return self

    def forward(self, wav: torch.Tensor) -> torch.Tensor:
        """wav: (B, samples) float32 in [-1, 1] -> (B, T, hidden_dim)."""
        layer = self.config.layer
        if layer == -1:
            out = self.model(wav).last_hidden_state
        else:
            out = self.model(wav, output_hidden_states=True).hidden_states[layer]
        return out

    @torch.inference_mode()
    def extract(self, wav: np.ndarray, device: str = "cpu") -> np.ndarray:
        """Single-utterance convenience path used by the extraction script."""
        tensor = torch.from_numpy(np.asarray(wav, dtype=np.float32)).unsqueeze(0).to(device)
        feats = self.forward(tensor)
        return feats.squeeze(0).cpu().numpy()

    def frames_for(self, n_samples: int) -> int:
        """Number of output frames for an input length.

        wav2vec2's convolutional feature extractor downsamples by 320, giving
        50 frames per second at 16 kHz.  A 64,600-sample window yields ~201.
        """
        return max(1, n_samples // 320 - 1)


def load_frontend(
    config: FrontendConfig | None = None,
    device: str = "cpu",
    cache_dir: str | Path | None = None,
) -> SSLFrontend:
    """Build the front end, honouring a repo-local cache.

    Pointing HF_HOME at a directory inside the project is what makes the
    offline demo possible: the cache can be zipped and carried to a machine
    with no network.
    """
    if cache_dir is not None:
        import os

        os.environ.setdefault("HF_HOME", str(cache_dir))
    frontend = SSLFrontend(config)
    return frontend.to(device)
