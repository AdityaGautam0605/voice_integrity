"""Fetch a pre-created Metered credential without exposing its API key.

Metered credentials need up to two minutes to propagate, so creation is an
operator setup step. Their expiry/revocation is controlled by Metered, not by
the room TTL. The same demo credential can be reused by both participants.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import HTTPException


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_servers(domain: str, api_key: str) -> list:
    # Fixed HTTPS provider suffix is validated by OnlineConfig; redirects must
    # not forward a secret query parameter to another host. Never log this URL.
    url = f"https://{domain}/api/v1/turn/credentials?{urlencode({'apiKey': api_key})}"
    try:
        request = Request(url, headers={"Accept": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("Oversized provider response")
            return validate_servers(json.loads(body))
    except HTTPError as error:
        if error.code in (401, 403):
            detail = "Metered rejected the TURN API key. Check the credential in .env.tunnel."
        elif error.code in (402, 429):
            detail = "Metered TURN usage or request limit reached. Check the free-plan allowance."
        else:
            detail = "Metered TURN is unavailable. Retry after checking the provider dashboard."
        raise HTTPException(503, detail) from None
    except (URLError, OSError, ValueError, TypeError):
        raise HTTPException(503, "Could not load Metered TURN servers. Check the app domain, credential and internet connection.") from None


def validate_servers(value) -> list:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("Expected an ICE server array")
    result = []
    has_turn = False
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Expected an ICE server object")
        urls = item.get("urls")
        urls = [urls] if isinstance(urls, str) else urls
        if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
            raise ValueError("Missing ICE server URLs")
        if any(not isinstance(url, str) or len(url) > 1024
               or not re.fullmatch(r"(?:stun|stuns|turn|turns):[^\s/]+", url) for url in urls):
            raise ValueError("Invalid ICE server URL")
        server = {"urls": urls}
        if any(url.startswith(("turn:", "turns:")) for url in urls):
            for key in ("username", "credential"):
                if not isinstance(item.get(key), str) or not 1 <= len(item[key]) <= 1024:
                    raise ValueError("Missing TURN authentication")
                server[key] = item[key]
            has_turn = True
        result.append(server)
    if not has_turn:
        raise ValueError("Provider returned no authenticated relay")
    return result


class MeteredIce:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()
        self.cached = None
        self.valid_until = 0.0
        self.retry_after = 0.0
        self.error = "Metered TURN is unavailable. Retry shortly."

    async def ice(self, force_relay: bool) -> dict:
        async with self.lock:
            if time.monotonic() < self.retry_after:
                raise HTTPException(503, self.error)
            if self.cached is None or time.monotonic() >= self.valid_until:
                try:
                    self.cached = await asyncio.to_thread(
                        fetch_servers, self.config.metered_domain, self.config.metered_api_key,
                    )
                except HTTPException as error:
                    self.error = error.detail
                    self.retry_after = time.monotonic() + 10
                    raise
                self.valid_until = time.monotonic() + 60
            return {"iceServers": copy.deepcopy(self.cached),
                    "iceTransportPolicy": "relay" if force_relay else "all",
                    "online_mode": True, "public_origin": self.config.public_origin,
                    # The provider does not return the credential's expiry.
                    "expires_at": None}
