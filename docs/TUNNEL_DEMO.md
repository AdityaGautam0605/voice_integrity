# Free laptop-hosted cross-network demo

Use this setup when you have no VPS or payment card. Your laptop serves the built website, signaling and analysis. Cloudflare Quick Tunnel supplies a temporary HTTPS URL, and Metered supplies TURN relay connectivity between the two browsers.

The existing local demo and VPS deployment are also available. Model integration still happens on the laptop after the transport demo works.

## 1. Create a Metered TURN credential

In your Metered app dashboard:

1. Open **TURN Server**.
2. Click **Add Credential** (or **Generate Your First Credential**).
3. Label it `voice-integrity-demo` and select the free plan if prompted.
4. Copy that credential's **API key**. This key belongs to the TURN credential; the app-wide **Secret Key** under Developers is not needed.
5. Note your full Metered app domain, such as `amulyavif.metered.live`.

Allow two minutes after creating a credential before starting a call. Metered documents a propagation delay, so this launcher uses a credential prepared in advance. [Credential setup](https://www.metered.ca/docs/turn-server-service/creating-turn-credentials/)

## 2. Configure the laptop

Install the dependencies from [frontend setup](../README_frontend.md#setup), then install Cloudflare's tunnel client. On this Mac, the existing demo dependencies and `cloudflared` have been installed.

```bash
# Only if cloudflared is missing on another Mac:
brew install cloudflared
```

From the repository root, run:

```bash
./scripts/demo.sh --tunnel
```

The first run creates `.env.tunnel` with permissions restricted to your user, then stops for configuration. Open it in your editor and fill in:

```dotenv
VIF_METERED_DOMAIN=amulyavif.metered.live
VIF_METERED_API_KEY=YOUR_TURN_CREDENTIAL_API_KEY
VIF_BACKEND=stub
VIF_DEMO_SCENARIOS=1
VIF_VAD=energy
```

Use the actual app domain you registered. Keep the key in this local, Git-ignored file. It is fetched by the Python backend and is not put into browser bundles, command-line arguments or public configuration. The browser receives the TURN username/password after it proves room membership, as required by WebRTC.

## 3. Start and call

```bash
./scripts/demo.sh --tunnel
```

The launcher checks Metered credentials, builds the frontend, obtains a Quick Tunnel URL, and starts the online backend on loopback port 8000. It prints:

```text
Demo URL: https://<generated-name>.trycloudflare.com
Operator access code: <generated-code>
```

Open that **Demo URL** on your laptop, sign in, select **Force relay**, and create a call. On Android, turn off Wi-Fi, enable mobile data, scan the invitation, and tap **Join call**. Confirm **Relayed**, audible speech in both directions, caller analysis and a verified verdict after hanging up.

Keep the laptop awake and both browsers in the foreground. Ctrl+C stops the backend and tunnel. The next startup creates a new public URL and operator code; use fresh invitations. The existing Duck DNS names are not used by Quick Tunnel.

## Limits and credential lifetime

- Quick Tunnels are for testing and development, with no uptime guarantee. They have a 200 simultaneous-request limit and no SSE support; this demo uses WebSockets. [Cloudflare Quick Tunnel documentation](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
- Metered currently lists a $0 plan with 500 MB monthly TURN traffic, counting ingress and egress, and no card requirement. Check your account's allowance and remaining usage before rehearsing. This is a capped free demo, not unlimited hosting. [Metered pricing](https://www.metered.ca/stun-turn)
- Both participants use your pre-created demo TURN credential. Metered controls its expiry and revocation. The API does not return an expiry, so the app does not claim it lasts two hours. You can disable/delete the credential in Metered after the demo; restart with a replacement key for the next rehearsal. If you configure expiry, allow for propagation, rehearsal preparation and the entire call.
- Local room access to ICE configuration stops after two hours; this does **not** revoke the provider credential already received by a browser. Keep invitations limited to your demo participants.
- Stable connections and calls under 30 minutes are the supported rehearsal target. If the tunnel or analysis connection drops during an established call, audio may continue through WebRTC/TURN; create a new call after service returns to restore analysis.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Missing API key | Fill in `.env.tunnel` using the API key of the TURN credential. |
| Metered rejects the key | Check the app domain and credential status; the app Secret Key is a different credential. |
| TURN preflight succeeds but relay fails | Wait two minutes after credential creation; check available traffic and provider status. |
| Port 8000 is occupied | Stop the previous local/online demo before starting this mode. |
| Cloudflare cannot create a URL | Check internet access and whether the network blocks tunnel traffic. Restart the launcher. |
| New URL briefly returns 502 | Wait for the launcher to print its final Demo URL after backend startup. |
| Browser shows sign-in errors | Open the current printed HTTPS URL and use the current operator code. |
| No audio | Grant microphone access, use headphones, and tap Enable audio playback if shown. |

## Checks

```bash
npm test
npm run lint
npm run test:relay
cd voice-integrity
.venv/bin/python -m pytest -q
```

The relay test uses the built site with local HTTPS and real coturn. Backend tests exercise Metered response validation, caching, errors and invitation authentication with a provider fixture. These checks do not prove the live Metered account, Quick Tunnel or Android mobile network; complete the actual two-device rehearsal after entering the credential.
