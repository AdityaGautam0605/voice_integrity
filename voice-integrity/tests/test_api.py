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
