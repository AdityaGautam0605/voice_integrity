"""Detector wrapper (L2/L3).

Deliberately synchronous and blocking.  The async layer above is responsible
for keeping it off the event loop, and making that boundary explicit here
prevents the single worst bug in this design: calling torch inline from the
frame-receive coroutine, which stalls ingestion, drops audio, and smears the
very timestamps the liveness branch depends on.  The two branches then corrupt
each other, which is a miserable thing to debug at 3am.

Two backends:

    TorchDetector  full stack, the accuracy numbers we report
    OnnxDetector   int8 CPU, what makes the offline laptop demo possible

Both expose the same `score_window`, so the session layer never knows which is
running.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from vif.common.config import ModelConfig
from vif.common.logging import get_logger

log = get_logger(__name__)


def file_checksum(path: str | Path, length: int = 12) -> str:
    """Short SHA-256 of a model artifact.

    Replaces artifact signing for the prototype.  A checksum does not prove
    who produced the weights, but it does pin *which* weights produced a given
    verdict - which is the property that makes a result reproducible and an
    accidental swap visible.  Signing is the production step.
    """
    path = Path(path)
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:length]


class BaseDetector(ABC):
    """Scores one window.  Higher means more synthetic, always."""

    model_version: str = "unknown"
    model_checksum: str = ""

    @abstractmethod
    def score_window(self, wav: np.ndarray) -> float: ...

    def embed_speaker(self, wav: np.ndarray) -> np.ndarray | None:
        """Speaker embedding, or None when the branch is unavailable."""
        return None


class TorchDetector(BaseDetector):
    """Front end plus head, in one process.

    Thread-safe by a coarse lock: the session layer dispatches to an executor,
    so several calls can arrive concurrently and torch modules are not
    reentrant in a way we want to rely on.
    """

    def __init__(
        self,
        config: ModelConfig,
        checkpoint: str | Path | None = None,
        device: str = "cpu",
        load_speaker: bool = True,
    ):
        import torch

        from vif.models.frontend import SSLFrontend, load_released_layers
        from vif.models.heads import load_checkpoint

        self._torch = torch
        self.config = config
        self.device = device
        self._lock = threading.Lock()

        checkpoint = Path(checkpoint or config.head.checkpoint)
        if not checkpoint.exists():
            # Check before building the front end, which may start a 1.2 GB download.
            raise FileNotFoundError(f"detector checkpoint not found: {checkpoint}")
        self.frontend = SSLFrontend(config.frontend).to(device).eval()

        self.head, meta = load_checkpoint(
            checkpoint,
            config.head,
            feat_dim=self.frontend.hidden_dim,
            expect_window=config.audio.window_samples,
            expect_frontend=config.frontend.model_id,
        )
        self.head = self.head.to(device).eval()
        if meta.get("condition") == "finetuned":
            # Trained against released front-end layers, not the stock ones.
            load_released_layers(self.frontend, checkpoint)
        self.model_version = (
            f"{meta.get('arch', 'head')}-{meta.get('condition', 'unknown')}-e{meta.get('epoch', 0)}"
        )
        self.model_checksum = file_checksum(checkpoint)

        self.speaker = None
        if load_speaker and config.speaker.enabled:
            self.speaker = _load_speaker_model(config.speaker.model_id, device)

    def score_window(self, wav: np.ndarray) -> float:
        torch = self._torch
        expected = self.config.audio.window_samples
        if len(wav) != expected:
            raise ValueError(f"expected {expected} samples, got {len(wav)}")

        with self._lock, torch.inference_mode():
            tensor = (
                torch.from_numpy(np.asarray(wav, dtype=np.float32)).unsqueeze(0).to(self.device)
            )
            feats = self.frontend(tensor)
            logits = self.head(feats)
            score = self.head.score_from_logits(logits)
            return float(score.item())

    def embed_speaker(self, wav: np.ndarray) -> np.ndarray | None:
        if self.speaker is None:
            return None
        torch = self._torch
        with self._lock, torch.inference_mode():
            tensor = (
                torch.from_numpy(np.asarray(wav, dtype=np.float32)).unsqueeze(0).to(self.device)
            )
            emb = self.speaker.encode_batch(tensor)
            return emb.squeeze().cpu().numpy()


class OnnxDetector(BaseDetector):
    """int8 CPU inference.

    The tier the demo actually runs on, because a cloud tunnel cannot carry
    WebRTC UDP and venue wifi cannot be trusted.
    """

    def __init__(self, model_path: str | Path, window_samples: int = 64600, version: str = "onnx"):
        import onnxruntime as ort

        self.window_samples = window_samples
        self.model_version = version
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = 0  # let ORT pick
        self.session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.model_checksum = file_checksum(model_path)
        self._lock = threading.Lock()
        log.info("loaded ONNX detector from %s", model_path)

    def score_window(self, wav: np.ndarray) -> float:
        if len(wav) != self.window_samples:
            raise ValueError(f"expected {self.window_samples} samples, got {len(wav)}")
        batch = np.asarray(wav, dtype=np.float32)[None, :]
        with self._lock:
            logits = self.session.run(None, {self.input_name: batch})[0]
        return float(logits[0, 0] - logits[0, 1])  # spoof minus bonafide


class StubDetector(BaseDetector):
    """Deterministic stand-in with no model weights.

    Exists so the whole pipeline - ingest, VAD, windowing, fusion, policy,
    signing, audit log - can be exercised end to end with no downloads.  It
    reads the spectral tilt of the window, which correlates weakly with
    synthetic audio but is emphatically not a detector.  Never ship it.
    """

    model_version = "stub-0"
    model_checksum = "nostub"

    def __init__(self, window_samples: int = 64600, bias: float = 0.0):
        self.window_samples = window_samples
        self.bias = bias

    def score_window(self, wav: np.ndarray) -> float:
        wav = np.asarray(wav, dtype=np.float32)
        spectrum = np.abs(np.fft.rfft(wav * np.hanning(len(wav))))
        spectrum = spectrum / (spectrum.sum() + 1e-9)
        split = len(spectrum) // 4
        low = float(spectrum[:split].sum())
        high = float(spectrum[split:].sum())
        tilt = np.log((high + 1e-6) / (low + 1e-6))
        return float(np.clip(tilt + self.bias, -6.0, 6.0))

    def embed_speaker(self, wav: np.ndarray) -> np.ndarray | None:
        rng = np.random.default_rng(abs(int(np.sum(wav) * 1000)) % (2**31))
        vec = rng.normal(size=192).astype(np.float32)
        return vec / (np.linalg.norm(vec) + 1e-9)


def _load_speaker_model(model_id: str, device: str):
    try:
        from speechbrain.inference import EncoderClassifier

        return EncoderClassifier.from_hparams(
            source=model_id,
            savedir=f"cache/speechbrain/{model_id.replace('/', '_')}",
            run_opts={"device": device},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("speaker model unavailable (%s) - branch B will abstain", exc)
        return None


DEFAULT_ONNX_PATH = "models/exported/detector-int8.onnx"


def build_detector(
    config: ModelConfig,
    checkpoint: str | Path | None = None,
    backend: str = "auto",
    device: str = "cpu",
    onnx_path: str | Path | None = None,
) -> BaseDetector:
    """Pick a backend.

    'auto' tries the full torch stack, then an exported ONNX model.  It never
    falls back to the stub on its own: a server that silently scored with a
    stand-in would sign verdicts that detect nothing.  Ask for 'stub'
    explicitly to run the pipeline without model weights.
    """
    onnx_path = Path(onnx_path or DEFAULT_ONNX_PATH)

    if backend == "stub":
        log.warning("using StubDetector - no real detection is happening")
        return StubDetector(config.audio.window_samples)
    if backend == "onnx":
        return OnnxDetector(onnx_path, config.audio.window_samples)
    if backend == "torch":
        return TorchDetector(config, checkpoint=checkpoint, device=device)
    if backend != "auto":
        raise ValueError(f"unknown detector backend: {backend}")

    try:
        return TorchDetector(config, checkpoint=checkpoint, device=device)
    except Exception as torch_error:  # noqa: BLE001
        if onnx_path.exists():
            log.warning("torch backend unavailable (%s) - using %s", torch_error, onnx_path)
            return OnnxDetector(onnx_path, config.audio.window_samples)
        raise RuntimeError(
            f"no usable detector: the torch backend failed ({torch_error}) and there is no "
            f"exported model at {onnx_path}.  Train and export one, or set VIF_BACKEND=stub "
            "to run the pipeline without detection."
        ) from torch_error
