import asyncio
import io
import json
from urllib.error import HTTPError, URLError

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_online import CODE, ORIGIN, create_room, sign_in
from test_online import client as client

from vif.serve import metered, server
from vif.serve.online import OnlineConfig
from vif.serve.site import DemoSite

SERVERS = [{"urls": "stun:stun.relay.metered.ca:80"},
           {"urls": ["turn:global.relay.metered.ca:80", "turns:global.relay.metered.ca:443?transport=tcp"],
            "username": "fixture-user", "credential": "fixture-password", "apiKey": "never-forward-this"}]


@pytest.fixture
def provider_env(monkeypatch):
    monkeypatch.setenv("VIF_ONLINE", "1")
    monkeypatch.setenv("VIF_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("VIF_TURN_PROVIDER", "metered")
    monkeypatch.setenv("VIF_METERED_DOMAIN", "demo.metered.live")
    monkeypatch.setenv("VIF_METERED_API_KEY", "fixture-key-with-enough-characters")
    monkeypatch.delenv("VIF_TURN_SECRET", raising=False)
    return OnlineConfig.from_env()


def test_metered_configuration_requires_its_own_key(provider_env, monkeypatch):
    assert provider_env.turn_provider == "metered"
    assert provider_env.turn_secret == ""
    assert provider_env.metered_api_key not in repr(provider_env)
    for name, value in (("VIF_METERED_DOMAIN", "demo.metered.live.evil.test"),
                        ("VIF_METERED_API_KEY", ""), ("VIF_TURN_PROVIDER", "unknown")):
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            with pytest.raises(ValueError, match=name):
                OnlineConfig.from_env()


def test_metered_response_is_validated_and_secret_metadata_removed():
    clean = metered.validate_servers(SERVERS)
    assert clean[1]["credential"] == "fixture-password"
    assert "apiKey" not in clean[1]
    for bad in ({}, [], SERVERS[:1], [{"urls": "https://attacker.test"}],
                [{"urls": "turn:relay.example.test", "username": "user"}], [None]):
        with pytest.raises(ValueError):
            metered.validate_servers(bad)


def test_metered_http_uses_fixed_endpoint_and_bounded_response(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://demo.metered.live/api/v1/turn/credentials?apiKey=a%26b'
            assert timeout == 10
            return io.BytesIO(json.dumps(SERVERS).encode())
    monkeypatch.setattr(metered, "build_opener", lambda *_: Opener())
    assert metered.fetch_servers("demo.metered.live", "a&b")[1]["username"] == "fixture-user"
    assert metered.NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.test") is None


@pytest.mark.parametrize("error", [HTTPError("https://secret-url?apiKey=hidden", code, "hidden", {}, None)
                                   for code in (302, 401, 403, 429, 500)] + [URLError("hidden")])
def test_metered_failures_never_expose_the_secret_url(monkeypatch, error):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise error
    monkeypatch.setattr(metered, "build_opener", lambda *_: Opener())
    with pytest.raises(HTTPException) as rejected:
        metered.fetch_servers("demo.metered.live", "hidden")
    assert rejected.value.status_code == 503
    assert "hidden" not in rejected.value.detail


@pytest.mark.asyncio
async def test_metered_cache_coalesces_requests_and_retries_after_failure(provider_env, monkeypatch):
    requests = []
    def fetch(*_):
        requests.append(True)
        return metered.validate_servers(SERVERS)
    monkeypatch.setattr(metered, "fetch_servers", fetch)
    provider = metered.MeteredIce(provider_env)
    results = await asyncio.gather(provider.ice(True), provider.ice(False))
    assert len(requests) == 1
    assert results[0]["iceTransportPolicy"] == "relay"
    assert results[1]["iceTransportPolicy"] == "all"
    assert results[0]["expires_at"] is None
    results[0]["iceServers"].clear()
    assert (await provider.ice(False))["iceServers"]
    def fail(*_):
        requests.append(True)
        raise HTTPException(503, "unavailable")
    monkeypatch.setattr(metered, "fetch_servers", fail)
    provider.valid_until = 0
    for _ in range(2):
        with pytest.raises(HTTPException):
            await provider.ice(False)
    assert len(requests) == 2
    provider.retry_after = 0
    monkeypatch.setattr(metered, "fetch_servers", fetch)
    await provider.ice(False)
    assert len(requests) == 3


def test_metered_ice_still_requires_room_membership(client, monkeypatch):
    monkeypatch.setenv("VIF_TURN_PROVIDER", "metered")
    monkeypatch.setenv("VIF_METERED_DOMAIN", "demo.metered.live")
    monkeypatch.setenv("VIF_METERED_API_KEY", "fixture-key-with-enough-characters")
    requests = []
    def fetch(*_):
        requests.append(True)
        return metered.validate_servers(SERVERS)
    monkeypatch.setattr(metered, "fetch_servers", fetch)
    app = server.create_app()
    app.router.lifespan_context = client.app.router.lifespan_context
    with TestClient(app) as connection:
        room = create_room(connection, sign_in(connection), True)
        path = f"/v1/calls/{room['call_id']}/ice"
        response = connection.post(path, headers={"Origin": ORIGIN}, json={"role": "caller", "token": "wrong"})
        assert response.status_code == 403
        assert requests == []
        response = connection.post(path, headers={"Origin": ORIGIN}, json={"role": "caller", "token": room["credentials"]["caller"]})
        assert response.status_code == 200
        assert response.json()["iceTransportPolicy"] == "relay"
        assert "fixture-key-with-enough-characters" not in response.text
        assert len(requests) == 1


def test_public_site_routes_assets_cookie_auth_websockets_and_private_files(client, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>Demo</html>")
    (dist / "assets/app.js").write_text("// built app")
    (tmp_path / "private.key").write_text("private-content")
    (dist / "assets/leak.js").symlink_to(tmp_path / "private.key")
    with TestClient(DemoSite(client.app, dist), base_url=ORIGIN) as site:
        for path in ("/", "/join/room-token", "/assets/app.js"):
            response = site.get(path)
            assert response.status_code == 200
            assert response.headers["Referrer-Policy"] == "no-referrer"
        for path in ("/v1/audit", "/health", "/.env.tunnel", "/voice-integrity/keys/private.key",
                     "/assets/leak.js", "/assets/missing.js", "/join/room-token/extra"):
            assert site.get(path).status_code == 404
        assert site.get("/", headers={"Host": "evil.example"}).status_code == 400
        assert site.get("/api/health").status_code == 200
        assert site.get("/api/v1/audit").status_code == 401
        assert site.post("/api/v1/operator/login", headers={"Origin": ORIGIN}, content=b"x" * (1024 * 1024 + 1)).status_code == 413
        assert site.post("/api/v1/operator/login", headers={"Origin": ORIGIN}, json={"code": CODE}).status_code == 200
        assert site.get("/api/v1/audit").status_code == 200  # Real /api cookie path.
        assert site.post("/api/v1/calls", json={}).status_code == 403  # Still checks Origin.
        room = site.post("/api/v1/calls", headers={"Origin": ORIGIN}, json={}).json()
        ws_origin = ORIGIN.replace("https:", "wss:")
        with site.websocket_connect(f"{ws_origin}/api/v1/calls/{room['call_id']}/signal", headers={"Origin": ORIGIN}) as ws:
            ws.send_json({"role": "caller", "token": room["credentials"]["caller"]})
            assert ws.receive_json()["type"] == "joined"
        with pytest.raises(WebSocketDisconnect):
            with site.websocket_connect(f"{ws_origin}/api/v1/stream/private", headers={"Origin": "https://evil.test"}):
                pass
