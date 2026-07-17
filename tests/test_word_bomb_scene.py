"""Headless tests for the Word Bomb scene + lobby routing.

Mirrors ``tests.test_snakes_scene``'s dummy-app pattern (reusing its ``FakeApp`` /
``FakeNet``). The pure design helpers (pressure curve, heat ramp, fuse/seat
geometry, feed text) are pinned without a Surface; ``draw`` is exercised on a real
480x800 Surface across every visual mode, and the feed-``id`` dedup / bomb-pass
transition rules (the two anti-spam contracts) are pinned by ingestion tests.
"""

from __future__ import annotations

import time
from typing import Any

import pygame
import pytest

from client import ui
from client import sfx
from client.scenes.lobby import LobbyScene
from client.scenes.snakes_and_ladders import SnakesAndLaddersScene
from client.scenes.word_bomb import (
    WordBombScene,
    HOT,
    SHAKE_TIME,
    FLASH_TIME,
    feed_line,
    fmt_options,
    fuse_points,
    heat_color,
    press_of,
    seat_positions,
    tail_that_fits,
)
from shared import protocol
from tests.test_snakes_scene import FakeApp, FakeNet  # noqa: F401


@pytest.fixture(autouse=True)
def _font():
    if not pygame.font.get_init():
        pygame.font.init()


def _wb_gs(**over: Any) -> dict[str, Any]:
    """A synthetic Word Bomb game_state matching the server's public() shape."""
    gs = {
        "name": protocol.GAME_WORD_BOMB,
        "phase": protocol.PHASE_PLAY,
        "awaiting": protocol.AWAIT_WORD,
        "prompt": "ca",
        "players": [
            {"id": "p1", "name": "Alice", "lives": 2, "is_bot": False, "alive": True},
            {"id": "p2", "name": "Bob", "lives": 2, "is_bot": True, "alive": True},
        ],
        "current_pid": "p1",
        "your_turn": True,
        "your_id": "p1",
        "you_role": "contestant",
        "deadline": None,
        "feed": [],
        "winner": None,
        "used_count": 0,
    }
    gs.update(over)
    return gs


class _RecSfx:
    """A recording sound stub so tests can prove which cues fired (headless)."""

    def __init__(self) -> None:
        self.played: list[str] = []

    def init(self) -> None:
        pass

    def pump(self) -> None:
        pass

    def play(self, name: str) -> None:
        self.played.append(name)


def _scene(**app_over) -> tuple[Any, WordBombScene]:
    app = FakeApp(**app_over)
    scene = WordBombScene(app)
    scene.on_enter()
    return app, scene


# --- lobby routing --------------------------------------------------------

def test_lobby_routes_word_bomb_to_the_word_bomb_scene() -> None:
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    lobby.on_message({"type": protocol.S_GAME_STARTED, "game": protocol.GAME_WORD_BOMB})
    assert isinstance(app.scene, WordBombScene)


def test_lobby_still_routes_snakes_to_the_snakes_scene() -> None:
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    lobby.on_message({"type": protocol.S_GAME_STARTED, "game": protocol.GAME_SNAKES_AND_LADDERS})
    assert isinstance(app.scene, SnakesAndLaddersScene)


def test_lobby_start_includes_the_chosen_game() -> None:
    app = FakeApp()
    lobby = LobbyScene(app)
    lobby.on_enter()
    lobby._start()
    msg_type, payload = app.net.sent[-1]
    assert msg_type == protocol.C_START_GAME
    assert payload.get("game") == protocol.GAME_WORD_BOMB   # the default the lobby picks


# --- pure design helpers --------------------------------------------------

def test_press_of() -> None:
    assert press_of(12, 12) == 0.0
    assert press_of(0, 12) == 1.0
    assert press_of(None, 12) == 0.25
    assert press_of(None, None) == 0.25


def test_heat_color_endpoints() -> None:
    assert heat_color(0) == ui.MUTED
    assert heat_color(0.6) == ui.ACCENT
    assert heat_color(1) == HOT


def test_fuse_points_has_24() -> None:
    assert len(fuse_points()) == 24


def test_seat_positions_on_canvas_seat0_topmost() -> None:
    for n in range(2, 9):
        pts = seat_positions(n)
        assert len(pts) == n
        for x, y in pts:
            assert 0 <= x <= 480 and 0 <= y <= 800
        assert min(pts, key=lambda p: p[1]) == pts[0]   # seat 0 is the topmost


def test_fmt_options_switches_to_k_at_ten_thousand() -> None:
    assert fmt_options(0) == "0 words"
    assert fmt_options(804) == "804 words"
    assert fmt_options(9999) == "9999 words"
    assert fmt_options(10000) == "10k words"
    assert fmt_options(19983) == "20k words"


def test_feed_line_formats_every_kind_in_ascii() -> None:
    events = [
        {"kind": "accept", "name": "Al", "word": "cat"},
        {"kind": "reject", "name": "Al", "word": "dog", "reason": "not_in_prompt", "prompt": "ca"},
        {"kind": "reject", "name": "Al", "word": "dog", "reason": "not_a_word"},
        {"kind": "reject", "name": "Al", "word": "dog", "reason": "already_used"},
        {"kind": "explode", "name": "Al"},
        {"kind": "eliminated", "name": "Al"},
    ]
    for e in events:
        s = feed_line(e)
        assert s and s == s.encode("ascii", "ignore").decode()


# --- draw across every visual mode ----------------------------------------

def test_draw_runs_in_all_visual_modes() -> None:
    app, scene = _scene()
    surf = pygame.Surface((480, 800))

    scene.draw(surf)   # no state yet -> title only

    # mid-game with a nearly-expired fuse (critical band + strobe + vignette).
    app.gamestate = _wb_gs(deadline=time.time() + 0.5)
    scene._turn_total = 12.0
    scene.draw(surf)

    # human-host mode: no fuse, and we are the show-runner.
    app.room = {"host_mode": protocol.HOST_HUMAN, "owner_id": "p1", "host_id": "p1",
                "code": "ABCD", "players": []}
    app.gamestate = _wb_gs(deadline=None, you_role="host")
    scene.draw(surf)

    # sudden death: 2 alive, one life each.
    app.gamestate = _wb_gs(players=[
        {"id": "p1", "name": "Alice", "lives": 1, "is_bot": False, "alive": True},
        {"id": "p2", "name": "Bob", "lives": 1, "is_bot": True, "alive": True},
    ])
    scene.draw(surf)

    # gameover with a winner.
    app.gamestate = _wb_gs(phase=protocol.PHASE_GAMEOVER,
                           winner={"id": "p1", "name": "Alice"})
    scene.draw(surf)

    # after ingesting an explosion (particles + shake + flash active).
    app.gamestate = None
    scene._ingest_state(_wb_gs(feed=[{"id": 1, "kind": "explode", "name": "Alice",
                                      "pid": "p1", "prompt": "ca", "seq": 1}]))
    scene.draw(surf)


# --- event ingestion (feed id dedup + transition rules) -------------------

def test_explode_event_shakes_flashes_and_spawns_26() -> None:
    _app, scene = _scene()
    scene._ingest_state(_wb_gs(feed=[{"id": 1, "kind": "explode", "name": "A", "pid": "p1"}]))
    assert scene._shake > 0 and scene._flash > 0
    assert len(scene._particles) == 26


def test_reingesting_the_same_feed_spawns_nothing() -> None:
    _app, scene = _scene()
    state = _wb_gs(feed=[{"id": 1, "kind": "explode", "name": "A", "pid": "p1"}])
    scene._ingest_state(state)
    assert len(scene._particles) == 26
    scene._ingest_state(state)             # same ids -> the _seen_id guard blocks it
    assert len(scene._particles) == 26


def test_two_rejects_sharing_a_seq_both_react() -> None:
    # The double-reject bug the event `id` exists to prevent: a seq-keyed dedup would
    # swallow the second reject. Both must react (both fire type_bad).
    _app, scene = _scene()
    scene.sfx = _RecSfx()
    scene._ingest_state(_wb_gs(feed=[
        {"id": 1, "kind": "reject", "name": "B", "pid": "p2", "reason": "not_a_word",
         "word": "x", "prompt": "ca", "seq": 5},
        {"id": 2, "kind": "reject", "name": "A", "pid": "p1", "reason": "not_a_word",
         "word": "y", "prompt": "ca", "seq": 5},
    ]))
    assert scene.sfx.played.count("type_bad") == 2
    assert scene._input_flash > 0          # the second reject was mine


def test_eliminated_event_adds_14_more_and_extends_the_shake() -> None:
    _app, scene = _scene()
    scene._ingest_state(_wb_gs(feed=[
        {"id": 1, "kind": "explode", "name": "A", "pid": "p1"},
        {"id": 2, "kind": "eliminated", "name": "A", "pid": "p1"},
    ]))
    assert len(scene._particles) == 26 + 14
    assert scene._shake == 0.8             # the bigger blast


def test_bomb_pass_spawns_a_trail_only_on_a_real_transition() -> None:
    _app, scene = _scene()
    scene.sfx = _RecSfx()
    scene._ingest_state(_wb_gs(current_pid="p1"))   # None -> p1: no trail
    assert scene._particles == []
    scene._ingest_state(_wb_gs(current_pid="p2"))   # p1 -> p2: 10-particle trail
    assert len(scene._particles) == 10
    assert scene.sfx.played.count("pass") == 1
    scene._ingest_state(_wb_gs(current_pid="p2"))   # same holder: nothing (anti-spam)
    assert len(scene._particles) == 10
    assert scene.sfx.played.count("pass") == 1


def test_particle_population_never_exceeds_120() -> None:
    _app, scene = _scene()
    for i in range(10):                    # 10 explosions * 26 = 260, capped at 120
        scene._ingest_state(_wb_gs(feed=[{"id": i + 1, "kind": "explode",
                                          "name": "A", "pid": "p1"}]))
    assert len(scene._particles) <= 120


def test_reject_input_flash_is_personal() -> None:
    _app, scene = _scene()
    scene._ingest_state(_wb_gs(feed=[{"id": 1, "kind": "reject", "name": "B", "pid": "p2",
                                      "reason": "not_a_word", "word": "x", "prompt": "ca"}]))
    assert scene._input_flash == 0         # someone else's reject
    scene._ingest_state(_wb_gs(feed=[{"id": 2, "kind": "reject", "name": "A", "pid": "p1",
                                      "reason": "not_a_word", "word": "y", "prompt": "ca"}]))
    assert scene._input_flash > 0          # my reject rattles my input


# --- submit -----------------------------------------------------------------

def test_submit_sends_the_word_and_clears_but_ignores_empty() -> None:
    app, scene = _scene()
    scene.typed = "hello"
    scene._submit()
    assert app.net.sent == [
        (protocol.C_SUBMIT_WORD, {"word": "hello"}),
        (protocol.C_TYPING, {"text": ""}),      # clears everyone's live view
    ]
    assert scene.typed == ""
    app.net.sent.clear()
    scene._submit()                        # empty -> nothing sent
    assert app.net.sent == []


# --- typing: keys land on the bomb, every edit is relayed --------------------

def _key(key: int, unicode: str = "") -> pygame.event.Event:
    return pygame.event.Event(pygame.KEYDOWN, key=key, unicode=unicode)


def test_typing_a_key_appends_and_relays_but_a_bare_modifier_is_silent() -> None:
    app, scene = _scene()
    app.gamestate = _wb_gs()               # your turn, PLAY phase
    scene.sfx = _RecSfx()

    scene.handle_event(_key(pygame.K_a, "a"))
    assert scene.typed == "a"
    assert app.net.sent == [(protocol.C_TYPING, {"text": "a"})]

    app.net.sent.clear()
    scene.handle_event(_key(pygame.K_LSHIFT, ""))   # modifier -> no text, no send
    assert scene.typed == "a"
    assert app.net.sent == []


def test_enter_submits_then_clears_everyone() -> None:
    app, scene = _scene()
    app.gamestate = _wb_gs()
    scene.sfx = _RecSfx()
    scene.typed = "cat"

    scene.handle_event(_key(pygame.K_RETURN))
    assert scene.typed == ""
    assert app.net.sent == [
        (protocol.C_SUBMIT_WORD, {"word": "cat"}),
        (protocol.C_TYPING, {"text": ""}),
    ]


def test_on_message_typing_sets_live_only_for_the_current_other_player() -> None:
    app, scene = _scene()
    app.gamestate = _wb_gs(current_pid="p2", your_turn=False)

    scene.on_message({"type": protocol.S_TYPING, "pid": "p2", "text": "ab"})
    assert scene.live_typing == "ab"       # current + not me -> shown

    scene.live_typing = "sentinel"
    scene.on_message({"type": protocol.S_TYPING, "pid": "p1", "text": "zz"})
    assert scene.live_typing == "sentinel"  # my own id -> ignored


def test_current_pid_change_clears_typed_and_live() -> None:
    app, scene = _scene()
    scene._prev_current = "p2"
    scene.typed = "half"
    scene.live_typing = "theirs"
    scene._ingest_state(_wb_gs(current_pid="p1"))   # turn hands to me
    assert scene.typed == ""
    assert scene.live_typing == ""


def test_browser_bomb_tap_prompts_and_submits_only_on_the_bomb(monkeypatch) -> None:
    from client import browser_io
    app, scene = _scene()
    app.gamestate = _wb_gs()
    scene.sfx = _RecSfx()

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(browser_io, "is_browser", lambda: True)
    monkeypatch.setattr(browser_io, "prompt", lambda label, current: opened.append((label, current)) or "hello")

    scene.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(240, 300)))
    assert opened == [("type a word...", "")]
    assert (protocol.C_SUBMIT_WORD, {"word": "hello"}) in app.net.sent

    opened.clear()
    scene.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(24, 706)))
    assert opened == []                    # off-bomb tap opens no prompt


def test_tail_that_fits_keeps_the_end() -> None:
    assert tail_that_fits("abcdef", 3, len) == "def"
    assert tail_that_fits("ab", 3, len) == "ab"


# --- sfx catalog ------------------------------------------------------------

def test_word_bomb_sfx_cues_are_present_and_short() -> None:
    for name in ("tick", "tick_hot", "pass", "alarm", "type_ok", "type_bad",
                 "boom", "dirge", "sudden_death"):
        assert name in sfx.SOUNDS, f"missing cue {name!r}"
        segs = sfx.SOUNDS[name]
        for seg in segs:
            assert len(seg) == 3
            freq, dur, shape = seg
            assert isinstance(freq, float) and isinstance(dur, float)
            assert shape in {"sine", "square", "saw"}
        assert sum(d for _, d, _ in segs) < 0.8, f"{name} is not a short one-shot"
