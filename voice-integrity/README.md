# Voice Integrity Verification Framework

Backend for near-real-time detection of cloned and synthetic speech on live
calls. SIH 26104.

**Frontend is out of scope for this repository.** The system exposes contracts;
presentation consumes them.

```bash
PYTHONPATH=src python scripts/smoke_test.py     # end-to-end, no downloads needed
PYTHONPATH=src python -m pytest tests/ -q       # 93 tests
PYTHONPATH=src python -m vif.cli check          # startup guards and preflight
PYTHONPATH=src python -m vif.cli serve          # API on :8000
docker compose up --build                       # same thing, containerised
```

---

## What it does

Speech arrives, is normalised to 16 kHz, gated by voice activity, cut into
windows the detector was trained on, and scored. The backend returns a
**spoof probability** and a **risk band** several times a second, and at
session end produces an Ed25519-signed verdict.

Branches are reported **side by side, never fused into one number**:

| Branch | Question | Output |
|---|---|---|
| **Spoof** (primary) | Was this waveform manufactured? | `spoof_probability` 0–1, `risk` band |
| **Speaker** (optional) | Is this the enrolled person? | `speaker_similarity`, `speaker_status` |
| **Liveness** (stretch) | Is a machine in the conversational loop? | turn-timing features |

With one primary detector and one optional verifier, a combined score would
add the appearance of rigour without the evidence to support it — and would
hide which branch actually fired, which is what an operator and a judge both
need to see.

The system is a **passive observer**. It never occupies the media path, so its
failure modes degrade the verdict, never the call.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/session` | Open a session; returns `session_id` and the audio geometry to send |
| `WS /v1/stream/{session_id}` | Binary PCM in, score messages out; a text frame closes it |
| `POST /v1/enroll/{session_id}` | Optional reference identity for the speaker branch |
| `GET /v1/verdict/{session_id}` | The final signed verdict |
| `POST /v1/verdict/verify` | What a consuming system runs before acting |
| `GET /v1/audit` | Read the log and verify the hash chain |
| `GET /health`, `/metrics` | Status, model version, device, latency percentiles |

Set `VIF_API_TOKEN` to require `Authorization: Bearer <token>`. Leave it unset
for a local demo.

### Streaming message

```json
{
  "session_id": "8f3c...",
  "sequence": 12,
  "speech_seconds": 18.2,
  "spoof_probability": 0.87,
  "risk": "RED",
  "speaker_similarity": null,
  "speaker_status": "NOT_ENROLLED",
  "inference_ms": 142.0,
  "model_version": "aasist-codec_robust-e12"
}
```

### Signed verdict

```json
{
  "payload": {
    "session_id": "8f3c...",
    "spoof_probability": 0.87,
    "risk": "RED",
    "speaker_status": "NOT_ENROLLED",
    "action": "GATE_ACTION",
    "model_version": "aasist-codec_robust-e12",
    "model_checksum": "a1b2c3d4e5f6",
    "timestamp": 1789052362322,
    "nonce": "f11a88..."
  },
  "algorithm": "Ed25519",
  "key_id": "Wlf13hjzqFiBWsIG",
  "signature": "base64..."
}
```

---

## Layout

```
configs/            model.yaml · policy.yaml · augment.yaml
notebooks/          00-07, the training pipeline (Colab)
scripts/            data prep, corpus collection, ONNX export, smoke test
src/vif/
  common/           typed config, shared types, redacting logger
  data/             manifests · codec augmentation · datasets
  models/           SSL front end · AASIST · heads
  train/            feature extraction · head training · fine-tune
  eval/             metrics · calibration · multi-condition runner
  serve/            vad · ringbuffer · detector · scoring · policy
                    session · server · liveness · prosody · challenge
    adapters/       WebRTC (aiortc) · file · synthetic
  crypto/           keys · verdict signing · vault · audit log · cancelable
tests/              93 tests, every security control with a negative case
Dockerfile          single-container backend
```

---

## Install

```bash
# WSL2 or Linux.  aiortc needs PyAV, which needs ffmpeg dev libraries -
# that build is painful on native Windows.
sudo apt install -y ffmpeg libopencore-amrnb-dev libsndfile1

curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 && source .venv/bin/activate
uv pip install -r requirements-base.txt -r requirements-serve.txt -r requirements-dev.txt
```

Point the model cache at the repo **before** first use, or the offline demo
will reach for the network at the venue:

```bash
export HF_HOME="$PWD/cache/huggingface"
export TORCH_HOME="$PWD/cache/torch"
```

### Verify AMR-NB encoding before training

```bash
ffmpeg -encoders | grep amr
```

Stock builds often decode but cannot **encode** AMR-NB, in which case the
mobile-codec augmentation silently does nothing and the central result of the
project quietly evaporates. `vif check` reports this.

---

## Training

Colab notebooks, in order. Each mounts Drive so a disconnect does not cost you
the extraction pass.

| Notebook | What it does |
|---|---|
| `00_setup_and_data` | Environment, corpora, manifests, **the metric gate** |
| `01_extract_features` | The one-time GPU pass — two caches, clean and codec |
| `02_train_baseline` | AASIST head on clean features |
| `03_train_codec_robust` | Identical run, codec-degraded features |
| `04_evaluate` | Four conditions, the comparison chart |
| `05_finetune` | Release the top 6 front-end layers, overnight |
| `06_liveness_analysis` | Branch D corpus and validation |
| `07_indian_language_set` | Build and evaluate the multilingual set |

**The extraction pass in notebook 01 governs everything after it.** Running the
300M-parameter front end forward-only once and caching the result turns a
day-long experiment into a two-minute one.

---

## Decisions worth knowing before you change anything

**Window length is not a tunable.** 64,600 samples is the crop AASIST trains
on. Matching inference geometry to training geometry is free accuracy;
mismatching it is a silent regression, so a mismatch raises at startup.

**Inference runs off the event loop.** `run_in_executor` in `session.py` is a
correctness requirement, not an optimisation. Calling torch inline stalls
frame reception and drops audio.

**Branches are independent.** The speaker branch raises a separate
`IDENTITY_WARNING`; it never moves the spoof probability. A synthetic voice
and the wrong person are different findings and deserve different words.

**Smoothing is not fusion.** The spoof probability is averaged in the log-odds
domain across recent windows, because 4 seconds is a small sample and a raw
per-window score jitters. That is one estimator smoothed over time, not two
combined. Set `smoothing_windows: 1` to report the raw window.

**Abstention is not a score of zero.** A branch with no enrolment or too few
turns returns `None` / `NOT_ENROLLED` and does not move anything.

**The score gates the action, never the call.** A configuration that tries to
terminate calls is rejected at load.

**Fail closed.** A missing verdict is a 404 and elevated risk, not absence of
risk — otherwise the cheapest attack is a cut cable rather than a voice clone.

**Never delete the baseline.** The argument is the *gap* between the two
models. `models/checkpoints/baseline.pt` stays loadable forever.

**Calibration is fitted on dev, never eval.** The loader refuses anything
marked `fitted_on: eval`. Without calibration the probability is monotone but
not a real probability, and the API reports `calibrated: false`.

---

## Enabled by config, not by rewriting

These are implemented and tested, and default **off** so the prototype's
default path stays small. Flip the flag when the need is real.

| Flag | What it turns on | Why it is off |
|---|---|---|
| `model.liveness.enabled` | Conversational liveness (branch D) | Needs both call directions and 10–20 turn transitions before it says anything trustworthy |
| `model.prosody.enabled` | Prosody features | Language-dependent; needs per-language tuning or it generates false alarms |
| `security.cancelable_templates` | Revocable, unlinkable voiceprints | Matters once enrolments are long-lived; a voiceprint is irrevocable |
| `security.merkle_checkpoints` | Signed Merkle roots over the audit log | The hash chain alone already detects any edit |

## Production roadmap, deliberately not built

KMS/HSM key custody · sender-constrained credentials (mTLS, DPoP) · external
log anchoring · trusted execution environments · homomorphic template matching
· federated learning with differential privacy · SIPREC and carrier
integration · the edge cascade · multi-node scaling.

Each is defensible architecture for a deployed financial system. None of them
improves the quality or credibility of clone detection during a prototype, so
they are described rather than implemented.

---

## Running without models or corpora

`StubDetector` and `SyntheticAdapter` exercise every layer with no downloads.
The smoke test drives synthetic conversations and verifies scoring, policy,
signing, the vault, the transform and the audit log.

```
$ PYTHONPATH=src python scripts/smoke_test.py
...
[  ok  ] output contract carries the expected fields  -  p=0.612 risk=AMBER speech=19.4s
[  ok  ] machine conversation shows a higher response floor  -  human=-106ms machine=320ms
[  ok  ] tampered verdict is rejected  -  signature does not verify
...
RESULT: ALL CHECKS PASSED
```

With liveness enabled, the detected caller floor matching the simulated one is
the check that matters: it means the turn tracker is segmenting correctly.

---

## Companion documents

- `../SIH26104-voice-clone-detection-notes.md` — strategy, traps, build phases
- `../SIH26104-SRS-backend.md` — the original specification
- `../SIH26104-implementation.md` — environment, dependencies, build order
- `../Voice_Integrity_SIH_Tech_Stack_Recommendations.md` — the scope review this
  build follows
