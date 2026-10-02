# Cloud backend deployment

The Vercel project currently builds the frontend only. Deploy the Python API as
a separate, long-running service with HTTPS and WebSocket support.

## Container

Use `voice-integrity/` as the Docker build context and `Dockerfile.cloud` as the
Dockerfile. The process binds `0.0.0.0` and reads the platform's `PORT` variable
(default 8000). Its health endpoint is `/health`.

This image starts with `VIF_BACKEND=disabled` and simulated scenarios disabled.
Health, authentication, call rooms, and audit endpoints remain available without
model weights. Health reports `detection_available: false`; analysis session
creation returns HTTP 503 with an explicit model-unavailable message.

The image reuses the shared API dependency list named `requirements-demo.txt`;
that filename does not enable simulation. It does not install ONNX Runtime.
The optional SpeechBrain speaker verifier is not installed by this image and
will abstain. Use the existing full `Dockerfile` and provide trained weights
for the Torch detector instead.

## Render native Python service

Choose Python 3, root directory `voice-integrity`, and the Free instance type.
Build command: `pip install -r requirements-demo.txt`.
Start command: `python scripts/serve_cloud.py`.
Health check path: `/health`. Python is pinned to 3.11 by `.python-version`.
In addition to the variables below, set `PYTHONPATH=src`, `VIF_ONLINE=1`,
`VIF_BACKEND=disabled`, `VIF_VAD=energy`, and `VIF_DEMO_SCENARIOS=0`.
For this native service, leave `VIF_KEYS_DIR` and `VIF_DATA_DIR` unset so they
default to local `keys/` and `data/` directories.

Model files are ignored by Git. A GitHub-connected host needs a secure model
download during its build or an image built with the exported model and pushed
to a container registry. Never expect an ignored local artifact to arrive with
a GitHub deployment.

Set these variables in the host's dashboard:

| Variable | Value |
| --- | --- |
| `VIF_PUBLIC_ORIGIN` | `https://voice-integrity-omega.vercel.app` |
| `VIF_OPERATOR_CODE` | Your own random code of at least 16 characters |
| `VIF_TURN_PROVIDER` | `metered` |
| `VIF_METERED_DOMAIN` | Your Metered app hostname |
| `VIF_METERED_API_KEY` | The API key of your TURN credential |

The existing `.env.tunnel` contains the Metered configuration. Enter those values
as secrets in the host's dashboard; do not commit that file.

Mount persistent storage at `/var/lib/vif` to retain signing keys, voiceprint
storage, and audit records. Run one instance and one worker: operator sessions,
call rooms, and active analysis sessions are currently held in process memory.
They are lost when the process restarts.

Render's Free web service plan has 512 MB RAM, sleeps after 15 minutes without
traffic, and cannot attach a persistent disk. Whether the exported detector fits
requires a memory and latency measurement. The configured 300-million-parameter
Torch frontend is not a suitable target for that memory budget. Free hosting
does not guarantee an always-on service or retained local audit data.

References: [Render Free](https://render.com/docs/free),
[compute plans](https://render.com/docs/compute-plans),
[WebSockets](https://render.com/docs/websocket).

## Connect the frontend

After deployment, obtain the backend's public HTTPS URL and test `/health`.
The frontend currently uses `/api` on its own origin for both HTTP and WebSocket
requests. Production routing must forward `/api/*` to the backend and strip the
`/api` prefix. Confirm support for WebSocket upgrades in the chosen routing path
before enabling it; an HTTP health check alone does not verify audio streaming.

Keep browser requests on the frontend origin so the existing secure operator
cookie (`Path=/api`, `SameSite=Strict`) and origin checks work. Pointing the
browser directly at an unrelated backend domain requires changes to CORS,
cookie handling, and WebSocket authentication.

Verify operator sign-in, session creation, a streaming connection, a signed
verdict, and the audit trail from the public frontend before calling the
deployment connected.
