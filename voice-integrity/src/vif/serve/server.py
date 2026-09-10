"""FastAPI + WebSocket server (L0/L6).

The integration surface, and it is smaller than people expect: we receive a
*copy* of the media and observe it.  Nothing routes through us on its way
somewhere.  If this process crashes, lags or restarts, the call carries on
unaffected and the worst case is a stale score, never a broken conversation.
That property is what makes the system deployable in a bank at all.

Lifecycle, end to end:

    client opens WS -> sends {"type": "start", sdp} -> CallSession is BORN
    ICE completes   -> on("track") fires -> one consume() task per direction
    audio flows     -> score frames stream back on the same socket
    socket closes   -> teardown() -> verdict signed, logged, buffers zeroed

Endpoints match section 11 of the SRS.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from vif.common.config import AppConfig, load_config
from vif.common.logging import get_logger
from vif.common.types import Side, Verdict
from vif.crypto.auditlog import AuditLog
from vif.crypto.keys import LocalKeyStore
from vif.crypto.vault import VoiceprintVault, embedding_from_b64
from vif.crypto.verdict import (
    VerdictSigner,
    VerdictVerifier,
    generate_keypair,
    load_private_key,
)
from vif.eval.calibration import Calibrator
from vif.serve.detector import BaseDetector, build_detector
from vif.serve.session import CallSession
from vif.serve.vad import build_vad

log = get_logger(__name__)


class AppState:
    """Process-global, read-only after startup.

    Models load once and are shared across every call.  Everything mutable is
    inside a CallSession.
    """

    config: AppConfig
    detector: BaseDetector
    calibrator: Calibrator
    signer: VerdictSigner
    verifier: VerdictVerifier
    vault: VoiceprintVault
    audit: AuditLog
    sessions: dict[str, CallSession] = {}
    verdicts: dict[str, Verdict] = {}


state = AppState()


def bootstrap(
    config_dir: str | Path = "configs",
    backend: str = "auto",
    device: str = "cpu",
    keys_dir: str | Path = "keys",
    data_dir: str | Path = "data",
) -> AppState:
    """Load config, models and keys.  Fails loudly on a mismatch."""
    state.config = load_config(config_dir)
    state.detector = build_detector(state.config.model, backend=backend, device=device)

    calibration_path = state.config.path(state.config.model.fusion.calibration)
    state.calibrator = Calibrator.load(calibration_path)

    keys_dir = Path(keys_dir)
    private_path = keys_dir / "verdict_ed25519.pem"
    if private_path.exists():
        keypair = load_private_key(private_path)
    else:
        keypair = generate_keypair()
        from vif.crypto.verdict import save_keypair

        save_keypair(keypair, private_path, keys_dir / "verdict_ed25519.pub.pem")
        log.warning("generated a NEW verdict signing key - development only")

    state.signer = VerdictSigner(keypair)
    state.verifier = VerdictVerifier(keypair.public_key)

    keystore = LocalKeyStore(keys_dir / "keystore.json")
    state.vault = VoiceprintVault(Path(data_dir) / "voiceprints.db", keystore)
    state.audit = AuditLog(Path(data_dir) / "audit.db", signer=keypair)

    log.info("bootstrap complete - detector=%s", state.detector.model_version)
    return state


@asynccontextmanager
async def lifespan(app):  # pragma: no cover - exercised by uvicorn
    bootstrap()
    yield
    for session in list(state.sessions.values()):
        session.close()
    state.audit.checkpoint()
    state.vault.close()
    state.audit.close()


def create_app():
    """Build the FastAPI application."""
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from pydantic import BaseModel

    app = FastAPI(title="Voice Integrity Verification", version="0.1.0", lifespan=lifespan)

    # -- health --------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "sessions": len(state.sessions)}

    @app.get("/metrics")
    async def metrics():
        return {
            "active_sessions": len(state.sessions),
            "audit_entries": state.audit.count(),
            "model_version": state.detector.model_version,
            "sessions": {sid: s.stats.as_dict() for sid, s in state.sessions.items()},
        }

    # -- enrolment -----------------------------------------------------

    class EnrolRequest(BaseModel):
        speaker_id: str
        embedding_b64: str

    @app.post("/v1/enroll")
    async def enroll(req: EnrolRequest):
        embedding = embedding_from_b64(req.embedding_b64)
        record = state.vault.enrol(req.speaker_id, embedding)
        return {"enrolled": record.speaker_id, "template_dim": record.template_dim}

    @app.post("/v1/enroll/{speaker_id}/revoke")
    async def revoke(speaker_id: str):
        state.vault.revoke(speaker_id)
        return {"revoked": speaker_id, "note": "re-enrolment requires fresh reference audio"}

    # -- verdicts ------------------------------------------------------

    @app.get("/v1/verdict/{call_id}")
    async def get_verdict(call_id: str):
        verdict = state.verdicts.get(call_id)
        if verdict is None:
            # Fail closed: absence is not evidence of safety.
            raise HTTPException(
                status_code=404,
                detail="no verdict for this call - treat as elevated risk",
            )
        return verdict.model_dump(mode="json")

    @app.post("/v1/verdict/verify")
    async def verify_verdict(payload: dict):
        verdict = Verdict(**payload)
        ok, reason = state.verifier.verify(verdict, check_replay=False)
        return {"valid": ok, "reason": reason}

    # -- audit ---------------------------------------------------------

    @app.get("/v1/audit")
    async def audit(limit: int = 50):
        ok, reason = state.audit.verify_chain()
        return {
            "chain_valid": ok,
            "reason": reason,
            "count": state.audit.count(),
            "entries": [e.__dict__ for e in state.audit.entries(limit)],
        }

    # -- the live path -------------------------------------------------

    @app.websocket("/v1/analyze")
    async def analyze(ws: WebSocket):
        await ws.accept()
        session: CallSession | None = None
        adapter = None
        tasks: list[asyncio.Task] = []

        try:
            offer = await ws.receive_json()
            if offer.get("type") != "start":
                await ws.close(code=1003)
                return

            call_id = offer.get("call_id") or str(uuid.uuid4())

            async def emit(payload: dict) -> None:
                await ws.send_json(payload)

            # --- a call begins, for us, right here ---
            session = CallSession(
                call_id=call_id,
                config=state.config,
                detector=state.detector,
                vad=build_vad("auto", state.config.model.audio.vad_frame),
                on_score=emit,
                calibrator=state.calibrator,
                metadata=offer.get("metadata", {}),
                speaker_id=offer.get("speaker_id"),
                vault=state.vault,
            )
            state.sessions[call_id] = session

            adapter, answer = await _negotiate(offer)
            await ws.send_json({"type": "answer", **answer})

            rtt = await adapter.rtt_ms()
            session.liveness.set_rtt(rtt)

            tasks = [
                asyncio.create_task(session.consume(adapter, Side.CALLER)),
                asyncio.create_task(session.consume(adapter, Side.AGENT)),
            ]

            # Park until the client goes away or the media ends.
            done, pending = await asyncio.wait(
                [*tasks, asyncio.create_task(_wait_for_close(ws))],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

        except WebSocketDisconnect:
            log.info("client disconnected")
        except Exception as exc:  # noqa: BLE001
            log.exception("session error: %s", exc)
        finally:
            # Teardown runs on EVERY exit path.  This is where the privacy
            # claim is actually implemented.
            for task in tasks:
                task.cancel()
            if adapter is not None:
                await adapter.close()
            if session is not None:
                await _teardown(session, ws)

    return app


async def _negotiate(offer: dict):
    """Set up the media transport described by the offer.

    Returns an adapter and whatever must be echoed back to the client.
    """
    kind = offer.get("transport", "webrtc")

    if kind == "synthetic":
        from vif.serve.adapters.file import SyntheticAdapter

        adapter = SyntheticAdapter(
            n_turns=offer.get("n_turns", 12),
            pipeline_floor_ms=offer.get("pipeline_floor_ms", 0.0),
            realtime=offer.get("realtime", True),
        )
        return adapter, {"transport": "synthetic"}

    if kind == "file":
        from vif.serve.adapters.file import FileAdapter

        adapter = FileAdapter(offer["caller_path"], offer.get("agent_path"))
        return adapter, {"transport": "file"}

    from aiortc import RTCPeerConnection, RTCSessionDescription

    from vif.serve.adapters.webrtc import WebRTCAdapter, side_for_transceiver

    pc = RTCPeerConnection()
    adapter = WebRTCAdapter()
    adapter.attach_peer_connection(pc)
    arrived: list = []

    @pc.on("track")
    def on_track(track):  # pragma: no cover - requires a live peer
        if track.kind != "audio":
            return
        side = side_for_transceiver(len(arrived))
        arrived.append(track)
        adapter.register_track(track, side)

    await pc.setRemoteDescription(RTCSessionDescription(**offer["desc"]))
    await pc.setLocalDescription(await pc.createAnswer())
    return adapter, {"desc": {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}}


async def _wait_for_close(ws) -> None:
    """Block until the client closes the socket."""
    try:
        while True:
            await ws.receive_text()
    except Exception:  # noqa: BLE001
        return


async def _teardown(session: CallSession, ws=None) -> None:
    """Sign, log, and destroy.  Every sensitive thing dies in this function."""
    try:
        payload = session.finalise()
        verdict = state.signer.sign(payload)
        state.verdicts[session.call_id] = verdict
        state.audit.append(verdict)

        if session.stats.windows_scored % 20 == 0:
            state.audit.checkpoint()

        if ws is not None:
            try:
                await ws.send_json({"type": "verdict", **verdict.model_dump(mode="json")})
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.exception("teardown failed for %s: %s", session.call_id, exc)
    finally:
        session.close()
        state.sessions.pop(session.call_id, None)


def run(host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
