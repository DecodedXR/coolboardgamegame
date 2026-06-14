"""Headless integration tests for the gameshow server.

These drive :class:`GameServer` directly through fake connection objects (no real
websockets, no pygame), so the whole networking core — rooms, broadcasts, host
toggle/handoff, start_game, and grace-period disconnect — is verified end to end
in milliseconds. This is the "headless pytest" from the plan's verification step.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

import server.connection as connection
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
    await a.push(protocol.C_CREATE_ROOM, name="A", host_mode=protocol.HOST_HUMAN)
    code = a.last(protocol.S_ROOM_CREATED)["code"]
    await b.push(protocol.C_JOIN_ROOM, code=code, name="B")

    await a.push(protocol.C_START_GAME)  # A is the human host

    assert protocol.S_GAME_STARTED in a.types()
    assert protocol.S_GAME_STARTED in b.types()
    assert a.room()["in_game"] is True

    await a.drop(); await b.drop()
    await asyncio.gather(ta, tb)


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
