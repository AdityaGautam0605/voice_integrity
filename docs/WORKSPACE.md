# Workspace and calling workflows

The frontend has two entry points sharing the same backend:

- `/`: responsive session dashboard, local microphone analysis, historical verdicts, and audit search.
- `/call`: operator two-device calling. Use the **Two-device call** button in the workspace header.
- `/join/<id>#token=...`: caller invitation. The caller does not need the operator access code.

## Windows setup and launch

From `D:\sih_problem_statements\voice-integrity`, create a virtual environment with your installed Python if `.venv` is missing:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-demo.txt -r requirements-dev.txt
```

If `python` is not on PATH, use its full executable path for the first command. From the parent frontend folder:

```bat
cd /d D:\sih_problem_statements
npm ci
npm run demo -- --http
```

The launcher uses `.venv\Scripts\python.exe` on Windows and `.venv/bin/python` elsewhere. It starts both services. Open `http://localhost:5173` for single-computer development. For two physical devices, use a trusted HTTPS LAN address or the configured public HTTPS deployment; a phone cannot access your laptop through `localhost`.

## Two-device calls

Open **Two-device call**, choose the configured detector or an explicitly enabled policy rehearsal, and click **Create call**. Share the invitation URL or QR code privately. The caller opens it, grants microphone access, and clicks **Join call**. Headphones avoid feedback. Start with two seconds of quiet for energy VAD, then talk normally.

The operator's received caller audio feeds spoof analysis. Operator audio is sent separately as a reference for experimental conversational liveness; it never enters the spoof-analysis buffer. Liveness needs enough alternating turns and is shown as an evidence score, not an identity probability. It is not a validated production classifier.

Mute, playback recovery, and hang-up are available on both devices. Losing analysis clears the displayed score while the WebRTC voice call continues. Peer hang-up can use the data channel even when signaling is lost. A local microphone-only session instead releases its microphone after an analysis failure. Backend status refreshes every five seconds.

## Speaker enrollment

During an active session, provide a speaker ID and a **trusted 3–10 second reference recording**, with the speaker's permission. Do not use an unverified live caller as their own reference. The browser decodes and converts the recording to mono 16 kHz audio; the backend computes the embedding and stores the protected voiceprint using the existing vault. A revoke action removes the reference. Unavailable speaker models return an explicit error. In stub mode, comparison is simulated.

## Historical verification and audit

Signature integrity and freshness are independent. An old intact record can display **Verified** with a warning that it is too old to authorize a new action. The API's existing `valid` field still requires freshness; `signature_valid` reports cryptographic integrity and `fresh` reports freshness when the signature is intact. No freshness or replay checks were relaxed for action authorization.

Audit search and risk filtering run on the server across all stored records. **Previous records** and **Next records** navigate pages of 50, newest first. Query parameters are `offset`, `limit` (1–200), `q` (up to 200 characters), and `risk` (`GREEN`, `AMBER`, `RED`, or empty).

## Verification commands

```bat
npm test
npm run lint
npm run build
npx playwright test --config playwright.ui.config.js
npx playwright test tests/browser/call.spec.js tests/browser/online.spec.js
```

The UI suite uses deterministic API and audio fixtures. The calling suite starts the real stub backend and tests real browser WebRTC, including caller-only scoring, tamper rejection, and failure cleanup. Chrome is required for these configs. The separate relay suite still requires its TURN/deployment environment; local browser checks do not establish cross-network performance.

For backend tests:

```bat
cd voice-integrity
set PYTHONPATH=src
set VIF_VAD=energy
.venv\Scripts\python.exe -m pytest tests -q
```

Real trained detector weights, calibration, and accuracy evaluation remain outside these changes. No simulated result should be presented as real detection evidence.
