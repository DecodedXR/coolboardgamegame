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
from client.wheel import rest_angle, slice_at_pointer, spin_angle
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


# --- animation queueing (BUG: a fresh broadcast clobbered the in-flight replay) ---

class _CueLog:
    """A cue sink that records what the animator played, so a test can prove which
    queued turns actually replayed."""

    def __init__(self) -> None:
        self.played: list[str] = []

    def play(self, name: str) -> None:
        self.played.append(name)


def run_scene(scene: SnakesAndLaddersScene, *, until: Any, dt: float = 0.05,
              cap: int = 100000) -> None:
    """Pump ``scene.update`` (the real per-frame call) until ``until()`` holds."""
    n = 0
    while not until() and n < cap:
        scene.update(dt)
        n += 1
    assert until(), "scene did not reach the expected state"


def test_a_rapid_second_turn_does_not_clobber_the_first_replay() -> None:
    # Bots broadcast turns ~1.2s apart, but one turn's replay (roll + hops + wheel +
    # gold) can run ~3s. The scene used to call animator.begin() on every broadcast,
    # hard-resetting the in-flight replay -> the token teleported and the prior
    # turn's hops/wheel/gold were silently dropped. A fresh turn must QUEUE behind
    # the one still playing.
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "roll", "die": 5, "raw": 5, "modifier": None},
            {"t": "move", "frm": 1, "to": 6, "path": [2, 3, 4, 5, 6]},
        ]},
    )})
    assert scene.animator.is_playing and scene.animator.mover == "p1"
    # Advance partway through P1's replay (past the roll, into the hops).
    scene.update(TokenAnimator.ROLL_SECONDS + TokenAnimator.HOP_SECONDS)
    assert scene.animator.mover == "p1"
    # P2's turn arrives while P1 is still mid-replay.
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p1", your_turn=True,
        last_turn={"seq": 2, "pid": "p2", "name": "Bob", "steps": [
            {"t": "roll", "die": 2, "raw": 2, "modifier": None},
            {"t": "move", "frm": 1, "to": 3, "path": [2, 3]},
        ]},
    )})
    # The in-flight P1 replay is untouched; P2 is queued, not started.
    assert scene.animator.mover == "p1", "P2's broadcast clobbered P1's in-flight replay"
    assert scene.animator.progress() is not None or scene.animator.anchor_cell is not None
    # P1 finishes, then the queued P2 turn takes over, then the board goes idle.
    run_scene(scene, until=lambda: scene.animator.mover == "p2")
    run_scene(scene, until=lambda: not scene._busy)
    assert not scene.animator.is_playing and not scene._pending


def test_snaps_past_stale_turns_when_more_than_one_is_queued() -> None:
    # When the animator falls 2+ turns behind, the backlog snaps: only the NEWEST
    # queued turn is replayed (the others' results already show via players[*].pos),
    # so the board never drifts seconds behind the live game (the user's call).
    rec = _CueLog()
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.animator = TokenAnimator(rec)   # capture which queued turns truly replay
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "move", "frm": 1, "to": 7, "path": [2, 3, 4, 5, 6, 7]},
        ]},
    )})
    assert scene.animator.mover == "p1"
    scene.update(TokenAnimator.HOP_SECONDS)        # just into P1's long replay
    # Two more turns pile up: seq 2 carries a SNAKE, seq 3 a LADDER (distinct cues).
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p1", your_turn=True,
        last_turn={"seq": 2, "pid": "p2", "name": "Bob", "steps": [
            {"t": "move", "frm": 1, "to": 5, "path": [5]},
            {"t": "snake", "frm": 5, "to": 1},
        ]},
    )})
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 3, "pid": "p1", "name": "Alice", "steps": [
            {"t": "move", "frm": 7, "to": 8, "path": [8]},
            {"t": "ladder", "frm": 8, "to": 28},
        ]},
    )})
    assert len(scene._pending) == 2 and scene.animator.mover == "p1"
    run_scene(scene, until=lambda: not scene._busy)
    assert "ladder" in rec.played      # the newest queued turn replayed
    assert "snake" not in rec.played   # the stale middle turn was snapped past


def test_a_queued_turns_banner_fires_at_its_start_not_at_ingest() -> None:
    # The fix moved the cutscene announce from ingest to the turn's animation START.
    # A turn that arrives while a prior replay is in flight must not flash its banner
    # early — it shows only when its own replay begins.
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "move", "frm": 1, "to": 6, "path": [2, 3, 4, 5, 6]},
        ]},
    )})
    scene.update(TokenAnimator.HOP_SECONDS)
    # P2's turn (which SKIPPED Alice) arrives mid-replay -> it queues.
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p1", your_turn=True,
        last_turn={"seq": 2, "pid": "p2", "name": "Bob", "steps": [
            {"t": "move", "frm": 1, "to": 3, "path": [2, 3]},
            {"t": "skipped", "pid": "p1", "name": "Alice"},
        ]},
    )})
    assert scene.cutscene.kind != "skip"        # not flashed early, at ingest
    # Once P1 finishes and P2's queued replay begins, the banner fires.
    run_scene(scene, until=lambda: scene.animator.mover == "p2")
    assert scene.cutscene.kind == "skip" and scene.cutscene.text == "Alice skipped!"


def test_a_queued_turn_keeps_input_locked_before_update_drains_it() -> None:
    # There is a one-frame window (handle_event runs before update drains) where a
    # turn has finished but the next queued turn has not yet begun: the queue is
    # non-empty while the animator is idle. Input must stay locked there so nobody
    # rolls on a board that is about to move.
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    # P1 takes a one-hop turn; P2's turn arrives while it is still playing -> queued.
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "move", "frm": 1, "to": 2, "path": [2]},
        ]},
    )})
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p1", your_turn=True,
        last_turn={"seq": 2, "pid": "p2", "name": "Bob", "steps": [
            {"t": "move", "frm": 1, "to": 3, "path": [2, 3]},
        ]},
    )})
    # One frame finishes P1's hop but does NOT yet drain P2 (drain runs at the START
    # of update, before animator.update ends the hop): idle animator, non-empty queue.
    scene.update(TokenAnimator.HOP_SECONDS + 0.01)
    assert not scene.animator.is_playing and scene._pending   # the one-frame gap
    assert scene._busy and not can_roll(scene.gs, scene._busy)
    scene._roll()
    assert (protocol.C_ROLL_DICE, {}) not in app.net.sent


def test_leaving_the_scene_drops_queued_turns() -> None:
    # Tearing down the scene must clear the replay queue too, so a stale backlog can
    # never restart an animation on a torn-down board (the queue parallels the
    # animator's in-flight state and is reset with it).
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False,
        last_turn={"seq": 1, "pid": "p1", "name": "Alice", "steps": [
            {"t": "move", "frm": 1, "to": 7, "path": [2, 3, 4, 5, 6, 7]},
        ]},
    )})
    scene.update(TokenAnimator.HOP_SECONDS)            # P1 replaying
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p1", last_turn={"seq": 2, "pid": "p2", "name": "Bob", "steps": [
            {"t": "move", "frm": 1, "to": 3, "path": [2, 3]},
        ]},
    )})
    assert scene._pending and scene.animator.is_playing   # one queued behind the other
    scene.on_message({"type": protocol.S_RETURN_TO_LOBBY})
    from client.scenes.lobby import LobbyScene
    assert scene._pending == [] and not scene.animator.is_playing
    assert isinstance(app.scene, LobbyScene)   # the teardown path actually ran


# --- wheel hand-off (BUG: a long frame flashed the un-spun wheel, then vanished) ---

_WHEEL_TABLE = [
    {"kind": "gold", "amount": 5}, {"kind": "debuff", "debuff": "skip_next"},
    {"kind": "item", "item": "boost"}, {"kind": "gold", "amount": 9},
]


def _wheel_turn(seq: int = 1, index: int = 1) -> dict[str, Any]:
    """A one-roll turn that lands on a wheel tile: roll -> 3 hops -> wheel -> gold."""
    return {"seq": seq, "pid": "p1", "name": "Alice", "steps": [
        {"t": "roll", "die": 3, "raw": 3, "modifier": None},
        {"t": "move", "frm": 1, "to": 4, "path": [2, 3, 4]},
        {"t": "wheel", "table": _WHEEL_TABLE, "index": index,
         "outcome": _WHEEL_TABLE[index]},
        {"t": "gold", "amount": 5},
    ]}


def _start_wheel_turn(index: int = 1) -> SnakesAndLaddersScene:
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p2", your_turn=False, last_turn=_wheel_turn(index=index))})
    return scene


def test_a_long_frame_renders_the_wheel_at_its_true_spin_not_unspun() -> None:
    # clock.tick() isn't clamped, so a backgrounded browser tab resumes with ONE big
    # dt and the animator blows deep into the wheel beat that single frame. The wheel
    # must render at its TRUE progress in the spin, not flash at its un-spun start
    # (angle 0) and vanish -- the old parallel widget clock began() at _t=0 a frame
    # behind the animator, so a long frame only ever showed frac 0.
    scene = _start_wheel_turn(index=1)
    # One big frame: past roll (.4) + 3 hops (.54), then halfway (0.8s) into the 1.6s beat.
    scene.update(TokenAnimator.ROLL_SECONDS + 3 * TokenAnimator.HOP_SECONDS
                 + TokenAnimator.WHEEL_SECONDS / 2)
    assert scene.wheel.is_visible
    # The widget tracks the animator's beat exactly (it is driven from wheel_progress),
    # so it sits ~half-spun -- well past the un-spun start, never at angle 0.
    prog = scene.animator.wheel_progress
    assert prog is not None and prog > 0.4
    assert scene.wheel.angle == spin_angle(1, len(_WHEEL_TABLE), prog)
    assert scene.wheel.angle > 0.5 * rest_angle(1, len(_WHEEL_TABLE))


def test_the_wheel_tracks_the_animator_across_the_beat_then_clears() -> None:
    # Integrated animator+wheel seam (the audit found no test covered it): stepped
    # frame-by-frame, the wheel follows the animator's wheel beat without ever visibly
    # reversing, settles on the chosen slice, and disappears the instant the beat ends
    # and the trailing gold beat begins.
    scene = _start_wheel_turn(index=2)
    run_scene(scene, until=lambda: scene.animator.wheel_progress is not None, dt=0.02)
    prev = -1.0
    last_angle = 0.0
    saw_spin = False
    while scene.animator.wheel_progress is not None:
        assert scene.wheel.is_visible
        angle = scene.wheel.angle
        assert angle >= prev - 1e-9          # monotonic: the wheel never visibly reverses
        prev = angle
        last_angle = angle
        saw_spin = True
        scene.update(0.05)
    assert saw_spin
    assert slice_at_pointer(last_angle, len(_WHEEL_TABLE)) == 2   # settled on the chosen slice
    # The beat is over; the overlay is gone even though the animator (gold beat) plays on.
    assert not scene.wheel.is_visible
    assert scene.animator.is_playing


def test_leaving_the_scene_clears_the_wheel_overlay() -> None:
    # The wheel overlay is downstream of the animator, so tearing down the scene
    # mid-spin must clear it too -- it must not linger on a torn-down board.
    scene = _start_wheel_turn(index=1)
    run_scene(scene, until=lambda: scene.wheel.is_visible, dt=0.02)
    assert scene.wheel.is_visible
    scene.on_message({"type": protocol.S_RETURN_TO_LOBBY})
    assert not scene.wheel.is_visible


# --- item buttons (the powerup row rebuilt per frame from the hand) --------
#
# usable_items() (the data) is pinned above; these pin _sync_item_buttons() (the
# widgets it builds from that data) -- the per-frame rebuild that the scene's
# update() runs and handle_event() forwards clicks to. None of it touches a
# Surface, so it runs headless: we set a game_state, sync, and assert on the
# button list (names, callbacks, layout) and the message a click would send.

from client.shop_ui import item_label   # noqa: E402  (grouped with its tests)


def _my_turn_holding(*items: str) -> tuple[FakeApp, SnakesAndLaddersScene]:
    """A scene parked on our own pre-roll turn, holding the given powerups."""
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    gs = _gs()
    gs["players"][0]["items"] = list(items)
    app.gamestate = gs
    return app, scene


def test_item_buttons_build_one_per_held_powerup_in_order() -> None:
    _app, scene = _my_turn_holding("boost", "immunity", "double")
    scene._sync_item_buttons()
    assert [name for name, _ in scene._item_buttons] == ["boost", "immunity", "double"]
    # Each carries its friendly catalog label, not the raw item key.
    assert [btn.label for _, btn in scene._item_buttons] == [
        item_label("boost"), item_label("immunity"), item_label("double")
    ]


def test_each_item_button_arms_its_own_powerup() -> None:
    # The classic late-binding trap: a `lambda: self._use(item)` would make EVERY
    # button arm the last item. The `it=item` capture must bind per button, so
    # firing button i sends C_USE_POWERUP for item i -- not the last one.
    app, scene = _my_turn_holding("boost", "immunity", "double")
    scene._sync_item_buttons()
    for name, btn in scene._item_buttons:
        app.net.sent.clear()
        btn.on_click()
        assert app.net.sent == [(protocol.C_USE_POWERUP, {"item": name})]


def test_clicking_an_item_button_arms_that_powerup_through_handle_event() -> None:
    # The full input path: update() builds the row, then a real MOUSEBUTTONDOWN at
    # the second button's center routes through handle_event's item-button loop and
    # arms exactly that powerup.
    app, scene = _my_turn_holding("boost", "immunity")
    scene.update(0.0)
    target_name, target_btn = scene._item_buttons[1]
    assert target_name == "immunity"
    cx, cy = target_btn.rect.center
    scene.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(cx, cy)))
    assert (protocol.C_USE_POWERUP, {"item": "immunity"}) in app.net.sent


def test_no_item_buttons_off_turn_or_while_shopping() -> None:
    app = FakeApp()
    scene = SnakesAndLaddersScene(app)
    scene.on_enter()
    # Holding powerups, but it isn't our roll-turn -> no row.
    not_my_turn = _gs(your_turn=False)
    not_my_turn["players"][0]["items"] = ["boost"]
    app.gamestate = not_my_turn
    scene._sync_item_buttons()
    assert scene._item_buttons == []
    # Our turn but in the shop sub-state -> still no arming row.
    shopping = _gs(awaiting="shop")
    shopping["players"][0]["items"] = ["boost"]
    app.gamestate = shopping
    scene._sync_item_buttons()
    assert scene._item_buttons == []


def test_no_item_buttons_while_the_board_is_busy() -> None:
    # Arming is locked while a replay is in flight or a turn is still queued, so the
    # row must clear even though we hold the items and it is our pre-roll turn.
    _app, scene = _my_turn_holding("boost", "immunity")
    scene._sync_item_buttons()
    assert [name for name, _ in scene._item_buttons] == ["boost", "immunity"]
    scene._pending.append({"last_turn": None, "current_pid": "p1", "winner": False})
    assert scene._busy
    scene._sync_item_buttons()
    assert scene._item_buttons == []


def test_item_row_is_not_rebuilt_until_the_hand_changes() -> None:
    # An unchanged hand keeps the SAME button objects across frames (so hover state
    # and the like survive); a changed hand rebuilds fresh.
    _app, scene = _my_turn_holding("boost", "immunity")
    scene._sync_item_buttons()
    first_row = scene._item_buttons
    first_btn = first_row[0][1]
    scene._sync_item_buttons()
    assert scene._item_buttons is first_row          # not rebuilt
    assert scene._item_buttons[0][1] is first_btn
    # Acquire a third powerup -> the row is rebuilt with new buttons.
    scene.gs["players"][0]["items"] = ["boost", "immunity", "double"]
    scene._sync_item_buttons()
    assert [name for name, _ in scene._item_buttons] == ["boost", "immunity", "double"]
    assert scene._item_buttons[0][1] is not first_btn


def test_item_button_row_is_horizontally_centered() -> None:
    _app, scene = _my_turn_holding("boost", "immunity", "double")
    scene._sync_item_buttons()
    left_margin = scene._item_buttons[0][1].rect.x
    right_margin = scene.app.width - scene._item_buttons[-1][1].rect.right
    assert abs(left_margin - right_margin) <= 1      # centered (tolerate int rounding)
    # Laid left-to-right without overlap.
    xs = [btn.rect.x for _, btn in scene._item_buttons]
    assert xs == sorted(xs)
    for (_, a), (_, b) in zip(scene._item_buttons, scene._item_buttons[1:]):
        assert b.rect.x >= a.rect.right
    # A realistic hand (<=4 distinct powerups) fits on the canvas. NB: a hand of 5+
    # items overflows the row off-canvas (centered but unconstrained) -- a known
    # cosmetic follow-up that needs a UX call on laying many buttons out, so it is
    # deliberately left unpinned here rather than asserted either way.
    assert all(0 <= btn.rect.x and btn.rect.right <= scene.app.width
               for _, btn in scene._item_buttons)


def test_duplicate_held_items_each_get_their_own_button() -> None:
    # The server can hand a player two of the same powerup (e.g. buy a second
    # boost), so the row builds one independently-wired button per copy rather than
    # collapsing them, and each still arms that powerup.
    app, scene = _my_turn_holding("boost", "boost", "immunity")
    scene._sync_item_buttons()
    assert [name for name, _ in scene._item_buttons] == ["boost", "boost", "immunity"]
    fired = []
    for _name, btn in scene._item_buttons:
        app.net.sent.clear()
        btn.on_click()
        fired.append(app.net.sent[0])
    assert fired == [
        (protocol.C_USE_POWERUP, {"item": "boost"}),
        (protocol.C_USE_POWERUP, {"item": "boost"}),
        (protocol.C_USE_POWERUP, {"item": "immunity"}),
    ]


def test_item_buttons_stay_hidden_through_a_real_replay_then_reappear() -> None:
    # _busy has two causes; the test above pins the _pending-queue one, this pins the
    # animator actually playing. A broadcast that makes it our pre-roll turn but
    # carries a bot's last_turn to replay must keep the arming row hidden for the
    # whole replay (update() re-syncs every frame), then show it once the board idles.
    _app, scene = _my_turn_holding("boost")
    scene.on_message({"type": protocol.S_GAME_STATE, "game": _gs(
        current_pid="p1", your_turn=True,
        last_turn={"seq": 1, "pid": "p2", "name": "Bob", "steps": [
            {"t": "move", "frm": 1, "to": 5, "path": [2, 3, 4, 5]},
        ]},
        players=[
            {"id": "p1", "name": "Alice", "pos": 1, "gold": 100, "items": ["boost"],
             "is_bot": False, "finished": False},
            {"id": "p2", "name": "Bob", "pos": 5, "gold": 100, "items": [],
             "is_bot": True, "finished": False},
        ],
    )})
    assert scene.animator.is_playing and scene._busy
    saw_busy_frame = False
    for _ in range(500):
        if not scene._busy:
            break
        saw_busy_frame = True
        assert scene._item_buttons == []      # hidden for every in-flight replay frame
        scene.update(0.05)
    assert saw_busy_frame and not scene._busy, "the replay never finished"
    scene.update(0.0)                          # idle board: our pre-roll turn, boost in hand
    assert [name for name, _ in scene._item_buttons] == ["boost"]
