"""FastAPI application (L0/L6).

The integration surface, and it is smaller than people expect: we receive a
*copy* of the audio and observe it.  Nothing routes through us on its way
somewhere.  If this process crashes, lags or restarts, the call carries on
unaffected and the worst case is a stale score, never a broken conversation.

Session lifecycle:

    POST /v1/session              -> session_id, model version, audio geometry
    WS   /v1/stream/{session_id}  -> frames in, StreamMessage out at the hop rate
    POST /v1/enroll/{session_id}  -> optional reference identity
    GET  /v1/verdict/{session_id} -> the final signed verdict
    GET  /health                  -> status, model version, device

Splitting session creation from the socket keeps the frontend simple: it gets
the window geometry and model version up front, and the socket carries nothing
but audio and results.

Audio arrives over the WebSocket as raw little-endian float32 or int16 PCM at
the sample rate reported by the session endpoint.  Binary frames are audio;
text frames are control messages.
"""

# NOTE: no `from __future__ import annotations` here.  FastAPI resolves
# handler annotations through the module globals, and the framework types
# are imported lazily inside create_app().  Deferring annotations would
# make WebSocket unresolvable and FastAPI would silently treat it as a
# query parameter.

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np

from vif.common.config import AppConfig, load_config
from vif.common.logging import get_logger
from vif.common.types import SessionInfo, Side, Verdict
from vif.serve.adapters.base import AudioFrame, now_ns
from vif.serve.detector import BaseDetector, build_detector
from vif.serve.session import CallSession
from vif.serve.vad import build_vad

log = get_logger(__name__)


class AppState:
    """Process-global, read-only after startup.

    Models load once and are shared across every session.  Everything mutable
    lives inside a CallSession.
    """

    config: AppConfig
    detector: BaseDetector
    calibrator = None
    signer = None
    verifier = None
    vault = None
    audit = None
    device: str = "cpu"
    started_at: float = 0.0
    sessions: dict[str, CallSession] = {}
    verdicts: dict[str, Verdict] = {}
    latency_ms: list[float] = []


state = AppState()


def bootstrap(
    config_dir: str | Path = "configs",
    backend: str = "auto",
    device: str = "cpu",
    keys_dir: str | Path = "keys",
    data_dir: str | Path = "data",
) -> AppState:
    """Load config, models and keys.  Fails loudly on a mismatch."""
    from vif.crypto.auditlog import AuditLog
    from vif.crypto.keys import LocalKeyStore
    from vif.crypto.vault import VoiceprintVault
    from vif.crypto.verdict import (
        VerdictSigner,
        VerdictVerifier,
        generate_keypair,
        load_private_key,
        save_keypair,
    )
    from vif.eval.calibration import Calibrator

    state.config = load_config(config_dir)
    state.device = device
    state.started_at = time.time()
    state.detector = build_detector(state.config.model, backend=backend, device=device)
    state.calibrator = Calibrator.load(state.config.path(state.config.model.scoring.calibration))

    security = state.config.security

    if security.sign_verdicts:
        keys_dir = Path(keys_dir)
        private_path = keys_dir / "verdict_ed25519.pem"
        if private_path.exists():
            keypair = load_private_key(private_path)
        else:
            keypair = generate_keypair()
            save_keypair(keypair, private_path, keys_dir / "verdict_ed25519.pub.pem")
            log.warning("generated a NEW verdict signing key - development only")
        state.signer = VerdictSigner(keypair)
        state.verifier = VerdictVerifier(keypair.public_key)

        if security.audit_log:
            state.audit = AuditLog(
                Path(data_dir) / "audit.db",
                signer=keypair,
                merkle_checkpoints=security.merkle_checkpoints,
            )

    if state.config.model.speaker.enabled:
        keystore = LocalKeyStore(Path(keys_dir) / "keystore.json")
        state.vault = VoiceprintVault(
            Path(data_dir) / "voiceprints.db",
            keystore,
            use_cancelable=security.cancelable_templates,
        )

    log.info(
        "bootstrap complete - detector=%s checksum=%s device=%s",
        state.detector.model_version,
        state.detector.model_checksum or "n/a",
        device,
    )
    return state


@asynccontextmanager
async def lifespan(app):  # pragma: no cover - exercised by uvicorn
    bootstrap(
        backend=os.environ.get("VIF_BACKEND", "auto"),
        device=os.environ.get("VIF_DEVICE", "cpu"),
    )
    yield
    for session in list(state.sessions.values()):
        session.close()
    if state.vault is not None:
        state.vault.close()
    if state.audit is not None:
        state.audit.close()


def _require_token(authorization: str | None) -> None:
    """Bearer-token check.

    A single shared token, which is the right weight for a demo backend behind
    HTTPS.  Sender-constrained credentials belong in the production roadmap;
    what matters here is that the endpoint is not simply open when deployed.
    Set VIF_API_TOKEN, or leave it empty to disable the check locally.
    """
    from fastapi import HTTPException

    expected = os.environ.get("VIF_API_TOKEN") or state.config.security.api_token
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def create_app():
    """Build the FastAPI application."""
    from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
    from pydantic import BaseModel

    app = FastAPI(title="Voice Integrity Verification", version="0.1.0", lifespan=lifespan)

    # -- health and metrics ------------------------------------------------

    @app.get("/health")
    async def health():
        recent = state.latency_ms[-200:]
        return {
            "status": "ok",
            "model_version": state.detector.model_version,
            "model_checksum": state.detector.model_checksum,
            "device": state.device,
            "active_sessions": len(state.sessions),
            "uptime_s": round(time.time() - state.started_at, 1),
            "inference_ms_mean": round(float(np.mean(recent)), 1) if recent else None,
            "inference_ms_p95": (
                round(float(np.percentile(recent, 95)), 1) if len(recent) >= 20 else None
            ),
        }

    @app.get("/metrics")
    async def metrics():
        return {
            "active_sessions": len(state.sessions),
            "verdicts_issued": len(state.verdicts),
            "audit_entries": state.audit.count() if state.audit else 0,
            "sessions": {sid: s.stats.as_dict() for sid, s in state.sessions.items()},
        }

    # -- session -----------------------------------------------------------

    class CreateSession(BaseModel):
        speaker_id: str | None = None

    @app.post("/v1/session", response_model=SessionInfo)
    async def create_session(
        body: CreateSession | None = None,
        authorization: str | None = Header(default=None),
    ):
        """Open an analysis session and report the audio geometry to send."""
        _require_token(authorization)
        session_id = str(uuid.uuid4())
        audio = state.config.model.audio

        state.sessions[session_id] = CallSession(
            session_id=session_id,
            config=state.config,
            detector=state.detector,
            vad=build_vad("auto", audio.vad_frame),
            calibrator=state.calibrator,
            speaker_id=(body.speaker_id if body else None),
            vault=state.vault,
        )
        return SessionInfo(
            session_id=session_id,
            model_version=state.detector.model_version,
            device=state.device,
            sample_rate=audio.sample_rate,
            window_samples=audio.window_samples,
            hop_samples=audio.hop_samples,
        )

    # -- streaming ---------------------------------------------------------

    @app.websocket("/v1/stream/{session_id}")
    async def stream(ws: WebSocket, session_id: str):
        """Audio in, results out.

        Binary frames are PCM.  A text frame closes the session cleanly and
        returns the verdict.
        """
        await ws.accept()
        session = state.sessions.get(session_id)
        if session is None:
            await ws.send_json({"type": "error", "detail": "unknown session_id"})
            await ws.close(code=1008)
            return

        async def emit(payload: dict) -> None:
            state.latency_ms.append(payload.get("inference_ms", 0.0))
            await ws.send_json(payload)

        session.on_message = emit

        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                data = message.get("bytes")
                if data:
                    await session.handle_frame(
                        AudioFrame(
                            side=Side.CALLER,
                            pcm=_decode_pcm(data),
                            timestamp_ns=now_ns(),
                        )
                    )
                    continue

                text = message.get("text")
                if text in ("close", "end", "finish"):
                    break

        except WebSocketDisconnect:
            log.info("client disconnected from session %s", session_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("stream error on session %s: %s", session_id, exc)
        finally:
            # Teardown runs on EVERY exit path.  This is where the privacy
            # claim is actually implemented.
            verdict = await _teardown(session)
            try:
                if verdict is not None:
                    await ws.send_json({"type": "verdict", **verdict.model_dump(mode="json")})
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- enrolment ---------------------------------------------------------

    class EnrolRequest(BaseModel):
        speaker_id: str
        embedding_b64: str

    @app.post("/v1/enroll/{session_id}")
    async def enroll(
        session_id: str,
        req: EnrolRequest,
        authorization: str | None = Header(default=None),
    ):
        """Register a reference identity for the speaker branch."""
        _require_token(authorization)
        if state.vault is None:
            raise HTTPException(status_code=503, detail="speaker branch is disabled")

        from vif.crypto.vault import embedding_from_b64

        record = state.vault.enrol(req.speaker_id, embedding_from_b64(req.embedding_b64))
        session = state.sessions.get(session_id)
        if session is not None:
            session.speaker_id = req.speaker_id
        return {"status": "ENROLLED", "speaker_id": record.speaker_id}

    @app.post("/v1/enroll/{session_id}/revoke")
    async def revoke(
        session_id: str,
        speaker_id: str,
        authorization: str | None = Header(default=None),
    ):
        _require_token(authorization)
        if state.vault is None:
            raise HTTPException(status_code=503, detail="speaker branch is disabled")
        state.vault.revoke(speaker_id)
        return {"status": "REVOKED", "speaker_id": speaker_id}

    # -- verdict -----------------------------------------------------------

    @app.get("/v1/verdict/{session_id}")
    async def get_verdict(session_id: str, authorization: str | None = Header(default=None)):
        _require_token(authorization)
        verdict = state.verdicts.get(session_id)
        if verdict is None:
            # Fail closed: absence is not evidence of safety.
            raise HTTPException(
                status_code=404,
                detail="no verdict for this session - treat as elevated risk",
            )
        return verdict.model_dump(mode="json")

    @app.post("/v1/verdict/verify")
    async def verify_verdict(payload: dict):
        """What a consuming system runs before acting on a verdict."""
        if state.verifier is None:
            raise HTTPException(status_code=503, detail="verdict signing is disabled")
        ok, reason = state.verifier.verify(Verdict(**payload), check_replay=False)
        return {"valid": ok, "reason": reason}

    # -- audit -------------------------------------------------------------

    @app.get("/v1/audit")
    async def audit(limit: int = 50, authorization: str | None = Header(default=None)):
        _require_token(authorization)
        if state.audit is None:
            raise HTTPException(status_code=503, detail="audit log is disabled")
        ok, reason = state.audit.verify_chain()
        return {
            "chain_valid": ok,
            "reason": reason,
            "count": state.audit.count(),
            "entries": [e.__dict__ for e in state.audit.entries(limit)],
        }

    return app


def _decode_pcm(data: bytes) -> np.ndarray:
    """Interpret a binary frame as PCM.

    float32 when the length divides by four and the values are in range,
    otherwise int16.  Guessing is acceptable here because the session endpoint
    already told the client what to send; this is a tolerance, not a protocol.
    """
    if len(data) % 4 == 0:
        candidate = np.frombuffer(data, dtype=np.float32)
        if candidate.size and np.all(np.isfinite(candidate)) and np.abs(candidate).max() <= 1.5:
            return candidate.astype(np.float32)
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


async def _teardown(session: CallSession) -> Verdict | None:
    """Sign, log, and destroy.  Every sensitive thing dies in this function."""
    verdict = None
    try:
        payload = session.finalise()
        if state.signer is not None:
            verdict = state.signer.sign(payload)
            state.verdicts[session.session_id] = verdict
            if state.audit is not None:
                state.audit.append(verdict)
                state.audit.checkpoint()  # no-op unless merkle_checkpoints is on
    except Exception as exc:  # noqa: BLE001
        log.exception("teardown failed for %s: %s", session.session_id, exc)
    finally:
        session.close()
        state.sessions.pop(session.session_id, None)
    return verdict


def run(host: str = "0.0.0.0", port: int = 8000) -> None:  # pragma: no cover  # noqa: S104
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
