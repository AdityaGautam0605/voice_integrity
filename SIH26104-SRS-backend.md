# Voice Integrity Verification Framework
## Software Requirements Specification — Backend Scope

| | |
|---|---|
| **Problem statement** | SIH 26104 — real-time voice clone / synthetic speech detection |
| **Revision** | 1.0 |
| **Scope** | Server and edge backend. Frontend excluded |
| **Modules** | 15 |
| **Requirements** | 78 |
| **Keyword convention** | RFC 2119 |
| **Web version** | https://claude.ai/code/artifact/186d5181-21f4-4b04-9ee6-ec64846cd168 |

---

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [System context](#2-system-context)
3. [Module inventory](#3-module-inventory)
4. [Module specifications](#4-module-specifications)
5. [Input features](#5-input-features)
6. [Output contracts](#6-output-contracts)
7. [Functional requirements](#7-functional-requirements)
8. [Non-functional requirements](#8-non-functional-requirements)
9. [Cryptography](#9-cryptography)
10. [Testing and verification](#10-testing-and-verification)
11. [External interfaces](#11-external-interfaces)
12. [Constraints and exclusions](#12-constraints-and-exclusions)

---

# 1. Purpose and scope

This document specifies the backend of a system that observes live voice calls, estimates the probability that the far-end speaker is synthetic or cloned, and emits a signed, auditable verdict that downstream systems consult before permitting a sensitive action.

Requirement keywords follow RFC 2119. **MUST** denotes a requirement whose violation makes the build non-conforming. **SHOULD** denotes a strong recommendation. **MAY** denotes an option.

**In scope:** ingestion adapters, audio conditioning, feature extraction, four detection branches, calibration and fusion, policy evaluation, verdict attestation, the voiceprint vault, key management, service APIs, and the evaluation harness.

**Out of scope for revision 1.0:** operator dashboard, alert delivery channels, mobile application shells, and any human-facing interface. The system exposes contracts; presentation consumes them.

## 1.1 Design position

Three separable problems are addressed by three separable mechanisms, deliberately not merged into one model.

| Question | Mechanism | Fails when |
|---|---|---|
| **Was this waveform manufactured?** | Artifact detection | The synthesizer is unseen, or the codec has erased the evidence |
| **Does it belong to an enrolled identity?** | Speaker verification | No enrollment exists |
| **Is a machine in the conversational loop?** | Conversational liveness | Too few turns, or the operator anticipates |

Each fails differently, which is the reason to keep them apart.

The system is a **passive observer**. It never occupies the media path. Its failure modes degrade the verdict, never the call.

---

# 2. System context

## 2.1 Deployment configurations

| Configuration | Media source | Stack | Concurrency |
|---|---|---|---|
| **T1 — On-premise GPU** | WebRTC, SIPREC | Full, fp16 | 50–80 calls / T4-class GPU |
| **T2 — CPU server** | WebRTC, SIPREC | SSL int8, 2 s hop | 2–5 calls |
| **T3 — Workstation** | WebRTC | SSL int8, 2 s hop | 1 call |
| **T4 — Edge / on-device** | In-app audio | Standalone detector, no SSL front end | 1 call |

> **Cascade.** T4 and T1 compose. The edge tier runs a high-recall lightweight detector that clears the large majority of calls locally; only ambiguous calls escalate to the full stack. Audio for cleared calls never leaves the device, and server load falls by roughly the clearance rate.

## 2.2 Actors and external systems

| Actor | Interaction |
|---|---|
| Media source | Supplies bidirectional audio and transport statistics |
| Consuming business system | Requests a verdict, verifies its signature, gates an action |
| Enrollment administrator | Registers protected identities; never reads stored templates |
| Key custodian | Holds KMS/HSM authority; must not hold database access |
| Auditor | Reads the attestation log; cannot read audio, features, or templates |

---

# 3. Module inventory

```
 ┌──────────────┐   pcm16k  ┌──────────────┐  frames  ┌──────────────┐  window  ┌──────────────┐
 │ M1  Ingest   │──────────►│ M2  Session  │─────────►│ M3 Condition │─────────►│ M4  Features │
 │   adapters   │           │   manager    │          │              │          │  (SSL, 300M) │
 └──────────────┘           └──────┬───────┘          └──────────────┘          └──────┬───────┘
                                   │ timestamps only                                   │
                                   │ (bypasses M3/M4)                                  │
        ┌──────────────┬───────────┼──────────────┬──────────────────────────────┬─────┘
        ▼              ▼           ▼              ▼                              ▼
 ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
 │  M5.A      │ │  M5.B      │ │  M5.C      │ │  M5.D      │
 │  Spoof     │ │  Speaker   │ │  Prosody   │ │  Liveness  │
 │  PRIMARY   │ │  needs     │ │  weight-   │ │  no audio  │
 │            │ │  enrollment│ │  capped    │ │            │
 └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
       └──────────────┴──────────────┴──────────────┘
                      │ calibrated LLRs
                      ▼
              ┌──────────────┐  risk  ┌──────────────┐  band  ┌──────────────┐  signed
              │ M6  Fusion   │───────►│ M7  Policy   │───────►│ M8 Attestation│─────────►
              │  & scoring   │        │   engine     │        │              │  verdict
              └──────────────┘        └──────────────┘        └──────────────┘

 SUPPORTING (provide, not dataflow)
 ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
 │ M12 Model    │  │ M9 Voiceprint│  │ M10 Key      │  │ M13 Observ-  │
 │   registry   │  │    vault     │  │  management  │  │   ability    │
 └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────────────┘
        └─► M4, M5        └─► M5.B          └─► M8, M9
```

Two exceptions to the main spine are load-bearing: **liveness bypasses feature extraction entirely** and receives only timestamps, and **attestation is the sole module producing an externally consumable artifact**.

| ID | Module | Purpose | In | Out |
|---|---|---|---|---|
| **M1** | Ingest adapters | Normalises every media source to one internal representation. Adapter-per-transport behind a single interface | WebRTC, SIPREC, Twilio, file | `(side, float32[], rtp_ts)` |
| **M2** | Session manager | One isolated session per call. Owns the bounded ring buffer, guarantees teardown on every exit path | `call_id`, metadata | Lifecycle events |
| **M3** | Audio conditioning | Resample, VAD-gate, assemble windows. Hosts the training-time codec augmentation chain | `float32[]` @ 16 kHz | `float32[64600]` |
| **M4** | Feature extraction | Self-supervised front end. Frozen with disk caching in development; partially released for the final run | `float32[64600]` | `float32[T, 1024]` |
| **M5.A** | Spoof detector | Graph-attention head over SSL features. Primary signal, language-agnostic by construction | `float32[T, 1024]` | scalar logit |
| **M5.B** | Speaker verification | Embeds the window, compares against enrolled template. The only branch catching a skilled human impersonator | window, `speaker_id` | cosine ∈ [−1, 1] |
| **M5.C** | Prosody analyser | Pitch, pause and rate statistics. Language-dependent, therefore weight-capped and never decisive | `float32[64600]` | feature vector |
| **M5.D** | Conversational liveness | Turn-taking analysis across both sides. Detects a machine in the loop by response-gap distribution shape | `(side, event, ts)[]` | asymmetry LLR |
| **M6** | Fusion & scoring | Calibrates each branch to an LLR, accumulates by summation, applies the metadata prior | branch outputs | risk, ci, band |
| **M7** | Policy engine | Maps risk to bands and actions under signed configuration, tiered by transaction value | risk, context | action directive |
| **M8** | Verdict attestation | Signs the final verdict, appends to a hash-chained log with signed Merkle checkpoints | verdict payload | signed verdict, log entry |
| **M9** | Voiceprint vault | Enrolment, cancelable transform, encrypted storage and matching of biometric templates | embedding, `speaker_id` | protected template |
| **M10** | Key management | KMS/HSM interface. Envelope encryption, versioned keys, rotation without re-enrolment | `key_id`, version | wrapped DEK |
| **M11** | API gateway | Transport security, client authentication, rate limiting, request routing | REST, WebSocket | authorised requests |
| **M12** | Model registry | Signed model and configuration artifacts, version pinning, signature verification at load | artifact + signature | verified weights |
| **M13** | Observability | Metrics, health probes, structured logs. Emits no personal data by construction | internal counters | metrics, health |
| **M14** | Evaluation harness | Development-time only. Computes the metrics of record across all evaluation conditions | scores, labels | EER, TPR@FPR, DET |
| **M15** | Cascade controller | Edge tier only. Runs the lightweight detector locally, decides whether a call escalates | local score | escalate \| clear |

---

# 4. Module specifications

## 4.1 Fixed parameters

Not tunable at runtime. The window length in particular is fixed by the training crop of the detection head; a mismatch between training and inference geometry silently degrades accuracy without raising an error, so the system treats disagreement as a startup failure.

| Parameter | Value | Rationale |
|---|---|---|
| Internal sample rate | `16 000 Hz` | Front-end pretraining rate |
| Sample format | `float32 ∈ [−1, 1]` | Mono, single channel |
| Analysis window | `64 600 samples (4.04 s)` | Equals the detection head's training crop |
| Default hop | `16 000 samples (1 s)` | Configurable to 2 s on CPU tiers |
| VAD frame | `512 samples` | Gate operates on speech only |
| Speaker embedding | `192 dimensions` | Compared by cosine similarity |
| Per-window LLR clamp | `±4.0` | Bounds single-window influence |

## 4.2 Conditioning chain, training and inference

The codec augmentation applied during training exists to make the training distribution match the deployment distribution. Telephony discards the frequency band where synthesis artifacts are most concentrated, so a detector trained on clean audio measures evidence that will not survive to inference.

```
# Training-time degradation chain. Applied stochastically per utterance.
G.711 μ-law   8 kHz               # landline
AMR-NB        8 kHz, 12.2 kbit/s  # mobile
Opus          16 kHz, 16 kbit/s   # VoIP
              ↓
resample to 16 kHz · drop random 20–60 ms spans · additive noise
```

## 4.3 Fusion arithmetic

Platt scaling converts a raw logit into a calibrated log-likelihood ratio through an affine transform. Calibration is therefore not a cosmetic step but the operation that makes branch outputs commensurable and additive. Uncalibrated scores have no common unit and must never be summed.

```
llr_branch   = a · logit + b               # Platt, fitted on dev split only
running_llr += clamp(llr_branch, −4, +4)   # evidence accumulates by SUM
posterior    = running_llr + prior_llr     # metadata enters as log-odds
risk         = 100 · σ(posterior / 4)
ci           = 100 / √n_windows            # tightens as evidence arrives
```

> **Why sum, not average.** Averaging per-window scores discards everything the passage of time provides: ten windows each weakly indicating synthesis remain weak under a mean and become strong under a sum. The confidence interval narrowing over a call is a direct consequence of this choice, and is the behaviour the operator interface is built to display.

## 4.4 Conversational liveness

M5.D measures the **shape** of the response-gap distribution, never its mean. A degraded network shifts all gaps uniformly; it does not impose a hard floor, remove overlaps, or compress variance. Absolute latency thresholds are therefore prohibited — they would be indistinguishable from poor connectivity.

Comparison is **within-call**: the agent side is a known human and serves as the reference. This cancels language, task, register, network and speaker-pair effects in a single operation, and is what prevents this branch from repeating the language-dependence problem that constrains M5.C.

| Feature | Signal | Confound resistance |
|---|---|---|
| Response floor differential | A buffered pipeline cannot emit faster than its buffer; humans routinely respond in under 100 ms | Network delay shifts, never truncates |
| Overlap rate | Half-duplex conversion cannot produce correct overlap | Latency cannot manufacture a negative gap |
| Variance ratio | Constant pipeline offset compresses relative variance | Ratio, not absolute |
| Entrainment divergence | Humans converge on shared rhythm; a machine cannot | Within-call, self-referencing |

---

# 5. Input features

## 5.1 System inputs

| Input | Type | Required | Source |
|---|---|---|---|
| Far-end audio stream | `float32[] @ 16 kHz mono` | **MUST** | M1 |
| Near-end audio stream | `float32[] @ 16 kHz mono` | **MUST** | M1 — required by M5.D |
| RTP arrival timestamps | `int64 ns, monotonic` | **MUST** | M1 |
| Transport round-trip time | `float ms` | **MUST** | RTCP / getStats |
| Call identifier | `uuid` | **MUST** | Caller |
| Origin descriptor | `hashed number, direction` | **SHOULD** | Caller |
| Transaction context | `value, type` | **SHOULD** | Business system |
| Enrolled speaker reference | `speaker_id` | **MAY** | M9 — enables M5.B |

## 5.2 Model-level features by branch

| Branch | Feature | Shape | Language-dependent |
|---|---|---|---|
| M5.A spoof | SSL hidden states | `[T ≈ 201, 1024]` | No — artifact physics |
| M5.B speaker | ECAPA embedding | `[192]` | No |
| M5.C prosody | F0 contour, pause distribution, speaking rate, jitter, shimmer | `[~12]` | **Yes** — hence capped |
| M5.D liveness | `(side, event, monotonic_ns)` tuples | variable | Cancelled by within-call reference |

> **Feature sensitivity.** SSL representations and speaker embeddings are **partially invertible** — published work reconstructs intelligible speech from both. "We store features, not audio" is therefore not by itself a privacy guarantee, and is the reason §9 requires template protection above and beyond encryption at rest.

## 5.3 Metadata prior

Contextual signals enter fusion as a log-odds offset applied before the first acoustic window is scored. This makes an early, weak acoustic observation actionable in a high-risk context without lowering the threshold globally.

| Signal | Direction |
|---|---|
| First contact from this origin | Raises prior |
| Outside business hours | Raises prior |
| Transaction value above tier threshold | Raises prior |
| Origin on hashed local allowlist | Lowers prior |
| Prior verified calls from origin | Lowers prior |

---

# 6. Output contracts

## 6.1 Streaming score frame

Emitted at the hop rate for the duration of the call. **Advisory only; no action may be taken on a score frame.**

```json
{
  "call_id":  "uuid",
  "seq":      0,                    // monotonic, gap-free
  "risk":     0.0,                  // 0-100
  "ci":       0.0,                  // interval half-width, narrows over the call
  "band":     "green|amber|red",
  "branches": { "spoof": 0.0, "speaker": null,
                "prosody": 0.0, "liveness": null },
  "speech_s": 0.0,                  // accumulated SPEECH, not wall clock
  "model_version": "string"
}
```

## 6.2 Final signed verdict

The only output on which an action may be taken. Emitted once, at teardown.

```json
{
  "payload": {
    "call_id":        "uuid",
    "risk":           0.0,
    "band":           "green|amber|red",
    "liveness":       { "floor_delta_ms": 0.0, "overlap_rate": 0.0,
                        "n_transitions": 0 },
    "speech_s":       0.0,
    "model_version":  "string",     // binds the decision to a specific model
    "policy_version": "string",
    "ts":             0,            // unix ms
    "nonce":          "hex"         // replay protection
  },
  "alg": "Ed25519",
  "key_id": "string",
  "signature": "base64"
}
```

> ⚠️ **Fail closed.** A missing, malformed or unverifiable verdict **MUST** be treated by the consumer as elevated risk, never as absence of risk. A system that fails open can be disabled by severing a network path, which makes the cheapest attack on the whole design a cut cable rather than a voice clone.

## 6.3 Audit log entry

Append-only. Contains no audio, no features, no embeddings and no transcript.

```json
{
  "seq": 0, "prev_hash": "hex", "entry_hash": "hex",
  "verdict": { "...as above, signature included..." },
  "checkpoint": { "merkle_root": "hex", "signature": "base64" }  // periodic
}
```

---

# 7. Functional requirements

## 7.1 Ingest

| ID | Requirement | Verified by |
|---|---|---|
| **FR-IN-01** | The system **MUST** normalise all media sources to 16 kHz mono float32 regardless of transport codec or rate | TS-2 |
| **FR-IN-02** | The system **MUST** ingest both call directions as separately labelled streams | TS-2 |
| **FR-IN-03** | The system **MUST** upsample 8 kHz narrowband telephony to the internal rate rather than reject it | TS-2 |
| **FR-IN-04** | The system **MUST** operate outside the media path. Detector failure, stall or restart **MUST NOT** degrade call audio | TS-8 |
| **FR-IN-05** | Timestamps **MUST** be taken at RTP arrival, not at playout, to exclude adaptive jitter-buffer delay | TS-6 |
| **FR-IN-06** | The system **MUST** obtain transport RTT from RTCP or equivalent, and **MUST NOT** estimate it acoustically | TS-6 |
| **FR-IN-07** | Adapters for SIPREC and hosted media-stream APIs **SHOULD** implement the same interface as the WebRTC adapter | TS-1 |
| **FR-IN-08** | Track termination or peer loss **MUST** be handled without process failure or session leakage | TS-2 |

## 7.2 Session and conditioning

| ID | Requirement | Verified by |
|---|---|---|
| **FR-SE-01** | Each call **MUST** occupy an isolated session; no state may cross between concurrent sessions | TS-2 |
| **FR-SE-02** | Audio **MUST** reside only in a bounded in-memory ring buffer | TS-10 |
| **FR-SE-03** | The system **MUST NOT** write audio to persistent storage under any configuration, including on error paths | TS-10 |
| **FR-SE-04** | Teardown **MUST** execute on every exit path, including abnormal termination and dropped transport | TS-2 |
| **FR-SE-05** | Audio buffers **MUST** be overwritten at teardown on a best-effort basis before release | TS-10 |
| **FR-CO-01** | Only speech-classified audio **MUST** enter the analysis buffer; silence **MUST NOT** dilute the score | TS-4 |
| **FR-CO-02** | Analysis windows **MUST** contain exactly 64 600 samples | TS-4 |
| **FR-CO-03** | Window length **MUST** equal the head's training crop. Disagreement **MUST** raise a startup error, not a warning | TS-1 |
| **FR-CO-04** | The training pipeline **MUST** apply the codec degradation chain of §4.2 | TS-3 |
| **FR-CO-05** | Echo cancellation, noise suppression and automatic gain control **MUST** be disabled on analysed audio | TS-4 |

## 7.3 Detection and fusion

| ID | Requirement | Verified by |
|---|---|---|
| **FR-DE-01** | Front-end weights **MUST** be byte-identical between training and inference. Version mismatch **MUST** prevent startup | TS-1 |
| **FR-DE-02** | Model inference **MUST** execute off the asynchronous event loop so that ingestion and timestamping are never stalled | TS-8 |
| **FR-DE-03** | M5.B **MUST** be skipped, not defaulted, when no enrolment exists for the claimed identity | TS-1 |
| **FR-DE-04** | M5.C **MUST NOT** be able to change the policy band on its own; its fusion weight is capped by configuration | TS-5 |
| **FR-DE-05** | M5.D **MUST** retain only timestamp tuples and **MUST NOT** require access to audio content | TS-10 |
| **FR-DE-06** | M5.D **MUST** compute within-call asymmetry against the near-end reference and **MUST NOT** apply absolute latency thresholds | TS-6 |
| **FR-FU-01** | Every branch output **MUST** be calibrated to a log-likelihood ratio before fusion | TS-5 |
| **FR-FU-02** | Calibration **MUST** be fitted on the development split only. Contamination with evaluation data **MUST** fail the build | TS-3 |
| **FR-FU-03** | Evidence **MUST** accumulate by summation of calibrated LLRs, never by averaging | TS-5 |
| **FR-FU-04** | Each window's LLR contribution **MUST** be clamped to bound single-window influence | TS-5 |
| **FR-FU-05** | Contextual metadata **MUST** enter as a log-odds prior, not as a post-hoc multiplier | TS-5 |
| **FR-FU-06** | Score frames **MUST** report a confidence interval and accumulated speech seconds distinct from wall-clock time | TS-4 |

## 7.4 Policy, verdict and enrolment

| ID | Requirement | Verified by |
|---|---|---|
| **FR-PO-01** | Thresholds **MUST** be tierable by transaction value; a single global operating point is non-conforming | TS-1 |
| **FR-PO-02** | The system **MUST** gate the sensitive action and **MUST NOT** terminate or degrade the call | TS-2 |
| **FR-PO-03** | Operating points **MUST** be expressed as detection rate at a bounded false-positive rate, not as accuracy | TS-3 |
| **FR-PO-04** | Policy configuration **MUST** be signed and verified at load; unsigned configuration **MUST** be rejected | TS-9 |
| **FR-VE-01** | The final verdict **MUST** be signed with Ed25519 over a canonical encoding of the payload | TS-9 |
| **FR-VE-02** | The signed payload **MUST** include model version, policy version, timestamp and nonce | TS-9 |
| **FR-VE-03** | Consumers **MUST** verify the signature before acting, and **MUST** treat failure as elevated risk | TS-9 |
| **FR-VE-04** | Every verdict **MUST** append to a hash-chained log with periodic signed Merkle checkpoints | TS-9 |
| **FR-VE-05** | Log entries **MUST NOT** contain audio, features, embeddings or transcript | TS-10 |
| **FR-VA-01** | Embeddings **MUST** pass through a revocable, non-invertible transform before storage | TS-9 |
| **FR-VA-02** | Protected templates **MUST** be stored under AES-256-GCM envelope encryption | TS-9 |
| **FR-VA-03** | Revocation **MUST** be possible without requiring the subject to re-enrol from scratch where a new transform seed suffices | TS-9 |

---

# 8. Non-functional requirements

| ID | Requirement | Target | Priority |
|---|---|---|---|
| **NFR-01** | p95 per-window inference latency below the hop duration on the target tier | `< 1000 ms @ 1 s hop` | P1 |
| **NFR-02** | Concurrent sessions per T1 node | `≥ 50` | P1 |
| **NFR-03** | Accumulated speech before the first score frame | `4.04 s` | P1 |
| **NFR-04** | Accumulated speech before a policy-actionable verdict | `≤ 15 s` | P2 |
| **NFR-05** | Audio bytes at rest, verified by filesystem audit during a load run | `0` | P1 |
| **NFR-06** | Session teardown latency | `< 100 ms` | P2 |
| **NFR-07** | Per-session memory, excluding shared model weights | `≤ 1 MB` | P2 |
| **NFR-08** | Cold start, including model signature verification | `< 60 s` | P3 |
| **NFR-09** | Score-frame delivery jitter | `< 200 ms` | P2 |
| **NFR-10** | Edge-tier model artifact size | `≤ 5 MB` | P2 |
| **NFR-11** | Edge configuration operates with no network connectivity | `fully offline` | P1 |
| **NFR-12** | Backpressure policy: stale windows are dropped, never queued | `bounded lag` | P1 |

> **NFR-12 in particular.** If inference is slower than the hop, unbounded queueing causes the reported score to fall progressively further behind the live call while appearing healthy. The system must drop stale windows and surface the drop rate as a metric rather than silently accumulate lag.

---

# 9. Cryptography

## 9.1 Trust boundaries

```
┌─────────────┐          ┌──────────────────────┐        ┌─────────────┐
│  UNTRUSTED  │ DTLS-SRTP│   TRUSTED COMPUTE    │  wrap  │ KEY CUSTODY │
│             │  mTLS    │                      │───────►│   KMS/HSM   │
│ far-end     │─────────►│ plaintext audio      │        │ keys never  │
│ caller      │          │ memory only          │        │ leave       │
│ public net  │          │ ring buffer, zeroed  │        │ SEPARATE    │
└─────────────┘          │ no swap, no dumps    │        │ OPERATOR    │
                         │ ──────────────────── │        └─────────────┘
                         │ models · session ·   │
                         │ fusion               │  template┌─────────────┐
                         │ optionally in a TEE  │─────────►│  TEMPLATE   │
                         │ host operator excl.  │          │   STORE     │
                         └──────────┬───────────┘          │ cancelable  │
                                    │                      │ + AES-GCM   │
                                    │ Ed25519-signed       └─────────────┘
                                    │ verdict
                                    ▼
                         ┌──────────────────────┐
                         │      CONSUMER        │
                         │  banking system      │
                         │  VERIFIES SIGNATURE  │
                         │  BEFORE ACTING       │
                         │  fails closed        │
                         └──────────────────────┘
```

The only asset that leaves trusted compute in a form anyone can act on is a signed verdict. **Audio never crosses a boundary. Templates cross only in protected form. Keys never cross at all.**

Separation of duties is structural: the operator who can read the template store must not hold authority in key custody, or the encryption reduces to compliance paperwork.

## 9.2 Requirements

| ID | Requirement | Primitive |
|---|---|---|
| **SEC-01** | Final verdicts **MUST** be signed; consumers **MUST** verify before acting and fail closed | Ed25519 |
| **SEC-02** | Signed payloads **MUST** carry a nonce and timestamp sufficient to reject replay | — |
| **SEC-03** | All transport **MUST** use TLS 1.3; service-to-service **MUST** be mutually authenticated | TLS 1.3, mTLS |
| **SEC-04** | Stored templates **MUST** use authenticated encryption with a unique nonce per record | AES-256-GCM |
| **SEC-05** | Associated data **MUST** bind ciphertext to record, tenant and key version, preventing record substitution | GCM AAD |
| **SEC-06** | Data keys **MUST** be wrapped by a key-encrypting key held in KMS or HSM | Envelope |
| **SEC-07** | Key material **MUST NOT** appear in source, images, environment variables or logs | — |
| **SEC-08** | Embeddings **MUST** undergo a revocable, non-invertible transform before storage. Encryption alone is insufficient for irrevocable biometrics | Cancelable transform |
| **SEC-09** | Templates for one subject across tenants **SHOULD** be unlinkable | Per-tenant seed |
| **SEC-10** | Key rotation **MUST** be possible without re-encrypting the full store or re-enrolling subjects | Versioned KEK |
| **SEC-11** | Database read authority and key read authority **MUST** be held by different principals | IAM separation |
| **SEC-12** | The audit log **MUST** be tamper-evident through hash chaining | SHA-256 chain |
| **SEC-13** | Log checkpoints **MUST** be signed so the log is non-repudiable, not merely self-consistent | Merkle + Ed25519 |
| **SEC-14** | Checkpoint roots **SHOULD** be anchored externally to constrain an insider holding the signing key | External anchor |
| **SEC-15** | Model and configuration artifacts **MUST** be signed and verified at load; mismatch **MUST** prevent startup | Ed25519 / Sigstore |
| **SEC-16** | API clients **MUST** present sender-constrained credentials; bare bearer tokens are non-conforming | mTLS-bound, DPoP |
| **SEC-17** | Mobile deployments **MUST** hold key material in hardware-backed storage | Keystore, Secure Enclave |
| **SEC-18** | Process memory holding audio **MUST NOT** be swappable, and core dumps **MUST** be disabled | mlock |
| **SEC-19** | Inference on untrusted infrastructure **SHOULD** execute inside a hardware-attested enclave | TEE |
| **SEC-20** | Template comparison **MAY** be performed under homomorphic encryption where the operator must never observe plaintext embeddings | CKKS |
| **SEC-21** | Model improvement from production traffic **SHOULD** use federated updates with secure aggregation and a formal privacy budget | DP-SGD |

> **Highest value per line of code.** SEC-01 through SEC-03 defend the part of this system an attacker would actually target. The verdict is a number that authorises money movement; unsigned, it is forgeable by anyone on the network path, and no amount of model accuracy compensates. Implementation is a few dozen lines.

## 9.3 Implementation note on memory

SEC-18 is only partially achievable in a managed runtime. Python cannot guarantee memory zeroing — strings are immutable, the collector relocates objects, and freed pages carry no erasure guarantee. Mutable buffers overwritten explicitly are a best-effort measure; a genuine guarantee requires a native extension. The specification records this as a known limitation rather than an implemented control.

---

# 10. Testing and verification

| Suite | Scope | Acceptance criterion |
|---|---|---|
| **TS-1** | **Unit** — per-module contracts, boundary conditions, configuration validation | Full pass; startup guards provably raise on mismatch |
| **TS-2** | **Integration** — synthetic streams end to end, session lifecycle, abnormal termination, concurrent isolation | No leaked sessions; teardown on every injected failure path |
| **TS-3** | **Model evaluation** — the metrics of record across all conditions | See §10.1 |
| **TS-4** | **Streaming correctness** — window alignment, VAD gating, frame-drop accounting under load | Zero misaligned windows; speech accounting matches ground truth |
| **TS-5** | **Calibration** — reliability diagrams and expected calibration error per branch | ECE below threshold; monotone reliability |
| **TS-6** | **Liveness** — collected corpus, shape features, robustness to injected network delay | Shape features hold where a mean-latency threshold fails |
| **TS-7** | **Adversarial** — unseen synthesizers, unseen codec chains, re-recording, anticipation counter-measure | Degradation characterised and reported, not concealed |
| **TS-8** | **Performance** — latency percentiles, concurrency ceiling, memory, backpressure behaviour | NFR-01, 02, 06, 07, 09, 12 met on target tier |
| **TS-9** | **Security** — signature verification, negative tampering cases, key rotation, fail-closed behaviour | Every negative case rejected; no silent acceptance |
| **TS-10** | **Privacy** — filesystem audit during load, log content inspection, buffer-zeroing verification | Zero audio bytes written; no personal data in any log |

## 10.1 Model evaluation protocol

Three evaluation conditions, all three reported. The in-domain figure alone is misleading and **MUST NOT** be presented without its companions.

| Condition | Purpose | Expectation |
|---|---|---|
| **In-domain** — held-out eval from the training corpus | Comparability with published work | Strong. Never quoted alone |
| **Out-of-domain** — real-world deepfakes, unseen generators and conditions | Honest generalisation estimate | Materially worse. Reported regardless |
| **Codec-degraded** — the §4.2 chain applied to both eval sets | Validates the design thesis | Augmented model holds where the control collapses |
| **Leave-one-attack-out** — one synthesizer withheld from training | Zero-day generalisation | The only figure reflecting real deployment |
| **Multilingual** — matched genuine/spoofed pairs, Indian languages, codec-degraded | Validates language-agnostic claim | Comparable to in-domain if the claim holds |

## 10.2 Metrics of record

| Metric | Definition | Why |
|---|---|---|
| **EER** | Rate at which false accepts equal false rejects | Single comparable figure across systems |
| **TPR@1%FPR** | Detection rate at a bounded false-positive rate | **The operational number.** Rare-event base rates make accuracy meaningless |
| **min t-DCF** | Cost-weighted detection function | Comparability with the anti-spoofing literature |
| **ECE** | Expected calibration error | Fusion is invalid if branches are miscalibrated |

> **Sequencing.** TS-3 and its metric harness are built **before** the first model is trained, and validated by confirming that a random-guess classifier reports an EER near 50%. A harness trusted only after models exist has no independent authority to declare them wrong. The training corpus is heavily imbalanced toward spoofed utterances; accuracy on it will look excellent and mean nothing.

---

# 11. External interfaces

| Endpoint | Protocol | Purpose | Auth |
|---|---|---|---|
| `/v1/analyze` | WebSocket | Session negotiation and streaming score frames | mTLS + token |
| `/v1/verdict/{call_id}` | REST GET | Retrieve the final signed verdict | mTLS + token |
| `/v1/enroll` | REST POST | Register a protected identity from reference audio | mTLS + admin role |
| `/v1/enroll/{id}/revoke` | REST POST | Invalidate stored templates by rotating the transform seed | mTLS + admin role |
| `/v1/policy` | REST PUT | Install signed policy configuration | mTLS + signature |
| `/v1/audit` | REST GET | Read log entries and verify chain inclusion | mTLS + auditor role |
| `/healthz`, `/metrics` | REST GET | Liveness, readiness, operational metrics | Network-restricted |

A gRPC surface **MAY** be generated from the same contracts. It is a transport wrapper and introduces no additional requirements.

---

# 12. Constraints and exclusions

## 12.1 Platform constraints

| Constraint | Consequence |
|---|---|
| Neither major mobile OS permits third-party capture of cellular call audio | Consumer handset deployment requires OEM or carrier partnership. Enterprise and in-app configurations carry no such restriction |
| End-to-end encrypted media and server-side analysis are mutually exclusive | Resolve by analysing at an endpoint, or inside an attested enclave. Both are supported; the choice is explicit |
| The full front end is not viable on mobile hardware in real time | The edge tier runs the standalone detector. The accuracy differential is measured and published, not estimated |
| Managed runtimes cannot guarantee memory erasure | SEC-18 is best-effort without a native extension, and is documented as such |

## 12.2 Known limitations

- **Re-recording.** Audio replayed through a loudspeaker into a microphone smears the artifacts the primary branch detects. Codec augmentation partially mitigates this; it is not a solution.
- **Anticipation.** A skilled operator driving a conversion pipeline can pre-empt turn boundaries and mask the response floor. The residual signature appears as inflated timing variance or as disfluency induced by delayed auditory feedback, and is characterised under TS-7.
- **Skilled human impersonation.** Outside the reach of the primary branch by construction. Detection depends entirely on M5.B and therefore on prior enrolment.
- **Sub-buffer conversion.** A low-latency pipeline imposes a smaller floor and is correspondingly harder to detect. This bounds the achievable performance of M5.D, and the bound is measurable.

## 12.3 Excluded from revision 1.0

Operator dashboards and all human-facing interfaces; alert delivery channels; mobile application shells; carrier-side network integration; and challenge-phrase generation logic, which is specified as a policy output but whose presentation is a frontend concern.

---

*SRS rev 1.0 · backend scope · SIH 26104 voice integrity verification framework*
