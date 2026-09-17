"""Two-party signaling only. Call audio never passes through this service."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Header, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from vif.serve.metered import MeteredIce
from vif.serve.online import TURN_TTL_S, OnlineConfig

ROOM_TTL_S = 600


@dataclass
class Room:
    call_id: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    credentials: dict[str, str] = field(default_factory=lambda: {
        "operator": secrets.token_urlsafe(32), "caller": secrets.token_urlsafe(32),
    })
    created: float = field(default_factory=time.monotonic)
    peers: dict[str, WebSocket] = field(default_factory=dict)
    joined: bool = False
    closed: bool = False
    force_relay: bool = False
    turn_expires_at: int = field(default_factory=lambda: int(time.time()) + TURN_TTL_S)


class CallRooms:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.pending_signals = 0

    async def participant(self, call_id: str, role: str, token: str) -> Room:
        await self.prune()
        room = self.rooms.get(call_id)
        if (room is None or room.closed or role not in ("operator", "caller")
                or not secrets.compare_digest(token.encode(), room.credentials[role].encode())):
            raise HTTPException(403, "Invalid or expired invitation")
        return room

    async def end(self, room: Room, reason: str = "Call ended") -> None:
        if room.closed:
            return
        room.closed = True
        self.rooms.pop(room.call_id, None)
        for peer in list(room.peers.values()):
            with contextlib.suppress(Exception):
                await peer.send_json({"type": "hangup", "reason": reason})
                await peer.close()

    async def prune(self) -> None:
        for room in list(self.rooms.values()):
            if not room.joined and time.monotonic() - room.created >= ROOM_TTL_S:
                await self.end(room, "Invitation expired. Create a new call.")

    async def sweep(self) -> None:
        while True:
            await asyncio.sleep(15)
            await self.prune()

    async def close(self) -> None:
        for room in list(self.rooms.values()):
            # Shutdown signaling without a hangup: an established peer call
            # continues even when the analysis/signaling process is stopped.
            for peer in list(room.peers.values()):
                with contextlib.suppress(Exception):
                    await peer.close(code=1012)
        self.rooms.clear()


class CreateCall(BaseModel):
    force_relay: bool = False


class IceRequest(BaseModel):
    role: str = Field(max_length=16)
    token: str = Field(min_length=1, max_length=128)


def register_call_routes(app, authorize, online: OnlineConfig | None = None) -> CallRooms:
    rooms = CallRooms()
    online = online or OnlineConfig()
    metered = MeteredIce(online) if online.turn_provider == "metered" else None

    @app.post("/v1/calls")
    async def create_call(body: CreateCall | None = None, authorization: str | None = Header(default=None)):
        authorize(authorization)
        await rooms.prune()
        force_relay = bool(body and body.force_relay)
        if force_relay and not online.enabled:
            raise HTTPException(422, "Force relay is only available in online mode")
        if len(rooms.rooms) >= 32:
            raise HTTPException(429, "Too many call rooms. End an existing call.")
        room = Room(force_relay=force_relay)
        rooms.rooms[room.call_id] = room
        return {"call_id": room.call_id, "credentials": room.credentials,
                "expires_in": ROOM_TTL_S, "online_mode": online.enabled,
                "public_origin": online.public_origin}

    @app.post("/v1/calls/{call_id}/ice")
    async def ice(call_id: str, body: IceRequest, response: Response):
        room = await rooms.participant(call_id, body.role, body.token)
        if online.enabled and room.turn_expires_at <= time.time():
            raise HTTPException(410, "Relay credentials expired. Create a new call.")
        response.headers["Cache-Control"] = "no-store"
        if metered is not None:
            return await metered.ice(room.force_relay)
        return online.ice(call_id, body.role, room.turn_expires_at, room.force_relay)

    @app.websocket("/v1/calls/{call_id}/signal")
    async def signal(ws: WebSocket, call_id: str):
        await ws.accept()
        if rooms.pending_signals >= 64:
            await ws.close(code=1013)
            return
        rooms.pending_signals += 1
        awaiting_auth = True
        room = None
        role = None
        attached = False
        try:
            # Credentials are sent in the first frame, never in access-log URLs.
            hello = await asyncio.wait_for(ws.receive_json(), 10)
            role = hello.get("role") if isinstance(hello, dict) else None
            token = hello.get("token") if isinstance(hello, dict) else None
            try:
                if not isinstance(token, str) or len(token) > 128:
                    raise HTTPException(403, "Invalid or expired invitation")
                room = await rooms.participant(call_id, role, token)
            except HTTPException:
                await ws.send_json({"type": "error", "detail": "Invalid or expired invitation"})
                await ws.close(code=1008)
                return
            rooms.pending_signals -= 1
            awaiting_auth = False
            if role in room.peers:
                await ws.send_json({"type": "error", "detail": "This role is already connected"})
                await ws.close(code=1008)
                return
            room.peers[role] = ws
            attached = True
            await ws.send_json({"type": "joined", "role": role})
            if len(room.peers) == 2:
                room.joined = True
                for peer in room.peers.values():
                    await peer.send_json({"type": "ready"})
            while not room.closed:
                message = await ws.receive_json()
                if not isinstance(message, dict):
                    raise ValueError("Expected signaling object")
                kind = message.get("type")
                if kind == "hangup":
                    await rooms.end(room)
                    return
                if kind not in ("offer", "answer", "ice"):
                    raise ValueError("Unknown signaling message")
                if kind == "offer" and role != "operator":
                    raise ValueError("Only the operator may offer")
                if kind == "answer" and role != "caller":
                    raise ValueError("Only the caller may answer")
                other = room.peers.get("caller" if role == "operator" else "operator")
                if other is not None:
                    await other.send_json({"type": kind, "data": message.get("data")})
        except WebSocketDisconnect:
            pass
        except (ValueError, TimeoutError):
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "detail": "Invalid signaling message"})
                await ws.close(code=1008)
        finally:
            if awaiting_auth:
                rooms.pending_signals -= 1
            if attached and room is not None:
                room.peers.pop(role, None)
                if not room.joined:
                    await rooms.end(room, "Invitation cancelled")
                elif not room.peers:
                    rooms.rooms.pop(room.call_id, None)
                # A lost signaling connection alone must not end peer audio.

    return rooms
