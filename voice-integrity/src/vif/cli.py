"""Command-line entry points.

python -m vif.cli serve            start the API
python -m vif.cli keys             generate a signing key pair
python -m vif.cli check            startup guards and environment preflight
python -m vif.cli audit            verify the audit chain
python -m vif.cli demo             run a synthetic call end to end
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from vif.common.config import load_config
from vif.common.logging import get_logger

log = get_logger("vif")


def cmd_serve(args: argparse.Namespace) -> int:
    from vif.serve.server import run

    run(host=args.host, port=args.port)
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    from vif.crypto.verdict import generate_keypair, save_keypair

    keys_dir = Path(args.out)
    pair = generate_keypair()
    save_keypair(pair, keys_dir / "verdict_ed25519.pem", keys_dir / "verdict_ed25519.pub.pem")
    print(f"key_id: {pair.key_id}")
    print(f"private: {keys_dir / 'verdict_ed25519.pem'}  (keep out of version control)")
    print(f"public:  {keys_dir / 'verdict_ed25519.pub.pem'}  (ship to consumers)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Preflight.  Everything that would otherwise fail silently."""
    ok = True
    config = load_config(args.config)
    print("configuration")
    print(
        f"  window      {config.model.audio.window_samples} samples "
        f"({config.model.audio.window_seconds:.2f} s)"
    )
    print(
        f"  hop         {config.model.audio.hop_samples} samples "
        f"({config.model.audio.hop_seconds:.2f} s)"
    )
    print(f"  front end   {config.model.frontend.model_id}")
    print(f"  policy      {config.policy.version}, {len(config.policy.tiers)} tiers")

    print("\ncodec support")
    from vif.data.augment import check_ffmpeg_codecs, ffmpeg_available

    if not ffmpeg_available():
        print("  ffmpeg MISSING - codec augmentation cannot run")
        ok = False
    else:
        for name, available in check_ffmpeg_codecs(config.augment.codecs).items():
            print(f"  {name:<12} {'ok' if available else 'MISSING ENCODER'}")
            ok &= available

    print("\ncheckpoints")
    for label, rel in (
        ("baseline", config.model.head.baseline_checkpoint),
        ("codec_robust", config.model.head.checkpoint),
    ):
        path = config.path(rel)
        print(f"  {label:<14} {'present' if path.exists() else 'absent'}  {path}")

    print("\ncalibration")
    calib = config.path(config.model.fusion.calibration)
    if calib.exists():
        blob = json.loads(calib.read_text(encoding="utf-8"))
        for branch, values in blob.items():
            split = values.get("fitted_on", "?")
            flag = "  <-- CONTAMINATED" if split == "eval" else ""
            print(f"  {branch:<10} fitted on {split}{flag}")
            ok &= split != "eval"
    else:
        print("  absent - scores will pass through uncalibrated")

    print("\nmetric harness")
    from vif.eval.metrics import sanity_check_random

    result = sanity_check_random()
    gate = 0.45 < result.eer < 0.55
    print(f"  random classifier EER {result.eer * 100:.2f}%  {'ok' if gate else 'BROKEN'}")
    ok &= gate

    print("\n" + ("all checks passed" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def cmd_audit(args: argparse.Namespace) -> int:
    from vif.crypto.auditlog import AuditLog

    audit = AuditLog(args.db)
    valid, reason = audit.verify_chain()
    print(f"entries: {audit.count()}")
    print(f"chain:   {'VALID' if valid else 'BROKEN'} - {reason}")
    for entry in audit.entries(args.limit):
        payload = entry.payload.get("payload", {})
        print(
            f"  #{entry.seq:<5} {payload.get('call_id', '?'):<24} "
            f"risk={payload.get('risk', '?'):<6} band={payload.get('band', '?'):<6} "
            f"{entry.entry_hash[:12]}"
        )
    audit.close()
    return 0 if valid else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Run one synthetic call and print the verdict."""
    from vif.common.types import Side
    from vif.eval.calibration import Calibrator
    from vif.serve.adapters.file import SyntheticAdapter
    from vif.serve.detector import build_detector
    from vif.serve.session import CallSession
    from vif.serve.vad import build_vad

    config = load_config(args.config)

    async def go() -> int:
        adapter = SyntheticAdapter(n_turns=args.turns, pipeline_floor_ms=args.floor, realtime=False)
        session = CallSession(
            call_id="cli-demo",
            config=config,
            detector=build_detector(config.model, backend=args.backend),
            vad=build_vad("auto", config.model.audio.vad_frame),
            on_score=None,
            calibrator=Calibrator({}),
            metadata={"first_contact": True, "transaction_value": args.value},
        )
        session.liveness.set_rtt(await adapter.rtt_ms())
        await asyncio.gather(
            session.consume(adapter, Side.CALLER),
            session.consume(adapter, Side.AGENT),
        )
        payload = session.finalise()
        session.close()
        print(json.dumps(payload.model_dump(mode="json"), indent=2))
        return 0

    return asyncio.run(go())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vif", description=__doc__)
    parser.add_argument("--config", default="configs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="start the API server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("keys", help="generate a verdict signing key pair")
    p.add_argument("--out", default="keys")
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("check", help="startup guards and preflight")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("audit", help="verify the audit chain")
    p.add_argument("--db", default="data/audit.db")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("demo", help="run a synthetic call end to end")
    p.add_argument("--turns", type=int, default=14)
    p.add_argument("--floor", type=float, default=0.0, help="simulated pipeline floor in ms")
    p.add_argument("--value", type=float, default=5_000_000)
    p.add_argument("--backend", default="stub", choices=["auto", "stub", "torch", "onnx"])
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
