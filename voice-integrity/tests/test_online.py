import base64
import hashlib
import hmac
import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vif.serve import server
from vif.serve.online import COOKIE_NAME, OnlineConfig

ORIGIN = "https://demo.example.test"
CODE = "operator-code-for-tests-only"
SECRET = "relay-secret-for-tests-with-at-least-32-characters"


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name, value in {
        "VIF_ONLINE": "1", "VIF_PUBLIC_ORIGIN": ORIGIN,
        "VIF_TURN_HOST": "turn.example.test", "VIF_TURN_SECRET": SECRET,
        "VIF_OPERATOR_CODE": CODE, "VIF_VAD": "energy",
    }.items():
        monkeypatch.setenv(name, value)
    server.bootstrap(backend="stub", keys_dir=tmp_path / "keys", data_dir=tmp_path / "data")
    app = server.create_app()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await app.state.call_rooms.close()

    app.router.lifespan_context = lifespan
    with TestClient(app) as connection:
        yield connection
    for session in server.state.sessions.values():
        session.close()
    server.state.sessions.clear()
    server.state.verdicts.clear()
    server.state.audit.close()
    server.state.vault.close()


def sign_in(client):
    response = client.post("/v1/operator/login", headers={"Origin": ORIGIN}, json={"code": CODE})
    assert response.status_code == 200
    # Real clients send the Secure /api cookie through the HTTPS reverse proxy.
    # TestClient accesses unprefixed backend routes directly.
    return {"Origin": ORIGIN, "Cookie": f"{COOKIE_NAME}={response.cookies[COOKIE_NAME]}"}


def create_room(client, headers, force_relay=False):
    response = client.post("/v1/calls", headers=headers, json={"force_relay": force_relay})
    assert response.status_code == 200
    return response.json()


def test_public_health_and_operator_cookie_lifecycle(client):
    assert client.get("/health").json()["online_mode"] is True
    assert client.get("/v1/operator").json()["authenticated"] is False
    login = client.post("/v1/operator/login", headers={"Origin": ORIGIN}, json={"code": CODE})
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    for attribute in ("secure", "httponly", "samesite=strict", "path=/api", "max-age=28800"):
        assert attribute in cookie
    headers = {"Origin": ORIGIN, "Cookie": f"{COOKIE_NAME}={login.cookies[COOKIE_NAME]}"}
    assert client.get("/v1/operator", headers=headers).json()["authenticated"]
    assert client.get("/metrics", headers=headers).status_code == 200
    assert client.post("/v1/operator/logout", headers=headers).status_code == 200
    assert client.get("/metrics", headers=headers).status_code == 401


def test_online_requires_cookie_even_with_legacy_api_token(client, monkeypatch):
    monkeypatch.setenv("VIF_API_TOKEN", "legacy-secret")
    headers = {"Origin": ORIGIN, "Authorization": "Bearer legacy-secret"}
    for method, path in (("GET", "/metrics"), ("GET", "/v1/audit"),
                         ("GET", "/v1/verdict/id"), ("POST", "/v1/session"),
                         ("POST", "/v1/calls"), ("POST", "/v1/verdict/verify"),
                         ("POST", "/v1/enroll/id"), ("GET", "/docs")):
        assert client.request(method, path, headers=headers).status_code == 401
    # The same token must not interfere with valid online cookie authentication.
    assert client.post("/v1/calls", headers=sign_in(client), json={}).status_code == 200


def test_missing_or_cross_site_origin_rejected_even_with_cookie(client):
    headers = sign_in(client)
    for origin in (None, "https://attacker.example", "null", ORIGIN + ".attacker.example"):
        attack = {"Cookie": headers["Cookie"]}
        if origin is not None:
            attack["Origin"] = origin
        for path in ("/v1/calls", "/v1/operator/login", "/v1/operator/logout"):
            assert client.post(path, headers=attack, json={"code": CODE}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/stream/any", headers=attack):
                pass


def test_cookie_expires_and_backend_restart_discards_sessions(client):
    headers = sign_in(client)
    access = client.app.state.online_access
    for digest in access.sessions:
        access.sessions[digest] = time.monotonic() - 1
    assert client.get("/v1/operator", headers=headers).json()["authenticated"] is False
    assert client.get("/metrics", headers=headers).status_code == 401
    headers = sign_in(client)
    new_app = server.create_app()
    @asynccontextmanager
    async def lifespan(_app):
        yield
    new_app.router.lifespan_context = lifespan
    with TestClient(new_app) as restarted:
        assert restarted.get("/metrics", headers=headers).status_code == 401


def test_login_rate_limit(client):
    for _ in range(5):
        response = client.post("/v1/operator/login", headers={"Origin": ORIGIN}, json={"code": "incorrect"})
        assert response.status_code == 401
    blocked = client.post("/v1/operator/login", headers={"Origin": ORIGIN}, json={"code": CODE})
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"


def test_room_ice_credentials_match_coturn_and_do_not_require_operator_cookie(client):
    room = create_room(client, sign_in(client), force_relay=True)
    responses = []
    for role in ("operator", "caller"):
        response = client.post(f"/v1/calls/{room['call_id']}/ice", headers={"Origin": ORIGIN},
                               json={"role": role, "token": room["credentials"][role]})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        ice = response.json()
        assert ice["iceTransportPolicy"] == "relay"
        assert ice["public_origin"] == ORIGIN
        credential = ice["iceServers"][1]
        assert credential["username"] == f"{ice['expires_at']}:{room['call_id']}:{role}"
        assert 7100 < ice["expires_at"] - time.time() <= 7200
        expected = base64.b64encode(hmac.new(SECRET.encode(), credential["username"].encode(), hashlib.sha1).digest()).decode()
        assert credential["credential"] == expected
        assert SECRET not in response.text
        assert "turns:turn.example.test:5349?transport=tcp" in credential["urls"]
        responses.append(credential)
    assert responses[0]["username"] != responses[1]["username"]


def test_ice_rejects_wrong_role_cross_room_and_expired_credentials(client):
    headers = sign_in(client)
    room = create_room(client, headers)
    other = create_room(client, headers)
    path = f"/v1/calls/{room['call_id']}/ice"
    for role, token in (("caller", other["credentials"]["caller"]),
                        ("operator", room["credentials"]["caller"]), ("unknown", "bad")):
        assert client.post(path, headers={"Origin": ORIGIN}, json={"role": role, "token": token}).status_code == 403
    client.app.state.call_rooms.rooms[room["call_id"]].turn_expires_at = int(time.time()) - 1
    assert client.post(path, headers={"Origin": ORIGIN}, json={"role": "caller", "token": room["credentials"]["caller"]}).status_code == 410


def test_online_stream_requires_cookie_but_signaling_uses_invitation(client):
    headers = sign_in(client)
    room = create_room(client, headers)
    session_id = client.post("/v1/session", headers=headers, json={}).json()["session_id"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/stream/{session_id}", headers={"Origin": ORIGIN}):
            pass
    with client.websocket_connect(f"/v1/calls/{room['call_id']}/signal", headers={"Origin": ORIGIN}) as caller:
        caller.send_json({"role": "caller", "token": room["credentials"]["caller"]})
        assert caller.receive_json()["type"] == "joined"
    with client.websocket_connect(f"/v1/stream/{session_id}", headers=headers) as ws:
        ws.send_text("end")
        assert ws.receive_json()["type"] == "verdict"


@pytest.mark.parametrize(("name", "value"), [
    ("VIF_ONLINE", "true"), ("VIF_PUBLIC_ORIGIN", "http://demo.example.test"),
    ("VIF_PUBLIC_ORIGIN", ORIGIN + "/path"), ("VIF_PUBLIC_ORIGIN", "https://user:pass@demo.example.test"),
    ("VIF_TURN_HOST", "https://turn.example.test"), ("VIF_TURN_SECRET", "short"),
    ("VIF_TURN_PORT", "0"), ("VIF_TURN_TLS_PORT", "invalid"),
])
def test_online_configuration_fails_closed(client, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        OnlineConfig.from_env()


def test_explicit_https_default_port_is_normalized(client, monkeypatch):
    monkeypatch.setenv("VIF_PUBLIC_ORIGIN", "https://DEMO.example.test:443/")
    assert OnlineConfig.from_env().public_origin == ORIGIN
