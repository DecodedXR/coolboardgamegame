"""Websocket connection handling, message dispatch, and broadcasting.

``GameServer`` is transport-aware (it holds the asyncio event loop and the live
websockets) but leans on :mod:`server.rooms` for all room/host logic. The same
``GameServer`` instance is reused by the headless tests, which feed it fake
connection objects, so this module avoids importing ``websockets`` directly and
treats a connection as "any object with an async ``send(str)``".
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from shared import protocol
from config import (
    DISCONNECT_GRACE_SECONDS,
    WAO_ANSWER_SECONDS,
    WAO_MAX_ANSWER_LEN,
    WAO_MIN_CONTESTANTS,
    WAO_POINTS_PER_VOTE,
    WAO_REVEAL_SECONDS,
    WAO_TOTAL_ROUNDS,
    WAO_VOTE_SECONDS,
)
from server.rooms import Room, RoomManager
from server.games.prompts import WRONG_ANSWERS_PROMPTS
from server.games.wrong_answers import WrongAnswersGame

# Auto-host budget for each phase, in seconds. ``None`` phases are terminal.
_PHASE_SECONDS = {
    protocol.PHASE_PROMPT: WAO_ANSWER_SECONDS,
    protocol.PHASE_VOTE: WAO_VOTE_SECONDS,
    protocol.PHASE_REVEAL: WAO_REVEAL_SECONDS,
}


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
        # Active minigames + their auto-mode phase timers, keyed by room code.
        self.games: dict[str, WrongAnswersGame] = {}
        self._phase_tasks: dict[str, asyncio.Task] = {}
        self._dispatch: dict[str, Handler] = {
            protocol.C_CREATE_ROOM: GameServer._on_create_room,
            protocol.C_JOIN_ROOM: GameServer._on_join_room,
            protocol.C_SET_READY: GameServer._on_set_ready,
            protocol.C_SET_HOST_MODE: GameServer._on_set_host_mode,
            protocol.C_TRANSFER_HOST: GameServer._on_transfer_host,
            protocol.C_START_GAME: GameServer._on_start_game,
            protocol.C_LEAVE_ROOM: GameServer._on_leave_room,
            protocol.C_PING: GameServer._on_ping,
            protocol.C_SUBMIT_ANSWER: GameServer._on_submit_answer,
            protocol.C_SUBMIT_VOTE: GameServer._on_submit_vote,
            protocol.C_ADVANCE_PHASE: GameServer._on_advance_phase,
            protocol.C_RETURN_TO_LOBBY: GameServer._on_return_to_lobby,
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
        if room.in_game:
            await self._send_error(ctx.conn, protocol.ERR_GAME_IN_PROGRESS, "a game is already running")
            return

        # Contestants = connected players, minus the human host (who runs the show).
        contestants = [
            (p.id, p.name)
            for p in room.players.values()
            if p.connected and not (room.host_mode == protocol.HOST_HUMAN and p.id == room.host_id)
        ]
        if len(contestants) < WAO_MIN_CONTESTANTS:
            await self._send_error(
                ctx.conn,
                protocol.ERR_NOT_ENOUGH_PLAYERS,
                f"need at least {WAO_MIN_CONTESTANTS} contestants to play",
            )
            return

        prompts = random.sample(WRONG_ANSWERS_PROMPTS, WAO_TOTAL_ROUNDS)
        game = WrongAnswersGame(
            contestants,
            prompts,
            total_rounds=WAO_TOTAL_ROUNDS,
            points_per_vote=WAO_POINTS_PER_VOTE,
            max_answer_len=WAO_MAX_ANSWER_LEN,
        )
        self.games[room.code] = game
        room.in_game = True

        started = protocol.encode(protocol.S_GAME_STARTED)
        await asyncio.gather(
            *(self._safe_send(p.conn, started) for p in room.players.values() if p.connected),
            return_exceptions=True,
        )
        await self._broadcast(room)
        self._arm_timer(room, game)
        await self._broadcast_game(room, game)

    async def _on_submit_answer(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not game.submit_answer(player.id, msg.get("text")):
            reason, detail = self._reject_reason(game, player.id, protocol.PHASE_PROMPT, protocol.ERR_BAD_ANSWER)
            await self._send_error(ctx.conn, reason, detail)
            return
        await self._broadcast_game(room, game)
        await self._maybe_autoadvance(room, game)

    async def _on_submit_vote(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not game.submit_vote(player.id, msg.get("answer_id")):
            reason, detail = self._reject_reason(game, player.id, protocol.PHASE_VOTE, protocol.ERR_BAD_VOTE)
            await self._send_error(ctx.conn, reason, detail)
            return
        await self._broadcast_game(room, game)
        await self._maybe_autoadvance(room, game)

    async def _on_advance_phase(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not room.can_start(player.id):
            await self._send_error(ctx.conn, protocol.ERR_NOT_HOST, "only the show-runner can advance the game")
            return
        if game.is_over:
            return
        await self._advance(room, game)

    async def _on_return_to_lobby(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not room.can_start(player.id):
            await self._send_error(ctx.conn, protocol.ERR_NOT_HOST, "only the show-runner can end the game")
            return
        await self._end_game(room)

    async def _on_leave_room(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        await self._leave(ctx)

    # --- game driving -----------------------------------------------------

    def _require_game(self, ctx: ConnCtx):
        room, player = self._require_membership(ctx)
        if room is None:
            return None, None, None
        return room, player, self.games.get(room.code)

    async def _game_membership_error(self, ctx: ConnCtx, room: Optional[Room]) -> None:
        if room is None:
            await self._send_error(ctx.conn, protocol.ERR_NOT_IN_ROOM, "join a room first")
        else:
            await self._send_error(ctx.conn, protocol.ERR_NO_GAME, "no game is running")

    @staticmethod
    def _reject_reason(game: WrongAnswersGame, pid: str, want_phase: str, bad_input: str):
        """Translate a rejected submission into a specific error code."""
        if game.phase != want_phase:
            return protocol.ERR_WRONG_PHASE, f"not the {want_phase} phase"
        if not game.is_contestant(pid):
            return protocol.ERR_NOT_CONTESTANT, "you are not a contestant in this game"
        return bad_input, "submission was empty or invalid"

    async def _broadcast_game(self, room: Room, game: WrongAnswersGame) -> None:
        await asyncio.gather(
            *(
                self._safe_send(p.conn, protocol.encode(protocol.S_GAME_STATE, game=game.public(p.id, room.host_id)))
                for p in room.players.values()
                if p.connected
            ),
            return_exceptions=True,
        )

    async def _advance(self, room: Room, game: WrongAnswersGame) -> None:
        game.advance()
        self._arm_timer(room, game)
        await self._broadcast_game(room, game)

    async def _maybe_autoadvance(self, room: Room, game: WrongAnswersGame) -> None:
        """In auto-host mode, jump ahead the moment everyone connected has acted."""
        if room.host_mode != protocol.HOST_AUTO:
            return
        connected = {p.id for p in room.players.values() if p.connected}
        if game.all_submitted(connected):
            await self._advance(room, game)

    def _arm_timer(self, room: Room, game: WrongAnswersGame) -> None:
        """(Re)schedule the auto-mode phase deadline; clears it in human mode."""
        code = room.code
        self._cancel_timer(code)
        seconds = _PHASE_SECONDS.get(game.phase)
        if room.host_mode != protocol.HOST_AUTO or seconds is None:
            game.deadline = None
            return
        game.deadline = time.time() + seconds
        self._phase_tasks[code] = asyncio.ensure_future(self._phase_deadline(code, seconds))

    def _cancel_timer(self, code: str) -> None:
        task = self._phase_tasks.pop(code, None)
        if task is not None:
            task.cancel()

    async def _phase_deadline(self, code: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        finally:
            self._phase_tasks.pop(code, None)
        room = self.rooms.get(code)
        game = self.games.get(code)
        if room is not None and game is not None and not game.is_over:
            await self._advance(room, game)

    async def _end_game(self, room: Room) -> None:
        """Tear down the game and send everyone back to the lobby."""
        self._cancel_timer(room.code)
        self.games.pop(room.code, None)
        room.in_game = False
        for p in room.players.values():
            p.ready = False
        await asyncio.gather(
            *(self._safe_send(p.conn, protocol.encode(protocol.S_RETURN_TO_LOBBY)) for p in room.players.values() if p.connected),
            return_exceptions=True,
        )
        await self._broadcast(room)

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
            self._discard_room(room.code)
        else:
            await self._broadcast(room)
            await self._on_roster_change(room)

    async def _on_disconnect(self, ctx: ConnCtx) -> None:
        """Transport dropped: keep the slot for a grace window, then remove it."""
        room, player = self._require_membership(ctx)
        if room is None:
            return
        room.mark_disconnected(player.id)
        await self._broadcast(room)
        await self._on_roster_change(room)
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
            self._discard_room(code)
        else:
            await self._broadcast(room)
            await self._on_roster_change(room)

    async def _on_roster_change(self, room: Room) -> None:
        """A player left/dropped mid-game: refresh the per-player views (roles may
        have shifted) and let auto mode advance if the room is now all-acted."""
        game = self.games.get(room.code)
        if game is None or game.is_over:
            return
        await self._broadcast_game(room, game)
        await self._maybe_autoadvance(room, game)

    def _discard_room(self, code: str) -> None:
        self._cancel_timer(code)
        self.games.pop(code, None)
        self.rooms.discard_if_empty(code)


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        return "Anonymous"
    return name[:24]
