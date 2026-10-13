"""API surface: session lifecycle, streaming, auth, fail-closed verdict.

Driven through FastAPI's TestClient against the stub detector, so the whole
HTTP and WebSocket path is exercised with no model weights and no network.
"""

from __future__ import annotations

import struct
from contextlib import asynccontextmanager

import numpy as np
import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
from fastapi.testclient import TestClient  # noqa: E402

from vif.serve import server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app wired to the stub detector and a temp data directory."""
    monkeypatch.delenv("VIF_API_TOKEN", raising=False)
    server.bootstrap(
        config_dir="configs",
        backend="stub",
        device="cpu",
        keys_dir=tmp_path / "keys",
        data_dir=tmp_path / "data",
    )
    app = server.create_app()
    # bootstrap() already ran above; replace the lifespan so it does not run
    # a second time and overwrite the temp-directory wiring.
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as test_client:
        yield test_client
    server.state.sessions.clear()
    server.state.verdicts.clear()


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def pcm_frame(n_samples: int = 512, amplitude: float = 0.4, seed: int = 0) -> bytes:
    """A float32 PCM frame loud enough to pass the VAD gate."""
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples) / 16000.0
    signal = amplitude * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.normal(size=n_samples)
    return signal.astype(np.float32).tobytes()


def silent_frame(n_samples: int = 512) -> bytes:
    return np.zeros(n_samples, dtype=np.float32).tobytes()


def send_speech(ws, frames: int = 200, lead_in: int = 30) -> None:
    """Open with silence, then speak.

    The lead-in is not padding for the test's convenience: the energy VAD
    calibrates its noise floor from the opening frames, so a stream that
    starts at full volume reads as uniformly silent.  Real calls open quiet.
    """
    for _ in range(lead_in):
        ws.send_bytes(silent_frame())
    for i in range(frames):
        ws.send_bytes(pcm_frame(512, seed=i))


class TestHealth:
    def test_health_reports_model_and_device(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["model_version"]
        assert body["device"] == "cpu"

    def test_metrics_available(self, client):
        assert client.get("/metrics").status_code == 200

    def test_historical_integrity_is_separate_from_freshness(self, client):
        from vif.common.types import VerdictPayload
        payload = VerdictPayload(session_id="historical", spoof_probability=.1, risk="GREEN", timestamp=1)
        signed = server.state.signer.sign(payload).model_dump(mode="json")
        result = client.post("/v1/verdict/verify", json=signed).json()
        assert result["signature_valid"] is True
        assert result["fresh"] is False
        assert result["valid"] is False
        signed["payload"]["spoof_probability"] = .9
        assert client.post("/v1/verdict/verify", json=signed).json()["signature_valid"] is False

    def test_audit_search_reaches_beyond_first_page(self, client):
        from vif.common.types import VerdictPayload
        for i in range(65):
            signed = server.state.signer.sign(VerdictPayload(session_id=f"record-{i:03}", spoof_probability=.1, risk="GREEN"))
            server.state.audit.append(signed)
        first = client.get("/v1/audit").json()
        assert len(first["entries"]) == 50 and first["has_more"]
        second = client.get("/v1/audit?offset=50").json()
        assert len(second["entries"]) == 15 and not second["has_more"]
        assert first["entries"][0]["seq"] > second["entries"][0]["seq"]
        found = client.get("/v1/audit?q=record-000&risk=GREEN").json()
        assert found["total"] == 1
        assert client.get("/v1/audit?limit=0").status_code == 422

    def test_reference_audio_enrollment_and_revocation(self, client):
        import base64
        sid = client.post("/v1/session", json={}).json()["session_id"]
        body = {"speaker_id": "trusted-speaker", "pcm_b64": base64.b64encode(pcm_frame(64000)).decode()}
        result = client.post(f"/v1/enroll/{sid}/audio", json=body)
        assert result.status_code == 200, result.text
        assert server.state.sessions[sid].speaker_id == "trusted-speaker"
        assert client.post(f"/v1/enroll/{sid}/revoke?speaker_id=trusted-speaker").status_code == 200
        assert server.state.sessions[sid].speaker_id is None
        body["pcm_b64"] = base64.b64encode(silent_frame(64000)).decode()
        assert client.post(f"/v1/enroll/{sid}/audio", json=body).status_code == 422
        assert client.post("/v1/enroll/missing/audio", json=body).status_code == 404

    def test_agent_reference_audio_does_not_enter_spoof_buffer(self, client):
        import base64
        sid = client.post("/v1/session", json={"enable_liveness": True}).json()["session_id"]
        assert server.state.sessions[sid].liveness is not None
        assert server.state.sessions[sid].agent_vad is not server.state.sessions[sid].vad
        with client.websocket_connect(f"/v1/stream/{sid}") as ws:
            for _ in range(30):
                ws.send_json({"type": "agent_audio", "pcm_b64": base64.b64encode(silent_frame()).decode()})
            for _ in range(150):
                ws.send_json({"type": "agent_audio", "pcm_b64": base64.b64encode(pcm_frame()).decode()})
            ws.send_text("end")
            while True:
                message = ws.receive_json()
                if message["type"] == "verdict":
                    assert message["payload"]["windows_scored"] == 0
                    assert message["payload"]["speech_seconds"] == 0
                    break


class TestSession:
    def test_create_returns_audio_geometry(self, client):
        """The frontend needs the window geometry before it starts sending."""
        body = client.post("/v1/session", json={}).json()
        assert body["session_id"]
        assert body["sample_rate"] == 16000
        assert body["window_samples"] == 64600
        assert body["hop_samples"] == 16000

    def test_each_session_is_distinct(self, client):
        a = client.post("/v1/session", json={}).json()["session_id"]
        b = client.post("/v1/session", json={}).json()["session_id"]
        assert a != b


class TestStreaming:
    def test_unknown_session_is_rejected(self, client):
        with client.websocket_connect("/v1/stream/does-not-exist") as ws:
            assert ws.receive_json()["type"] == "error"

    def test_audio_produces_score_messages(self, client):
        session_id = client.post("/v1/session", json={}).json()["session_id"]
        received = []

        with client.websocket_connect(f"/v1/stream/{session_id}") as ws:
            # A window is 64,600 samples of SPEECH: 127 frames at minimum.
            send_speech(ws, frames=200)
            ws.send_text("close")
            while True:
                message = ws.receive_json()
                received.append(message)
                if message.get("type") == "verdict":
                    break

        scores = [m for m in received if m.get("type") == "score"]
        assert scores, "no score messages were emitted"

        first = scores[0]
        for field in (
            "session_id",
            "sequence",
            "speech_seconds",
            "spoof_probability",
            "risk",
            "speaker_status",
            "inference_ms",
            "model_version",
        ):
            assert field in first
        assert 0.0 <= first["spoof_probability"] <= 1.0
        assert first["risk"] in ("GREEN", "AMBER", "RED")
        assert first["speaker_status"] == "NOT_ENROLLED"

    def test_int16_pcm_is_accepted(self, client):
        """The decoder tolerates either PCM format the client might send."""
        session_id = client.post("/v1/session", json={}).json()["session_id"]
        samples = (np.sin(np.linspace(0, 40, 512)) * 12000).astype(np.int16)
        with client.websocket_connect(f"/v1/stream/{session_id}") as ws:
            ws.send_bytes(struct.pack(f"<{len(samples)}h", *samples))
            ws.send_text("close")
            ws.receive_json()

    def test_session_is_removed_at_teardown(self, client):
        session_id = client.post("/v1/session", json={}).json()["session_id"]
        with client.websocket_connect(f"/v1/stream/{session_id}") as ws:
            ws.send_bytes(pcm_frame())
            ws.send_text("close")
            ws.receive_json()
        assert session_id not in server.state.sessions


class TestVerdict:
    def test_missing_verdict_is_a_404_not_a_default(self, client):
        """Absence of a verdict must not read as 'safe'."""
        response = client.get("/v1/verdict/never-existed")
        assert response.status_code == 404
        assert "elevated risk" in response.json()["detail"]

    def test_verdict_is_signed_and_verifies(self, client):
        session_id = client.post("/v1/session", json={}).json()["session_id"]
        with client.websocket_connect(f"/v1/stream/{session_id}") as ws:
            send_speech(ws, frames=200)
            ws.send_text("close")
            while ws.receive_json().get("type") != "verdict":
                pass

        verdict = client.get(f"/v1/verdict/{session_id}").json()
        assert verdict["algorithm"] == "Ed25519"
        assert verdict["signature"]
        assert verdict["payload"]["session_id"] == session_id

        check = client.post("/v1/verdict/verify", json=verdict).json()
        assert check["valid"] is True, check["reason"]

    def test_tampered_verdict_fails_verification(self, client):
        session_id = client.post("/v1/session", json={}).json()["session_id"]
        with client.websocket_connect(f"/v1/stream/{session_id}") as ws:
            send_speech(ws, frames=200)
            ws.send_text("close")
            while ws.receive_json().get("type") != "verdict":
                pass

        verdict = client.get(f"/v1/verdict/{session_id}").json()
        verdict["payload"]["spoof_probability"] = 0.0
        check = client.post("/v1/verdict/verify", json=verdict).json()
        assert check["valid"] is False


class TestAuth:
    def test_token_is_enforced_when_set(self, client, monkeypatch):
        monkeypatch.setenv("VIF_API_TOKEN", "s3cret")
        assert client.post("/v1/session", json={}).status_code == 401
        ok = client.post("/v1/session", json={}, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200

    def test_open_when_no_token_configured(self, client):
        assert client.post("/v1/session", json={}).status_code == 200


class TestEnrolment:
    def test_enrol_then_session_uses_the_identity(self, client):
        import base64

        embedding = np.random.default_rng(0).normal(size=192).astype(np.float32)
        session_id = client.post("/v1/session", json={}).json()["session_id"]

        response = client.post(
            f"/v1/enroll/{session_id}",
            json={
                "speaker_id": "ceo-001",
                "embedding_b64": base64.b64encode(embedding.tobytes()).decode(),
            },
        )
        assert response.json()["status"] == "ENROLLED"
        assert server.state.sessions[session_id].speaker_id == "ceo-001"
