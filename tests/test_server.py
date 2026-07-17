"""Headless integration tests for the gameshow server.

These drive :class:`GameServer` directly through fake connection objects (no real
websockets, no pygame), so the whole networking core — rooms, broadcasts, host
toggle/handoff, start_game, and grace-period disconnect — is verified end to end
in milliseconds. This is the "headless pytest" from the plan's verification step.
"""

from __future__ import annotations

import asyncio
import http.client
import random
from typing import Any, Optional

import pytest
import websockets

import server.connection as connection
from server.__main__ import health_check
from server.connection import GameServer
from server.games.snakes_and_ladders import AWAIT_ROLL, AWAIT_SHOP
from server.rooms import Room
from shared import protocol

_CLOSE = object()


class FakeConn:
    """Async-iterable stand-in for a websocket: ``send`` records outgoing frames,
    ``push`` injects an incoming client message, ``drop`` ends the connection."""

    def __init__(self) -> None:
        self._inbox: "asyncio.Queue[Any]" = asyncio.Queue()
        self.outbox: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.outbox.append(protocol.decode(data))

    def __aiter__(self) -> "FakeConn":
        return self

    async def __anext__(self) -> str:
        item = await self._inbox.get()
        if item is _CLOSE:
            raise StopAsyncIteration
        return item

    async def push(self, msg_type: str, **payload: Any) -> None:
        await self._inbox.put(protocol.encode(msg_type, **payload))
        await settle()

    async def drop(self) -> None:
        await self._inbox.put(_CLOSE)
        await settle()

    # --- assertions helpers ----------------------------------------------

    def last(self, msg_type: str) -> Optional[dict[str, Any]]:
        for msg in reversed(self.outbox):
            if msg["type"] == msg_type:
                return msg
        return None

    def room(self) -> dict[str, Any]:
        """Most recent room snapshot this conn has seen, from any message type."""
        for msg in reversed(self.outbox):
            if "room" in msg:
                return msg["room"]
        raise AssertionError("no room snapshot received")

    def types(self) -> list[str]:
        return [m["type"] for m in self.outbox]

    def game(self) -> dict[str, Any]:
        """Most recent per-player game snapshot this conn has seen."""
        g = self.last(protocol.S_GAME_STATE)
        if g is None:
            raise AssertionError("no game snapshot received")
        return g["game"]


async def settle(rounds: int = 8) -> None:
    """Yield control so scheduled sends/broadcasts/tasks run to completion."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def player_named(room: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in room["players"] if p["name"] == name)


async def open_conn(server: GameServer) -> tuple[FakeConn, asyncio.Task]:
    conn = FakeConn()
    task = asyncio.ensure_future(server.serve_connection(conn))
    await settle()
    return conn, task


# --- tests ----------------------------------------------------------------


async def test_create_room_human_owner_is_host():
    server = GameServer()
    a, ta = await open_conn(server)

    await a.push(protocol.C_CREATE_ROOM, name="Noah", host_mode=protocol.HOST_HUMAN)

    created = a.last(protocol.S_ROOM_CREATED)
    assert created is not None
    code = created["code"]
    assert len(code) == 4
    room = created["room"]
    you = created["you"]
    assert you["is_owner"] and you["is_host"]
    assert room["host_mode"] == protocol.HOST_HUMAN
    assert room["host_id"] == you["id"]

    await a.drop()
    await ta


async def test_join_broadcasts_to_all():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)

    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_AUTO)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")

    # Both sides converge on a 2-player room.
    assert {p["name"] for p in a.room()["players"]} == {"A", "B"}
    assert {p["name"] for p in b.room()["players"]} == {"A", "B"}
    # A learned about B via a broadcast room_update.
    assert a.last(protocol.S_ROOM_UPDATE) is not None

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_ready_propagates():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_AUTO)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")

    await b.push(protocol.C_SET_READY, ready=True)

    assert player_named(a.room(), "B")["ready"] is True

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_host_mode_toggle_owner_only():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")

    # Non-owner B is rejected.
    await b.push(protocol.C_SET_HOST_MODE, mode=protocol.HOST_AUTO)
    assert b.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_HOST
    assert a.room()["host_mode"] == protocol.HOST_HUMAN

    # Owner A flips to auto: host role clears everywhere.
    await a.push(protocol.C_SET_HOST_MODE, mode=protocol.HOST_AUTO)
    assert a.room()["host_mode"] == protocol.HOST_AUTO
    assert a.room()["host_id"] is None

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_transfer_host_moves_badge():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")

    b_id = player_named(a.room(), "B")["id"]
    await a.push(protocol.C_TRANSFER_HOST, target_id=b_id)

    assert b.room()["host_id"] == b_id
    # And now A (no longer host) cannot start a human-host game.
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)
    assert a.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_HOST

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_start_game_reaches_everyone():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    c, tc = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")
    await c.push(protocol.C_JOIN_ROOM, code=code, name="C")  # host + 2 contestants

    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)  # A is the human host

    assert protocol.S_GAME_STARTED in a.types()
    assert protocol.S_GAME_STARTED in b.types()
    assert a.room()["in_game"] is True

    await a.drop(); await b.drop(); await c.drop()
    await asyncio.gather(ta, tb, tc)


async def test_host_disconnect_promotes_next(monkeypatch):
    monkeypatch.setattr(connection, "DISCONNECT_GRACE_SECONDS", 0.05)
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")
    b_id = player_named(a.room(), "B")["id"]

    await a.drop()  # the human host vanishes
    await ta

    # Host immediately moves to B (the room isn't left leaderless)...
    assert b.room()["host_id"] == b_id
    # ...and after the grace window A's slot is gone entirely.
    await asyncio.sleep(0.1)
    await settle()
    assert {p["name"] for p in b.room()["players"]} == {"B"}

    await b.drop()
    await tb


# --- Room role repair: pure unit tests (no sockets) -----------------------
# _next_connected_id underlies every owner/host repair (mark_disconnected,
# remove_player, set_host_mode). Its contract is in the name: a role must never
# be parked on a disconnected ghost — that is the very "stuck" state the
# disconnect bookkeeping exists to avoid.


def test_next_connected_id_never_returns_a_disconnected_ghost():
    # Both players in a 2-seat room drop within the grace window (their slots are
    # still present, awaiting expiry). The role-repair must hand ownership to NOBODY
    # (None), not to a disconnected ghost.
    room = Room(code="TEST", host_mode=protocol.HOST_HUMAN)
    a = room.add_player("A", None)
    b = room.add_player("B", None)
    assert room.owner_id == a.id and room.host_id == a.id  # first player runs the show

    room.mark_disconnected(a.id)
    assert room.owner_id == b.id and room.host_id == b.id  # role moves to the live player

    room.mark_disconnected(b.id)  # now EVERY remaining slot is disconnected
    # The bug: the fallback returned next(iter(players)) — a disconnected ghost.
    assert room.owner_id is None
    assert room.host_id is None
    # Stated directly: the helper itself must not surface a disconnected pid.
    assert room._next_connected_id() is None


def test_fresh_joiner_owns_a_room_whose_members_all_dropped():
    # Observable consequence of the above: a player who JOINS during the grace window
    # (the room isn't discarded until empty) must become owner/host and be allowed to
    # start — otherwise the only connected player is locked out by a ghost owner.
    room = Room(code="TEST", host_mode=protocol.HOST_HUMAN)
    a = room.add_player("A", None)
    b = room.add_player("B", None)
    room.mark_disconnected(a.id)
    room.mark_disconnected(b.id)

    c = room.add_player("C", None)  # fresh connection joins the still-alive room
    assert room.owner_id == c.id  # add_player claims ownership only when owner_id is None
    assert room.host_id == c.id   # human-host mode: the new owner runs the show
    assert room.can_start(c.id) is True


# --- Health endpoint (M3 W2) ----------------------------------------------
# The only test that opens a real socket: it stands up an actual websockets
# server with the production process_request hook so both branches — plain HTTP
# probe vs. WebSocket upgrade on the same path "/" — are verified end to end.


def _http_get(port: int, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection("localhost", port, timeout=2)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


async def test_health_check_responds_and_still_upgrades():
    async def handler(ws):  # echoes, just to prove the upgrade succeeded
        async for msg in ws:
            await ws.send(msg)

    async with websockets.serve(
        handler, "localhost", 0, process_request=health_check
    ) as server:
        port = server.sockets[0].getsockname()[1]

        # Plain HTTP probes (health checker / browser) get 200, not 426.
        for path in ("/healthz", "/"):
            status, body = await asyncio.to_thread(_http_get, port, path)
            assert status == 200, f"{path} -> {status}"
            assert "ok" in body

        # A real WebSocket client still upgrades on "/" and round-trips.
        async with websockets.connect(f"ws://localhost:{port}") as ws:
            await ws.send("hi")
            assert await ws.recv() == "hi"


# --- Snakes & Ladders turn driver (in-game) -------------------------------


async def _host_auto_room(server, conns, names):
    """Create an auto room with the first conn and join the rest. Returns code."""
    first, *rest = conns
    await first.push(protocol.C_CREATE_ROOM, name=names[0], host_mode=protocol.HOST_AUTO)
    code = first.last(protocol.S_ROOM_CREATED)["code"]
    for conn, name in zip(rest, names[1:]):
        await conn.push(protocol.C_JOIN_ROOM, code=code, name=name)
    return code


async def test_start_game_needs_two_contestants():
    server = GameServer()
    a, ta = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_AUTO)
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)
    assert a.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_ENOUGH_PLAYERS
    assert protocol.S_GAME_STARTED not in a.types()
    await a.drop(); await ta


async def _wait(cond, *, tries: int = 80) -> None:
    """Spin the loop with small real sleeps until ``cond()`` holds, giving a
    bot/timer chain scheduled with a tiny delay time to actually run."""
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.005)
        await settle()
    assert cond(), "condition still false after waiting"


def _conn_for(conns, pid):
    """The FakeConn whose player id is ``pid`` (matched via its game view)."""
    for c in conns:
        g = c.last(protocol.S_GAME_STATE)
        if g is not None and g["game"]["your_id"] == pid:
            return c
    raise AssertionError(f"no connected conn owns pid {pid!r}")


async def test_one_human_plus_bot_starts():
    server = GameServer()
    a, ta = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Solo", host_mode=protocol.HOST_AUTO)
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots=1)  # 1 human + 1 bot = 2 players

    assert protocol.S_GAME_STARTED in a.types()
    g = a.game()
    assert g["phase"] == protocol.PHASE_PLAY
    assert g["awaiting"] == AWAIT_ROLL
    assert len(g["players"]) == 2
    assert sum(1 for p in g["players"] if p["is_bot"]) == 1
    assert g["your_turn"] is True  # the human acts first (order = humans then bots)

    await a.push(protocol.C_RETURN_TO_LOBBY)  # cancel the pending turn timer
    await a.drop(); await ta


async def test_bad_bots_value_is_ignored():
    server = GameServer()
    a, ta = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Solo", host_mode=protocol.HOST_AUTO)
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots="lots")  # garbage -> treated as 0 bots
    assert a.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_ENOUGH_PLAYERS
    assert protocol.S_GAME_STARTED not in a.types()
    await a.drop(); await ta


async def test_bot_count_clamped_to_room_cap():
    server = GameServer()
    a, ta = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Solo", host_mode=protocol.HOST_AUTO)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots=99)  # absurd -> clamped to fill the room

    game = server.games[code]
    assert len(game.bot_ids) == connection.MAX_PLAYERS_PER_ROOM - 1  # one human seat
    assert len(game.order) == connection.MAX_PLAYERS_PER_ROOM
    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await ta


async def test_bots_are_not_room_players():
    server = GameServer()
    a, ta = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Solo", host_mode=protocol.HOST_AUTO)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots=3)

    game = server.games[code]
    room = server.rooms.get(code)
    # Bots live only in the game object -> never a room player -> no broadcast ever
    # tries to send to them (they have no connection).
    assert len(game.bot_ids) == 3
    assert all(bid not in room.players for bid in game.bot_ids)
    assert set(room.players) == {a.game()["your_id"]}
    assert sum(1 for p in a.game()["players"] if p["is_bot"]) == 3

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await ta


async def test_roll_alternates_turn_and_rejects_off_turn():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)  # 2 contestants, no bots

    cur = a.game()["current_pid"]
    cur_conn = _conn_for([a, b], cur)
    off_conn = b if cur_conn is a else a

    # The off-turn player cannot roll.
    await off_conn.push(protocol.C_ROLL_DICE)
    assert off_conn.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_YOUR_TURN
    # Buying while it's a roll turn is the wrong sub-state.
    await cur_conn.push(protocol.C_BUY_ITEM, item="boost")
    assert cur_conn.last(protocol.S_ERROR)["code"] == protocol.ERR_WRONG_SUBSTATE

    assert a.game()["last_turn"] is None  # nobody has rolled yet
    # The current player rolls; the turn resolves and (after any shop) passes on.
    await cur_conn.push(protocol.C_ROLL_DICE)
    g = a.game()
    assert g["last_turn"]["seq"] == 1
    if g["awaiting"] == AWAIT_SHOP:
        await cur_conn.push(protocol.C_SKIP_SHOP)
        g = a.game()
    assert g["current_pid"] != cur  # the turn passed to the other player

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_shop_buy_over_the_wire():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    code = await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)

    cur = a.game()["current_pid"]
    cur_conn = _conn_for([a, b], cur)
    # Put the current actor into the shop sub-state directly (landing on a shop tile
    # is the engine's job, tested elsewhere); here we verify the C_BUY_ITEM wire path.
    game = server.games[code]
    game.awaiting = AWAIT_SHOP
    game.gold[cur] = 500
    await server._broadcast_game(server.rooms.get(code), game)
    assert cur_conn.game()["awaiting"] == AWAIT_SHOP
    assert "shop" in cur_conn.game()  # stock is secret to the current player

    await cur_conn.push(protocol.C_BUY_ITEM, item="boost")
    g = cur_conn.game()
    me = next(p for p in g["players"] if p["id"] == cur)
    assert me["gold"] < 500 and "boost" in me["items"]  # gold spent, item held
    assert g["current_pid"] != cur and g["awaiting"] == AWAIT_ROLL  # buying passed the turn

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_bot_takes_its_turn_in_auto(monkeypatch):
    monkeypatch.setattr(connection, "SAL_BOT_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(connection, "SAL_ROLL_SECONDS", 999)  # the human won't auto-roll
    server = GameServer()
    a, ta = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Solo", host_mode=protocol.HOST_AUTO)
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots=1)

    human = a.game()["your_id"]
    assert a.game()["current_pid"] == human  # human first
    await a.push(protocol.C_ROLL_DICE)
    if a.game()["awaiting"] == AWAIT_SHOP:
        await a.push(protocol.C_SKIP_SHOP)
    assert a.game()["current_pid"] != human  # it's the bot's turn now

    # The driver auto-plays the bot; the turn comes back to the human.
    await _wait(lambda: a.game()["current_pid"] == human)
    assert a.game()["last_turn"]["seq"] >= 2  # human roll + at least one bot turn

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await ta


async def test_bot_plays_in_human_host_mode(monkeypatch):
    monkeypatch.setattr(connection, "SAL_BOT_DELAY_SECONDS", 0.01)
    server = GameServer()
    a, ta = await open_conn(server)  # the human host (runs the show, not a player)
    await a.push(protocol.C_CREATE_ROOM, name="Host", host_mode=protocol.HOST_HUMAN)
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots=2)  # 0 human contestants + 2 bots

    g = a.game()
    assert g["you_role"] == "host"
    assert len(g["players"]) == 2 and all(p["is_bot"] for p in g["players"])
    assert g["your_turn"] is False
    # Bots play themselves in human-host mode too (no host action needed).
    await _wait(lambda: a.game()["last_turn"] is not None)
    assert a.game()["last_turn"]["seq"] >= 1

    await a.push(protocol.C_RETURN_TO_LOBBY)  # stop the bot chain
    await a.drop(); await ta


async def test_auto_deadline_auto_rolls(monkeypatch):
    monkeypatch.setattr(connection, "SAL_ROLL_SECONDS", 0.02)
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)

    assert a.game()["last_turn"] is None
    # Nobody rolls; after the (tiny) deadline the driver auto-rolls the current actor.
    await _wait(lambda: a.game()["last_turn"] is not None)
    assert a.game()["last_turn"]["seq"] >= 1

    await a.push(protocol.C_RETURN_TO_LOBBY)  # stop the auto-roll chain
    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_human_host_force_advance_and_non_host_rejected():
    server = GameServer()
    a, ta = await open_conn(server)  # host
    b, tb = await open_conn(server)
    c, tc = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Host", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")
    await c.push(protocol.C_JOIN_ROOM, code=code, name="C")
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)  # contestants B & C; host A runs the show

    g = a.game()
    assert g["you_role"] == "host" and len(g["players"]) == 2
    assert g["deadline"] is None  # human-host parks (no auto deadline)
    cur = g["current_pid"]

    # A non-host cannot force-advance.
    await b.push(protocol.C_ADVANCE_PHASE)
    assert b.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_HOST
    assert a.game()["last_turn"] is None

    # The host clicks NEXT -> the current actor's turn is force-resolved.
    await a.push(protocol.C_ADVANCE_PHASE)
    g = a.game()
    assert g["last_turn"]["seq"] == 1
    if g["awaiting"] == AWAIT_SHOP:
        await a.push(protocol.C_ADVANCE_PHASE)  # NEXT again leaves the shop
        g = a.game()
    assert g["current_pid"] != cur

    await a.drop(); await b.drop(); await c.drop()
    await asyncio.gather(ta, tb, tc)


async def test_disconnect_during_turn_unsticks(monkeypatch):
    monkeypatch.setattr(connection, "SAL_BOT_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(connection, "DISCONNECT_GRACE_SECONDS", 0.05)
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)

    cur = a.game()["current_pid"]
    cur_conn = _conn_for([a, b], cur)
    other = b if cur_conn is a else a

    # The current player vanishes mid-turn. The driver treats an absent human like a
    # bot and auto-plays their turn so the game can't deadlock.
    await cur_conn.drop()
    await _wait(lambda: other.game()["last_turn"] is not None
                and other.game()["last_turn"]["seq"] >= 1)
    assert other.game()["last_turn"]["seq"] >= 1

    await other.push(protocol.C_RETURN_TO_LOBBY)
    await other.drop()
    await asyncio.gather(ta, tb)


async def test_make_rng_seam_determinizes_board(monkeypatch):
    boards = []
    for _ in range(2):
        server = GameServer()
        monkeypatch.setattr(server, "_make_rng", lambda: random.Random(12345))
        a, ta = await open_conn(server)
        await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_AUTO)
        await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS, bots=1)
        boards.append(a.game()["board"])
        await a.push(protocol.C_RETURN_TO_LOBBY)
        await a.drop(); await ta
    assert boards[0] == boards[1]  # same seed -> identical freshly-randomized board


async def test_use_powerup_over_the_wire():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    code = await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)

    cur = a.game()["current_pid"]
    cur_conn = _conn_for([a, b], cur)
    game = server.games[code]
    game.items[cur] = ["boost"]  # grant a held powerup (landing-driven in real play)
    await server._broadcast_game(server.rooms.get(code), game)

    # Using a held powerup is consumed and ARMED, without passing the turn.
    await cur_conn.push(protocol.C_USE_POWERUP, item="boost")
    g = cur_conn.game()
    me = next(p for p in g["players"] if p["id"] == cur)
    assert "boost" not in me["items"]            # consumed
    assert g["current_pid"] == cur               # the turn did NOT pass
    assert "boost" in g.get("your_armed", [])    # armed for the upcoming roll
    # Using a powerup you don't hold is rejected as a bad item.
    await cur_conn.push(protocol.C_USE_POWERUP, item="double")
    assert cur_conn.last(protocol.S_ERROR)["code"] == protocol.ERR_BAD_ITEM

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_roster_change_does_not_reset_current_deadline(monkeypatch):
    monkeypatch.setattr(connection, "DISCONNECT_GRACE_SECONDS", 0.05)
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    c, tc = await open_conn(server)
    code = await _host_auto_room(server, [a, b, c], ["A", "B", "C"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)

    game = server.games[code]
    cur = game.current_pid
    deadline_before = game.deadline
    assert deadline_before is not None
    # A NON-current player (a non-owner B/C) disconnects. The unrelated roster change
    # must NOT reset the current actor's already-ticking auto-roll deadline.
    dropper = next(x for x in (b, c) if x.game()["your_id"] != cur)
    await dropper.drop()
    await settle()
    assert game.deadline == deadline_before

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop(); await c.drop()
    await asyncio.gather(ta, tb, tc)


async def test_superseded_turn_timer_resolves_nothing(monkeypatch):
    monkeypatch.setattr(connection, "SAL_BOT_DELAY_SECONDS", 0)
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    code = await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)

    game = server.games[code]
    stale_pid, stale_seq = game.current_pid, game.seq  # the turn a timer was armed for
    # Resolve that turn for real, so any timer armed for (stale_pid, stale_seq) is now
    # superseded.
    cur_conn = _conn_for([a, b], stale_pid)
    await cur_conn.push(protocol.C_ROLL_DICE)
    if game.awaiting == AWAIT_SHOP:
        await cur_conn.push(protocol.C_SKIP_SHOP)
    seq_after = game.seq
    assert seq_after > stale_seq

    # A stale deadline / auto-turn for the old turn must be a no-op (the local
    # (pid, seq) guard) — never a second resolution.
    await server._turn_deadline(code, 0, stale_pid, stale_seq)
    await server._auto_turn(code, stale_pid, stale_seq)
    assert game.seq == seq_after  # no extra turn resolved

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


async def test_return_to_lobby_ends_game():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)
    assert a.room()["in_game"] is True

    await a.push(protocol.C_RETURN_TO_LOBBY)
    assert protocol.S_RETURN_TO_LOBBY in a.types() and protocol.S_RETURN_TO_LOBBY in b.types()
    assert a.room()["in_game"] is False
    assert a.last(protocol.S_GAME_STATE) is not None  # game ran, then torn down

    # Non-owner can't end the game.
    await a.push(protocol.C_START_GAME, game=protocol.GAME_SNAKES_AND_LADDERS)
    await b.push(protocol.C_RETURN_TO_LOBBY)
    assert b.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_HOST

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)
