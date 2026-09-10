# SIH 26104 — Real-Time Voice Clone Detection

**Working notes and build plan** · rev 2

| | |
|---|---|
| **Problem statement** | AI-driven detection of voice cloning / synthetic speech impersonation during live calls |
| **Target** | Hackathon prototype |
| **Team** | 6 people — mostly ML, some backend, some encryption |
| **Focus** | ML-heavy, thin platform layer |
| **Compute** | Kaggle free tier (30 GPU-hrs/week, P100 or 2×T4) for training. Demo runs offline on a laptop |
| **Architecture page** | https://claude.ai/code/artifact/c8e18343-1995-4cb0-8402-118ba8b6c9a6 |
| **SRS page** | https://claude.ai/code/artifact/186d5181-21f4-4b04-9ee6-ec64846cd168 |
| **Companion docs** | `SIH26104-SRS-backend.md` · `SIH26104-implementation.md` |

> **Changes in rev 2:** Branch D (conversational liveness) added as a fourth detection branch and a new build phase. L0 becomes bidirectional. New sections on deployment shapes and OS permissions, hardware, and cryptography.

---

## Table of contents

1. [The problem, restated](#1-the-problem-restated)
2. [The five traps](#2-the-five-traps)
3. [Trap 4 in depth — conversational liveness](#3-trap-4-in-depth--conversational-liveness)
4. [The Indian-language requirement](#4-the-indian-language-requirement)
5. [Plain-language concept guide](#5-plain-language-concept-guide)
6. [The decisions](#6-the-decisions)
7. [Runtime architecture (L0–L6)](#7-runtime-architecture-l0l6)
8. [Build phases (P0–P7)](#8-build-phases-p0p7)
9. [Deployment shapes and permissions](#9-deployment-shapes-and-permissions)
10. [Hardware](#10-hardware)
11. [Cryptography](#11-cryptography)
12. [The demo script](#12-the-demo-script)
13. [Team split](#13-team-split)
14. [Invariants and prepared answers](#14-invariants-and-prepared-answers)
15. [Appendix](#15-appendix)

---

# 1. The problem, restated

The problem statement collapses several problems into one. They have different models, different training data, and different failure modes. Keeping them separate is more correct and easier to demo.

| Question | Mechanism | Catches | Blind to |
|---|---|---|---|
| **Was this voice manufactured?** | Artifact detection (Branch A) | Any synthetic speech | Skilled human impersonators |
| **Is this the right person?** | Speaker verification (Branch B) | Human impersonators | Anyone not enrolled |
| **Is a machine in the loop?** | Conversational liveness (Branch D) | Pipelines, bots, playback | A genuinely live human |
| **So what do we do?** | Fusion + policy | — | — |

## Problem A — "Is this voice manufactured?"

Every AI voice generator leaves a manufacturing fingerprint. Real speech is *messy* — turbulent airflow, irregular vocal-fold vibration, tiny random imperfections. AI speech is smooth in specific, measurable ways it shouldn't be.

> **Analogy:** a printed photo of a face. From far away it's a face; under a magnifying glass it's a grid of ink dots. We are building the magnifying glass.

Doesn't care *who* is speaking or *what language*. Asks only: were these sound waves produced by a throat or by a computer? **This is the primary signal.**

## Problem B — "Is this the right person?"

Compares the caller's voice against a stored voiceprint. Requires prior enrollment. Catches a **skilled human impersonator**, which Problem A structurally cannot, because a human impersonator's voice is genuinely human.

## Problem C — "Is a machine in the loop?"

Different attack surface entirely. Rather than fingerprinting the *output*, this probes the *pipeline* — see §3. It partially dissolves Trap 2, because you're not recognising a synthesizer, you're detecting that processing is happening at all.

## Problem D — "So what do we do about it?"

Combining everything plus call context into one risk number, and deciding what that number triggers.

---

# 2. The five traps

## Trap 1 — The phone network erases your evidence 🔴

**The biggest issue, and our entire differentiator.**

| | Sample rate | Frequency range |
|---|---|---|
| Normal audio | 16,000 /sec | 0 – 8,000 Hz |
| A phone call | 8,000 /sec | ~300 – 3,400 Hz |

Phone networks throw away more than half the frequency range to save bandwidth, then compress what's left. **The AI fingerprints we look for live mostly in the fine detail and high frequencies — exactly what the network deletes.**

> **Analogy:** you're trying to spot a forged painting by its brushstroke texture, but you're only allowed to look at a fax of a low-res JPEG of it. The forger didn't hide the evidence. The transmission did.

Models scoring ~1% error on clean lab audio jump to **15–25% error** through phone codecs.

If we demo on clean WAV files and claim "works on live phone calls," a knowledgeable judge will destroy us. If we deliberately train on codec-degraded audio and **show the before/after curve**, we're one of maybe two teams who understood the actual problem.

## Trap 2 — It only catches the fakes it has met before

Train on Tool A and Tool B; the attacker uses Tool C, released last month. Miss.

> **Analogy:** an antivirus that only knows viruses already in its database.

**Honest measurement: leave-one-out testing.** Hide one AI voice tool from training, test only on that one. The only number that reflects reality. Everyone quotes their score on familiar attacks; almost nobody quotes this one.

## Trap 3 — False alarms kill the product, not misses

Fraud calls are ~1 in 10,000. A 1% false-alarm rate means: out of 10,000 calls we catch the 1 real fraud **and flag ~100 innocent people.** 99% of alerts are wrong. Staff learn to ignore them within a week.

> This is the **base-rate problem**. Rare events + imperfect detectors = mostly false alarms.

**Never say "our accuracy is 95%."** Say **"at a 1% false-positive rate, we catch X% of fakes."**

## Trap 4 — "Real-time" is partly a fiction

- 2 seconds of speech → close to a coin flip
- 10–15 seconds → trustworthy

By then the fraudster has been talking for 15 seconds and the social engineering is underway. See §3 for the full treatment — this is where most of the design thinking went.

## Trap 5 — The analog hole

Attacker plays the cloned voice out of a laptop speaker into a phone microphone. Room echo plus a second recording smears the fingerprint.

> **Analogy:** photocopying a photocopy. The forgery evidence blurs out along with everything else.

We don't have to solve it. We do have to know it exists.

---

# 3. Trap 4 in depth — conversational liveness

## The gap, stated precisely

Detection confidence is a function of **accumulated speech seconds**. The attacker's leverage is highest in the **first 30 seconds**. Those two curves point in opposite directions.

## Two obvious answers, and why both leak

**"Gate the action, not the call."** Only works if there *is* an action to gate. If the goal is an OTP read aloud or a confirmation of an internal detail, the damage is verbal and there's no approve button anywhere. This quietly assumes every attack terminates in a system transaction. Plenty don't.

**"Challenge-response."** The hole is social. Asking your CFO to repeat *"seventeen–mango–blue"* is expensive. Under pressure from a senior voice, a junior employee skips it — precisely when it was needed. Any countermeasure requiring a subordinate to be rude to a superior during a manufactured emergency has a compliance rate near zero. Same reason manual call-back already fails.

## The solution space

| Family | Move | Buys you |
|---|---|---|
| Delay the decision | Gate the action, not the call | Time — if an action exists |
| Get evidence faster | Active challenge | Seconds — at a social cost |
| Lower the evidence bar | Strong metadata prior + weak likelihood → actionable posterior | Confidence borrowed from context |
| **Signals available instantly** | Room tone, breath, noise floor, silence structure | Speed, lower ceiling |
| Skip detection entirely | Cryptographic caller attestation before connect | Certainty — needs infrastructure |
| Accept and contain | Post-hoc analysis + reversal window | Nothing preventive |
| **Formalise the stopping rule** | SPRT — two thresholds, sample between them | Provably minimal time-to-decision |

The LLR accumulation in L4 is already the machinery of a **Sequential Probability Ratio Test** — the classic answer to "decide as early as the evidence permits." Naming it means the design has a principled stopping rule rather than a hand-tuned one.

## What we chose: covert passive timing

The insight: **probe the channel, not the waveform.** Covert, so no social cost. But the mechanism matters.

### Why we do not inject a signal

An earlier design injected an inaudible marker hidden under the speech. Two problems killed it:

**Topology.** In the main threat model your outbound audio never touches the attacker's synthesis pipeline. A real-time voice converter takes the *attacker's microphone* as input; your audio goes to *their headphones*. A pre-recorded clone ingests nothing at all. The probe has no path to the thing you want to disturb. It only works against an autonomous AI agent, which does ingest your audio.

**Physics.** To be inaudible the signal must be low-energy — and low-energy fine detail is exactly what the codec deletes. **Our own Trap 1 thesis eats the probe.** Modern acoustic echo cancellers would also actively suppress it, since they use the received signal as their reference.

> **The most covert probe is no probe.** We already know when our side stopped speaking — it's our own outbound stream. We detect when their speech starts with the VAD already at L1. The gap is measurable with **zero injection**: nothing to be inaudible, nothing for the codec to destroy, nothing to detect or adapt to, no legal grey area.

### The signal is the shape, not the mean

"VC adds 300 ms, flag slow responses" dies immediately — a bad mobile connection also adds 300 ms. Network RTT is a fatal confound for absolute latency.

| Feature | Why it works |
|---|---|
| **Truncated left tail** | A streaming pipeline has a fixed buffer and structurally cannot respond faster than it. Humans routinely respond under 100 ms — backchannels, anticipatory completions, reflexive answers. A machine imposes a hard floor conversation doesn't have |
| **Overlap rate** | Real conversation overlaps at ~10–20% of turn transitions. A half-duplex loop cannot overlap correctly. **Negative gaps are the cleanest signal in the scheme**, because latency cannot manufacture one |
| **Variance structure** | Human latency is wide and content-dependent. A pipeline adds a roughly constant offset, compressing relative variance |

Network RTT shifts the whole distribution uniformly. It does **not** create a hard floor, remove overlaps, or compress variance. Shape survives the confound; absolute latency doesn't.

### Within-call asymmetry — the key design decision

Absolute thresholds need a reference distribution for "normal human turn-taking." **But we have a known human in every call** — our own agent. Measure both sides and compare them *to each other*.

This cancels, in one move: network conditions, task type, urgency, speaker pair, conversational register, **and language**.

**Why language matters here:** turn-taking is language-dependent, the same trap that constrains prosody. Stivers et al. (PNAS 2009) found the *universal* is avoidance of overlap and minimisation of gap — but actual numbers vary widely across languages, roughly from near-zero modal gaps in Japanese to several hundred milliseconds in Danish. That range is wide enough to swallow the entire effect. There is no data at all for Hindi, Tamil, Bengali or Indian English telephone registers.

The asymmetry framing is what rescues the branch. It makes the measurement self-calibrating per call, per language, per speaker pair.

It also exploits **entrainment**: two humans converge on a shared rhythm within a minute or two. The null hypothesis becomes *"these two sides should be converging"* rather than *"this side should match a global constant."* A machine cannot entrain, because its floor is fixed by a buffer.

### Measure at the RTP layer, not at playout

The detail most likely to silently ruin the numbers.

Timestamping at browser playout measures their processing + network + **your jitter buffer**. WebRTC's jitter buffer is adaptive, so your measurement drifts with network conditions — exactly the confound you were eliminating.

Two more traps: **DTX** (Opus stops transmitting during silence and sends comfort noise — packet resumption may be a better onset marker than VAD, but only if you know whether it's on, and it may differ per side), and **noise suppression / AGC** on the far end gating quiet onsets.

**Terminate WebRTC server-side in Python with `aiortc`.** RTP timestamps and decoded frames directly, in the same process as L0. Browser-side encoded-frame access exists but support is uneven.

Take network RTT from the transport layer — `RTCStatsReport.roundTripTime`, or RTCP receiver reports. Exact, free, no DSP.

### Define "turn boundary" operationally

You cannot distinguish "turn ended" from "paused mid-sentence" without semantics. So don't try — define something computable and defend it:

> A **speaker-change event** occurs when side A's speech ends, side B's speech begins within a window, and A does not resume within *T* ms.

Then tune *T*, minimum utterance duration and backchannel filtering, and **report those parameters** — every number is conditional on them. "How do you define a turn?" is a sharp question, and a written operational definition is the difference between a rigorous answer and a shrug.

### Honest power calculation — do this before committing

If humans produce a sub-150 ms response at rate *p*, observing none after *n* transitions has probability (1−*p*)ⁿ under the human hypothesis. At *p* ≈ 0.15: ten transitions gives ~0.20 (suggestive, not conclusive), twenty gives ~0.04.

**So timing evidence alone plausibly needs 10–20 turns — a minute or two.** Don't let this get pitched as a fast detector.

Two places it *is* fast, and these are the ones to demo:

- **The first transition**, 2–3 seconds in, if the offset is large against a measured RTT.
- **Pre-recorded playback**, which has no turn-taking behaviour at all and collapses on turn one. The acoustic branch would have needed fifteen seconds for that same call.

The real gain is that it accumulates on a **different clock** — per turn transition rather than per second of speech. Two independent streams reaching threshold in parallel get there sooner than either alone, and they fail independently.

### The attacker's counter, and why it may backfire

A human operator **anticipates** — hears you with their own ears, starts speaking 300 ms before you finish, pipeline delay lands their output at your turn boundary. Apparent gap: zero. Assume a competent attacker does this.

But it costs them. To monitor what they're transmitting, they must listen to their own converted, delayed voice — and **delayed auditory feedback is famously disruptive to fluent speech**. A 100–200 ms delay on your own voice induces stuttering and halting delivery; it's the operating principle behind speech-jammer devices. So:

- **Monitor the output** → their speech becomes disfluent
- **Don't monitor** → they can't calibrate anticipation, and timing scatters

Either way it shows in the statistics. That's a testable prediction and a good line in the write-up: *"we anticipated the counter-measure and here's the residual signature it leaves."*

### Collect the validation data yourselves

No public dataset exists, and you don't need one:

```
Two laptops, a call between them, ~30 conversations per condition:
  A: human ↔ human                        (control)
  B: human ↔ human + streaming VC in loop
  C: human ↔ pre-recorded playback
Log every turn boundary. Add synthetic network delay to both, to prove the
shape signal survives what a naive mean-latency threshold does not.
```

A day of work for six people, and a real result nobody else has. That last line is the experiment that proves the whole design.

### The privacy bonus

**Branch D's entire feature set is timestamps.** No audio, no embeddings, no spectral content. The most privacy-preserving signal in the system — and it means Branch D can run where the acoustic branch legally cannot. A telecom operator barred from touching call content can still run it.

### Open questions

1. **The reference-side assumption.** Asymmetry needs our agent to be a known human. Fine for bank-agent calls; breaks for consumer-to-consumer.
2. **The floor is buffer-dependent.** A low-latency VC has a smaller floor. Measure what real streaming VC tools actually impose — one afternoon, and it sets the detection ceiling.
3. **Does entrainment show up in 90 seconds?** Convergence is documented over minutes. If not, the branch leans entirely on floor and overlap.

---

# 4. The Indian-language requirement

An asymmetry most teams miss:

| Signal | Language-dependent? | Why |
|---|---|---|
| **Synthesis artifacts** (Branch A) | **No** | Physics — how the waveform was manufactured. A vocoder leaves the same fingerprint in Tamil as in English |
| **Prosody** (Branch C) | **Very much yes** | Hindi, Tamil, Bengali and English have completely different rhythm and intonation |
| **Turn-taking** (Branch D) | **Yes, but cancelled** | By the within-call asymmetry design of §3 |

A prosody model trained on English flags a natural Tamil speaker as abnormal. A false-alarm factory.

**Our design position:** artifact detection is primary *precisely because* it's language-agnostic. Prosody is secondary and weight-capped. Liveness handles its language dependence structurally rather than by tuning.

## Building the dataset ourselves

```
Real:  AI4Bharat Kathbath / IndicSUPERB      → real speakers, ~12 Indian languages
Fake:  clone those same speakers with
       XTTS-v2 · OpenVoice v2 · F5-TTS · AI4Bharat Indic Parler-TTS
Both:  → ffmpeg codec simulation             → matched real/fake pairs, phone conditions
```

**~500 clips is plenty.** Not a research corpus — evidence for one slide.

---

# 5. Plain-language concept guide

*For explaining to teammates.*

**VAD (Voice Activity Detection)** — a cheap filter that says "someone is talking now." Don't waste compute on silence, and don't let silence dilute the score.

**Sliding window** — we can't wait for the call to end. Look at the most recent 4 seconds of speech, redo it every 1 second. A continuous rolling verdict.

**wav2vec2-XLS-R (the front-end)** — a large pre-trained model converting raw audio into rich numerical descriptions. A **pre-trained ear**. Trained on 128 languages, so it handles Indian languages without special effort. We download it.

**AASIST (the back-end)** — the small classifier on top of that ear making the real-vs-fake call. **The part we train.** 297K parameters.

**ECAPA-TDNN (voiceprint)** — turns a voice clip into a fingerprint of ~192 numbers, an **embedding**. Compare with **cosine similarity** (−1 to 1, higher = more alike).

**Calibration** — a model outputting "0.87" does **not** mean 87% probability. Raw outputs are systematically wrong about their own confidence. Calibration makes the number mean what it claims. **Required before combining models or setting thresholds** — otherwise you're adding apples to oranges.

**LLR, and why we add instead of average** — express each window as *"this evidence is 4× more likely if fake than if real."* You can add these up.

> Ten witnesses each 60% sure. **Average** → 60%, no better than one witness. **Accumulate** → near-certainty. Averaging window scores throws away everything time gives you.

**SPRT** — Sequential Probability Ratio Test. The formal name for "keep sampling until the evidence crosses one of two thresholds, then stop." Our LLR accumulation is its machinery.

**Entrainment** — two people in conversation converge on a shared rhythm. Machines can't. Branch D exploits this.

**DAF (Delayed Auditory Feedback)** — hearing your own voice delayed by 100–200 ms disrupts fluent speech and induces stuttering. Why a voice-conversion operator has a real cognitive burden.

**Metadata prior** — start the suspicion meter somewhere sensible before audio arrives. Unknown international number, 11pm, large transfer → start higher.

**Edge inference** — run the model where the call is, not in a cloud API. Audio never leaves the building.

**Feature-only logging** — store the embedding and score, delete the audio. ⚠️ But see §11: embeddings are **partially invertible**, so this is weaker than it sounds unless the features are also protected.

**DPDP Act 2023** — India's data protection law. Voice is personal data. Not storing it isn't a limitation we apologise for; it's why a bank's legal team would approve this.

---

# 6. The decisions

| Decision | **Answer** | Why this over the alternative |
|---|---|---|
| GPU | **Kaggle free tier.** Stop thinking about it | Actual need is one ~30-min extraction pass |
| Front-end | **`facebook/wav2vec2-xls-r-300m`** | 128 languages of pretraining is load-bearing for the Indian claim. WavLM is English-heavy |
| Front-end training | **Frozen + cached for dev; top-6 unfrozen for the final run** | Frozen alone leaves accuracy on the table; full fine-tuning destroys iteration speed |
| Back-end | **AASIST** | 297K params. A download, not a build |
| Training data | **ASVspoof 2019 LA** | The standard. Comparable to published work |
| Out-of-domain eval | **"In-the-Wild"** | Real deepfakes from the internet, 38 h, 58 public figures |
| Prosody branch | **Build it thin. Never in the critical path** | Language-dependent; SSL features already encode prosody |
| Speaker verification | **Include it.** Pretrained ECAPA, zero training | A day, transforms the demo narrative |
| **Liveness branch** | **Include it. Passive timing, no injection** | Independent evidence on a different clock; §3 |
| Window / hop | **4.04 s (64,600 samples), 1 s hop** | The exact crop AASIST trains on. Match inference to training |
| Backend | **FastAPI + `aiortc`.** No gRPC | `aiortc` gives RTP timestamps, which Branch D requires |
| Frontend | **Whatever the frontend person already knows** | Learning a framework during prep is a pure loss |
| Metrics | **EER and TPR@1%FPR**, wired in *before* training anything | Accuracy will lie to you for three weeks |
| Demo host | **Laptop, offline** | Colab/Kaggle tunnels can't carry WebRTC UDP, and Branch D's timing dies through a TCP relay |

## Why XLS-R, not WavLM

The "too heavy" objection only holds if fine-tuning from the start. Frozen, the parameter count costs thirty minutes once. XLS-R was pretrained on **436k hours across 128 languages**, including Indic. When asked *"why would this work in Tamil?"*, that's a fact rather than a hope.

- **Development (95% of time):** frozen, features cached, train the head. Minutes per experiment.
- **Final run (once):** unfreeze top 6 layers, low LR, batch 4 with gradient accumulation, mixed precision. Overnight on Kaggle.

Top-6 rather than full unfreeze fits comfortably in 16 GB and won't OOM at 2am.

## Why prosody is cut from the critical path

1. **The SSL front-end already encodes prosody** — wav2vec2 carries pitch, rhythm and timing implicitly.
2. **It's language-dependent** — the exact thing that generates false alarms across Indian languages.

But the PS asks for it and judges check bullets. So: half a day, displayed on the dashboard, small fusion weight. Then say the honest thing: *"We measured it. Here's the ablation."* An ablation showing you cut something deliberately reads far stronger than a branch included because the PS said so.

---

# 7. Runtime architecture (L0–L6)

```
audio in ── WebRTC (aiortc) · file upload · SIPREC (stub)
              │  BIDIRECTIONAL: caller + agent, 16 kHz mono, 20 ms frames
              │  RTP-layer timestamps, not playout. RTT from RTCP.
              ▼
L0   Session manager & ring buffer          one buffer per call_id, fixed length, no disk
              ▼
L1   Voice activity gate                    Silero VAD — silence never enters the score
              ▼  speech only (caller)       ────────────► turn events (both sides) ──┐
L1   Window assembler                       64,600 samples (4.04 s) · 1 s hop        │
              ▼                                                                      │
L2   wav2vec2-XLS-R-300m                    frozen in dev · top-6 unfrozen final     │
              │                                                                      │
      ┌───────┼───────────────┐                                                      │
      ▼       ▼               ▼                                                      ▼
L3  A·AASIST  B·ECAPA-TDNN   C·prosody stats                            D·LIVENESS
    297K      192-d           pitch/pauses/rate                          turn-taking
    trained   pretrained      thin                                       ZERO audio
    ↓ spoof   ↓ cosine vs     ↓ small weight                             ↓ asymmetry
      LLR       voiceprint                                                 LLR
      └───────┴───────────────┴──────────────────────────────────────────────┘
              ▼
L4   Calibrate → fuse → accumulate          Platt on dev · weighted LLR SUM (SPRT)
                                            · metadata log-odds prior
              ▼  risk 0–100 + confidence interval
L5   Policy bands                           thresholds tiered by transaction value
              │
      ┌───────┼───────────────┐
      ▼       ▼               ▼
L6  dashboard  challenge      action gate
               prompt

║ PRIVACY & AUDIT RAIL (spans L0–L6)
║ audio never persisted · features only, then dropped
║ AES-GCM voiceprint vault · Ed25519-signed verdicts · hash-chained log
║ inference stays on-premise · DPDP Act 2023
```

| Layer | What it does | Key spec |
|---|---|---|
| **L0** Ingest | **Bidirectional.** Normalises every source, then a fixed-length ring buffer per `call_id`. Nothing touches disk — when the buffer wraps, the oldest audio is gone. The privacy guarantee as a data structure rather than a policy document | `aiortc`; 16 kHz mono; RTP timestamps; RTT from RTCP |
| **L1** Condition | VAD drops silence; windows cut at **exactly the crop length the model trained on**. Matching inference geometry to training geometry is free accuracy; mismatching it is a silent regression. Also emits turn events for Branch D | 64,600 samples, 1 s hop. AEC/NS/AGC **off** |
| **L2** Encode | The pre-trained ear. Frozen in development and cached, so an experiment costs minutes instead of a day | Frozen → cached `.npy`; final run releases top 6 layers |
| **L3** Detect | **A** primary, language-agnostic. **B** catches human impersonators. **C** PS coverage, weight-capped. **D** liveness, bypasses L2 entirely and consumes only timestamps | See §3 for D |
| **L4** Fuse | Raw outputs are overconfident, so each branch is Platt-scaled into a calibrated LLR before combining. LLRs then **sum** — this is SPRT | Calibration fit on dev, **never** on eval |
| **L5** Decide | The score never blocks a call. It gates the **action** | Tuned at 1% FPR, not max accuracy |
| **L6** Act | Dashboard, challenge-phrase generator, gated approval screen | Same WebSocket, score frames at 1 Hz |

## Policy bands

| Band | Range | Action |
|---|---|---|
| 🟢 Green | 0–39 | Proceed. Score logged, nothing shown. Silence is a feature |
| 🟡 Amber | 40–74 | Challenge. Agent handed a random phrase |
| 🔴 Red | 75–100 | Gate the action, with reason and timeline. Call continues; money doesn't move |

---

# 8. Build phases (P0–P7)

**Build strictly top to bottom.** Each phase leaves the system demonstrable, so wherever the calendar runs out, everything above that line is a complete submission.

### P0 — Foundation: data and measurement · *ML 1 + ML 2*

Corpora, feature extraction and caching, and the evaluation harness **before a single model is trained**. The cache converts a day-long experiment into a two-minute one.

- **In:** ASVspoof 2019 LA (25,380 train: 2,580 bonafide / 22,800 spoof); In-the-Wild (38 h, 58 figures)
- **Out:** cached `.npy` features; harness printing EER and TPR@1%FPR; deterministic manifests
- ✅ **Exit:** a random-guess classifier runs end to end and reports EER ≈ 50%. The harness is trusted before any model is.

### P1 — Baseline detector · *ML 1*

AASIST head on cached features, **no augmentation**. Not a stepping stone — the control condition the entire pitch rests on.

- ✅ **Exit:** strong in-domain EER, visibly worse In-the-Wild EER, **both recorded**. Report the gap rather than tuning it away.

### P2 — The contribution: codec robustness ★ · *ML 2*

Second model, identical to P1 except its training distribution. **The deliverable is a comparison, not a model.**

```bash
ffmpeg -i in.wav -ar 8000  -acodec pcm_mulaw         out.wav   # G.711   landline
ffmpeg -i in.wav -ar 8000  -acodec amr_nb -b:a 12.2k out.amr   # AMR-NB  mobile
ffmpeg -i in.wav -ar 16000 -acodec libopus -b:a 16k  out.opus  # Opus    VoIP
# then resample to 16 kHz, drop random 20-60 ms chunks, add light noise
```

- ✅ **Exit:** baseline collapses toward chance on codec-degraded audio; the augmented model holds. **That chart is the presentation.**

---

> ### 🚩 GATE
> **A complete, defensible submission exists here.** Nothing below is worth cutting into P0–P2 to reach.

---

### P3 — Streaming inference (bidirectional) · *Backend + ML 1*

L0, L1 and L4. Ring buffer, VAD gate, window assembler, Platt calibration on dev, running LLR sum. **Bidirectional from the start** — retrofitting the second stream later is painful.

- ✅ **Exit:** speak into a laptop mic; a score updates every second and its interval visibly narrows. Both models scored on the same stream. Both directions ingested with RTP timestamps logged.

### P4 — Demo surface · *Frontend + Backend*

One comparison unmissable: **two needles, same audio, baseline versus codec-trained.** Then challenge-response and the gated approval screen.

- ✅ **Exit:** the six-beat run-through completes twice, unassisted, **on venue wifi and on no wifi at all.**

### P5 — Branch D: conversational liveness · *ML 3 + Backend*

Collect the corpus (§3), implement the shape features, validate against injected network delay.

- **In:** P3's bidirectional turn-event stream; ~90 recorded conversations across three conditions
- **Out:** floor differential, overlap rate, variance ratio; a calibrated liveness LLR into L4
- ✅ **Exit:** shape features separate the conditions where a mean-latency threshold does not. Power calculation run on real data.

### P6 — Coverage: the remaining requirements · *ML 3 + Security*

Four parallel, individually droppable workstreams:

| Workstream | Content |
|---|---|
| Indian-language eval set | Kathbath/IndicSUPERB real + XTTS-v2/OpenVoice/F5-TTS/Indic Parler-TTS fake, through the P2 codec chain. ~500 clips |
| Speaker branch | `spkrec-ecapa-voxceleb`, pretrained. Live 10-second enrollment in the demo |
| Crypto module | Ed25519 signed verdicts, AES-GCM vault, cancelable transform, hash-chained log (§11) |
| Prosody panel | Pitch stats, pause distribution, speaking rate. Half a day. Ship the ablation |

- ✅ **Exit:** every PS bullet maps to a chart, a running module, or a stated and reasoned exclusion.

### P7 — Final numbers · *ML 1*

One overnight Kaggle run: unfreeze the top six XLS-R layers, low LR, batch 4 with gradient accumulation, mixed precision.

- ✅ **Exit:** all evaluation sets rerun, all charts regenerated, and **the P2 checkpoints kept as the fallback**.

## Phase × layer matrix

`●` built  ·  `◐` touched  ·  `○` untouched

| Phase | L0 | L1 | L2 | L3 | L4 | L5 | L6 | rail |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **P0** Foundation | ○ | ◐ | ● | ○ | ○ | ○ | ○ | ○ |
| **P1** Baseline | ○ | ◐ | ● | ● | ○ | ○ | ○ | ○ |
| **P2** Codec | ○ | ● | ● | ● | ○ | ○ | ○ | ○ |
| **P3** Streaming | ● | ● | ● | ● | ● | ◐ | ○ | ◐ |
| **P4** Demo surface | ◐ | ○ | ○ | ○ | ○ | ● | ● | ○ |
| **P5** Liveness | ● | ● | ○ | ● | ● | ○ | ◐ | ◐ |
| **P6** Coverage | ○ | ◐ | ○ | ● | ◐ | ○ | ◐ | ● |
| **P7** Final numbers | ○ | ○ | ● | ● | ◐ | ○ | ○ | ○ |

**L5 and L6 stay dark until P4** — the parts that make it *look* finished are built last, and the parts that make it *true* are built first.

---

# 9. Deployment shapes and permissions

## The finding that should shape the pitch

**Neither Android nor iOS lets a third-party app capture the audio of a regular phone call.** Not a difficulty — a platform-level prohibition.

**Android.** Capturing far-end audio requires `AudioSource.VOICE_CALL`, gated since Android 10 behind `CAPTURE_AUDIO_OUTPUT` — a `signature|privileged` permission. The app must be signed by the OS build key or preinstalled by OEM or carrier. There is no runtime prompt, because there is no prompt at all. The Accessibility-service workaround was banned by Google Play policy in 2022.

**iOS.** No public API, no private entitlement, no enterprise workaround for cellular call audio. CallKit tells you a call is happening; it will not give you a sample.

> **This reframes the PS.** Its mention of *telecom operator infrastructures* is not scope ambition — it's the **only viable path to consumer call audio**. Saying that out loud shows you understood the deployment reality.

## Permissions by deployment shape

| Shape | Device permissions | Buildable now? |
|---|---|---|
| **Enterprise contact center** | **None.** Audio lives on the bank's PBX/SBC | ✅ Yes |
| **In-app VoIP** | Only what the call feature already has | ✅ Yes |
| **Consumer app, cellular calls** | Privileged/system permissions | ❌ Needs OEM or carrier |
| **Telecom network side** | No device involvement | Regulatory, not technical |

**The first two are where the prototype lives.** Both need essentially nothing from the user.

### The in-app case is lighter than expected

- **The acoustic branch needs no microphone permission.** It analyses the *inbound* stream — audio the app already receives. Mic permission governs capturing the user's own voice.
- **Branch D needs the outbound stream**, so it does need mic access — but in a VoIP app that's already granted.

**Zero new permission prompts.**

### If Android were pursued anyway

| Permission | For | Friction |
|---|---|---|
| `CAPTURE_AUDIO_OUTPUT` | Call audio | **Privileged — OEM/carrier only** |
| `RECORD_AUDIO` | Local side | Runtime prompt |
| `READ_PHONE_STATE` | Call start/end, number | Runtime prompt |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_MICROPHONE` | Run during calls (14+) | Persistent notification required |
| `POST_NOTIFICATIONS` | Alerts (13+) | Runtime prompt |
| `SYSTEM_ALERT_WINDOW` | Risk overlay during a call | Special grant via Settings |
| Battery-optimisation exemption | Not being killed mid-call | Play restricts justification |
| `READ_CONTACTS` / `READ_CALL_LOG` | Known-contact prior | **Play "restricted permissions"** |

Design around the last row: let the user mark trusted numbers in-app, store them **hashed locally**, compare hashes. Same signal, no contact access, no Play review.

**Background activity:** none required. Event-driven — wakes on call-start, stops at call-end. A foreground service with a visible notification is a feature, not a nuisance.

## What we explicitly do not need

| Permission | Needed? | Why not |
|---|---|---|
| Storage / file access | **No** | Audio is never written |
| Location, Camera, SMS | **No** | — |
| Contacts, Call log | **No** | Hashed local allowlist instead |
| Internet | **Optional** | Edge tier works fully offline |

Most call-protection apps request contacts, call log and storage. Requesting none, in a product that does more, is a one-line differentiator.

## The legal layer, which is separate

- **DPDP Act 2023** — voice is personal data. Processing needs notice and consent even when nothing is stored.
- **Two-party consent norms** — the *caller* has not consented. Analysis without retention is defensible; recording is not.
- Enterprise deployments handle this through the call-recording notice contact centers already play. Another reason the enterprise shape is the clean one.

---

# 10. Hardware

## What drives the cost

Per 4.04-second window, fp16:

| Component | Compute |
|---|---|
| wav2vec2-XLS-R-300m | **~140 GFLOPs** — 96% of total |
| ECAPA-TDNN | ~5 GFLOPs |
| AASIST head, Silero VAD, prosody | negligible |
| **Branch D (timing)** | **zero** |
| AES-GCM, Ed25519, SHA-256 | negligible — hardware-accelerated |
| CKKS homomorphic match | significant — the one crypto item with real cost |

A 4-second window on a 1-second hop means 75% of each computation is redundant and cannot be cached, because wav2vec2 attends bidirectionally across the window. **Widening the hop to 2 seconds halves compute** — the main knob.

## Deployment tiers

| | **T1** On-prem GPU | **T2** CPU server | **T3** Workstation | **T4** Mobile |
|---|---|---|---|---|
| **GPU** | T4 16 GB / L4 24 GB | — | optional NPU | — |
| **CPU** | 8–16 cores | 16+ cores AVX-512 | 8+ cores | any, 2019+ |
| **RAM** | 32 GB | 32 GB | 16 GB | < 100 MB working set |
| **Storage** | 100 GB SSD | 100 GB SSD | — | ~2 MB model |
| **Stack** | Full | XLS-R int8, 2 s hop | XLS-R int8, 2 s hop | AASIST standalone |
| **Concurrent** | **50–80** | 2–5 | 1 | 1 |

**T1 sizing:** ~9 ms of GPU per window at realistic utilisation, one window per call-second → ~110 calls theoretically, 50–80 in practice. Reserve 2–3 CPU cores purely for WebRTC termination.

**Bandwidth is a non-issue** — fifty bidirectional calls at Opus voice rates is ~2.4 Mbps. What matters is low jitter, because Branch D's precision depends on it.

**The 100 GB is not call data.** It's the CUDA stack (5–10 GB), OS (10–20 GB), container images, model checkpoints (2–4 GB), and multi-year audit log retention (~25 GB at 10k calls/day). Voiceprints for 100,000 enrolled people fit in under 100 MB. **Storage does not scale with concurrent calls** — only with retention period and enrollment.

## Crypto hardware

| Need | Hardware | When |
|---|---|---|
| Verdict signing key | Cloud KMS, or PKCS#11 HSM | T1 production |
| AES-GCM / TLS throughput | AES-NI or ARMv8 crypto extensions | Present on everything modern |
| Mobile key storage | Android Keystore / iOS Secure Enclave | T4 — **required** |
| Confidential computing | AMD SEV-SNP, Intel TDX/SGX, AWS Nitro Enclaves | Only for TEE inference |
| CKKS matching | +8–16 GB RAM, ~10–100 ms per comparison | Only if demoed |

## Development and demo

| Task | Hardware | Time |
|---|---|---|
| Feature extraction (one-time) | Kaggle free T4/P100 | ~30 min |
| Head training | Any GPU, or CPU | minutes per run |
| P7 final fine-tune | 16 GB VRAM | overnight |

> ⚠️ **The demo runs on T3, not T1.** Colab and Kaggle give no inbound network; a tunnel means TURN-over-TCP, which adds jitter that dominates exactly what Branch D measures. Plus Colab disconnects and venue wifi is unreliable. **Validate T3 during P3.**
>
> **If XLS-R won't fit on the laptop, demo the cascade.** Run the on-device tier live — standalone AASIST, ~2 MB, trivially fast — and show escalation to the full stack with GPU-trained numbers alongside. Not a workaround: a live demonstration of the architecture you designed. The hardware constraint becomes the reason the design exists.

---

# 11. Cryptography

## Classify the data first

| Asset | Sensitivity | Lifetime | Key property |
|---|---|---|---|
| Raw call audio | Highest | Seconds | Never persisted |
| **Speaker embeddings** | Highest — **irrevocable** | Years | See below |
| SSL features | High — partially invertible | Seconds | Never persisted |
| Risk verdicts | Medium | Years | **Integrity-critical** |
| Model weights, thresholds | IP + attack surface | Static | **Integrity-critical** |
| Audit log | — | Years | **Integrity-critical** |

**A voiceprint is irrevocable.** A password can be rotated; a voice cannot. If the embedding database leaks, those people are permanently compromised against every voice biometric system. That single fact raises the bar above ordinary PII handling.

⚠️ **Correction to an earlier claim.** "You cannot reconstruct a conversation from 192 numbers" is overstated. There is real work on reconstructing intelligible speech from wav2vec2 representations and inverting speaker embeddings. Embeddings are **partially invertible** — they need protecting in their own right.

Note how many rows say **integrity-critical** rather than confidential. That's unusual and it's the most commonly missed part of securing this system.

## Data at rest

**Voiceprint vault:**

| Choice | Value | Why |
|---|---|---|
| Cipher | **AES-256-GCM** | Confidentiality *and* tamper detection |
| Nonce | 96-bit random per record | GCM fails catastrophically on nonce reuse |
| AAD | `record_id ‖ tenant_id ‖ key_version` | Stops an attacker swapping records |
| Structure | **Envelope** — per-record DEK, KEK in KMS | Rotate without re-encrypting |

**Above encryption: biometric template protection** (ISO/IEC 24745). Encryption doesn't help when plaintext necessarily exists in memory on every match. Three needed properties: **irreversibility**, **revocability**, **unlinkability**.

> **Why you can't just hash it:** biometric matching is *fuzzy*. Two recordings of the same person give different embeddings, so `hash(a) == hash(b)` is always false. Password-style hashing is structurally inapplicable.

| Technique | How | Cost |
|---|---|---|
| **Cancelable transform** (random projection / BioHashing) | Project through a user-specific random matrix from a stored seed, then quantize. Distances approximately preserved. Compromised? New seed | **~30 lines** |
| Fuzzy commitment / vault | Bind to a secret with error-correcting codes | Moderate |
| **CKKS homomorphic comparison** | Cosine similarity on *encrypted* embeddings | Heavy but real |

**Audit log integrity**, escalating: hash chain → Merkle tree → **signed checkpoints** (Ed25519) → external anchoring. Level 3 is the right target; a hash chain alone protects nothing if the attacker can recompute it.

**Model and config integrity — the silent failure.** If someone edits `threshold: 75` to `threshold: 100`, the system reports all-clear forever with no crash and no log entry. Sign artifacts, verify at load, refuse to start on mismatch. Treat threshold config as seriously as the model.

## Data in transit

| Path | Protection |
|---|---|
| Service ↔ service | **mTLS, TLS 1.3** |
| WebRTC media | **DTLS-SRTP** — mandatory, free |
| SIP telephony | SIPS + SRTP. Many carrier interconnects are still cleartext |

**The tension to name:** DTLS-SRTP is hop-by-hop. Terminating server-side with `aiortc` means decrypting there — a trust boundary. WebRTC also supports true E2EE via SFrame, but **you need plaintext to analyse it.** Two resolutions, pick deliberately: **analyse at an endpoint** (edge tier, preserves E2EE, costs accuracy), or **analyse in a TEE**. Naming this tradeoff is a strong signal; most teams don't notice it exists.

## Key management

Never hardcode. KMS or HSM. Envelope encryption with `key_version` on every record. Quarterly rotation. Hardware-backed keys on mobile. HKDF for subkeys, Argon2id for password-derived.

**Separation of duties:** whoever can read the database must **not** be able to read the keys. Different IAM roles, enforced. Encryption where one operator holds both buys compliance paperwork and very little security.

## Integrity of the verdict — highest value, most forgotten

The system's output is a number telling a bank whether to move money. **If an attacker can forge or suppress that number, they never need to touch the audio.** Right now the cheapest link is an unsigned JSON field on an internal network.

```
verdict   = { call_id, risk_score, band, model_version, timestamp, nonce }
signature = Ed25519_sign(scoring_service_key, canonical_encode(verdict))
```

- Consumer **verifies before acting**; missing or invalid signature = high risk, not absence of risk
- `nonce` + `timestamp` prevent replaying an old "safe" verdict
- `model_version` makes every decision auditable to a specific model
- **Fail closed.** A system that fails open can be disabled by cutting a cable

**The highest security-value-per-hour item in the project, and about forty lines of code.**

## Learning without collecting

Federated learning (gradients not audio) + secure aggregation (server sees only sums) + DP-SGD (formal ε, δ guarantee the model can't memorise individuals). The credible answer to *"how does this improve without building a voice database?"*

## The adjacent layer

**STIR/SHAKEN** authenticates caller ID cryptographically via X.509 certs and signed PASSporT tokens. Deployed in the US and Canada; India moving similarly through TRAI's caller-name work.

> **STIR/SHAKEN proves the number. We assess the voice.** A spoofed number with a real human, and a legitimate number with a cloned voice, are both attacks caught by different layers.

## What to build

| Tier | Items |
|---|---|
| **1 — Non-negotiable** | Signed verdicts (Ed25519), verified, fail-closed · TLS 1.3 + mTLS · AES-256-GCM envelope vault with AAD · signed model and config, verified at load · hash-chained log with signed checkpoints · no audio at rest, explicit zeroing, no swap |
| **2 — Correct biometric handling** | Cancelable transform before storage · hardware-backed mobile keys · versioned keys and a written rotation procedure · separation of duties |
| **3 — Name it, demo one** | CKKS homomorphic cosine similarity · TEE inference · federated + DP-SGD |

**All of Tier 1** — a few hundred lines combined, not a research project. **Tier 2 items 1 and 2.** **One Tier 3 item at toy scale**, timed honestly.

If the encryption person wants one headline contribution: **the signed verdict chain end to end** — a risk score cryptographically bound to a model version and a moment in time, verified before money moves, permanently recorded in a tamper-evident log.

⚠️ **Honest caveat:** you cannot reliably zero memory in Python. Strings are immutable, the GC relocates objects, freed pages carry no guarantee. Use `bytearray` with explicit overwrite, and accept that a real guarantee needs a Rust or C extension. Claiming secure memory wiping in pure Python is a claim a sharp judge could take apart.

---

# 12. The demo script

*Write this before the code. 50% of the score. Rehearse until boring.*

1. **Dashboard open, live mic streaming.** A teammate speaks. **Green.** The confidence band visibly tightens — LLR accumulation made visual.

2. **Play a clone of that same teammate's voice** (from 10 seconds of their audio via XTTS-v2). **Score climbs → amber → red.** The room reacts, because you cloned someone standing in front of them.

3. **★ THE MONEY SHOT.** Same clone, now through the phone-codec simulator. **Two needles side by side:**
   - *Baseline (no codec training):* collapses to chance. Says "real."
   - *Ours:* holds. Still says fake.

   Eight seconds, and it proves the one thing nobody else thought about.

4. **Branch D on a pre-recorded call.** No turn-taking behaviour at all — flagged on the first transition, before the acoustic branch has enough speech to speak.

5. **Score crosses threshold → challenge-response fires.** *"Ask the caller to repeat: seventeen–mango–blue."* The clone can't. Hard block.

6. **The banking screen:** *Approve ₹50,00,000 transfer* greyed out, reason and risk timeline attached.

7. **The audit log.** Zero audio stored. Signed verdict, hash-chained. Tie to DPDP Act 2023.

---

# 13. Team split

| Role | Owns |
|---|---|
| **ML 1 (lead)** | Feature extraction pipeline, training loop, the two models |
| **ML 2** | Codec/augmentation pipeline, the evaluation harnesses, all charts |
| **ML 3** | Indian dataset construction, Branch D features and corpus, prosody if time |
| **Backend** | `aiortc` bidirectional ingest, ring buffer, LLR accumulation, fusion, REST API |
| **Frontend** | Dashboard, two-needle view, challenge-response UI, mock banking screen |
| **Encryption** | Signed verdicts, AES-GCM vault, cancelable transform, hash-chained log |

**On the encryption role:** skip cancelable-biometrics *research* — a rabbit hole. Build the basic transform (30 lines), the signed verdict chain, and the tamper-evident log. All real, all a day or two each, and together they answer *"so you're storing people's voices?"* with a demo instead of a promise.

**On Branch D ownership:** it's ML-adjacent but really signal bookkeeping and statistics, living in L0 and L4 rather than in a model. Splitting it between ML 3 (features, corpus) and Backend (timestamps, RTP plumbing) works well.

---

# 14. Invariants and prepared answers

## Three invariants — cheap now, unrecoverable later

**1. Never delete the baseline.** The instinct once the augmented model works is to retire the weaker one. **The entire argument is the gap between them.** Tag both, keep both loadable, put both on screen.

**2. Wire the metrics in at P0.** EER and TPR@1%FPR print from the first training run or they print too late. ASVspoof 2019 LA is roughly **nine spoofed utterances to every bonafide one** — accuracy will look excellent while meaning nothing.

**3. Make L0 bidirectional at P3.** Branch D needs both streams and a synchronised clock across them. Retrofitting the second stream after the pipeline is built is significantly more painful than starting with it.

## What we cut, and the answer when asked

| Cut | Say this |
|---|---|
| Real SIP/telecom integration | *"We consume a generic audio stream. A SIP gateway is a connector, not a research problem — here's the interface it plugs into."* |
| Training our own TTS | *"We use the same off-the-shelf cloning tools an attacker would. That's the realistic threat model."* |
| Full multilingual coverage | *"Our primary signal is deliberately language-agnostic. Here's the Indian-language evaluation confirming that."* |
| Adversarial robustness | *"Known limitation. Re-recording degrades our signal — same physics as the codec problem, and codec augmentation partially mitigates it."* |
| Signal injection for liveness | *"We designed it, then found the topology doesn't support it — outbound audio never enters their pipeline. Passive timing gets the same evidence with none of the fragility."* |

> **Prepared answers to your own weaknesses read as maturity. Discovered weaknesses read as sloppiness. Same facts, opposite impression.**

## The thing to say out loud

Report the poor out-of-domain number on In-the-Wild rather than only the flattering in-domain one. A team that shows where its model fails, and can explain why, reads as the team that understood the problem.

---

# 15. Appendix

## Models — all pretrained, all downloadable

| Role | Model | Notes |
|---|---|---|
| Front-end | `facebook/wav2vec2-xls-r-300m` | 300M params · 436k hrs · 128 languages |
| Back-end | AASIST (clovaai reference impl.) | 297K params · graph attention · **a repo, not a package** |
| Speaker | `speechbrain/spkrec-ecapa-voxceleb` | 192-dim, pretrained on VoxCeleb |
| VAD | Silero VAD | pip-installable, tiny, fast |

## Datasets

| Dataset | Use | Size |
|---|---|---|
| **ASVspoof 2019 LA** | Training + in-domain eval | train 25,380 (2,580 bonafide / 22,800 spoof); dev 24,844; eval 71,237 |
| **In-the-Wild** (Müller et al. 2022) | Out-of-domain honesty eval | ~38 h, 58 public figures |
| **ASVspoof 2021 DF** | Optional — codec conditions | eval only, large |
| **AI4Bharat Kathbath / IndicSUPERB** | Real Indian speech | ~12 languages |
| **Own turn-taking corpus** | Branch D validation | ~90 conversations, 3 conditions |

## Voice cloning tools (attack data generation)

XTTS-v2 · OpenVoice v2 · F5-TTS · AI4Bharat Indic Parler-TTS

## Key numbers

```
Window            64,600 samples = 4.04 s @ 16 kHz   (AASIST's standard crop)
Hop               1 s (2 s on CPU tiers)
Frame             20 ms
Phone passband    ~300 - 3,400 Hz  (vs 0 - 8,000 Hz full)
Phone rate        8 kHz            (vs 16 kHz full)
Extraction cost   ~30 min, once, on a free T4
Speaker embedding 192 dimensions
Per-window LLR    clamped to +/-4.0
Human turn gap    ~200 ms typical, varies widely by language
VC pipeline delay 100-400 ms typical
```

## Glossary

| Term | Meaning |
|---|---|
| **TTS** | Text-to-Speech — type text, get a voice |
| **VC** | Voice Conversion — speak yourself, sound like someone else (real-time) |
| **Vocoder** | The stage turning model output into sound waves. Source of most detectable fingerprints |
| **CM** | Countermeasure — the fake/real detector |
| **ASV** | Automatic Speaker Verification — the who-is-it check |
| **Bonafide** | ASVspoof's term for genuine, non-synthetic speech |
| **EER** | Equal Error Rate — where false alarms equal misses. Lower is better |
| **FPR** | False Positive Rate — how often we wrongly accuse a genuine caller |
| **TPR@1%FPR** | Fakes caught while wrongly flagging only 1% of real callers. **The number that matters** |
| **VAD** | Voice Activity Detection |
| **Embedding** | A voice compressed into a short list of numbers; a fingerprint |
| **Cosine similarity** | How alike two embeddings are, from −1 to 1 |
| **Codec** | Phone-network compression (G.711, AMR-NB, Opus). Our main enemy |
| **LLR** | Log-Likelihood Ratio — a score format you can add up over time |
| **SPRT** | Sequential Probability Ratio Test — optimal stopping rule for accumulating evidence |
| **Platt scaling** | The calibration making a model's confidence number mean what it says |
| **ECE** | Expected Calibration Error — how wrong a model is about its own confidence |
| **SSL** | Self-Supervised Learning — how the front-end was pretrained |
| **Leave-one-out** | Hide one attack type from training, test only on it |
| **Base-rate problem** | Rare events + imperfect detectors = mostly false alarms |
| **Analog hole** | Playing fake audio through a speaker into a mic to smear the fingerprint |
| **Entrainment** | Conversational partners converging on a shared rhythm |
| **DAF** | Delayed Auditory Feedback — hearing your own delayed voice disrupts fluent speech |
| **DTX** | Discontinuous Transmission — codec stops sending during silence |
| **AEC / NS / AGC** | Echo cancellation / noise suppression / automatic gain control. All must be **off** |
| **SIPREC** | RFC 7865/7866 — forks a copy of call media to a third party |
| **AAD** | Additional Authenticated Data — binds ciphertext to its context in AES-GCM |
| **KEK / DEK** | Key-Encrypting Key / Data-Encrypting Key — envelope encryption |
| **TEE** | Trusted Execution Environment — enclave the host OS cannot read |
| **CKKS** | Homomorphic encryption scheme for approximate arithmetic on real numbers |
| **DP-SGD** | Differentially private training — formal guarantee against memorisation |
| **STIR/SHAKEN** | Cryptographic caller-ID attestation |
| **ASVspoof** | The main international challenge/dataset for this problem |
| **DPDP Act 2023** | India's Digital Personal Data Protection Act. Voice = personal data |
