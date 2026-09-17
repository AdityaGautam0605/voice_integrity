# Two-device voice demo

A laptop operator and Android caller talk in Chrome over local Wi-Fi. WebRTC carries audio directly between browsers. The laptop sends a copy of the **received caller track** to the analysis backend; the operator microphone is not scored. The detector is replaceable, so the whole transport, policy, signing and audit flow works before model training.

## Setup

Requirements: Node.js 22.12+, Python 3.11, `uv`, and `mkcert`. On this Mac, Node 22 and mkcert are installed; the backend virtual environment and frontend dependencies have been prepared.

For another machine, from the repository root:

```bash
# macOS; install uv separately if it is not already available
brew install node@22 mkcert uv
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
npm ci
uv venv --python 3.11 voice-integrity/.venv
uv pip install --python voice-integrity/.venv/bin/python -r voice-integrity/requirements-demo.txt -r voice-integrity/requirements-dev.txt
```

The small demo dependency set requires no Torch, ONNX, aiortc, trained weights, corpora or GPU. Energy-based voice activity detection (VAD) is selected explicitly by the launcher. Training dependencies remain separate.

## Calls across different networks

For a free demo with your laptop as the server, use [the tunnel guide](docs/TUNNEL_DEMO.md) and `./scripts/demo.sh --tunnel` with a Metered TURN credential.

Use [the online deployment guide](docs/ONLINE_DEMO.md) for laptop Wi-Fi ↔ Android mobile-data calls. It includes the VPS HTTPS frontend, TURN relay, operator sign-in, and `./scripts/demo.sh --online` launcher. The local setup below remains available.

### Local HTTPS: once per laptop/phone setup

1. Put the laptop and phone on the same Wi-Fi with client isolation disabled. Find the laptop IPv4 address in Wi-Fi settings. A private router is preferable to venue or guest Wi-Fi.
2. Generate the certificate, substituting your laptop address:

   ```bash
   npm run demo:cert -- 192.168.1.20
   ```

   This installs a local development CA into the laptop trust store and may request your macOS administrator password. It creates `.certs/demo.pem`, `.certs/demo-key.pem` and `.certs/phone-rootCA.crt`. The whole directory is ignored by Git and blocked by the development server.
3. Transfer **only `.certs/phone-rootCA.crt`** to the phone, for example using a USB cable. In Android Settings, search for **Install a certificate**, choose **CA certificate**, and select it. The exact menu varies by manufacturer. Never transfer `rootCA-key.pem` or `demo-key.pem`.
4. Restart Chrome if necessary. Open the HTTPS address on both devices without a certificate warning. Clicking past a warning is not a substitute for installing trust. Microphone access requires a secure context. See [browser microphone requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia) and [mkcert mobile setup](https://github.com/FiloSottile/mkcert#mobile-devices).
5. Allow the app through the laptop firewall on this private network. If the laptop's IP changes, rerun the certificate command for the new address; the existing CA is reused.

## Start

```bash
./scripts/demo.sh
# Equivalent when npm is in PATH: npm run demo
```

The launcher starts FastAPI on loopback port 8000 and Vite on port 5173. Open the displayed **HTTPS LAN address on the laptop**, then create a call and scan its QR code on the phone. An invitation made at `localhost` cannot be used by the phone. Ctrl+C stops both services.

For laptop-only development, without certificates:

```bash
./scripts/demo.sh --http
# http://localhost:5173
```

No internet is required after dependencies, certificates and any future model assets are installed. Keep Wi-Fi connected. No external STUN/TURN service is used; networks that prevent direct peer connectivity are outside this demo's supported setup.

## Rehearsal

1. Select **Live audio · simulated detector**, create a call, and join from the phone. Grant microphone access. Use headphones and keep both pages in the foreground.
2. Begin with **two seconds of quiet** so energy VAD can learn the noise floor. Exchange a phrase in each direction, then have the caller speak for 15–20 seconds.
3. Watch received audio, detected speech, scored windows, score history and policy. First scoring needs **4.04 seconds of detected speech**; each additional speech second produces another window. Silence is excluded. Inference time measures detector compute, not end-to-end latency.
4. End from either device. The laptop verifies the Ed25519 signature and loads the matching hash-chained audit record. **Test an altered verdict** verifies a changed copy without altering the stored record.
5. Create separate GREEN, AMBER and RED **policy rehearsal** calls. Their probabilities are deliberately simulated; they are not detection evidence. AMBER displays a phrase for manual checking. RED recommends gating sensitive actions; it does not cut the call. There is no automated spoken-answer verifier or banking transaction integration.
6. To demonstrate analysis independence, run backend and frontend in separate terminals, stop just the backend during an established call, and continue talking. The dashboard marks analysis unavailable. Hang-up can still travel over the peer data channel. Create a new call after restarting the backend.

Separate terminals, from the repo root:

```bash
# Terminal 1
cd voice-integrity
VIF_BACKEND=stub VIF_DEMO_SCENARIOS=1 VIF_VAD=energy PYTHONPATH=src .venv/bin/python -m vif.cli serve --host 127.0.0.1

# Terminal 2: repo root
npm run dev
```

Speaker verification remains NOT_ENROLLED and liveness is disabled. A stub verdict proves processing and signing, not authenticity. Unavailable results and calls with insufficient speech never display a current safe score. Live policies are advisory.

## Interfaces and model handoff

- `POST /v1/calls` → room ID and separate operator/caller credentials; unused invitations expire after 10 minutes.
- `WS /v1/calls/{id}/signal`: first JSON frame `{role, token}`; server sends `joined` and `ready`, relays `{type: "offer" | "answer" | "ice", data}`, and accepts `hangup`. Credentials in invitation URL fragments are not sent in HTTP access logs. One connection per role.
- `POST /v1/session`: existing endpoint, with optional `demo_scenario: "GREEN" | "AMBER" | "RED"`. Scenarios require both a stub backend and `VIF_DEMO_SCENARIOS=1`. They create a session-local detector with a scenario-specific signed model version; real-model mode rejects them.
- `WS /v1/stream/{id}`: mono little-endian float32 PCM at the returned sample rate, in 20 ms chunks. Server sends `score`, one-second `progress` messages and a final `verdict`. Send `end` to finish. Each session admits one audio producer. The previous int16 decoding path remains for older clients.
- `/health` adds demo availability, VAD kind and risk thresholds. Session creation adds `demo_mode` and `vad`.
- Existing verdict, verification, audit and metrics endpoints remain. Set `VIF_API_TOKEN` in both frontend and backend processes to have Vite add the bearer token on proxied HTTP requests. This is a trusted-LAN development app, not public multi-user hosting.

To integrate a compatible trained model later, install its inference dependencies, place its weights/configuration, and start with `VIF_BACKEND=torch` or `VIF_BACKEND=onnx`. The launcher honors these overrides and the rehearsal selector disappears. The current ONNX wrapper has no speaker embeddings. The interface is `BaseDetector.score_window(wav)` with 64,600 mono samples at 16 kHz, returning a raw score where **higher means more synthetic**. Match preprocessing/window geometry and fit calibration on development data. Wrap another model behind this interface if needed; the phone, call and dashboard need no transport changes. The optional speaker and two-sided liveness integrations remain separate future work.

## Checks

```bash
npm run build
npm run lint
npm test
npx playwright install chromium  # once, before offline use
npm run test:browser
cd voice-integrity
.venv/bin/python -m pytest tests/ -q
PYTHONPATH=src .venv/bin/python scripts/smoke_test.py
```

Browser tests start isolated browser pages and use controlled audio sources through real WebRTC and the real backend. They do not replace a physical Android/Wi-Fi rehearsal. Optional ML/codec tests skip when their dependencies are absent. The launcher is intended for macOS/Linux; native Windows setup is not covered.
