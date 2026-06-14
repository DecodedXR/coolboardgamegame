"""Websocket connection handling, message dispatch, and broadcasting.

``GameServer`` is transport-aware (it holds the asyncio event loop and the live
websockets) but leans on :mod:`server.rooms` for all room/host logic. The same
``GameServer`` instance is reused by the headless tests, which feed it fake
connection objects, so this module avoids importing ``websockets`` directly and
treats a connection as "any object with an async ``send(str)``".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from shared import protocol
from config import DISCONNECT_GRACE_SECONDS
from server.rooms import Room, RoomManager


@dataclass
class ConnCtx:
    """Per-connection state: which room/player this socket is bound to."""

    conn: Any
    room: Optional[Room] = field(default=None)
    player_id: Optional[str] = field(default=None)


# A handler takes the connection context and the decoded message.
Handler = Callable[["GameServer", ConnCtx, dict[str, Any]], Awaitable[None]]


class GameServer:
    def __init__(self) -> None:
        self.rooms = RoomManager()
        self._grace_tasks: dict[str, asyncio.Task] = {}
        self._dispatch: dict[str, Handler] = {
            protocol.C_CREATE_ROOM: GameServer._on_create_room,
            protocol.C_JOIN_ROOM: GameServer._on_join_room,
            protocol.C_SET_READY: GameServer._on_set_ready,
            protocol.C_SET_HOST_MODE: GameServer._on_set_host_mode,
            protocol.C_TRANSFER_HOST: GameServer._on_transfer_host,
            protocol.C_START_GAME: GameServer._on_start_game,
            protocol.C_LEAVE_ROOM: GameServer._on_leave_room,
            protocol.C_PING: GameServer._on_ping,
        }

    # --- lifecycle: one coroutine per connection --------------------------

    async def serve_connection(self, conn: Any) -> None:
        """Drive a single connection until it closes. Pass any object that is
        async-iterable (yields raw frames) and has an async ``send``."""
        ctx = ConnCtx(conn=conn)
        try:
            async for raw in conn:
                await self._handle_raw(ctx, raw)
        except Exception:
            # Transport errors end the connection; cleanup happens in finally.
            pass
        finally:
            await self._on_disconnect(ctx)

    async def _handle_raw(self, ctx: ConnCtx, raw: Any) -> None:
        try:
            msg = protocol.decode(raw)
        except ValueError as exc:
            await self._send_error(ctx.conn, protocol.ERR_BAD_MESSAGE, str(exc))
            return
        handler = self._dispatch.get(msg["type"])
        if handler is None:
            await self._send_error(ctx.conn, protocol.ERR_BAD_MESSAGE, f"unknown type {msg['type']!r}")
            return
        await handler(self, ctx, msg)

    # --- send helpers -----------------------------------------------------

    async def _safe_send(self, conn: Any, data: str) -> None:
        try:
            await conn.send(data)
        except Exception:
            pass  # a dead socket is cleaned up by its own serve_connection loop

    async def _send(self, conn: Any, msg_type: str, **payload: Any) -> None:
        await self._safe_send(conn, protocol.encode(msg_type, **payload))

    async def _send_error(self, conn: Any, code: str, message: str) -> None:
        await self._send(conn, protocol.S_ERROR, code=code, message=message)

    async def _broadcast(self, room: Room) -> None:
        state = protocol.encode(protocol.S_ROOM_UPDATE, room=room.public())
        await asyncio.gather(
            *(self._safe_send(p.conn, state) for p in room.players.values() if p.connected),
            return_exceptions=True,
        )

    def _you(self, room: Room, player_id: str) -> dict[str, Any]:
        player = room.players[player_id]
        return {
            "id": player.id,
            "name": player.name,
            "is_owner": room.owner_id == player_id,
            "is_host": room.host_id == player_id,
        }

    # --- message handlers -------------------------------------------------

    async def _on_create_room(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        if ctx.room is not None:
            await self._send_error(ctx.conn, protocol.ERR_ALREADY_IN_ROOM, "leave your current room first")
            return
        name = _clean_name(msg.get("name"))
        mode = msg.get("host_mode")
        if mode not in protocol.HOST_MODES:
            await self._send_error(ctx.conn, protocol.ERR_BAD_HOST_MODE, f"host_mode must be one of {protocol.HOST_MODES}")
            return
        room = self.rooms.create_room(mode)
        player = room.add_player(name, ctx.conn)
        ctx.room, ctx.player_id = room, player.id
        await self._send(ctx.conn, protocol.S_ROOM_CREATED, code=room.code, you=self._you(room, player.id), room=room.public())

    async def _on_join_room(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        if ctx.room is not None:
            await self._send_error(ctx.conn, protocol.ERR_ALREADY_IN_ROOM, "leave your current room first")
            return
        code = str(msg.get("code", "")).strip().upper()
        room = self.rooms.get(code)
        if room is None:
            await self._send_error(ctx.conn, protocol.ERR_ROOM_NOT_FOUND, f"no room {code!r}")
            return
        if room.in_game:
            await self._send_error(ctx.conn, protocol.ERR_GAME_IN_PROGRESS, "game already started")
            return
        if room.is_full:
            await self._send_error(ctx.conn, protocol.ERR_ROOM_FULL, "room is full")
            return
        player = room.add_player(_clean_name(msg.get("name")), ctx.conn)
        ctx.room, ctx.player_id = room, player.id
        await self._send(ctx.conn, protocol.S_ROOM_JOINED, code=room.code, you=self._you(room, player.id), room=room.public())
        await self._broadcast(room)

    async def _on_set_ready(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player = self._require_membership(ctx)
        if room is None:
            await self._send_error(ctx.conn, protocol.ERR_NOT_IN_ROOM, "join a room first")
            return
        player.ready = bool(msg.get("ready"))
        await self._broadcast(room)

    async def _on_set_host_mode(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player = self._require_membership(ctx)
        if room is None:
            await self._send_error(ctx.conn, protocol.ERR_NOT_IN_ROOM, "join a room first")
            return
        if player.id != room.owner_id:
            await self._send_error(ctx.conn, protocol.ERR_NOT_HOST, "only the room owner can change host mode")
            return
        mode = msg.get("mode")
        if mode not in protocol.HOST_MODES:
            await self._send_error(ctx.conn, protocol.ERR_BAD_HOST_MODE, f"host_mode must be one of {protocol.HOST_MODES}")
            return
        room.set_host_mode(mode)
        await self._broadcast(room)

    async def _on_transfer_host(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player = self._require_membership(ctx)
        if room is None:
            await self._send_error(ctx.conn, protocol.ERR_NOT_IN_ROOM, "join a room first")
            return
        if player.id != room.host_id:
            await self._send_error(ctx.conn, protocol.ERR_NOT_HOST, "only the current host can transfer the host role")
            return
        if not room.transfer_host(str(msg.get("target_id", ""))):
            await self._send_error(ctx.conn, protocol.ERR_BAD_TARGET, "target must be a player in this room (human host mode)")
            return
        await self._broadcast(room)

    async def _on_start_game(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player = self._require_membership(ctx)
        if room is None:
            await self._send_error(ctx.conn, protocol.ERR_NOT_IN_ROOM, "join a room first")
            return
        if not room.can_start(player.id):
            await self._send_error(ctx.conn, protocol.ERR_NOT_HOST, "you are not allowed to start the game")
            return
        room.in_game = True
        # Stub for milestone 1: the minigame module plugs in here next.
        started = protocol.encode(protocol.S_GAME_STARTED)
        await asyncio.gather(
            *(self._safe_send(p.conn, started) for p in room.players.values() if p.connected),
            return_exceptions=True,
        )
        await self._broadcast(room)

    async def _on_leave_room(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        await self._leave(ctx)

    async def _on_ping(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        await self._send(ctx.conn, protocol.S_PONG)

    # --- membership / disconnect plumbing ---------------------------------

    def _require_membership(self, ctx: ConnCtx):
        if ctx.room is None or ctx.player_id is None:
            return None, None
        player = ctx.room.players.get(ctx.player_id)
        if player is None:
            return None, None
        return ctx.room, player

    async def _leave(self, ctx: ConnCtx) -> None:
        """Explicit, immediate departure (no grace period)."""
        room, player = self._require_membership(ctx)
        ctx.room, ctx.player_id = None, None
        if room is None:
            return
        room.remove_player(player.id)
        if room.is_empty:
            self.rooms.discard_if_empty(room.code)
        else:
            await self._broadcast(room)

    async def _on_disconnect(self, ctx: ConnCtx) -> None:
        """Transport dropped: keep the slot for a grace window, then remove it."""
        room, player = self._require_membership(ctx)
        if room is None:
            return
        room.mark_disconnected(player.id)
        await self._broadcast(room)
        pid, code = player.id, room.code
        self._grace_tasks[pid] = asyncio.ensure_future(self._expire_slot(code, pid))

    async def _expire_slot(self, code: str, player_id: str) -> None:
        try:
            await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            self._grace_tasks.pop(player_id, None)
        room = self.rooms.get(code)
        if room is None:
            return
        player = room.players.get(player_id)
        if player is None or player.connected:
            return  # already gone, or reconnected (future milestone)
        room.remove_player(player_id)
        if room.is_empty:
            self.rooms.discard_if_empty(code)
        else:
            await self._broadcast(room)


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        return "Anonymous"
    return name[:24]
