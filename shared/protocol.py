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
C_START_GAME = "start_game"          # {bots?:int}       (host / human-host or any player in auto)
C_LEAVE_ROOM = "leave_room"          # {}
C_PING = "ping"                      # {}

# --- In-game: Wrong Answers Only (client -> server) -----------------------

C_SUBMIT_ANSWER = "submit_answer"    # {text}            (contestants, prompt phase)
C_SUBMIT_VOTE = "submit_vote"        # {answer_id}       (contestants, vote phase)
C_ADVANCE_PHASE = "advance_phase"    # {}                (show-runner: human host / auto owner)
C_RETURN_TO_LOBBY = "return_to_lobby"  # {}              (show-runner) end game, back to lobby

# --- In-game: Snakes & Ladders (client -> server) -------------------------
# A turn-based board game: the current player rolls; landing on special tiles
# spins wheels, grants gold, applies debuffs, or opens a shop sub-state.

C_ROLL_DICE = "roll_dice"            # {}                (current player, awaiting "roll")
C_USE_POWERUP = "use_powerup"        # {item}            (current player, pre-roll; does not pass turn)
C_BUY_ITEM = "buy_item"              # {item}            (current player, awaiting "shop"; passes turn)
C_SKIP_SHOP = "skip_shop"            # {}                (current player, awaiting "shop"; passes turn)


# --- Server -> Client message types ---------------------------------------

S_ROOM_CREATED = "room_created"      # {code, you, room}
S_ROOM_JOINED = "room_joined"        # {code, you, room}
S_ROOM_UPDATE = "room_update"        # {room}
S_GAME_STARTED = "game_started"      # {}  (clients switch to the minigame scene)
S_GAME_STATE = "game_state"          # {game}  (per-player view, broadcast on every change)
S_RETURN_TO_LOBBY = "return_to_lobby"  # {}  (game ended; clients return to the lobby)
S_ERROR = "error"                    # {code, message}
S_PONG = "pong"                      # {}


# --- Game identifiers -----------------------------------------------------

GAME_WRONG_ANSWERS = "wrong_answers"
GAME_SNAKES_AND_LADDERS = "snakes_and_ladders"

# Wrong Answers Only phases (value of game["phase"] in an S_GAME_STATE payload).
PHASE_PROMPT = "prompt"   # contestants type an answer
PHASE_VOTE = "vote"       # contestants vote on the (anonymized) answers
PHASE_REVEAL = "reveal"   # authorship + votes + round scores revealed
PHASE_FINAL = "final"     # final scoreboard

# Snakes & Ladders phases. Only two: the shop is an ``awaiting`` sub-state of
# PHASE_PLAY (not a phase), which avoids "player changed but phase didn't" races.
PHASE_PLAY = "play"           # the turn loop (game["awaiting"] is "roll" or "shop")
PHASE_GAMEOVER = "gameover"   # someone reached the final cell; winner is set


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
ERR_BAD_ANSWER = "bad_answer"
ERR_BAD_VOTE = "bad_vote"
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
