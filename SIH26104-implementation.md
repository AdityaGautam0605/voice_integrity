# SIH 26104 — Implementation Guide

**Everything required to build this, in the order you need it.**

| | |
|---|---|
| **Companion docs** | `SIH26104-voice-clone-detection-notes.md` (strategy) · `SIH26104-SRS-backend.md` (spec) |
| **Architecture page** | https://claude.ai/code/artifact/c8e18343-1995-4cb0-8402-118ba8b6c9a6 |
| **SRS page** | https://claude.ai/code/artifact/186d5181-21f4-4b04-9ee6-ec64846cd168 |

---

## Contents

1. [Day-zero requirements](#1-day-zero-requirements)
2. [Environment setup](#2-environment-setup)
3. [Python dependencies](#3-python-dependencies)
4. [System packages](#4-system-packages)
5. [Repository structure](#5-repository-structure)
6. [Datasets](#6-datasets)
7. [Model artifacts and offline caching](#7-model-artifacts-and-offline-caching)
8. [Configuration files](#8-configuration-files)
9. [Implementation order](#9-implementation-order)
10. [Verification checklist per phase](#10-verification-checklist-per-phase)
11. [Gotchas that cost a day each](#11-gotchas-that-cost-a-day-each)
12. [Pre-demo checklist](#12-pre-demo-checklist)

---

# 1. Day-zero requirements

Start these on day one. Several have approval delays that will block you later.

## Accounts

| Account | For | Delay |
|---|---|---|
| **Edinburgh DataShare** | ASVspoof 2019 LA — requires accepting terms before download | Same day, but do it first |
| **Kaggle** | 30 GPU-hrs/week. Phone-verify the account or GPU access stays locked | Verification can take hours |
| **Hugging Face** | Model downloads. A token avoids rate limits | Immediate |
| **Weights & Biases** | Experiment tracking, free tier | Immediate |
| **GitHub** | Repo, six collaborators | Immediate |

## Hardware

| Item | Spec | Who needs it |
|---|---|---|
| Training | Kaggle free tier (P100 / 2×T4) | ML 1, ML 2 |
| **Demo machine** | 8+ core laptop, 16 GB RAM, working mic | Whole team — validate early |
| Second machine | Any laptop with a mic | Branch D corpus collection (§9, P5) |
| Headsets ×2 | Wired preferred | Branch D corpus — avoids acoustic coupling |
| Storage | ~150 GB free on the training machine | Corpora + features + checkpoints |

## Team prerequisites

Nobody needs prior audio ML experience. Two things are worth an hour each before P0:

- Everyone reads the **five traps** in the notes. Design decisions downstream stop looking arbitrary.
- Backend and ML 3 read **§3 of the notes** (conversational liveness) before P3, because it determines that ingest must be bidirectional from the start.

---

# 2. Environment setup

## Use WSL2, not Windows directly

`aiortc` depends on PyAV, which needs ffmpeg development libraries. That build is painful on native Windows. Decide this now, not at 2am.

```bash
# In PowerShell, once, as admin
wsl --install -d Ubuntu-22.04
```

Everything below runs inside WSL2 (or on Linux/macOS directly).

## Python and package manager

Python 3.11. Use `uv` — an order of magnitude faster than pip or conda, which matters when six people rebuild environments repeatedly.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11
source .venv/bin/activate
```

## Two environments, one repo

Training needs CUDA-enabled torch. The demo machine does not and should not carry it.

```bash
uv pip install -r requirements-base.txt -r requirements-train.txt   # training machine
uv pip install -r requirements-base.txt -r requirements-serve.txt   # demo machine
```

**Freeze after the first successful install** and commit the lockfile. Six people resolving dependencies independently is a guaranteed afternoon lost.

```bash
uv pip freeze > requirements-lock.txt
```

---

# 3. Python dependencies

Versions below are a starting point. Let `uv` resolve, then pin whatever works and commit it.

## `requirements-base.txt`

```
# --- numerics ---
numpy>=1.26,<2.0          # <2.0: several audio packages still lag
scipy>=1.14
scikit-learn>=1.5         # Platt calibration, ROC/DET, metrics

# --- audio I/O and DSP ---
soundfile>=0.12
librosa>=0.10
silero-vad>=5.1

# --- config and utilities ---
pydantic>=2.9
pyyaml>=6.0
tqdm>=4.66

# --- cryptography ---
cryptography>=43.0        # AES-256-GCM, Ed25519, HKDF, X.509
pynacl>=1.5               # libsodium; alternative Ed25519 + XChaCha20
```

## `requirements-train.txt`

```
torch>=2.4                # install the CUDA build on the training machine
torchaudio>=2.4
transformers>=4.44        # wav2vec2-XLS-R
speechbrain>=1.0          # pretrained ECAPA-TDNN

# --- evaluation and tracking ---
matplotlib>=3.9
pandas>=2.2
wandb>=0.18
```

## `requirements-serve.txt`

```
# --- inference ---
onnx>=1.16
onnxruntime>=1.19         # CPU int8 — what makes the offline demo possible

# --- streaming ---
aiortc>=1.9               # server-side WebRTC; gives RTP timestamps
av>=12.0                  # PyAV, aiortc's media layer
fastapi>=0.115
uvicorn[standard]>=0.30
websockets>=13.0
```

## `requirements-dev.txt`

```
pytest>=8.3
ruff>=0.6                 # lint + format; replaces black, flake8, isort
pre-commit>=3.8
```

## Optional, only if you demo it

```
tenseal>=0.3              # CKKS homomorphic cosine similarity
```

## Not a pip package

**AASIST.** Clone the reference implementation and vendor it into `src/models/aasist/`, pinning the commit. It is a research repo, not a published package.

```bash
git clone https://github.com/clovaai/aasist.git /tmp/aasist
cp -r /tmp/aasist/models src/models/aasist/
# record the commit hash in src/models/aasist/COMMIT
```

---

# 4. System packages

```bash
sudo apt update && sudo apt install -y \
  ffmpeg \
  libavdevice-dev libavfilter-dev libavformat-dev libavcodec-dev \
  libswscale-dev libswresample-dev libavutil-dev \
  libopus-dev libopencore-amrnb-dev \
  libsndfile1 \
  pkg-config build-essential
```

## ⚠️ Verify AMR-NB encoding before P2

Stock ffmpeg builds often decode AMR-NB but cannot **encode** it. Your mobile-codec augmentation would then silently do nothing.

```bash
ffmpeg -encoders 2>/dev/null | grep amr
```

Expect a line containing `libopencore_amrnb`. If it's absent, either install a build that includes it or drop AMR-NB from the augmentation chain and say so in the writeup. Silently missing augmentation is much worse than a documented gap.

---

# 5. Repository structure

```
voice-integrity/
├── configs/
│   ├── model.yaml              # window, hop, model ids, checkpoints
│   ├── policy.yaml             # thresholds, tiers — SIGNED in production
│   └── augment.yaml            # codec chain probabilities
├── data/
│   ├── raw/                    # downloaded corpora        [gitignored]
│   ├── manifests/              # jsonl: path,label,speaker,lang,condition
│   ├── features/               # cached .npy               [gitignored]
│   └── turntaking/             # Branch D corpus           [gitignored]
├── models/
│   ├── checkpoints/            # baseline.pt, codec_robust.pt
│   └── exported/               # onnx int8 for the demo machine
├── cache/                      # HF_HOME + TORCH_HOME      [gitignored]
├── src/
│   ├── common/
│   │   └── config.py           # typed config loading
│   ├── data/
│   │   ├── manifests.py        # corpus -> jsonl
│   │   ├── augment.py          # the ffmpeg codec chain
│   │   └── datasets.py         # torch Dataset over manifests
│   ├── models/
│   │   ├── frontend.py         # XLS-R wrapper, frozen / partial unfreeze
│   │   ├── aasist/             # VENDORED, pinned commit
│   │   └── head.py             # the trained classifier
│   ├── train/
│   │   ├── extract_features.py # the one-time GPU pass
│   │   ├── train_head.py       # minutes per run
│   │   └── finetune.py         # P7, overnight
│   ├── eval/
│   │   ├── metrics.py          # EER, TPR@FPR, DET, ECE
│   │   └── run_eval.py         # all conditions, one command
│   ├── serve/
│   │   ├── detector.py         # model wrapper, synchronous
│   │   ├── session.py          # CallSession — the per-call spine
│   │   ├── liveness.py         # Branch D statistics
│   │   ├── fusion.py           # calibration + LLR accumulation
│   │   ├── policy.py           # bands, actions
│   │   └── server.py           # FastAPI + aiortc wiring
│   └── crypto/
│       ├── verdict.py          # Ed25519 sign / verify
│       ├── vault.py            # AES-GCM envelope + cancelable transform
│       └── auditlog.py         # hash chain + Merkle checkpoints
├── scripts/
│   ├── fetch_data.sh
│   ├── prepare_indian_set.py   # clone + codec-degrade Indic speech
│   ├── collect_turntaking.py   # Branch D corpus recorder
│   └── export_onnx.py          # int8 export for the demo machine
├── tests/
├── requirements-*.txt
└── README.md
```

**Manifest format** — one JSON object per line, used by every stage:

```json
{"path": "data/raw/asvspoof19/LA_T_1000137.flac", "label": "spoof",
 "speaker": "LA_0079", "attack": "A03", "lang": "en",
 "condition": "clean", "split": "train"}
```

Everything downstream keys off `condition` and `attack` — that's what makes leave-one-attack-out and per-codec breakdowns one-line filters rather than bespoke scripts.

---

# 6. Datasets

| Dataset | Purpose | Access | Budget |
|---|---|---|---|
| **ASVspoof 2019 LA** | Training + in-domain eval | Edinburgh DataShare, accept terms first | ~10–25 GB |
| **In-the-Wild** | Out-of-domain honesty eval | Fraunhofer AISEC public release | ~8 GB |
| **AI4Bharat Kathbath / IndicSUPERB** | Real Indian speech for the multilingual set | AI4Bharat / Hugging Face | Subset only — a few GB |
| ASVspoof 2021 DF | Optional, codec conditions | Same as 2019 | Large — subsample |
| **Own turn-taking corpus** | Branch D validation | You record it (§9, P5) | < 1 GB |

## ASVspoof 2019 LA composition

| Split | Bonafide | Spoof | Total |
|---|---|---|---|
| Train | 2,580 | 22,800 | 25,380 |
| Dev | 2,548 | 22,296 | 24,844 |
| Eval | 7,355 | 63,882 | 71,237 |

> ⚠️ Roughly **nine spoofed utterances per bonafide one**. Accuracy on this split will look excellent and mean nothing. This is why §10's P0 gate exists.

## Voice cloning tools for the Indian attack set

Install these in a **separate environment** — they pull conflicting dependency versions and you don't want them near your training env.

| Tool | Why |
|---|---|
| **Coqui XTTS-v2** | Multilingual, clones from ~6 s of reference audio |
| **OpenVoice v2** | Different architecture, different artifacts |
| **F5-TTS** | Recent, tests generalisation to a newer generator |
| **AI4Bharat Indic Parler-TTS** | Native Indian-language synthesis |

Using four different generators matters. If all your fakes come from one tool, your model learns that tool rather than learning synthesis.

---

# 7. Model artifacts and offline caching

## What to download

| Artifact | Size | Source |
|---|---|---|
| `facebook/wav2vec2-xls-r-300m` | ~1.2 GB | Hugging Face |
| `speechbrain/spkrec-ecapa-voxceleb` | ~80 MB | Hugging Face |
| Silero VAD | ~2 MB | torch.hub or pip |

## Make the cache portable — do this before anything else

Every one of these fetches from the internet on first use. **Your offline demo dies at the venue unless the caches are warm and repo-local.**

```bash
# Put this in .envrc or your activate script, on every machine
export HF_HOME="$PWD/cache/huggingface"
export TORCH_HOME="$PWD/cache/torch"
export HF_HUB_DISABLE_TELEMETRY=1
```

Then warm them once and verify:

```bash
python -c "
from transformers import Wav2Vec2Model
from speechbrain.inference import EncoderClassifier
import torch
Wav2Vec2Model.from_pretrained('facebook/wav2vec2-xls-r-300m')
EncoderClassifier.from_hparams('speechbrain/spkrec-ecapa-voxceleb')
torch.hub.load('snakers4/silero-vad', 'silero_vad')
print('caches warm')
"
```

Now `cache/` can be zipped and copied to the demo machine. **Test it with the network physically off** — see §12.

---

# 8. Configuration files

## `configs/model.yaml`

```yaml
audio:
  sample_rate: 16000
  window_samples: 64600      # 4.04 s — MUST equal the head's training crop
  hop_samples: 16000         # 1 s; 32000 on CPU tiers
  vad_frame: 512

frontend:
  model_id: facebook/wav2vec2-xls-r-300m
  frozen: true               # false only for the P7 run
  unfreeze_top_n: 6          # P7 only
  hidden_dim: 1024

head:
  arch: aasist
  checkpoint: models/checkpoints/codec_robust.pt
  baseline_checkpoint: models/checkpoints/baseline.pt   # NEVER delete

speaker:
  model_id: speechbrain/spkrec-ecapa-voxceleb
  embedding_dim: 192

fusion:
  llr_clamp: 4.0
  weights: {spoof: 1.0, speaker: 0.6, prosody: 0.15, liveness: 0.8}
  calibration: configs/calibration.json    # fitted on DEV ONLY
```

## `configs/policy.yaml`

```yaml
bands:
  green: [0, 40]
  amber: [40, 75]
  red:   [75, 100]

tiers:                       # thresholds shift with what's at stake
  - max_value: 50000
    amber: 55
    red: 85
  - max_value: 1000000
    amber: 45
    red: 78
  - max_value: null          # above the highest tier
    amber: 35
    red: 70

actions:
  amber: challenge
  red: gate_action           # never terminate_call
fail_closed: true
```

## `configs/augment.yaml`

```yaml
apply_probability: 0.7
codecs:
  - {name: g711_ulaw, rate: 8000,  weight: 0.35}
  - {name: amr_nb,    rate: 8000,  bitrate: 12200, weight: 0.35}
  - {name: opus,      rate: 16000, bitrate: 16000, weight: 0.30}
packet_loss: {probability: 0.3, span_ms: [20, 60], max_drops: 4}
noise: {probability: 0.4, snr_db: [15, 35]}
```

---

# 9. Implementation order

Follow the phases. Each leaves the system demonstrable.

## P0 — Foundation

**First thing, before any model code: write `src/eval/metrics.py`.**

```python
# EER from an ROC curve is about ten lines.
def eer(labels, scores):
    fpr, tpr, _ = roc_curve(labels, scores)
    fnr = 1 - tpr
    i = np.nanargmin(np.abs(fnr - fpr))
    return (fpr[i] + fnr[i]) / 2

def tpr_at_fpr(labels, scores, target_fpr=0.01):
    fpr, tpr, _ = roc_curve(labels, scores)
    return float(np.interp(target_fpr, fpr, tpr))
```

Then, in order:

1. `scripts/fetch_data.sh` — download and unpack corpora
2. `src/data/manifests.py` — every corpus to the same jsonl schema
3. `src/models/frontend.py` — XLS-R wrapper, frozen
4. `src/train/extract_features.py` — **the one-time GPU pass**, writes `.npy`

> **The extraction step governs everything after it.** A day-long experiment becomes a two-minute one. Do not skip it and "just fine-tune" — you will get five experiments instead of three hundred.

## P1 — Baseline

5. Vendor AASIST, wire `src/models/head.py`
6. `src/train/train_head.py` — trains in minutes on cached features
7. `src/eval/run_eval.py` — in-domain and out-of-domain in one command
8. **Tag `baseline.pt` and never delete it**

## P2 — Codec robustness ★

9. `src/data/augment.py` — the ffmpeg chain via `subprocess`, applied on the fly
10. Retrain with augmentation → `codec_robust.pt`
11. Produce the 2×2 grid: both models × clean/degraded

## P3 — Streaming, bidirectional

12. `src/serve/detector.py` — model wrapper, synchronous by design
13. `src/serve/session.py` — `CallSession`, ring buffer, window assembly
14. `src/serve/fusion.py` — Platt calibration + LLR accumulation
15. `src/serve/server.py` — FastAPI + `aiortc`

> **Build ingest bidirectional now.** One `consume()` task per direction, labelled caller/agent, timestamped at RTP arrival. Retrofitting the second stream after the pipeline exists means reworking the session manager, the buffer and the timestamp path simultaneously.

Two things to get right in this phase:

```python
# 1. Inference MUST run off the event loop, or you stall frame reception
#    and corrupt exactly the timestamps Branch D needs.
logit = await loop.run_in_executor(None, detector.score_window, window)

# 2. Disable browser audio processing, or NS gates quiet onsets and
#    shifts the turn boundaries you are trying to measure.
getUserMedia({audio: {echoCancellation: false,
                      noiseSuppression: false,
                      autoGainControl: false}})
```

## P4 — Demo surface

16. Dashboard, two-needle comparison, challenge prompt, gated approval screen
17. `scripts/export_onnx.py` — int8 export so the demo runs offline on CPU

## P5 — Branch D

18. `scripts/collect_turntaking.py` — records both sides, logs every turn boundary
19. Collect ~90 conversations across three conditions (see notes §3)
20. `src/serve/liveness.py` — floor differential, overlap rate, variance ratio
21. Replay everything with injected network delay to prove shape survives what mean latency does not

## P6 — Coverage

22. `scripts/prepare_indian_set.py` — clone Indic speakers, apply the codec chain
23. Speaker branch — pretrained ECAPA, ~5 lines
24. `src/crypto/` — verdict signing, vault, audit log
25. Prosody panel + its ablation

## P7 — Final numbers

26. `src/train/finetune.py` — unfreeze top 6, overnight on Kaggle
27. Rerun every eval, regenerate every chart
28. **Keep the P2 checkpoints as the fallback**

---

# 10. Verification checklist per phase

Each phase is done when its check passes, not when the code compiles.

| Phase | Check |
|---|---|
| **P0** | A random-guess classifier runs end to end and reports **EER ≈ 50%**. If it reports anything else, the harness is broken — find out now, not in three weeks |
| **P1** | Strong in-domain EER **and** visibly worse out-of-domain EER, both recorded. The gap is expected; report it |
| **P2** | Baseline collapses toward chance on codec-degraded audio while the augmented model holds. **This chart is the presentation** |
| **P3** | Speak into a mic; the score updates every second and its interval visibly narrows. Both directions ingested, RTP timestamps logged, both models scored on one stream |
| **P4** | The six-beat run-through completes twice unassisted, **on venue wifi and with the network off** |
| **P5** | Shape features separate the three conditions where a mean-latency threshold does not. Power calculation run on real data |
| **P6** | Every PS bullet maps to a chart, a running module, or a stated and reasoned exclusion |
| **P7** | All evals rerun, all charts regenerated, P2 checkpoints retained |

---

# 11. Gotchas that cost a day each

**WSL2, not native Windows.** `aiortc` → PyAV → ffmpeg dev libraries. Decide at the start.

**AMR-NB encoding is often missing.** `ffmpeg -encoders | grep amr` before P2 begins.

**AASIST is a repo, not a package.** Vendor it, pin the commit.

**ASVspoof needs terms accepted** on Edinburgh DataShare before download. Start day one.

**Kaggle disables internet by default.** Toggle it on, or model downloads fail confusingly. GPU access also needs a phone-verified account.

**Pre-cache every model, repo-locally.** `HF_HOME` and `TORCH_HOME` pointed at `cache/`. Test with the network off.

**Never calibrate on the eval split.** Platt parameters come from dev. This is the single easiest way to produce numbers that look great and mean nothing.

**Inference must run off the async event loop.** Inline `torch` calls stall `track.recv()`, drop frames, and smear Branch D's timestamps. The two branches then corrupt each other, which is a miserable thing to debug at 3am.

**Turn off `echoCancellation`, `noiseSuppression`, `autoGainControl`.** All three default to on in `getUserMedia`. Noise suppression gates quiet speech onsets, shifting exactly the turn boundaries Branch D measures.

**Timestamp at RTP arrival, not playout.** WebRTC's jitter buffer is adaptive, so playout timing drifts with network conditions — reintroducing the confound you were eliminating.

**Backpressure.** If inference is slower than the hop, windows queue and the score falls silently further behind the live call while everything looks healthy. Drop stale windows and expose the drop rate as a metric. Put a `TODO` in P3 and fix it before P4.

**Don't host the demo on Colab or Kaggle.** No inbound network means a tunnel; a tunnel means TURN-over-TCP; that adds jitter that dominates what Branch D measures. Colab also disconnects. Train in the cloud, demo on the laptop.

---

# 12. Pre-demo checklist

Run this the day before, on the actual demo machine, with **Wi-Fi physically disabled**.

```
[ ] Wi-Fi OFF. Airplane mode. Then run everything below.
[ ] Server starts, loads both checkpoints, no network calls attempted
[ ] cache/ contains XLS-R, ECAPA and Silero; nothing tries to download
[ ] Browser reaches the dashboard over localhost
[ ] Mic permission granted; AEC/NS/AGC confirmed off in the console
[ ] Live speech scores green, confidence band visibly narrows
[ ] Cloned audio scores red within ~10 s
[ ] Two-needle view shows baseline collapsing, ours holding, on codec audio
[ ] Pre-recorded playback flagged by Branch D on the first turn
[ ] Challenge prompt fires at amber
[ ] Approval screen gates with reason and timeline attached
[ ] Audit log shows a signed verdict and zero audio files on disk
[ ] Full run-through completes twice, unassisted, start to finish
[ ] Laptop charged; charger packed; a second machine has the same build
```

That last line matters. One laptop is a single point of failure for the entire submission.

---

*Implementation guide · SIH 26104 · companion to the notes and the SRS*
