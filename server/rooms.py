"""In-memory room and player model for the gameshow server.

This module is deliberately free of any networking / asyncio code so the room
logic (codes, host assignment, host handoff, disconnect bookkeeping) can be unit
tested directly. The connection layer in ``server/connection.py`` owns the actual
websockets and schedules grace-period timers.

Two distinct authority roles live on a room:

* ``owner_id``  — the player who controls the room: can change the host mode and
  start the game. Stays with the room (reassigned if the owner leaves).
* ``host_id``   — the *gameplay* host (the "Magic the Noah" role). Only set when
  ``host_mode == HOST_HUMAN``; ``None`` in auto mode.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from shared import protocol
from config import MAX_PLAYERS_PER_ROOM, ROOM_CODE_LENGTH

# Codes use unambiguous characters (no O/0, I/1) so they read cleanly on stream.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class Player:
    """A connected (or recently-disconnected) participant in a room."""

    id: str
    name: str
    ready: bool = False
    connected: bool = True
    # Opaque connection handle owned by the connection layer (a websocket, or a
    # stand-in object in tests). Never serialized.
    conn: Any = None

    def public(self, *, host_id: Optional[str]) -> dict[str, Any]:
        """Serializable view sent to clients (no connection handle)."""
        return {
            "id": self.id,
            "name": self.name,
            "ready": self.ready,
            "connected": self.connected,
            "is_host": self.id == host_id,
        }


@dataclass
class Room:
    code: str
    host_mode: str = protocol.HOST_AUTO
    owner_id: Optional[str] = None
    host_id: Optional[str] = None
    in_game: bool = False
    # Insertion-ordered: dict preserves order, which we use for "next" promotion.
    players: dict[str, Player] = field(default_factory=dict)

    # --- membership -------------------------------------------------------

    def add_player(self, name: str, conn: Any) -> Player:
        player = Player(id=uuid.uuid4().hex, name=name, conn=conn)
        self.players[player.id] = player
        if self.owner_id is None:
            self.owner_id = player.id
            # The first player becomes host too, but only if a human runs the show.
            if self.host_mode == protocol.HOST_HUMAN:
                self.host_id = player.id
        return player

    def remove_player(self, player_id: str) -> None:
        """Fully remove a player and repair owner/host roles if needed."""
        self.players.pop(player_id, None)
        if self.owner_id == player_id:
            self.owner_id = self._next_connected_id()
        if self.host_id == player_id:
            self.host_id = self._next_connected_id() if self.host_mode == protocol.HOST_HUMAN else None

    def _next_connected_id(self) -> Optional[str]:
        """The next *connected* player to inherit a role, or ``None`` if every
        remaining slot is disconnected. Returning a disconnected ghost would defeat
        the whole point of role repair (``mark_disconnected``'s "move roles off them
        so play isn't stuck") and would lock out a fresh joiner — ``add_player``
        re-seats ownership only when ``owner_id`` is ``None``."""
        for pid, p in self.players.items():
            if p.connected:
                return pid
        return None

    # --- host control -----------------------------------------------------

    def set_host_mode(self, mode: str) -> None:
        self.host_mode = mode
        if mode == protocol.HOST_HUMAN:
            # Re-seat a host if there isn't a valid one.
            if self.host_id not in self.players:
                self.host_id = self.owner_id or self._next_connected_id()
        else:  # auto: no human host
            self.host_id = None

    def transfer_host(self, target_id: str) -> bool:
        if self.host_mode != protocol.HOST_HUMAN or target_id not in self.players:
            return False
        self.host_id = target_id
        return True

    def can_start(self, player_id: str) -> bool:
        """Who is allowed to start the game.

        Human mode → the gameplay host. Auto mode → the room owner.
        """
        if self.host_mode == protocol.HOST_HUMAN:
            return player_id == self.host_id
        return player_id == self.owner_id

    # --- disconnect bookkeeping ------------------------------------------

    def mark_disconnected(self, player_id: str) -> None:
        """Flag a player offline and move roles off them so play isn't stuck.

        The slot is kept (for a future reconnect) until the grace timer removes it.
        """
        player = self.players.get(player_id)
        if player is None:
            return
        player.connected = False
        player.ready = False
        if self.owner_id == player_id:
            self.owner_id = self._next_connected_id()
        if self.host_id == player_id and self.host_mode == protocol.HOST_HUMAN:
            self.host_id = self._next_connected_id()

    @property
    def is_empty(self) -> bool:
        return not self.players

    @property
    def is_full(self) -> bool:
        return len(self.players) >= MAX_PLAYERS_PER_ROOM

    # --- serialization ----------------------------------------------------

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "host_mode": self.host_mode,
            "owner_id": self.owner_id,
            "host_id": self.host_id,
            "in_game": self.in_game,
            "players": [p.public(host_id=self.host_id) for p in self.players.values()],
        }


class RoomManager:
    """Owns all live rooms and hands out unique room codes."""

    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def create_room(self, host_mode: str) -> Room:
        code = self._unique_code()
        room = Room(code=code, host_mode=host_mode)
        self.rooms[code] = room
        return room

    def get(self, code: str) -> Optional[Room]:
        return self.rooms.get(code)

    def discard_if_empty(self, code: str) -> None:
        room = self.rooms.get(code)
        if room is not None and room.is_empty:
            del self.rooms[code]

    def _unique_code(self) -> str:
        for _ in range(10000):
            code = "".join(random.choice(_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))
            if code not in self.rooms:
                return code
        raise RuntimeError("could not allocate a unique room code")
