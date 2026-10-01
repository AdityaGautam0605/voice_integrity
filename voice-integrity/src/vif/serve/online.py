"""Online-demo access control and short-lived coturn credentials.

Kept separate from the detector and offline API. Public requests enter through
Nginx; only the configured browser origin may mutate state or open sockets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

COOKIE_NAME = "vif_operator"
SESSION_TTL_S = 8 * 60 * 60
TURN_TTL_S = 2 * 60 * 60
ROOM_PATH = r"/v1/calls/[A-Za-z0-9_-]+"


@dataclass(frozen=True)
class OnlineConfig:
    enabled: bool = False
    public_origin: str | None = None
    turn_host: str = ""
    turn_secret: str = field(default="", repr=False)
    turn_port: int = 3478
    turn_tls_port: int = 5349
    turn_provider: str = "coturn"
    metered_domain: str = ""
    metered_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        mode = os.environ.get("VIF_ONLINE", "0")
        if mode not in ("0", "1"):
            raise ValueError("VIF_ONLINE must be 0 or 1")
        if mode == "0":
            return cls()
        origin = os.environ.get("VIF_PUBLIC_ORIGIN", "")
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment or parsed.port == 0):
                raise ValueError
        except ValueError:
            raise ValueError("VIF_PUBLIC_ORIGIN must be an HTTPS origin without a path") from None
        public_host = parsed.hostname.lower()
        if ":" in public_host:
            public_host = f"[{public_host}]"
        authority = public_host if parsed.port in (None, 443) else f"{public_host}:{parsed.port}"
        public_origin = f"https://{authority}"
        provider = os.environ.get("VIF_TURN_PROVIDER", "coturn")
        if provider == "metered":
            domain = os.environ.get("VIF_METERED_DOMAIN", "").lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.metered\.live", domain):
                raise ValueError("VIF_METERED_DOMAIN must be your appname.metered.live hostname")
            key = os.environ.get("VIF_METERED_API_KEY", "")
            if not 16 <= len(key) <= 512 or key.startswith("replace-") or any(c.isspace() for c in key):
                raise ValueError("VIF_METERED_API_KEY must be the API key of a TURN credential")
            return cls(True, public_origin, turn_provider=provider,
                       metered_domain=domain, metered_api_key=key)
        if provider != "coturn":
            raise ValueError("VIF_TURN_PROVIDER must be coturn or metered")
        host = os.environ.get("VIF_TURN_HOST", "")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("VIF_TURN_HOST must be a DNS hostname or IPv4 address")
        secret = os.environ.get("VIF_TURN_SECRET", "")
        if len(secret) < 32:
            raise ValueError("VIF_TURN_SECRET must contain at least 32 characters")
        ports = []
        for name, default in (("VIF_TURN_PORT", "3478"), ("VIF_TURN_TLS_PORT", "5349")):
            try:
                port = int(os.environ.get(name, default))
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{name} must be a port between 1 and 65535") from None
            ports.append(port)
        return cls(True, public_origin, host, secret, *ports)

    def ice(self, call_id: str, role: str, expires_at: int, force_relay: bool) -> dict:
        result = {
            "online_mode": self.enabled, "public_origin": self.public_origin,
            "iceServers": [], "iceTransportPolicy": "all", "expires_at": None,
        }
        if not self.enabled:
            return result
        # The expiry stays fixed for the room: repeated requests cannot mint
        # unlimited distinct TURN users to evade a per-user allocation quota.
        username = f"{expires_at}:{call_id}:{role}"
        credential = base64.b64encode(hmac.new(
            self.turn_secret.encode(), username.encode(), hashlib.sha1,
        ).digest()).decode()
        result.update({
            "iceTransportPolicy": "relay" if force_relay else "all",
            "expires_at": expires_at,
            "iceServers": [
                {"urls": [f"stun:{self.turn_host}:{self.turn_port}"]},
                {"urls": [
                    f"turn:{self.turn_host}:{self.turn_port}?transport=udp",
                    f"turn:{self.turn_host}:{self.turn_port}?transport=tcp",
                    f"turns:{self.turn_host}:{self.turn_tls_port}?transport=tcp",
                ], "username": username, "credential": credential},
            ],
        })
        return result


class OnlineAccess:
    def __init__(self, config: OnlineConfig):
        self.config = config
        self.sessions: dict[str, float] = {}
        self.attempts: dict[str, deque] = {}
        self.global_attempts: deque = deque()
        self.code_digest = b""
        if config.enabled:
            code = os.environ.get("VIF_OPERATOR_CODE")
            if not code:
                code = secrets.token_urlsafe(18)
                print(f"Online operator sign-in code: {code}", file=sys.stderr)
            if len(code) < 16:
                raise ValueError("VIF_OPERATOR_CODE must contain at least 16 characters")
            self.code_digest = hashlib.sha256(code.encode()).digest()

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def authenticated(self, connection: HTTPConnection) -> bool:
        if not self.config.enabled:
            return True
        now = time.monotonic()
        self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}
        cookie = connection.cookies.get(COOKIE_NAME, "")
        return len(cookie) <= 128 and self.digest(cookie) in self.sessions

    def valid_origin(self, connection: HTTPConnection) -> bool:
        return connection.headers.get("origin") == self.config.public_origin

    def login(self, connection: HTTPConnection, code: str) -> str:
        now = time.monotonic()
        for address, history in list(self.attempts.items()):
            while history and history[0] <= now - 60:
                history.popleft()
            if not history:
                self.attempts.pop(address, None)
        while self.global_attempts and self.global_attempts[0] <= now - 60:
            self.global_attempts.popleft()
        address = connection.client.host if connection.client else "unknown"
        history = self.attempts.get(address, deque())
        if len(history) >= 5 or len(self.global_attempts) >= 30:
            raise HTTPException(429, "Too many sign-in attempts. Try again in a minute.",
                                headers={"Retry-After": "60"})
        self.attempts[address] = history
        history.append(now)
        self.global_attempts.append(now)
        if not hmac.compare_digest(hashlib.sha256(code.encode()).digest(), self.code_digest):
            raise HTTPException(401, "Invalid operator code")
        self.authenticated(connection)  # prune expired sessions
        if len(self.sessions) >= 32:
            raise HTTPException(429, "Too many operator sessions. Sign out of an existing session.")
        token = secrets.token_urlsafe(32)
        self.sessions[self.digest(token)] = now + SESSION_TTL_S
        return token


class OnlineMiddleware:
    """Gate every API route by default, including newly added future routes."""

    def __init__(self, app, access: OnlineAccess):
        self.app = app
        self.access = access

    async def __call__(self, scope, receive, send):
        if not self.access.config.enabled or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        path = scope["path"]
        if scope["type"] == "websocket":
            signaling = re.fullmatch(ROOM_PATH + r"/signal", path)
            if not self.access.valid_origin(connection) or (not signaling and not self.access.authenticated(connection)):
                await send({"type": "websocket.close", "code": 1008})
                return
        else:
            method = scope["method"]
            if method not in ("GET", "HEAD") and not self.access.valid_origin(connection):
                return await JSONResponse({"detail": "Untrusted request origin"}, 403)(scope, receive, send)
            public = (method, path) in {
                ("GET", "/health"), ("GET", "/v1/operator"), ("POST", "/v1/operator/login"),
            } or (method == "POST" and re.fullmatch(ROOM_PATH + r"/ice", path))
            if not public and not self.access.authenticated(connection):
                return await JSONResponse({"detail": "Operator sign-in required"}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)


class LoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)


def register_online_routes(app) -> OnlineAccess:
    access = OnlineAccess(OnlineConfig.from_env())
    app.add_middleware(OnlineMiddleware, access=access)

    @app.get("/v1/operator")
    async def operator_status(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"online_mode": access.config.enabled, "authenticated": access.authenticated(request)}

    @app.post("/v1/operator/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        if not access.config.enabled:
            raise HTTPException(404, "Operator sign-in is only used in online mode")
        token = access.login(request, body.code)
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S,
                            httponly=True, secure=True, samesite="strict", path="/api")
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": True}

    @app.post("/v1/operator/logout")
    async def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE_NAME, "")
        access.sessions.pop(access.digest(token), None)
        response.delete_cookie(COOKIE_NAME, path="/api", httponly=True, secure=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return {"authenticated": False}

    return access
