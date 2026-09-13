# Voice Integrity Verification Framework
## Recommended SIH Tech Stack Simplification and Scope Revision

**Based on:** Voice Integrity Verification Framework — Backend SRS Rev 1.0  
**Target:** Smart India Hackathon (SIH) prototype / demonstrator  
**Problem statement:** SIH 26104 — real-time voice clone / synthetic speech detection  
**Purpose of this document:** Identify backend technologies and architectural choices that are overengineered for an SIH implementation, explain the concerns in detail, and define a simpler stack that preserves the strongest technical ideas of the original SRS.

---

# 1. Executive Summary

The current Backend SRS is designed much closer to a **production-grade security platform** than a hackathon prototype. It includes:

- 15 backend modules
- 78 functional requirements
- four independent detection branches
- calibrated log-likelihood-ratio fusion
- signed policy configuration
- signed verdicts
- hash-chained audit logs
- Merkle checkpoints
- encrypted biometric templates
- KMS/HSM integration
- model signing
- sender-constrained authentication
- hardware-backed storage
- trusted execution environments
- optional homomorphic encryption
- optional federated learning and differential privacy
- an edge/server cascade
- high-concurrency performance targets

Most of these ideas are technically valid. The problem is not that they are bad architecture. The problem is that implementing them for SIH would distribute engineering effort across too many independent problems and reduce the quality of the component that judges will care about most:

> **Can the system reliably detect synthetic / cloned speech in realistic live-call conditions?**

For SIH, the recommended strategy is therefore:

1. Make **real-time spoof / synthetic speech detection** the primary capability.
2. Preserve **audio conditioning, VAD, codec robustness, streaming inference, and rigorous evaluation**.
3. Make **speaker verification** an optional but valuable second signal.
4. Keep **simple risk classification and Ed25519-signed final verdicts**.
5. Defer production-security infrastructure such as KMS/HSM, Merkle logs, cancelable biometrics, TEE inference, homomorphic encryption, federated learning, and large-scale edge/server orchestration.
6. Use a **single Python/FastAPI/PyTorch backend** rather than a distributed microservice architecture.
7. Optimize for a credible, measurable demo rather than enterprise deployment scale.

The simplified backend should contain roughly six core components:

```text
Audio / WebRTC / Uploaded Stream
            |
            v
    Stream Ingestion
            |
            v
 Audio Conditioning + VAD
            |
            v
 Synthetic Speech Detector
            |
            +----------------------+
            |                      |
            v                      v
 Streaming Risk Score      Speaker Verification
                                (optional)
            |                      |
            +----------+-----------+
                       |
                       v
               Simple Policy Layer
                       |
                       v
             Signed Final Verdict
                       |
                       v
               UI / Consumer API
```

---

# 2. Guiding Principle for the SIH Version

The original SRS is optimized for:

- long-term deployment,
- financial-risk environments,
- multi-tenant security,
- cryptographic auditability,
- biometric-template protection,
- operational resilience,
- high concurrency,
- future edge deployment,
- strong trust boundaries.

The SIH version should instead optimize for:

- demonstrability,
- technical credibility,
- model performance,
- generalization,
- real-time inference,
- explainability,
- robustness to telephony conditions,
- clear evaluation,
- fast implementation,
- easy integration with a frontend demo.

The central decision rule for every backend feature should be:

> **Does this feature materially improve the quality, credibility, or demonstrability of the voice-clone detection system during SIH?**

If the answer is no, it should be deferred even if it would be valuable in a production deployment.

---

# 3. Recommended Final Technology Stack

## 3.1 Core Stack

| Layer | Original / implied direction | Recommended SIH choice | Status |
|---|---|---|---|
| Backend language | Python-oriented ML stack | **Python 3.x** | Keep |
| API framework | API gateway + REST/WebSocket contracts | **FastAPI** | Simplify |
| Streaming transport | WebSocket / WebRTC / SIPREC adapters | **WebSocket first**, WebRTC adapter if required | Simplify |
| ML framework | Model-dependent | **PyTorch** | Keep |
| Audio processing | Resampling, VAD, conditioning | **torchaudio + Silero VAD or WebRTC VAD** | Keep |
| Spoof model | SSL front-end + graph-attention detector | **AASIST or SSL-based anti-spoof classifier** | Keep core capability |
| SSL front-end | ~300M feature extractor | **Use only if chosen detector requires it** | Make model-dependent |
| Speaker verification | ECAPA embedding | **ECAPA-TDNN**, optional | Keep as differentiator |
| Streaming state | Session manager / bounded buffers | **In-process session object + bounded ring buffer** | Simplify |
| Database | Voiceprint/audit infrastructure | **SQLite for prototype; PostgreSQL if needed** | Simplify |
| Raw audio storage | Forbidden | **Do not persist raw audio** | Keep |
| Authentication | mTLS + sender-constrained credentials | **Simple API token / JWT for demo** | Simplify |
| Verdict integrity | Ed25519 | **Ed25519** | Keep |
| Audit | Hash chain + Merkle checkpoints | **Structured verdict log only** | Simplify |
| Model registry | Signed model registry | **Version string + checksum** | Simplify |
| Observability | Dedicated metrics subsystem | **Python logging + latency counters** | Simplify |
| Deployment | T1/T2/T3/T4 tiers | **Dockerized single backend** | Simplify |
| GPU execution | T4-class GPU target | **CUDA when available; CPU fallback if feasible** | Keep practical |
| Edge model | <=5 MB independent detector | **Stretch goal only** | Defer |
| KMS/HSM | Required production key custody | **Not required for SIH prototype** | Defer |
| TEE | Optional trusted execution | **Roadmap only** | Defer |
| Homomorphic encryption | Optional CKKS | **Remove from implementation scope** | Defer |
| Federated learning | Optional secure aggregation + DP-SGD | **Remove from implementation scope** | Defer |

---

# 4. Detailed Review of Each Original Module

# 4.1 M1 — Ingest Adapters

## Original role

The SRS proposes transport adapters for:

- WebRTC
- SIPREC
- Twilio / hosted media streams
- file input

with normalization to a common internal representation.

## Concern for SIH

Implementing several real telephony integrations is expensive and mostly integration work rather than ML or core backend work.

SIPREC in particular can introduce:

- SIP infrastructure requirements,
- enterprise telephony assumptions,
- network configuration issues,
- test-environment difficulty,
- debugging overhead unrelated to detection performance.

If the team spends significant time supporting every transport in the SRS, it risks having multiple partially functioning integrations rather than one stable live demonstration.

## Recommended change

Implement only:

1. **WebSocket audio ingestion**
2. **file upload / prerecorded audio path for repeatable testing**
3. **WebRTC adapter only if the frontend demo needs real microphone / call streaming**

Use one internal frame representation:

```python
AudioFrame(
    session_id,
    side,
    samples,
    timestamp
)
```

All input sources should convert to this format.

## SIH priority

**P0 — Required**

## Deferred

- SIPREC
- carrier integration
- Twilio-specific production adapter
- multi-provider adapter framework

---

# 4.2 M2 — Session Manager

## Original role

The SRS requires:

- isolated per-call sessions,
- bounded ring buffers,
- teardown on all exit paths,
- no cross-session state leakage.

## Concern for SIH

The concept is correct, but a full session-management subsystem would be unnecessary if the demo only serves a few concurrent users.

The dangerous simplification would be removing session isolation entirely. That should not be done.

## Recommended change

Use an in-memory dictionary:

```python
sessions: dict[str, SessionState]
```

Each `SessionState` should contain:

```text
session_id
audio_buffer
speech_seconds
last_prediction
prediction_history
speaker_reference (optional)
model_version
created_at
```

Use a fixed-size `deque` or NumPy/PyTorch buffer for audio.

On disconnect:

- remove session state,
- clear mutable buffers,
- store only final metadata if needed.

## SIH priority

**P0 — Required**

## Main implementation concern

Do not let model inference block the event loop.

FastAPI/WebSocket receives audio continuously, so inference should execute:

- in a worker thread,
- background task,
- process pool,
- or dedicated inference queue.

For a single-machine prototype, a lightweight worker is enough.

---

# 4.3 M3 — Audio Conditioning

## Original role

The conditioning pipeline includes:

- 16 kHz normalization,
- mono conversion,
- VAD gating,
- fixed analysis windows,
- codec augmentation during training.

## Recommendation

**Keep this module.**

This is one of the most valuable parts of the original design.

A real voice-clone detector can perform well on clean benchmark audio and fail badly after:

- telephony compression,
- resampling,
- packet loss,
- background noise,
- bandwidth reduction,
- codec artifacts.

Therefore the conditioning pipeline is essential.

## Recommended implementation

Use:

- `torchaudio` for loading, resampling and transformations
- `Silero VAD` or `WebRTC VAD`
- in-memory float32 mono samples
- fixed training-compatible windows

Pipeline:

```text
incoming audio
    |
    v
decode / PCM conversion
    |
    v
mono conversion
    |
    v
resample to 16 kHz
    |
    v
VAD
    |
    v
speech-only buffer
    |
    v
model-compatible window
```

## Important change to the SRS

The current SRS fixes the analysis window at exactly:

```text
64,600 samples / 4.04 seconds
```

That number should not be a system-wide architectural constant unless the selected final model is trained with that crop.

Instead use:

```text
MODEL_SAMPLE_RATE
MODEL_WINDOW_SAMPLES
MODEL_HOP_SAMPLES
```

loaded from model configuration.

The correct requirement is:

> **Inference geometry must match the geometry used when the model was trained or fine-tuned.**

This preserves the original intent without coupling the complete backend to one model implementation.

## SIH priority

**P0 — Required**

---

# 4.4 Codec Augmentation

## Recommendation

**Keep and emphasize this feature.**

Codec robustness is more valuable for SIH than many of the production security features in the original document.

Recommended augmentation conditions include:

- G.711 / narrowband simulation
- AMR-like narrowband degradation where practical
- Opus degradation
- 8 kHz downsample → 16 kHz upsample
- additive noise
- short dropouts
- packet-loss simulation
- gain variation

## Why this matters

The final demo is meant to represent phone-call conditions.

If a detector is tested only on clean WAV files, judges can reasonably question whether it will survive deployment conditions.

The team should therefore show:

```text
Clean evaluation
vs
Codec-degraded evaluation
```

This is a much stronger SIH demonstration than adding another infrastructure service.

## SIH priority

**P0 / P1 — Strongly recommended**

---

# 4.5 M4 — SSL Feature Extraction

## Original role

The SRS assumes a large self-supervised speech front end producing approximately:

```text
[T, 1024]
```

hidden-state features.

## Concern for SIH

A ~300M parameter SSL model can create:

- high GPU memory usage,
- poor CPU latency,
- slow cold starts,
- large Docker images,
- difficult live inference,
- unnecessary complexity if the chosen spoof model already contains an appropriate front-end.

## Recommended change

Do not define the SSL extractor as a permanent independent platform module.

Instead, make it part of the selected detection model.

Two valid approaches:

### Option A — AASIST-style model

Use a relatively self-contained anti-spoof architecture.

Advantages:

- easier integration,
- anti-spoofing-specific architecture,
- simpler deployment.

### Option B — SSL-based classifier

Use a pretrained speech encoder such as a wav2vec/WavLM-family model followed by a classification head.

Advantages:

- strong learned representations,
- easier experimentation with transfer learning.

Disadvantages:

- heavier inference,
- potentially greater latency,
- larger model artifact.

## SIH recommendation

Select whichever performs best under the team's available compute and target latency.

Do not force the backend design around a specific SSL dimensionality before the final detector is selected.

## SIH priority

**P0 — Model-dependent**

---

# 4.6 M5.A — Spoof Detector

## Recommendation

This must become the **primary system component**.

Everything else should support this module.

The detector should answer:

```text
How likely is this speech to be synthetic / cloned?
```

Recommended output:

```json
{
  "spoof_probability": 0.91,
  "confidence": 0.87,
  "model_version": "sih-spoof-v1"
}
```

## Main technical priorities

Invest engineering time in:

- dataset quality,
- augmentation,
- class balance,
- unseen-generator testing,
- multilingual testing,
- codec robustness,
- calibration,
- streaming stability.

These will matter far more than additional security infrastructure.

## SIH priority

**P0 — Core**

---

# 4.7 M5.B — Speaker Verification

## Original role

The SRS proposes ECAPA embeddings compared against an enrolled voiceprint.

## Recommendation

Keep this as an **optional differentiator**.

It solves a different problem than spoof detection:

```text
Spoof detection:
"Was this audio synthetically generated?"

Speaker verification:
"Does this audio match the claimed person?"
```

This distinction is valuable in a demo.

Example:

```text
Spoof score: LOW
Speaker match: LOW
=> possible human impersonation / wrong speaker
```

or:

```text
Spoof score: HIGH
Speaker match: HIGH
=> possible synthetic clone of the enrolled speaker
```

## Simplification

Do not implement the full biometric vault architecture.

For SIH:

1. user enrolls reference audio,
2. ECAPA creates an embedding,
3. embedding is stored only for the current demo/session or encrypted locally,
4. incoming speech receives an embedding,
5. cosine similarity is calculated.

## Concern

Speaker verification requires:

- enrollment,
- suitable reference audio,
- threshold tuning,
- protection against false interpretation.

It must not delay the primary spoof detector.

## SIH priority

**P1 — Valuable optional feature**

---

# 4.8 M5.C — Prosody Analysis

## Original role

The SRS proposes features such as:

- F0 contour,
- pause statistics,
- speaking rate,
- jitter,
- shimmer.

It also explicitly treats this branch as language-sensitive and prevents it from independently changing the policy band.

## Concern

This is a poor cost-to-value tradeoff for SIH.

The team would need to solve:

- pitch tracking,
- silence definition,
- speaking-rate normalization,
- multilingual behavior,
- speaker-specific variation,
- noisy-call robustness,
- feature calibration,
- fusion weighting.

After doing all of that, the branch is still considered too unreliable to independently determine the final risk band.

## Recommendation

**Remove from SIH MVP.**

Possible future use:

- explainability research,
- auxiliary features,
- abnormal speech pattern analysis.

## SIH priority

**P3 — Defer**

---

# 4.9 M5.D — Conversational Liveness

## Original role

The SRS proposes detecting a machine in the conversational loop using:

- response-floor differences,
- overlap rate,
- variance ratio,
- entrainment divergence,
- within-call comparison between near and far sides.

## Technical value

This is one of the most interesting research ideas in the SRS.

It may provide evidence that is independent of waveform artifacts.

## Concern for SIH

It introduces an entirely new experimental problem.

The team would require datasets containing:

- human-human conversations,
- human-AI conversations,
- different network delays,
- different languages,
- turn-taking behavior,
- overlap patterns,
- variable conversational styles.

It also depends on reliable dual-stream timing.

Without proper validation, the feature risks appearing sophisticated but unsupported.

## Recommendation

Move it to a **stretch goal / Phase 2**.

If implemented, present it as an experimental secondary signal rather than a production-ready detector.

## SIH priority

**P2 / P3 — Stretch goal**

---

# 4.10 M6 — Fusion and Scoring

## Original role

The SRS uses calibrated log-likelihood ratios:

```text
raw branch score
    |
    v
Platt calibration
    |
    v
LLR
    |
    v
clamp
    |
    v
sum across branches/windows
    |
    v
metadata prior
    |
    v
risk score
```

## Technical assessment

The reasoning is mathematically sound.

Uncalibrated outputs from different models should not simply be averaged.

## Concern for SIH

If only one primary detector and one optional speaker verifier are used, full LLR fusion becomes unnecessary.

More importantly, building a sophisticated fusion model without a sufficiently large development set creates the appearance of mathematical rigor without reliable calibration.

## Recommended SIH approach

Use independent outputs.

Example:

```json
{
  "spoof_probability": 0.93,
  "speaker_similarity": 0.42,
  "spoof_status": "RED",
  "identity_status": "MISMATCH"
}
```

Then derive the UI risk state with deterministic rules.

Example:

```text
spoof >= 0.80        -> RED
0.50 <= spoof < 0.80 -> AMBER
spoof < 0.50         -> GREEN
```

Speaker verification can produce a separate flag:

```text
speaker_similarity < threshold
=> IDENTITY_WARNING
```

## When to reintroduce calibrated fusion

Only after the team has:

- multiple reliable branches,
- a development split,
- calibration curves,
- enough samples for stable threshold estimation.

## SIH priority

**Simplify heavily**

---

# 4.11 M7 — Policy Engine

## Original role

The SRS includes:

- transaction-value tiers,
- policy versions,
- signed configuration,
- risk bands,
- action directives.

## Concern

This assumes integration into a banking or enterprise authorization workflow.

For SIH, there is no need to build a generalized policy engine.

## Recommended change

Implement a small deterministic rule layer.

Example:

```python
if spoof_probability >= RED_THRESHOLD:
    action = "BLOCK_OR_VERIFY"
elif spoof_probability >= AMBER_THRESHOLD:
    action = "CHALLENGE"
else:
    action = "ALLOW"
```

Optional additional rule:

```python
if speaker_check_enabled and speaker_match is False:
    action = max_risk(action, "CHALLENGE")
```

Thresholds can live in a simple configuration file.

## SIH priority

**P0, but minimal implementation**

---

# 4.12 M8 — Verdict Attestation

## Original role

The SRS signs final verdicts and stores them in a tamper-evident logging system.

## Recommendation

Keep **Ed25519 signing**, remove the rest of the attestation infrastructure.

This is one of the rare security features that offers:

- strong technical value,
- low implementation cost,
- good presentation value.

Signed output example:

```json
{
  "payload": {
    "session_id": "abc-123",
    "spoof_probability": 0.93,
    "risk": "RED",
    "model_version": "sih-v1",
    "timestamp": 0,
    "nonce": "..."
  },
  "algorithm": "Ed25519",
  "signature": "..."
}
```

## Why retain it

It allows the team to explain:

> The ML system does not merely return a number. The final decision artifact can be cryptographically verified by the consuming system.

That is credible and easy to demonstrate.

## Remove for SIH

- Merkle checkpoints
- external anchoring
- complex audit-consistency proofs

## SIH priority

**P1 — Recommended**

---

# 4.13 M9 — Voiceprint Vault

## Original role

The SRS proposes:

- cancelable biometrics,
- non-invertible transformations,
- AES-256-GCM,
- per-tenant protection,
- revocation.

## Concern

This is a separate biometric-security project.

It does not directly improve synthetic-speech detection.

Implementing it correctly requires:

- biometric-template research,
- secure key management,
- revocation design,
- storage design,
- cryptographic review.

## Recommended change

For SIH:

### If speaker verification is not implemented
Remove the module entirely.

### If speaker verification is implemented
Store embeddings:

- in memory for demo sessions, or
- encrypted in a local database.

Explicitly state:

> Production deployment would require cancelable biometric-template protection and managed key custody.

## SIH priority

**P3 — Remove from MVP**

---

# 4.14 M10 — Key Management

## Original role

The SRS includes KMS/HSM-backed envelope encryption and key rotation.

## Concern

KMS/HSM integration introduces:

- infrastructure dependencies,
- cloud account dependencies,
- permissions complexity,
- deployment configuration,
- additional failure modes.

For SIH it provides almost no visible improvement to the detection demo.

## Recommendation

Use local development keys stored outside source control.

For example:

```text
.env / mounted secret
```

for a prototype signing key.

Do **not** claim this is production secure.

In the architecture diagram, show:

```text
SIH: local secret
Production: KMS / HSM
```

## SIH priority

**P3 — Defer**

---

# 4.15 M11 — API Gateway

## Original role

The SRS calls for:

- client authentication,
- transport security,
- rate limiting,
- routing,
- mTLS.

## Concern

A separate API gateway is unnecessary for a single backend service.

## Recommended change

Use FastAPI directly.

Suggested endpoints:

```text
POST /v1/session
WS   /v1/stream/{session_id}
POST /v1/enroll/{session_id}       optional
GET  /v1/verdict/{session_id}
GET  /health
```

For demo authentication:

- static API key,
- simple JWT,
- or no authentication in an isolated local network.

## Production roadmap

Later add:

- TLS termination,
- reverse proxy,
- mTLS,
- rate limits,
- proper identity provider.

## SIH priority

**P0 — Simplified**

---

# 4.16 M12 — Model Registry

## Original role

The SRS includes:

- signed model artifacts,
- version pinning,
- signature verification.

## Concern

A model registry service is unnecessary when the prototype has one or a few models.

## Recommended change

Each model artifact should have:

```text
model_version
git_commit
config_hash
weights_checksum
training_dataset_version
```

At application startup:

1. load config,
2. load weights,
3. optionally verify SHA-256 checksum,
4. expose version in `/health`,
5. include version in every verdict.

Example:

```json
{
  "model_version": "spoof-v1.3",
  "weights_sha256": "..."
}
```

This preserves reproducibility without needing registry infrastructure.

## SIH priority

**P1 — Simplified**

---

# 4.17 M13 — Observability

## Original role

Dedicated operational metrics, health probes and privacy-safe logs.

## Recommendation

Keep the concept but not a full observability stack.

Record:

- session count,
- inference latency,
- dropped window count,
- speech seconds,
- model failures,
- GPU/CPU mode,
- final score.

Use:

```python
logging
time.perf_counter()
```

and simple application counters.

Optional:

- Prometheus-compatible `/metrics` if easy to add.

## Do not add solely for SIH

- Elasticsearch
- Logstash
- Kibana
- distributed tracing
- large monitoring infrastructure

## SIH priority

**P1 — Minimal**

---

# 4.18 M14 — Evaluation Harness

## Recommendation

**Keep this module and give it high priority.**

This is more important than most infrastructure features.

The evaluation harness should compute:

- EER
- ROC / DET data
- TPR at selected FPR
- FPR / FNR
- inference latency
- codec-specific results
- language-specific results
- generator-specific results

Recommended experiment matrix:

| Condition | Purpose |
|---|---|
| clean in-domain | baseline |
| codec-degraded in-domain | telephony robustness |
| unseen generator | generalization |
| noisy conditions | deployment robustness |
| English | language baseline |
| Hindi | multilingual test |
| additional Indian language if available | stronger SIH claim |

## Key principle

Do not report only accuracy.

For fraud-like detection problems, false positives matter greatly.

At minimum show:

```text
EER
TPR @ 1% FPR
```

## SIH priority

**P0 — Essential**

---

# 4.19 M15 — Edge Cascade Controller

## Original role

The SRS proposes:

```text
lightweight on-device detector
        |
        +-- clear locally
        |
        +-- ambiguous -> full server model
```

## Technical value

This is an excellent production architecture because it can improve:

- privacy,
- bandwidth,
- server load,
- latency.

## Concern for SIH

It effectively requires building:

- a second model,
- a second runtime,
- threshold calibration,
- mobile/edge deployment,
- cascade behavior,
- server escalation.

That is a separate engineering track.

## Recommendation

Present as a roadmap item.

If the team finishes early, an ONNX or quantized small model can be explored as a stretch goal.

## SIH priority

**P3 — Defer**

---

# 5. Security Stack: What to Keep and What to Defer

## 5.1 Keep

### Transport security

For hosted demonstration environments, use normal HTTPS/WSS if available.

### No persistent raw audio

Raw audio should remain in memory unless the user explicitly uploads a test sample.

### Signed final verdict

Use Ed25519.

### Secrets outside source control

Keys and tokens must not be committed to Git.

### Minimal logs

Logs should contain:

- session IDs,
- scores,
- latency,
- errors,
- model versions.

Avoid raw audio and embeddings unless required for controlled testing.

---

## 5.2 Defer

The following original-SRS security features should be moved to a **Production Hardening Roadmap**:

### KMS / HSM

Reason for deferral:

- external infrastructure,
- credential complexity,
- little value for prototype scoring.

### Cancelable biometric templates

Reason:

- significant research/implementation effort,
- only relevant if long-term speaker enrollment exists.

### Per-tenant unlinkability

Reason:

- multi-tenant threat model is outside the SIH MVP.

### Merkle audit checkpoints

Reason:

- simple signed records already demonstrate integrity.

### External log anchoring

Reason:

- not required for demonstration.

### Sender-constrained credentials / DPoP

Reason:

- unnecessary for single-client demo environment.

### Trusted Execution Environments

Reason:

- hardware/platform dependency,
- deployment complexity.

### Homomorphic encryption

Reason:

- very high implementation complexity,
- potential large performance cost,
- not required to demonstrate the core detection value.

### Federated learning

Reason:

- requires multi-client training architecture,
- aggregation infrastructure,
- additional threat model.

### Differential privacy

Reason:

- relevant to production model-improvement pipelines, not the SIH detector runtime.

---

# 6. Storage Strategy

## Recommended SIH storage model

### Raw audio

```text
Persistent storage: NO
Runtime: bounded in-memory buffer
```

### Prediction metadata

Can be stored.

Example:

```json
{
  "session_id": "abc",
  "timestamp": 123,
  "spoof_probability": 0.83,
  "risk": "RED",
  "model_version": "v1"
}
```

### Speaker embeddings

If speaker verification is implemented:

**Preferred SIH approach**

```text
session memory only
```

Alternative:

```text
encrypted local database
```

### Database recommendation

Start with:

**SQLite**

Move to PostgreSQL only if the application needs:

- multi-user persistence,
- concurrent writes,
- remote deployment,
- richer user/account state.

Do not add PostgreSQL just because it is more production-like.

---

# 7. Recommended Streaming Design

A simple streaming pipeline is enough.

```text
Frontend microphone / call simulator
            |
            | audio frames
            v
      FastAPI WebSocket
            |
            v
       SessionState
            |
            v
      bounded audio buffer
            |
            v
             VAD
            |
            v
      speech accumulation
            |
            v
    model-sized window ready?
        /            \
      no              yes
      |                |
      +----wait         v
                  inference worker
                        |
                        v
                spoof probability
                        |
                        v
                 WebSocket response
```

## Important engineering concerns

### 1. Never run heavy inference directly inside the async receive loop

Otherwise incoming frames can stall.

### 2. Bound the buffer

Do not allow unlimited audio accumulation.

### 3. Drop stale inference work

If the model cannot keep up with the stream, do not build an ever-growing queue.

Prefer:

```text
latest available window
```

over:

```text
process every historical window late
```

### 4. Track speech time separately

A four-second analysis window should represent approximately four seconds of speech, not four seconds of wall-clock silence.

---

# 8. Recommended Model Output Contract

Streaming message:

```json
{
  "session_id": "abc-123",
  "sequence": 12,
  "speech_seconds": 18.2,
  "spoof_probability": 0.87,
  "risk": "RED",
  "speaker_similarity": null,
  "speaker_status": "NOT_ENROLLED",
  "inference_ms": 142,
  "model_version": "spoof-v1"
}
```

Optional speaker-verification output:

```json
{
  "speaker_similarity": 0.41,
  "speaker_status": "MISMATCH"
}
```

Final signed verdict:

```json
{
  "payload": {
    "session_id": "abc-123",
    "spoof_probability": 0.87,
    "risk": "RED",
    "speaker_status": "MISMATCH",
    "model_version": "spoof-v1",
    "timestamp": 0,
    "nonce": "..."
  },
  "algorithm": "Ed25519",
  "signature": "..."
}
```

---

# 9. Recommended API Surface

## `POST /v1/session`

Creates an analysis session.

Response:

```json
{
  "session_id": "..."
}
```

---

## `WS /v1/stream/{session_id}`

Accepts audio frames and returns streaming inference results.

Purpose:

- real-time demo,
- progressive scoring,
- frontend risk visualization.

---

## `POST /v1/enroll/{session_id}`

Optional.

Uploads or streams reference speech for speaker enrollment.

Returns:

```json
{
  "status": "ENROLLED"
}
```

---

## `GET /v1/verdict/{session_id}`

Returns final signed verdict.

---

## `GET /health`

Returns:

```json
{
  "status": "ok",
  "model_version": "spoof-v1",
  "device": "cuda"
}
```

---

# 10. Recommended Deployment Architecture

For SIH:

```text
┌──────────────────────────────┐
│ Frontend / Call Simulator    │
└──────────────┬───────────────┘
               │
         HTTPS / WSS
               │
               v
┌──────────────────────────────┐
│ FastAPI Backend              │
│                              │
│ - Session handling           │
│ - Audio conditioning         │
│ - VAD                        │
│ - Inference orchestration    │
│ - Risk logic                 │
│ - Verdict signing            │
└──────────────┬───────────────┘
               │
               v
┌──────────────────────────────┐
│ PyTorch Model                │
│ GPU if available             │
└──────────────────────────────┘

Optional:
        |
        v
┌──────────────────────────────┐
│ SQLite / PostgreSQL          │
│ metadata only                │
└──────────────────────────────┘
```

Containerize the backend with Docker.

Do not introduce Kubernetes unless the deployment platform explicitly requires it.

---

# 11. Technologies That Should Not Be Added Without a Real Need

The following technologies are common sources of hackathon overengineering.

## Kafka

Not needed.

The application is not processing a distributed event stream at SIH scale.

## Redis

Not needed unless:

- sessions must survive across multiple backend workers, or
- there is an explicit shared-state requirement.

In-process memory is simpler.

## Kubernetes

Not needed for a single-service demonstrator.

## Elasticsearch

Not needed.

Structured Python logs are sufficient.

## Separate model-serving framework

Triton / TorchServe / dedicated inference service should be considered only if:

- model serving is already stable,
- deployment requires it,
- there is a measured performance benefit.

Otherwise direct PyTorch inference is simpler.

## Vector database

No requirement in this project justifies one.

## Microservices

Avoid splitting ingestion, inference, policy, signatures, logs and speaker verification into separate services.

For SIH:

> **A modular monolith is the correct architecture.**

Keep separate Python modules inside one deployable backend.

---

# 12. Recommended Python Project Structure

```text
backend/
│
├── app/
│   ├── main.py
│   │
│   ├── api/
│   │   ├── session.py
│   │   ├── stream.py
│   │   ├── enroll.py
│   │   └── verdict.py
│   │
│   ├── audio/
│   │   ├── resample.py
│   │   ├── vad.py
│   │   ├── buffering.py
│   │   └── augmentation.py
│   │
│   ├── models/
│   │   ├── spoof_detector.py
│   │   ├── speaker_verifier.py
│   │   └── model_loader.py
│   │
│   ├── inference/
│   │   ├── worker.py
│   │   └── scoring.py
│   │
│   ├── policy/
│   │   └── thresholds.py
│   │
│   ├── security/
│   │   └── signing.py
│   │
│   ├── sessions/
│   │   └── manager.py
│   │
│   └── schemas/
│       ├── stream.py
│       └── verdict.py
│
├── evaluation/
│   ├── evaluate.py
│   ├── metrics.py
│   ├── codec_tests.py
│   └── reports.py
│
├── training/
│   ├── dataset.py
│   ├── augment.py
│   ├── train.py
│   └── config.yaml
│
├── models/
│   └── weights/
│
├── tests/
│   ├── test_audio.py
│   ├── test_sessions.py
│   ├── test_streaming.py
│   └── test_verdict.py
│
├── Dockerfile
├── requirements.txt
└── README.md
```

This preserves module boundaries without creating deployment complexity.

---

# 13. Revised Non-Functional Requirements for SIH

The original SRS includes production targets such as dozens of concurrent calls and strict edge deployment constraints.

Recommended SIH targets:

| Requirement | SIH target |
|---|---:|
| First prediction | <= 5 seconds of accumulated speech |
| Update interval | 1–2 seconds |
| Per-window inference | < 1 second on target hardware |
| Concurrent demo sessions | 1–5 |
| Raw audio at rest | 0 bytes |
| Session buffer | bounded |
| Disconnect cleanup | immediate / best effort |
| Model version visible | yes |
| Codec-degraded evaluation | required |
| Unseen-generator evaluation | strongly recommended |
| Multilingual evaluation | strongly recommended |
| Signed final verdict | recommended |

These targets are:

- measurable,
- demonstrable,
- achievable,
- directly relevant to the solution.

---

# 14. Recommended Evaluation Scope

A technically strong SIH system should spend significant time on evaluation.

## 14.1 Minimum evaluation

### Dataset split

Maintain:

```text
train
development
test
```

Thresholds must be selected from the development set, not the final test set.

### Metrics

Report:

- EER
- ROC-AUC
- TPR @ 1% FPR
- confusion matrix at selected operating point
- inference latency

### Do not rely on

```text
accuracy alone
```

---

# 14.2 Robustness tests

## Codec degradation

Evaluate:

```text
clean
G.711-like
8 kHz narrowband
Opus-like
noisy
```

## Generator generalization

Where dataset availability allows:

```text
train on generators A, B, C
test on generator D
```

This is much more convincing than random train/test mixing across the same synthesis systems.

## Language evaluation

At least:

```text
English
Hindi
```

Additional Indian languages are valuable if matched data is available.

Do not claim language independence unless results support the claim.

---

# 15. Revised Feature Priorities

## P0 — Must work before presentation

- audio ingestion
- 16 kHz conditioning
- VAD
- model-compatible windowing
- spoof detector
- real-time / near-real-time inference
- simple risk bands
- streaming API
- evaluation harness
- codec-degraded testing
- basic frontend integration

## P1 — High-value additions

- speaker verification
- Ed25519-signed final verdict
- multilingual evaluation
- unseen-generator evaluation
- Docker deployment
- model checksum/version metadata
- latency dashboard

## P2 — Stretch features

- conversational liveness
- richer calibration
- persistent metadata database
- Prometheus metrics
- ONNX export / optimization

## P3 — Production roadmap

- SIPREC
- carrier integration
- full edge cascade
- KMS/HSM
- cancelable biometrics
- Merkle audit system
- external trust anchoring
- TEE
- homomorphic encryption
- federated learning
- differential privacy
- multi-node scaling
- large API gateway architecture

---

# 16. Recommended Final SIH Scope Statement

A concise technical scope should be:

> The SIH system passively analyzes live or streamed speech without entering the media path. Incoming speech is normalized, voice activity is extracted, and speech-only windows are evaluated by a trained synthetic-speech detector. The backend continuously returns a clone-risk score and classifies the session into green, amber or red risk bands. An optional speaker-verification branch compares the live speaker against an enrolled reference identity. Raw call audio is kept in bounded memory and is not persistently stored. At session completion, the backend can produce an Ed25519-signed verdict containing the model version, score, risk band and session metadata. Model quality is evaluated under clean, codec-degraded, unseen-generator and multilingual conditions.

This scope retains the strongest ideas in the original architecture while making the project feasible for an SIH team.

---

# 17. Final Recommended Architecture

```text
                         ┌─────────────────────┐
                         │ Frontend / WebRTC   │
                         │ / Audio Simulator   │
                         └──────────┬──────────┘
                                    │
                                    v
                         ┌─────────────────────┐
                         │ FastAPI + WebSocket │
                         │ Stream Ingestion    │
                         └──────────┬──────────┘
                                    │
                                    v
                         ┌─────────────────────┐
                         │ Session Manager     │
                         │ bounded buffers     │
                         └──────────┬──────────┘
                                    │
                                    v
                         ┌─────────────────────┐
                         │ Audio Conditioning  │
                         │ 16 kHz + VAD        │
                         │ model-size windows  │
                         └──────────┬──────────┘
                                    │
                                    v
                         ┌─────────────────────┐
                         │ Spoof Detector      │
                         │ PyTorch             │
                         └──────────┬──────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
                     v                             v
          ┌─────────────────────┐       ┌─────────────────────┐
          │ Spoof Probability   │       │ Speaker Verification│
          │                     │       │ ECAPA — optional    │
          └──────────┬──────────┘       └──────────┬──────────┘
                     │                             │
                     └──────────────┬──────────────┘
                                    │
                                    v
                         ┌─────────────────────┐
                         │ Simple Policy Layer │
                         │ GREEN/AMBER/RED     │
                         └──────────┬──────────┘
                                    │
                         ┌──────────┴──────────┐
                         │                     │
                         v                     v
              ┌─────────────────┐   ┌──────────────────┐
              │ Streaming Score │   │ Final Verdict    │
              │ to frontend     │   │ Ed25519 optional │
              └─────────────────┘   └──────────────────┘
```

---

# 18. Final Technology Decisions

The recommended SIH backend is therefore:

```text
Python
FastAPI
WebSocket
PyTorch
torchaudio
Silero VAD or WebRTC VAD
AASIST or SSL-based spoof detector
ECAPA-TDNN speaker verification (optional)
NumPy
SQLite initially
PostgreSQL only if persistence needs justify it
Ed25519 signing
Docker
CUDA when available
```

The backend should remain a **modular monolith**.

The team should specifically avoid adding infrastructure merely to make the architecture appear more advanced.

A smaller system with:

- strong detection,
- strong evaluation,
- real streaming,
- realistic codec testing,
- measured latency,
- clear explainability,

will be substantially more convincing than a large architecture containing many incomplete security and infrastructure modules.

---

# 19. What Should Remain in the Presentation but Not in the SIH Build

The following original-SRS ideas are worth preserving in architecture / roadmap slides because they demonstrate production awareness:

```text
KMS / HSM-backed key custody
Cancelable biometric templates
Hash-chained audit logs
Merkle checkpoints
TEE inference
Edge/server cascade
Carrier integration
mTLS
Multi-tenant isolation
Federated learning
Differential privacy
Homomorphic template matching
50+ concurrent-call scaling
```

They should be explicitly labelled:

> **Production hardening / future deployment architecture**

rather than presented as completed prototype functionality.

This distinction improves credibility.

---

# 20. Final Recommendation

The original SRS should not be discarded. It should be treated as the **production vision**.

For SIH, however, implementation should focus on approximately six primary capabilities:

1. **Audio ingestion**
2. **Audio conditioning and VAD**
3. **Synthetic / cloned speech detection**
4. **Streaming scoring**
5. **Simple risk policy**
6. **Final verdict**

Two high-value additions can be implemented after the core pipeline works:

7. **Speaker verification**
8. **Ed25519 verdict signing**

Everything else should be prioritized only after the detector, streaming pipeline and evaluation suite are stable.

The engineering objective should therefore shift from:

> "Implement every module in the production architecture."

to:

> **"Demonstrate the strongest possible real-time voice-clone detection pipeline, prove that it survives realistic audio degradation, and show a credible path from the SIH prototype to the larger production architecture."**

That is the recommended scope and technology direction for the SIH implementation.
