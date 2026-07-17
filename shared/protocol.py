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
C_START_GAME = "start_game"          # {bots?:int, game?:str}  (host / human-host or any player in auto)
C_LEAVE_ROOM = "leave_room"          # {}
C_PING = "ping"                      # {}

# --- In-game: generic show-runner controls (client -> server) -------------
# Both apply to any minigame: the human host (or the auto-mode owner) drives the
# flow and can end the game back to the lobby.

C_ADVANCE_PHASE = "advance_phase"    # {}                (show-runner: human host / auto owner)
C_RETURN_TO_LOBBY = "return_to_lobby"  # {}              (show-runner) end game, back to lobby

# --- In-game: Snakes & Ladders (client -> server) -------------------------
# A turn-based board game: the current player rolls; landing on special tiles
# spins wheels, grants gold, applies debuffs, or opens a shop sub-state.

C_ROLL_DICE = "roll_dice"            # {}                (current player, awaiting "roll")
C_USE_POWERUP = "use_powerup"        # {item}            (current player, pre-roll; does not pass turn)
C_BUY_ITEM = "buy_item"              # {item}            (current player, awaiting "shop"; passes turn)
C_SKIP_SHOP = "skip_shop"            # {}                (current player, awaiting "shop"; passes turn)

# --- In-game: Word Bomb (client -> server) --------------------------------
# A turn-based word game: the current player types a real word containing the
# prompt substring before the fuse runs out, or the bomb explodes (a lost life).

C_SUBMIT_WORD = "submit_word"        # {word}            (current player, word bomb)


# --- Server -> Client message types ---------------------------------------

S_ROOM_CREATED = "room_created"      # {code, you, room}
S_ROOM_JOINED = "room_joined"        # {code, you, room}
S_ROOM_UPDATE = "room_update"        # {room}
S_GAME_STARTED = "game_started"      # {game}  (clients switch to that minigame's scene)
S_GAME_STATE = "game_state"          # {game}  (per-player view, broadcast on every change)
S_RETURN_TO_LOBBY = "return_to_lobby"  # {}  (game ended; clients return to the lobby)
S_ERROR = "error"                    # {code, message}
S_PONG = "pong"                      # {}


# --- Game identifiers -----------------------------------------------------

GAME_WORD_BOMB = "word_bomb"
GAME_SNAKES_AND_LADDERS = "snakes_and_ladders"
GAMES = (GAME_WORD_BOMB, GAME_SNAKES_AND_LADDERS)

# Snakes & Ladders phases. Only two: the shop is an ``awaiting`` sub-state of
# PHASE_PLAY (not a phase), which avoids "player changed but phase didn't" races.
PHASE_PLAY = "play"           # the turn loop (game["awaiting"] is "roll" or "shop")
PHASE_GAMEOVER = "gameover"   # someone reached the final cell; winner is set

# ``awaiting`` sub-states within PHASE_PLAY (the ``game["awaiting"]`` value). The
# server (authority) and client (replay UI) both key off these, so they live here
# once rather than being re-declared in each module where they could drift.
AWAIT_ROLL = "roll"
AWAIT_SHOP = "shop"
AWAIT_WORD = "word"   # word bomb: the current player must submit a word


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
ERR_NOT_ENOUGH_PLAYERS = "not_enough_players"
ERR_NO_GAME = "no_game"
ERR_WRONG_PHASE = "wrong_phase"
ERR_NOT_CONTESTANT = "not_contestant"
# Snakes & Ladders turn errors.
ERR_NOT_YOUR_TURN = "not_your_turn"      # acted when it isn't your turn
ERR_WRONG_SUBSTATE = "wrong_substate"    # rolled while shopping, or shopped while rolling
ERR_BAD_ITEM = "bad_item"                # unknown / unaffordable / not-held item


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
