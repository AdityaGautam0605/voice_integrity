#!/usr/bin/env python3
"""Export the detector to int8 ONNX for the offline demo.

The demo runs on a laptop, not on a cloud GPU: a tunnel cannot carry WebRTC
UDP, and routing through TURN-over-TCP adds jitter that dominates exactly what
the liveness branch measures.  So the demo machine needs a CPU-viable model,
and that is what this produces.

Two things to verify after exporting, before demo day:
  1. p95 latency on the actual demo machine stays below the hop duration
  2. int8 quantisation has not moved EER materially

    PYTHONPATH=src python scripts/export_onnx.py --checkpoint models/checkpoints/codec_robust.pt
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vif.common.config import load_config  # noqa: E402
from vif.common.logging import get_logger  # noqa: E402

log = get_logger("export_onnx")


class _EndToEnd:
    """Wraps front end + head into a single traceable module."""

    def __new__(cls, frontend, head):
        import torch.nn as nn

        class Combined(nn.Module):
            def __init__(self):
                super().__init__()
                self.frontend = frontend
                self.head = head

            def forward(self, wav):
                return self.head(self.frontend(wav))

        return Combined()


def export(checkpoint: str, out_path: str, config_dir: str = "configs", quantize: bool = True):
    import torch

    from vif.models.frontend import SSLFrontend
    from vif.models.heads import load_checkpoint

    config = load_config(config_dir)
    window = config.model.audio.window_samples

    frontend = SSLFrontend(config.model.frontend).eval()
    head, meta = load_checkpoint(
        checkpoint,
        config.model.head,
        feat_dim=frontend.hidden_dim,
        expect_window=window,
        expect_frontend=config.model.frontend.model_id,
    )
    model = _EndToEnd(frontend, head).eval()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fp32_path = out_path.with_name(out_path.stem + "-fp32.onnx")

    dummy = torch.randn(1, window, dtype=torch.float32)
    torch.onnx.export(
        model,
        dummy,
        str(fp32_path),
        input_names=["waveform"],
        output_names=["logits"],
        dynamic_axes={"waveform": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17,
    )
    log.info("exported fp32 model to %s (%.1f MB)", fp32_path, fp32_path.stat().st_size / 1e6)

    if quantize:
        from onnxruntime.quantization import QuantType, quantize_dynamic

        quantize_dynamic(
            model_input=str(fp32_path),
            model_output=str(out_path),
            weight_type=QuantType.QInt8,
        )
        log.info("exported int8 model to %s (%.1f MB)", out_path, out_path.stat().st_size / 1e6)

    return out_path


def benchmark(model_path: str, window: int = 64600, n_runs: int = 30) -> dict:
    """Latency percentiles on this machine.

    The number that matters is p95 against the hop duration.  If p95 exceeds
    the hop, the system falls progressively behind the live call, so widen the
    hop to 2 seconds or drop to the lighter head.
    """
    import onnxruntime as ort

    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    rng = np.random.default_rng(0)

    for _ in range(3):  # warm up
        session.run(None, {name: rng.normal(size=(1, window)).astype(np.float32)})

    timings = []
    for _ in range(n_runs):
        batch = rng.normal(size=(1, window)).astype(np.float32)
        started = time.perf_counter()
        session.run(None, {name: batch})
        timings.append((time.perf_counter() - started) * 1000.0)

    timings = np.array(timings)
    result = {
        "p50_ms": round(float(np.percentile(timings, 50)), 1),
        "p95_ms": round(float(np.percentile(timings, 95)), 1),
        "mean_ms": round(float(timings.mean()), 1),
    }
    log.info("latency p50=%(p50_ms)s ms  p95=%(p95_ms)s ms", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", default="models/checkpoints/codec_robust.pt")
    parser.add_argument("--out", default="models/exported/detector-int8.onnx")
    parser.add_argument("--config", default="configs")
    parser.add_argument("--no-quantize", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()

    path = export(args.checkpoint, args.out, args.config, quantize=not args.no_quantize)

    if args.benchmark:
        config = load_config(args.config)
        stats = benchmark(path, config.model.audio.window_samples)
        hop_ms = config.model.audio.hop_seconds * 1000
        if stats["p95_ms"] > hop_ms:
            log.warning(
                "p95 %.0f ms exceeds the %.0f ms hop - widen the hop to 2 s "
                "or the score will fall behind the live call",
                stats["p95_ms"],
                hop_ms,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
