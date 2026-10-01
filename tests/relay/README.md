# Real TURN integration tests

Run from the repository root after installing the frontend and demo backend dependencies:

```bash
brew install coturn                     # macOS; on Ubuntu: apt install coturn
npx playwright install chromium
npx playwright test --config playwright.relay.config.js
```

Use Node 22.12+, OpenSSL, and `voice-integrity/.venv`. Set `VIF_TEST_TURNSERVER` to the full `turnserver` executable path if needed. Stop the normal demo first: the suite uses HTTPS port 5173, TURN TCP/UDP 53470 and relay UDP 53480–53500.

The suite builds the frontend, then starts an isolated authenticated coturn and the `DemoSite` server used by the laptop tunnel. Local HTTPS replaces Cloudflare, and coturn replaces Metered. Temporary signing keys, audit records, TURN secret and an untrusted one-day test certificate are deleted at shutdown. Playwright ignores certificate trust only for this suite; it does not install a CA on the computer.

Tests use two independent browser contexts, real WebRTC encoding and real TURN allocations. Controlled microphone signals replace physical microphone hardware. Assertions inspect both selected ICE candidates (`relay`), received audio packets, policy scoring, signed verdicts and the audit chain. Additional scenarios verify audio survives application WebSocket loss and invalid TURN credentials cannot establish a forced-relay call.

**The loopback relay exception belongs only to this harness.** Production coturn must continue blocking loopback/private relay destinations. This local suite verifies actual relay behavior; Wi-Fi/mobile-data rehearsal on the deployed VPS remains a separate acceptance check.
