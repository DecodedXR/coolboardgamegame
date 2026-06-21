"""Headless tests for the Snakes & Ladders scene wiring and the lobby go-live.

The scene's *decision* helpers (when ROLL is offered, which item buttons to show,
where the animating token sits, the deadline countdown) are pure and pinned here;
only ``draw`` needs a real Surface/font and is left to the manual desktop smoke in
the final chunk. The lobby tests cover the two go-live behaviours: routing a
started game to the new scene, and the show-runner's ``- bots N +`` stepper that
sends ``C_START_GAME`` with a bot count.

Everything runs without a display: we never call ``draw`` or ``pygame.init`` — we
construct scenes, feed them state dicts / fake messages, and assert on the pure
helpers and the messages they would send through a fake net.
"""

from __future__ import annotations

from typing import Any

import pygame

from client.board_render import BoardLayout
from client.token_anim import TokenAnimator
from client.scenes.snakes_and_ladders import (
    SnakesAndLaddersScene,
    animating_override,
    can_roll,
    countdown_seconds,
    is_runner,
    usable_items,
)
from shared import protocol


# --- test doubles ---------------------------------------------------------

class FakeNet:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send(self, msg_type: str, **payload: Any) -> None:
        self.sent.append((msg_type, payload))


class FakeApp:
    """Just enough of the App surface for a Scene to run headless."""

    def __init__(self, *, host_mode: str = protocol.HOST_AUTO) -> None:
        self.width, self.height = 480, 800
        self.net = FakeNet()
        self.you = {"id": "p1", "name": "Alice"}
        self.room = {
            "owner_id": "p1",
            "host_id": "p1",
            "host_mode": host_mode,
            "players": [{"id": "p1", "name": "Alice", "ready": True, "connected": True}],
        }
        self.gamestate: Any = None
        self.scene: Any = None

    def go_to(self, scene: Any) -> None:
        self.scene = scene
        scene.on_enter()


def _gs(**over: Any) -> dict[str, Any]:
    """A minimal in-play game_state, tweakable per test."""
    gs = {
        "name": protocol.GAME_SNAKES_AND_LADDERS,
        "phase": protocol.PHASE_PLAY,
        "awaiting": "roll",
        "your_turn": True,
        "your_id": "p1",
        "you_role": "contestant",
        "current_pid": "p1",
        "players": [
            {"id": "p1", "name": "Alice", "pos": 1, "gold": 100, "items": [], "is_bot": False, "finished": False},
            {"id": "p2", "name": "Bob", "pos": 1, "gold": 100, "items": [], "is_bot": True, "finished": False},
        ],
        "deadline": None,
        "last_turn": None,
        "winner": None,
    }
    gs.update(over)
    return gs


# --- can_roll -------------------------------------------------------------

def test_can_roll_only_on_your_roll_turn_when_idle() -> None:
    assert can_roll(_gs(), animating=False) is True


def test_cannot_roll_while_animating() -> None:
    assert can_roll(_gs(), animating=True) is False


def test_cannot_roll_when_not_your_turn() -> None:
    assert can_roll(_gs(your_turn=False), animating=False) is False


def test_cannot_roll_while_shopping() -> None:
    assert can_roll(_gs(awaiting="shop"), animating=False) is False


def test_cannot_roll_after_gameover() -> None:
    assert can_roll(_gs(phase=protocol.PHASE_GAMEOVER), animating=False) is False


# --- usable_items ---------------------------------------------------------

def test_usable_items_lists_your_held_powerups_pre_roll() -> None:
    gs = _gs()
    gs["players"][0]["items"] = ["boost", "immunity"]
    assert usable_items(gs) == ["boost", "immunity"]


def test_no_usable_items_when_not_your_turn() -> None:
    gs = _gs(your_turn=False)
    gs["players"][0]["items"] = ["boost"]
    assert usable_items(gs) == []


def test_no_usable_items_while_shopping() -> None:
    gs = _gs(awaiting="shop")
    gs["players"][0]["items"] = ["boost"]
    assert usable_items(gs) == []


# --- countdown_seconds ----------------------------------------------------

def test_no_countdown_without_a_deadline() -> None:
    assert countdown_seconds(None, now=1000.0) is None


def test_countdown_ceils_remaining_seconds() -> None:
    assert countdown_seconds(1010.4, now=1000.0) == 11


def test_countdown_floors_at_zero() -> None:
    assert countdown_seconds(1000.0, now=1005.0) == 0


# --- is_runner ------------------------------------------------------------

def test_runner_is_owner_in_auto_mode() -> None:
    room = {"host_mode": protocol.HOST_AUTO, "owner_id": "p1"}
    assert is_runner(room, my_id="p1", role="contestant") is True
    assert is_runner(room, my_id="p2", role="contestant") is False


def test_runner_is_host_in_human_mode() -> None:
    room = {"host_mode": protocol.HOST_HUMAN, "owner_id": "p1"}
    assert is_runner(room, my_id="p1", role="host") is True
    assert is_runner(room, my_id="p1", role="contestant") is False


# --- animating_override ---------------------------------------------------

_LAYOUT = BoardLayout(cells=100, cols=10, area=(0, 0, 480, 480))


def test_no_override_when_animator_idle() -> None:
    anim = TokenAnimator()
    assert animating_override(anim, _LAYOUT) is None


def test_override_pins_mover_at_start_cell_during_opening_roll_pause() -> None:
    anim = TokenAnimator()
    anim.begin({"seq": 1, "pid": "p1", "name": "Alice", "steps": [
        {"t": "roll", "die": 3, "raw": 3, "modifier": None},
        {"t": "move", "frm": 1, "to": 4, "path": [2, 3, 4]},
    ]})
    # First segment is the roll pause: the mover rests at its start cell (1).
    result = animating_override(anim, _LAYOUT)
    assert result is not None
    pid, (x, y) = result
    assert pid == "p1"
    assert (x, y) == _LAYOUT.cell_to_xy(1)


def test_override_lerps_between_cells_mid_hop() -> None:
    anim = TokenAnimator()
    anim.begin({"seq": 1, "pid": "p1", "name": "Alice", "steps": [
        {"t": "move", "frm": 1, "to": 2, "path": [2]},
    ]})
    # Advance to the exact middle of the single hop (1 -> 2).
    anim.update(TokenAnimator.HOP_SECONDS / 2)
    pid, (x, y) = animating_override(anim, _LAYOUT)
    assert pid == "p1"
    ax, ay = _LAYOUT.cell_to_xy(1)
    bx, by = _LAYOUT.cell_to_xy(2)
    assert x == round(ax + (bx - ax) * 0.5)
    assert y == round(ay + (by - ay) * 0.5)


# --- scene construction + actions -----------------------------------------

def test_scene_caches_static_board_and_forwards_roll() -> None:
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        board={"cells": 100, "cols": 10, "snakes": {}, "ladders": {},
               "wheel_tiles": [], "shop_tiles": [], "gold_tiles": [], "debuff_tiles": []},
    )})
    assert scene.board is not None and scene.layout is not None
    scene._roll()
    assert app.net.sent == [(protocol.C_ROLL_DICE, {})]


def test_scene_use_buy_skip_send_the_right_messages() -> None:
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene._use("boost")
    scene._buy("immunity")
    scene._skip_shop()
    assert app.net.sent == [
        (protocol.C_USE_POWERUP, {"item": "boost"}),
        (protocol.C_BUY_ITEM, {"item": "immunity"}),
        (protocol.C_SKIP_SHOP, {}),
    ]


def test_scene_return_to_lobby_routes_back() -> None:
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_RETURN_TO_LOBBY})
    from client.scenes.lobby import LobbyScene
    assert isinstance(app.scene, LobbyScene)


# --- lobby go-live --------------------------------------------------------

def test_lobby_routes_started_game_to_snakes_scene() -> None:
    from client.scenes.lobby import LobbyScene
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    lobby.on_message({"type": protocol.S_GAME_STARTED})
    assert isinstance(app.scene, SnakesAndLaddersScene)


def test_lobby_start_sends_bot_count() -> None:
    from client.scenes.lobby import LobbyScene
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    assert lobby.bots == 0
    lobby._start()
    assert app.net.sent[-1] == (protocol.C_START_GAME, {"bots": 0})


def test_lobby_bots_stepper_clamps() -> None:
    from client.scenes.lobby import LobbyScene
    from config import MAX_PLAYERS_PER_ROOM
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    for _ in range(20):
        lobby._bots_inc()
    assert lobby.bots == MAX_PLAYERS_PER_ROOM - 1   # leave room for >=1 human
    for _ in range(20):
        lobby._bots_dec()
    assert lobby.bots == 0


def test_lobby_start_forwards_chosen_bot_count() -> None:
    from client.scenes.lobby import LobbyScene
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    lobby._bots_inc()
    lobby._bots_inc()
    lobby._start()
    assert app.net.sent[-1] == (protocol.C_START_GAME, {"bots": 2})


def _roster(app: FakeApp, n: int) -> None:
    app.room["players"] = [
        {"id": f"p{i}", "name": f"P{i}", "ready": True, "connected": True}
        for i in range(1, n + 1)
    ]


def test_lobby_bots_capped_by_free_seats() -> None:
    # Six players already in the room -> only two open seats for bots, so the
    # stepper must not let the count climb past 2 (the server would clamp it away).
    from client.scenes.lobby import LobbyScene
    from config import MAX_PLAYERS_PER_ROOM
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    _roster(app, 6)
    for _ in range(10):
        lobby._bots_inc()
    assert lobby.bots == MAX_PLAYERS_PER_ROOM - 6 == 2


def test_lobby_stepper_hidden_and_count_clamped_when_room_full() -> None:
    from client.scenes.lobby import LobbyScene
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    _roster(app, 8)                 # room full -> no seat for a bot
    assert lobby._max_bots() == 0
    assert lobby._show_stepper() is False
    lobby._bots_inc()
    assert lobby.bots == 0
    lobby._start()                  # a stale count would be clamped at start, too
    assert app.net.sent[-1] == (protocol.C_START_GAME, {"bots": 0})


# --- cutscene wiring (BUG: skip banner must survive the turn advance) ------

def test_skip_banner_survives_the_turn_advance() -> None:
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(current_pid="p1")})  # P1's turn
    # P1 rolls; P2 was skipped; the turn advances to P2. The skip banner must show
    # the *skipped* player and not be clobbered by a "P2's turn" announcement.
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "roll", "die": 3, "raw": 3, "modifier": None},
            {"t": "move", "frm": 1, "to": 4, "path": [2, 3, 4]},
            {"t": "skipped", "pid": "p2", "name": "Bob"},
        ]},
    )})
    assert scene.cutscene.kind == "skip"
    assert scene.cutscene.text == "Bob skipped!"


# --- input lock (BUG: gameover BACK was live during the win animation) -----

def test_runner_cannot_tear_to_lobby_during_the_win_animation() -> None:
    app = FakeApp()  # auto mode, p1 is the owner => the show-runner
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        phase=protocol.PHASE_GAMEOVER, current_pid="p1",
        winner={"id": "p1", "name": "Alice"},
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "roll", "die": 4, "raw": 4, "modifier": None},
            {"t": "move", "frm": 96, "to": 100, "path": [97, 98, 99, 100]},
            {"t": "win", "pid": "p1", "name": "Alice"},
        ]},
    )})
    assert scene.animator.is_playing            # the win timeline is still replaying
    # Click squarely on the BACK button while the animation plays.
    cx, cy = scene.back_btn.rect.center
    scene.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(cx, cy)))
    assert (protocol.C_RETURN_TO_LOBBY, {}) not in app.net.sent
