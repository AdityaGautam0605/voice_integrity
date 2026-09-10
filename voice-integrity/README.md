# Voice Integrity Verification Framework

Backend for near-real-time detection of cloned and synthetic speech on live
calls. SIH 26104.

**Frontend is out of scope for this repository.** The system exposes contracts;
presentation consumes them.

```
PYTHONPATH=src python scripts/smoke_test.py     # end-to-end, no downloads needed
PYTHONPATH=src python -m pytest tests/ -q       # 74 tests
PYTHONPATH=src python -m vif.cli check          # startup guards and preflight
PYTHONPATH=src python -m vif.cli demo --floor 300
```

---

## What this is

Four separable questions, answered by four separable mechanisms. They fail
differently, which is the reason to keep them apart.

| Question | Branch | Catches | Blind to |
|---|---|---|---|
| Was this voice manufactured? | **A** artifact detection | any synthetic speech | skilled human impersonators |
| Is this the enrolled person? | **B** speaker verification | human impersonators | anyone not enrolled |
| Is a machine in the loop? | **D** conversational liveness | pipelines, bots, playback | a genuinely live human |
| So what do we do? | fusion + policy | — | — |

The system is a **passive observer**. It never occupies the media path. Its
failure modes degrade the verdict, never the call.

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
  serve/            VAD · ring buffer · detector · fusion · policy
                    liveness · prosody · challenge · session · server
    adapters/       WebRTC (aiortc) · file · synthetic
  crypto/           keys · verdict signing · cancelable transform
                    vault · audit log
tests/              74 tests, every security control with a negative case
```

Layers map to the SRS: `adapters` L0, `vad`/`ringbuffer` L1, `frontend` L2,
`heads`/`liveness` L3, `fusion` L4, `policy` L5, `server`/`challenge` L6, with
`crypto` cross-cutting.

---

## Install

```bash
# WSL2 or Linux.  aiortc needs PyAV, which needs ffmpeg dev libraries -
# that build is painful on native Windows.
sudo apt install -y ffmpeg libopencore-amrnb-dev libsndfile1 \
                    libavdevice-dev libavfilter-dev libavformat-dev

curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 && source .venv/bin/activate

uv pip install -r requirements-base.txt -r requirements-train.txt   # training
uv pip install -r requirements-base.txt -r requirements-serve.txt   # serving
uv pip install -r requirements-dev.txt
```

Point the model cache at the repo **before** first use, or the offline demo
will reach for the network at the venue:

```bash
export HF_HOME="$PWD/cache/huggingface"
export TORCH_HOME="$PWD/cache/torch"
```

### Verify AMR-NB encoding before P2

```bash
ffmpeg -encoders | grep amr
```

Stock builds often decode but cannot **encode** AMR-NB, in which case the
mobile-codec augmentation silently does nothing and the central result of the
project quietly evaporates. `vif check` reports this.

---

## Training

Colab notebooks, in order. Each is self-contained and mounts Drive so a
disconnect does not cost you the extraction pass.

| Notebook | Phase | What it does |
|---|---|---|
| `00_setup_and_data` | P0 | environment, corpora, manifests, **the metric gate** |
| `01_extract_features` | P0 | the one-time GPU pass — two caches, clean and codec |
| `02_train_baseline` | P1 | AASIST head on clean features |
| `03_train_codec_robust` | P2 | identical run, codec-degraded features |
| `04_evaluate` | P1–P2 | four conditions, the comparison chart |
| `05_finetune` | P7 | release the top 6 front-end layers, overnight |
| `06_liveness_analysis` | P5 | Branch D corpus and validation |
| `07_indian_language_set` | P6 | build and evaluate the multilingual set |

**The extraction pass in notebook 01 governs everything after it.** Running the
300M-parameter front end forward-only once and caching the result turns a
day-long experiment into a two-minute one. Over a prep period that is the
difference between five experiments and several hundred.

---

## Serving

```bash
PYTHONPATH=src python -m vif.cli keys       # generate a signing key pair
PYTHONPATH=src python -m vif.cli serve      # start on 127.0.0.1:8000
```

| Endpoint | Purpose |
|---|---|
| `WS /v1/analyze` | session negotiation, streaming score frames |
| `GET /v1/verdict/{call_id}` | the final signed verdict |
| `POST /v1/verdict/verify` | signature check, for the consuming system |
| `POST /v1/enroll` | register a protected identity |
| `POST /v1/enroll/{id}/revoke` | invalidate stored templates |
| `GET /v1/audit` | read the log, verify the chain |
| `GET /healthz`, `/metrics` | liveness and operational metrics |

---

## Decisions worth knowing before you change anything

**Window length is not a tunable.** 64,600 samples is the crop AASIST trains
on. Matching inference geometry to training geometry is free accuracy;
mismatching it is a silent regression, so a mismatch raises at startup rather
than warning.

**Inference runs off the event loop.** `run_in_executor` in `session.py` is a
correctness requirement, not an optimisation. Calling torch inline stalls
frame reception, drops audio, and smears exactly the timestamps Branch D
depends on — the two branches then corrupt each other.

**Evidence sums, it does not average.** Ten windows each weakly indicating
synthesis are stronger than one, not equal to it. Calibrated log-likelihood
ratios are additive, which is why calibration comes first and why the
confidence interval visibly narrows during a call. This is the machinery of a
sequential probability ratio test.

**Calibration is fitted on dev, never on eval.** The loader refuses any
calibration marked `fitted_on: eval`. Contamination there produces numbers
that look excellent and mean nothing.

**Abstention is not a score of zero.** A branch with no enrolment, too few
turns, or disabled returns `None` and does not move the posterior.

**The score gates the action, never the call.** A configuration that tries to
terminate calls is rejected at load.

**Fail closed.** A missing or unverifiable verdict is elevated risk, not
absence of risk — otherwise the cheapest attack on this system is a cut cable
rather than a voice clone.

**Never delete the baseline.** The argument is the *gap* between the two
models. `models/checkpoints/baseline.pt` stays loadable forever.

---

## Branch D in one paragraph

It measures the **shape** of the response-gap distribution, never its mean: a
poor connection also adds 300 ms, but network latency shifts a distribution
uniformly without imposing a hard floor, removing overlaps, or compressing
variance. Comparison is **within-call** against the agent side, a known human,
which cancels network, task, register, speaker pair and language in one move.
Its entire feature set is timestamps — no audio, no embeddings — which is why
it can run where the acoustic branch legally cannot. Honest limitation: strong
evidence needs 10–20 turns, so it is not a fast detector in general. It is
fast in two cases worth demoing: the first transition when the offset is large
against measured RTT, and pre-recorded playback, which has no turn-taking
behaviour at all.

---

## Cryptography

| Control | Why |
|---|---|
| Ed25519-signed verdicts, verified before acting | the output authorises money movement; unsigned it is forgeable by anyone on the path |
| Nonce and timestamp in the signed payload | an old "safe" verdict cannot be replayed |
| Cancelable transform before storage | a voiceprint is **irrevocable** — encryption alone leaves nothing to rotate after a leak |
| AES-256-GCM envelope, AAD-bound | tamper detection, and records cannot be swapped between rows |
| Versioned KEKs | rotation without re-encrypting the store or re-enrolling anybody |
| Hash chain + signed Merkle checkpoints | tamper-evident *and* non-repudiable, not merely self-consistent |
| Audit log refuses personal data | it holds decisions, never content |

**Known limitation, stated rather than hidden:** a managed runtime cannot
guarantee memory erasure. Buffers are overwritten at teardown on a best-effort
basis; a real guarantee needs a native extension.

---

## Running without models or corpora

`StubDetector` and `SyntheticAdapter` exercise every layer with no downloads.
The smoke test drives two synthetic calls — one behaving like a human, one
with a 320 ms pipeline floor — and checks that the liveness branch separates
them, then verifies signing, the vault, the transform and the audit log.

```
$ PYTHONPATH=src python scripts/smoke_test.py
...
[  ok  ] machine call shows a higher response floor  -  human floor=-106ms machine floor=320ms
[  ok  ] machine call loses caller-side overlap      -  human overlap=25% machine overlap=0%
[  ok  ] liveness LLR is higher for the machine call -  human=-3.545 machine=4.000
...
RESULT: ALL CHECKS PASSED
```

The detected floor matching the simulated one is the check that matters: it
means the turn tracker is segmenting correctly, and every number from Branch D
depends on that.

---

## Companion documents

- `../SIH26104-voice-clone-detection-notes.md` — strategy, traps, build phases
- `../SIH26104-SRS-backend.md` — the specification this implements
- `../SIH26104-implementation.md` — environment, dependencies, build order
