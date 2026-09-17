"""Serve the built demo and /api from one loopback port for an HTTPS tunnel."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles


class DemoSite:
    def __init__(self, api, directory):
        self.api = api
        access = api.state.online_access
        if not access.config.enabled:
            raise ValueError("Serving the public demo requires VIF_ONLINE=1")
        self.directory = Path(directory).resolve()
        if not (self.directory / "index.html").is_file():
            raise ValueError("Frontend build missing. Run npm run build first.")
        self.static = StaticFiles(directory=self.directory, follow_symlink=False)
        self.host = urlsplit(access.config.public_origin).netloc

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.api(scope, receive, send)
        is_ws = scope["type"] == "websocket"
        host = Headers(scope=scope).get("host", "")
        if host not in {self.host, "127.0.0.1:8000", "localhost:8000"}:
            if is_ws:
                return await send({"type": "websocket.close", "code": 1008})
            return await Response("Unexpected host", 400)(scope, receive, send)

        async def secured(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"x-content-type-options", b"nosniff"),
                                (b"referrer-policy", b"no-referrer"),
                                (b"x-frame-options", b"DENY"),
                                (b"permissions-policy", b"camera=(), microphone=(self)")])
                if not any(key.lower() == b"cache-control" for key, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        path = scope["path"]
        if path.startswith("/api/"):
            # Same prefix stripping as the VPS Nginx configuration. Cookie
            # Path=/api and the exact public Origin checks stay on the API.
            api_scope = {**scope, "path": path[4:],
                         "raw_path": scope.get("raw_path", path.encode())[4:]}
            received = 0

            async def bounded_receive():
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > 1024 * 1024:
                        raise HTTPException(413, "Request body too large")
                return message

            return await self.api(api_scope, bounded_receive, secured)
        if is_ws:
            return await send({"type": "websocket.close", "code": 1008})
        if scope["method"] not in ("GET", "HEAD"):
            return await Response(status_code=405)(scope, receive, secured)
        if path in ("/", "/index.html") or re.fullmatch(r"/join/[A-Za-z0-9_-]+/?", path):
            return await FileResponse(self.directory / "index.html")(scope, receive, secured)
        if path.startswith("/assets/") and not any(part.startswith(".") for part in path.split("/") if part):
            try:
                return await self.static(scope, receive, secured)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
        return await JSONResponse({"detail": "Not found"}, 404)(scope, receive, secured)
