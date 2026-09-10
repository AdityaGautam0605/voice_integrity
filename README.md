# SIH 26104 — Real-Time Voice Clone Detection

Detecting cloned and synthetic speech on live calls, in near real time.

## Layout

| Path | What it is |
|---|---|
| [`voice-integrity/`](voice-integrity/) | The backend implementation. Start with its README |
| [`SIH26104-voice-clone-detection-notes.md`](SIH26104-voice-clone-detection-notes.md) | Strategy: the traps, the design reasoning, build phases, demo script |
| [`SIH26104-SRS-backend.md`](SIH26104-SRS-backend.md) | Software requirements specification — 15 modules, 78 requirements |
| [`SIH26104-implementation.md`](SIH26104-implementation.md) | Environment setup, dependencies, build order, gotchas |
| `diagram-*.png`, `architecture-full.png` | Rendered architecture diagrams for slides |

## Quick start

```bash
cd voice-integrity
uv venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-base.txt -r requirements-serve.txt -r requirements-dev.txt

PYTHONPATH=src python scripts/smoke_test.py    # end to end, no downloads
PYTHONPATH=src python -m pytest tests/ -q      # 74 tests
```

The smoke test exercises every layer using a stub detector and synthetic
conversations, so the pipeline is verifiable before any corpus or model
weights exist.

## Status

| | |
|---|---|
| Backend | complete, 74 tests passing |
| Training notebooks | written, not yet run |
| Model weights | not trained yet |
| Corpora | not downloaded yet |
| Frontend | out of scope for this repository |

Training runs on Colab from `voice-integrity/notebooks/`, in numbered order.

## The idea in three lines

Four separable questions, four separable mechanisms, because they fail
differently: **was this voice manufactured** (artifact detection), **is this
the enrolled person** (speaker verification), **is a machine in the loop**
(conversational liveness), and **what do we do about it** (fusion and policy).

The system is a passive observer — it never sits in the media path, so its
failure modes degrade the verdict, never the call.

The contribution is codec robustness: a phone call discards the frequency band
where synthesis artifacts live, so a detector trained on clean audio measures
evidence that never survives to inference.
