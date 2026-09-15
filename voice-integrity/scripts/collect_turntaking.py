#!/usr/bin/env python3
"""Record the Branch D validation corpus.

No public dataset exists for conversational liveness, and you do not need one.
Two laptops and an afternoon produce something better, because you control the
ground truth.

    Condition A   human <-> human                        (control)
    Condition B   human <-> human + streaming VC in loop (the attack)
    Condition C   human <-> pre-recorded playback        (the easy case)

Target roughly 30 conversations per condition.  Then replay every one with
synthetic network delay injected, and confirm the shape features still
separate the conditions where a naive mean-latency threshold does not.  That
contrast is the experiment that justifies the whole branch.

Only timestamps are written.  No audio is recorded, which is both the point of
the branch and what makes the collection ethically simple.

    PYTHONPATH=src python scripts/collect_turntaking.py --condition A --label pair01
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vif.common.config import load_config  # noqa: E402
from vif.common.types import Side, TurnEvent, TurnEventKind  # noqa: E402
from vif.serve.liveness import build_transitions, compute_features  # noqa: E402
from vif.serve.vad import build_vad  # noqa: E402


def record_live(duration_s: float, config, device_index: int | None = None) -> list[dict]:
    """Capture turn boundaries from two input channels.

    Channel 0 is the local human (agent, the reference), channel 1 is the far
    end (caller, under assessment).  Requires sounddevice.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError:
        print("sounddevice is required for live capture:  pip install sounddevice")
        raise SystemExit(2) from None

    audio_cfg = config.model.audio
    vads = {
        Side.AGENT: build_vad("auto", audio_cfg.vad_frame),
        Side.CALLER: build_vad("auto", audio_cfg.vad_frame),
    }
    in_speech = {Side.AGENT: False, Side.CALLER: False}
    events: list[dict] = []
    started = time.monotonic()

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        now_ns = time.monotonic_ns()
        for channel, side in ((0, Side.AGENT), (1, Side.CALLER)):
            if indata.shape[1] <= channel:
                continue
            pcm = np.ascontiguousarray(indata[:, channel], dtype=np.float32)
            for start in range(0, max(len(pcm) - audio_cfg.vad_frame + 1, 0), audio_cfg.vad_frame):
                chunk = pcm[start : start + audio_cfg.vad_frame]
                speaking = vads[side].is_speech(chunk, audio_cfg.vad_threshold)
                if speaking != in_speech[side]:
                    in_speech[side] = speaking
                    events.append(
                        {
                            "side": side.value,
                            "kind": "start" if speaking else "end",
                            "monotonic_ns": now_ns + int(start / audio_cfg.sample_rate * 1e9),
                        }
                    )

    print(f"recording {duration_s:.0f}s - speak naturally, interrupt each other, use backchannels")
    with sd.InputStream(
        samplerate=audio_cfg.sample_rate,
        channels=2,
        dtype="float32",
        blocksize=audio_cfg.vad_frame,
        device=device_index,
        callback=callback,
    ):
        while time.monotonic() - started < duration_s:
            time.sleep(0.1)

    print(f"captured {len(events)} boundary events, zero bytes of audio")
    return events


def analyse(events: list[dict], config) -> dict:
    """Turn a recorded session into the features the branch will use."""
    from vif.serve.liveness import TurnTracker

    tracker = TurnTracker()
    for raw in events:
        tracker.record(
            TurnEvent(
                side=Side(raw["side"]),
                kind=TurnEventKind(raw["kind"]),
                monotonic_ns=raw["monotonic_ns"],
            )
        )
    last_ns = max((e["monotonic_ns"] for e in events), default=0)
    tracker.close(last_ns / 1e9)

    transitions = build_transitions(tracker.utterances, config.model.liveness)
    features = compute_features(transitions, config.model.liveness)
    return {
        "n_utterances": len(tracker.utterances),
        "n_transitions": len(transitions),
        "features": features.model_dump(),
        "gaps_ms": [round(t.gap_ms, 1) for t in transitions],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=["A", "B", "C"], required=True)
    parser.add_argument("--label", required=True, help="pair or session identifier")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--device", type=int, default=None, help="input device index")
    parser.add_argument("--out", type=str, default="data/turntaking")
    parser.add_argument("--analyse-only", type=str, help="re-analyse an existing recording")
    args = parser.parse_args()

    config = load_config("configs")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.analyse_only:
        blob = json.loads(Path(args.analyse_only).read_text(encoding="utf-8"))
        print(json.dumps(analyse(blob["events"], config), indent=2))
        return 0

    events = record_live(args.duration, config, args.device)
    result = analyse(events, config)

    path = out_dir / f"{args.condition}_{args.label}_{int(time.time())}.json"
    path.write_text(
        json.dumps(
            {
                "condition": args.condition,
                "label": args.label,
                "duration_s": args.duration,
                "events": events,
                "analysis": result,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {path}")
    print(json.dumps(result["features"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
