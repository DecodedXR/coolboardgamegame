"""Wire protocol shared by the server and the pygame client.

Every message is a JSON object with a ``type`` field plus a flat payload, e.g.::

    {"type": "join_room", "code": "ABCD", "name": "Noah"}

Keeping the message-type constants and the (de)serialization in one module means
the client and server can never drift out of sync over a typo'd string.
"""

from __future__ import annotations

import json
from typing import Any

# --- Host modes -----------------------------------------------------------

HOST_HUMAN = "human"  # one player runs the show
HOST_AUTO = "auto"  # the engine runs the show (no host player)
HOST_MODES = (HOST_HUMAN, HOST_AUTO)


# --- Client -> Server message types ---------------------------------------

C_CREATE_ROOM = "create_room"        # {name, host_mode}
C_JOIN_ROOM = "join_room"            # {code, name}
C_SET_READY = "set_ready"            # {ready}
C_SET_HOST_MODE = "set_host_mode"    # {mode}            (host only)
C_TRANSFER_HOST = "transfer_host"    # {target_id}       (host only)
C_START_GAME = "start_game"          # {}                (host / human-host or any player in auto)
C_LEAVE_ROOM = "leave_room"          # {}
C_PING = "ping"                      # {}


# --- Server -> Client message types ---------------------------------------

S_ROOM_CREATED = "room_created"      # {code, you, room}
S_ROOM_JOINED = "room_joined"        # {code, you, room}
S_ROOM_UPDATE = "room_update"        # {room}
S_GAME_STARTED = "game_started"      # {}  (stub until a minigame plugs in)
S_ERROR = "error"                    # {code, message}
S_PONG = "pong"                      # {}


# --- Error codes ----------------------------------------------------------

ERR_BAD_MESSAGE = "bad_message"
ERR_ROOM_NOT_FOUND = "room_not_found"
ERR_ROOM_FULL = "room_full"
ERR_ALREADY_IN_ROOM = "already_in_room"
ERR_NOT_IN_ROOM = "not_in_room"
ERR_NOT_HOST = "not_host"
ERR_BAD_HOST_MODE = "bad_host_mode"
ERR_BAD_TARGET = "bad_target"
ERR_GAME_IN_PROGRESS = "game_in_progress"


def encode(msg_type: str, **payload: Any) -> str:
    """Serialize a message to a JSON string ready to send over the socket."""
    payload["type"] = msg_type
    return json.dumps(payload)


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parse an inbound frame into a dict.

    Raises ``ValueError`` if the frame is not a JSON object with a string
    ``type`` field, so callers can uniformly reject malformed input.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("message must be a JSON object")
    if not isinstance(data.get("type"), str):
        raise ValueError("message missing string 'type' field")
    return data
