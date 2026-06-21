"""Websocket connection handling, message dispatch, and broadcasting.

``GameServer`` is transport-aware (it holds the asyncio event loop and the live
websockets) but leans on :mod:`server.rooms` for all room/host logic. The same
``GameServer`` instance is reused by the headless tests, which feed it fake
connection objects, so this module avoids importing ``websockets`` directly and
treats a connection as "any object with an async ``send(str)``".

The in-game logic it drives is :class:`~server.games.snakes_and_ladders.SnakesAndLaddersGame`,
a *turn* game. A single :meth:`GameServer._drive` funnel — called after every
mutation — decides who moves the current turn forward: a bot or an absent human
is auto-played after a short delay; in auto-host mode a present human gets a
per-turn deadline; in human-host mode play parks until the host advances. ``_drive``
always cancels the prior timer first, so there is never more than one pending
mover per room (the guard against double-advance races).
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
    MAX_PLAYERS_PER_ROOM,
    SAL_BOARD_CELLS,
    SAL_BOARD_COLS,
    SAL_SNAKE_COUNT,
    SAL_LADDER_COUNT,
    SAL_SHOP_TILES,
    SAL_WHEEL_TILES,
    SAL_GOLD_TILES,
    SAL_DEBUFF_TILES,
    SAL_STARTING_GOLD,
    SAL_DICE_SIDES,
    SAL_EXACT_FINISH,
    SAL_PRICE_IMMUNITY,
    SAL_PRICE_BOOST,
    SAL_PRICE_DOUBLE,
    SAL_PRICE_REROLL,
    SAL_BOOST_BONUS,
    SAL_GOLD_TILE_AMOUNT,
    SAL_SLIP_BACK,
    SAL_GOLD_TAX,
    SAL_MIN_CONTESTANTS,
    SAL_ROLL_SECONDS,
    SAL_SHOP_SECONDS,
    SAL_BOT_DELAY_SECONDS,
)
from server.rooms import Room, RoomManager
from server.games.snakes_and_ladders import (
    AWAIT_ROLL,
    AWAIT_SHOP,
    SnakesAndLaddersGame,
)


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
        # Active minigames + their per-room turn timers (auto deadline / bot delay),
        # keyed by room code. At most one turn task per room (see ``_drive``).
        self.games: dict[str, SnakesAndLaddersGame] = {}
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
            protocol.C_ROLL_DICE: GameServer._on_roll_dice,
            protocol.C_USE_POWERUP: GameServer._on_use_powerup,
            protocol.C_BUY_ITEM: GameServer._on_buy_item,
            protocol.C_SKIP_SHOP: GameServer._on_skip_shop,
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

        # Human contestants = connected players, minus the human host (who runs the
        # show). Bots fill the remaining seats; they are NOT room players (they live
        # only in the game object), so they never collide with the room cap that
        # limits *connections* — instead total seats (humans + bots) are capped here.
        contestants = [
            (p.id, p.name)
            for p in room.players.values()
            if p.connected and not (room.host_mode == protocol.HOST_HUMAN and p.id == room.host_id)
        ]
        human_count = len(contestants)
        n_bots = _clamp_bots(msg.get("bots"), human_count)
        bots = [(f"bot{i + 1}", f"Bot {i + 1}") for i in range(n_bots)]

        if human_count + n_bots < SAL_MIN_CONTESTANTS:
            await self._send_error(
                ctx.conn,
                protocol.ERR_NOT_ENOUGH_PLAYERS,
                f"need at least {SAL_MIN_CONTESTANTS} players (humans + bots) to play",
            )
            return

        game = SnakesAndLaddersGame(
            contestants,
            bots=bots,
            cells=SAL_BOARD_CELLS,
            cols=SAL_BOARD_COLS,
            snake_count=SAL_SNAKE_COUNT,
            ladder_count=SAL_LADDER_COUNT,
            shop_count=SAL_SHOP_TILES,
            wheel_count=SAL_WHEEL_TILES,
            gold_count=SAL_GOLD_TILES,
            debuff_count=SAL_DEBUFF_TILES,
            starting_gold=SAL_STARTING_GOLD,
            dice_sides=SAL_DICE_SIDES,
            exact_finish=SAL_EXACT_FINISH,
            prices={
                "immunity": SAL_PRICE_IMMUNITY,
                "boost": SAL_PRICE_BOOST,
                "double": SAL_PRICE_DOUBLE,
                "reroll": SAL_PRICE_REROLL,
            },
            boost_bonus=SAL_BOOST_BONUS,
            gold_tile_amount=SAL_GOLD_TILE_AMOUNT,
            slip_back=SAL_SLIP_BACK,
            gold_tax=SAL_GOLD_TAX,
            rng=self._make_rng(),
        )
        self.games[room.code] = game
        room.in_game = True

        started = protocol.encode(protocol.S_GAME_STARTED)
        await asyncio.gather(
            *(self._safe_send(p.conn, started) for p in room.players.values() if p.connected),
            return_exceptions=True,
        )
        await self._broadcast(room)
        await self._after_turn_change(room, game)

    async def _on_roll_dice(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not game.roll(player.id):
            return await self._reject_turn(ctx, game, player.id, AWAIT_ROLL)
        await self._after_turn_change(room, game)

    async def _on_use_powerup(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not game.use_powerup(player.id, msg.get("item")):
            return await self._reject_turn(ctx, game, player.id, AWAIT_ROLL)
        # Arming a powerup does NOT pass the turn; re-drive (refresh deadline) + broadcast.
        await self._after_turn_change(room, game)

    async def _on_buy_item(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not game.buy_item(player.id, msg.get("item")):
            return await self._reject_turn(ctx, game, player.id, AWAIT_SHOP)
        await self._after_turn_change(room, game)

    async def _on_skip_shop(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not game.skip_shop(player.id):
            return await self._reject_turn(ctx, game, player.id, AWAIT_SHOP)
        await self._after_turn_change(room, game)

    async def _on_advance_phase(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
        room, player, game = self._require_game(ctx)
        if game is None:
            return await self._game_membership_error(ctx, room)
        if not room.can_start(player.id):
            await self._send_error(ctx.conn, protocol.ERR_NOT_HOST, "only the show-runner can advance the game")
            return
        if game.is_over:
            return
        # Host force-advance: resolve the current actor's pending action (roll, or
        # leave the shop), then re-arm for whoever is up next.
        game.advance()
        await self._after_turn_change(room, game)

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

    async def _reject_turn(self, ctx: ConnCtx, game: SnakesAndLaddersGame, pid: str, want: str) -> None:
        """Translate a rejected turn action into a specific error: wrong turn,
        wrong sub-state, or a bad/unavailable item."""
        if not game.is_current(pid):
            code, detail = protocol.ERR_NOT_YOUR_TURN, "it isn't your turn"
        elif game.awaiting != want:
            code, detail = protocol.ERR_WRONG_SUBSTATE, f"you can't do that while awaiting {game.awaiting!r}"
        else:
            code, detail = protocol.ERR_BAD_ITEM, "unknown, unaffordable, or unavailable item"
        await self._send_error(ctx.conn, code, detail)

    async def _broadcast_game(self, room: Room, game: SnakesAndLaddersGame) -> None:
        await asyncio.gather(
            *(
                self._safe_send(p.conn, protocol.encode(protocol.S_GAME_STATE, game=game.public(p.id, room.host_id)))
                for p in room.players.values()
                if p.connected
            ),
            return_exceptions=True,
        )

    async def _after_turn_change(self, room: Room, game: SnakesAndLaddersGame) -> None:
        """Re-arm the driver for the (possibly new) current actor, then broadcast the
        fresh per-player state (which carries the new deadline)."""
        self._drive(room, game)
        await self._broadcast_game(room, game)

    def _drive(self, room: Room, game: SnakesAndLaddersGame) -> None:
        """Schedule whatever should move the *current* actor's turn forward. Called
        after every mutation; idempotent and self-rescheduling. Always cancels any
        prior turn timer first, and arms the replacement with the current actor +
        ``seq`` so that even a superseded timer which still fires resolves nothing —
        there is never more than one *effective* mover per room (the guard against
        double-advance races).

          * bot, or an absent/disconnected human -> auto-play after a short delay
            (bots play in BOTH host modes; auto-playing an absent human keeps a
            mid-turn disconnect from deadlocking the game);
          * auto-host mode, present human  -> arm a per-turn deadline that auto-resolves;
          * human-host mode, present human -> park (no deadline); the host clicks NEXT.
        """
        code = room.code
        self._cancel_timer(code)
        if game.is_over:
            game.deadline = None
            return
        cur, seq = game.current_pid, game.seq
        if self._actor_needs_autoplay(room, game):
            game.deadline = None
            self._phase_tasks[code] = asyncio.ensure_future(self._auto_turn(code, cur, seq))
        elif room.host_mode == protocol.HOST_AUTO:
            seconds = SAL_SHOP_SECONDS if game.awaiting == AWAIT_SHOP else SAL_ROLL_SECONDS
            game.deadline = time.time() + seconds
            self._phase_tasks[code] = asyncio.ensure_future(self._turn_deadline(code, seconds, cur, seq))
        else:  # human host: park until the host advances (or the human acts)
            game.deadline = None

    @staticmethod
    def _actor_needs_autoplay(room: Room, game: SnakesAndLaddersGame) -> bool:
        """True when the current actor can't act for themselves — a bot, or a human
        whose room slot is gone/disconnected — and so must be auto-played."""
        cur = game.current_pid
        if cur in game.bot_ids:
            return True
        player = room.players.get(cur)
        return player is None or not player.connected

    def _cancel_timer(self, code: str) -> None:
        task = self._phase_tasks.pop(code, None)
        if task is not None:
            task.cancel()

    async def _auto_turn(self, code: str, pid: str, seq: int) -> None:
        """Play one turn for a bot (its trivial policy) or an absent human (just
        roll / leave the shop) after a short delay, then re-drive. On wake it resolves
        only the exact turn it was armed for — same actor AND same ``seq`` — so a
        superseded timer that still fires is a no-op (the local single-resolution
        guard, independent of cancellation timing)."""
        try:
            await asyncio.sleep(SAL_BOT_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        if self._phase_tasks.get(code) is asyncio.current_task():
            self._phase_tasks.pop(code, None)
        room = self.rooms.get(code)
        game = self.games.get(code)
        if room is None or game is None or game.is_over or game.current_pid != pid or game.seq != seq:
            return
        self._apply_auto_action(game, pid)
        await self._after_turn_change(room, game)

    async def _turn_deadline(self, code: str, seconds: float, pid: str, seq: int) -> None:
        """Auto-host per-turn timeout: when the current actor doesn't act in time,
        resolve their turn (auto-roll, or skip the shop) and re-drive. Like
        :meth:`_auto_turn` it only resolves the exact turn it was armed for (same actor
        AND same ``seq``), so a superseded deadline that still fires resolves nothing."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if self._phase_tasks.get(code) is asyncio.current_task():
            self._phase_tasks.pop(code, None)
        room = self.rooms.get(code)
        game = self.games.get(code)
        if room is None or game is None or game.is_over or game.current_pid != pid or game.seq != seq:
            return
        game.advance()
        await self._after_turn_change(room, game)

    @staticmethod
    def _apply_auto_action(game: SnakesAndLaddersGame, pid: str) -> None:
        """Resolve one auto action for the current actor without going over the wire.
        Bots follow :meth:`bot_action`; an absent human just makes minimal progress
        (roll, or leave the shop) so the game can't stall on them."""
        if pid in game.bot_ids:
            action = game.bot_action(pid)
            kind = action.get("kind")
            if kind == "buy":
                game.buy_item(pid, action.get("item"))
            elif kind == "skip":
                game.skip_shop(pid)
            else:  # "roll" (or anything unexpected) -> roll to keep play moving
                game.roll(pid)
        elif game.awaiting == AWAIT_SHOP:
            game.skip_shop(pid)
        else:
            game.roll(pid)

    def _make_rng(self) -> random.Random:
        """Seam for tests: the RNG that seeds a new game's board and dice. Tests
        monkeypatch this on the instance to force a deterministic game."""
        return random.Random()

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
        """A player left/dropped mid-game: refresh the per-player views (roles may have
        shifted) and, ONLY when the current actor is a human who just went absent,
        re-drive so their turn is auto-played instead of deadlocking. A bot already has
        its mover, and an unrelated roster change must not reset a present actor's
        ticking deadline."""
        game = self.games.get(room.code)
        if game is None or game.is_over:
            return
        if game.current_pid not in game.bot_ids and self._actor_needs_autoplay(room, game):
            self._drive(room, game)
        await self._broadcast_game(room, game)

    def _discard_room(self, code: str) -> None:
        self._cancel_timer(code)
        self.games.pop(code, None)
        self.rooms.discard_if_empty(code)


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not name:
        return "Anonymous"
    return name[:24]


def _clamp_bots(raw: Any, human_count: int) -> int:
    """How many bots to actually create from a client's ``bots`` request: a
    non-negative int, capped so total seats (humans + bots) fit the room cap.
    A non-int request (including JSON ``true``) is treated as 0."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return 0
    room_for_bots = max(0, MAX_PLAYERS_PER_ROOM - human_count)
    return max(0, min(raw, room_for_bots))
