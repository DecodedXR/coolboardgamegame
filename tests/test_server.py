"""Headless integration tests for the gameshow server.

These drive :class:`GameServer` directly through fake connection objects (no real
websockets, no pygame), so the whole networking core — rooms, broadcasts, host
toggle/handoff, start_game, and grace-period disconnect — is verified end to end
in milliseconds. This is the "headless pytest" from the plan's verification step.
"""

from __future__ import annotations

import asyncio
import http.client
from typing import Any, Optional

import pytest
import websockets

import server.connection as connection
from server.__main__ import health_check
from server.connection import GameServer
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
    await a.push(protocol.C_START_GAME)
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

    await a.push(protocol.C_START_GAME)  # A is the human host

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


# --- Wrong Answers Only (in-game) -----------------------------------------


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
    await a.push(protocol.C_START_GAME)
    assert a.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_ENOUGH_PLAYERS
    assert protocol.S_GAME_STARTED not in a.types()
    await a.drop(); await ta


async def test_auto_game_full_flow(monkeypatch):
    monkeypatch.setattr(connection, "WAO_TOTAL_ROUNDS", 1)
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    c, tc = await open_conn(server)
    await _host_auto_room(server, [a, b, c], ["A", "B", "C"])

    await a.push(protocol.C_START_GAME)
    assert protocol.S_GAME_STARTED in a.types() and protocol.S_GAME_STARTED in c.types()
    gs = a.game()
    assert gs["phase"] == protocol.PHASE_PROMPT
    assert gs["contestant_count"] == 3
    assert gs["you_role"] == "contestant"

    # Everyone answers -> auto-advances to the vote phase.
    await a.push(protocol.C_SUBMIT_ANSWER, text="answer A")
    await b.push(protocol.C_SUBMIT_ANSWER, text="answer B")
    await c.push(protocol.C_SUBMIT_ANSWER, text="answer C")
    assert a.game()["phase"] == protocol.PHASE_VOTE

    # B and C both vote for A's answer; A votes for B's.
    def aid_for(voter_conn, author_text):
        return next(o["answer_id"] for o in voter_conn.game()["answers"] if o["text"] == author_text)

    await b.push(protocol.C_SUBMIT_VOTE, answer_id=aid_for(b, "answer A"))
    await c.push(protocol.C_SUBMIT_VOTE, answer_id=aid_for(c, "answer A"))
    await a.push(protocol.C_SUBMIT_VOTE, answer_id=aid_for(a, "answer B"))

    # All votes in -> reveal with scores.
    gs = a.game()
    assert gs["phase"] == protocol.PHASE_REVEAL
    scores = {row["name"]: row["score"] for row in gs["scores"]}
    assert scores == {"A": 200, "B": 100, "C": 0}

    # Owner advances the (single) round -> final.
    await a.push(protocol.C_ADVANCE_PHASE)
    assert a.game()["phase"] == protocol.PHASE_FINAL
    assert a.game()["scores"][0]["name"] == "A"

    await a.drop(); await b.drop(); await c.drop()
    await asyncio.gather(ta, tb, tc)


async def test_human_host_drives_phases_manually(monkeypatch):
    monkeypatch.setattr(connection, "WAO_TOTAL_ROUNDS", 1)
    server = GameServer()
    a, ta = await open_conn(server)  # host + owner
    b, tb = await open_conn(server)
    c, tc = await open_conn(server)
    await a.push(protocol.C_CREATE_ROOM, name="Host", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")
    await c.push(protocol.C_JOIN_ROOM, code=code, name="C")

    await a.push(protocol.C_START_GAME)
    assert a.game()["you_role"] == "host"
    assert a.game()["contestant_count"] == 2  # host is not a contestant
    assert b.game()["you_role"] == "contestant"

    # Both contestants answer; with a human host this does NOT auto-advance.
    await b.push(protocol.C_SUBMIT_ANSWER, text="bee")
    await c.push(protocol.C_SUBMIT_ANSWER, text="cee")
    assert a.game()["phase"] == protocol.PHASE_PROMPT

    # The host can't submit answers (not a contestant).
    await a.push(protocol.C_SUBMIT_ANSWER, text="nope")
    assert a.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_CONTESTANT

    # Host advances to voting.
    await a.push(protocol.C_ADVANCE_PHASE)
    assert b.game()["phase"] == protocol.PHASE_VOTE

    # A non-host cannot advance.
    await b.push(protocol.C_ADVANCE_PHASE)
    assert b.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_HOST

    # Contestants vote for each other, host advances to reveal.
    aid_b = next(o["answer_id"] for o in b.game()["answers"])  # the other answer (cee)
    aid_c = next(o["answer_id"] for o in c.game()["answers"])  # the other answer (bee)
    await b.push(protocol.C_SUBMIT_VOTE, answer_id=aid_b)
    await c.push(protocol.C_SUBMIT_VOTE, answer_id=aid_c)
    assert b.game()["phase"] == protocol.PHASE_VOTE  # still manual
    await a.push(protocol.C_ADVANCE_PHASE)
    assert a.game()["phase"] == protocol.PHASE_REVEAL
    scores = {row["name"]: row["score"] for row in a.game()["scores"]}
    assert scores == {"B": 100, "C": 100}

    await a.drop(); await b.drop(); await c.drop()
    await asyncio.gather(ta, tb, tc)


async def test_return_to_lobby_ends_game():
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _host_auto_room(server, [a, b], ["A", "B"])
    await a.push(protocol.C_START_GAME)
    assert a.room()["in_game"] is True

    await a.push(protocol.C_RETURN_TO_LOBBY)
    assert protocol.S_RETURN_TO_LOBBY in a.types() and protocol.S_RETURN_TO_LOBBY in b.types()
    assert a.room()["in_game"] is False
    assert a.last(protocol.S_GAME_STATE) is not None  # game ran, then torn down

    # Non-owner can't end the game.
    await a.push(protocol.C_START_GAME)
    await b.push(protocol.C_RETURN_TO_LOBBY)
    assert b.last(protocol.S_ERROR)["code"] == protocol.ERR_NOT_HOST

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)
