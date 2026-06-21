"""Unit tests for the turn-timeline animator (pure logic, no display).

The animator replays a server ``last_turn`` once (keyed off ``seq``), walking the
mover's token through per-cell hops and snake/ladder slides while firing sound
cues, and exposing the wheel hand-off seam. It works entirely in cell space and
fractional progress; the scene maps that to pixels, so all of this is testable
headless.
"""

from __future__ import annotations

from client.token_anim import TokenAnimator


class RecSfx:
    """Records which cues were played, in order."""

    def __init__(self) -> None:
        self.played: list[str] = []

    def play(self, name: str) -> None:
        self.played.append(name)


def turn(seq, steps, pid="p1"):
    return {"seq": seq, "pid": pid, "name": "P1", "ended": True, "steps": steps}


def run_to_end(a: TokenAnimator, step=0.05, cap=100000) -> None:
    n = 0
    while a.is_playing and n < cap:
        a.update(step)
        n += 1
    assert not a.is_playing, "animation did not terminate"


def test_begin_ignores_stale_or_equal_seq() -> None:
    a = TokenAnimator()
    assert a.begin(turn(1, [{"t": "move", "frm": 1, "to": 2, "path": [2]}])) is True
    # Re-feeding the same seq (every frame) must not restart it.
    assert a.begin(turn(1, [{"t": "move", "frm": 1, "to": 9, "path": [9]}])) is False
    run_to_end(a)
    assert a.begin(turn(1, [{"t": "move", "frm": 1, "to": 9, "path": [9]}])) is False
    # A newer turn does play.
    assert a.begin(turn(2, [{"t": "move", "frm": 1, "to": 9, "path": [9]}])) is True


def test_begin_with_no_turn_is_safe() -> None:
    a = TokenAnimator()
    assert a.begin(None) is False
    assert not a.is_playing


def test_move_splits_into_per_cell_hops_and_interpolates() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [{"t": "move", "frm": 1, "to": 3, "path": [2, 3]}]))
    frm, to, frac = a.progress()
    assert (frm, to) == (1, 2) and frac == 0.0
    a.update(TokenAnimator.HOP_SECONDS / 2)
    frm, to, frac = a.progress()
    assert (frm, to) == (1, 2) and 0.0 < frac < 1.0
    a.update(TokenAnimator.HOP_SECONDS)  # cross into the second hop
    frm, to, _ = a.progress()
    assert (frm, to) == (2, 3)
    run_to_end(a)
    assert a.progress() is None


def test_mover_is_the_turn_pid_and_clears_when_done() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [{"t": "move", "frm": 1, "to": 2, "path": [2]}], pid="p2"))
    assert a.mover == "p2"
    run_to_end(a)
    assert a.mover is None


def test_snake_and_ladder_become_slide_segments() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [
        {"t": "move", "frm": 1, "to": 16, "path": [16]},
        {"t": "snake", "frm": 16, "to": 6},
    ]))
    assert a.progress()[:2] == (1, 16)        # the hop to the landing cell
    a.update(TokenAnimator.HOP_SECONDS)
    assert a.progress()[:2] == (16, 6)        # then the snake slide

    b = TokenAnimator()
    b.begin(turn(1, [
        {"t": "move", "frm": 1, "to": 3, "path": [3]},
        {"t": "ladder", "frm": 3, "to": 21},
    ]))
    b.update(TokenAnimator.HOP_SECONDS)
    assert b.progress()[:2] == (3, 21)


def test_slip_back_debuff_animates_as_a_slide() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [
        {"t": "move", "frm": 1, "to": 8, "path": [8]},
        {"t": "debuff", "pid": "p1", "debuff": "slip_back", "frm": 8, "to": 2},
    ]))
    a.update(TokenAnimator.HOP_SECONDS)
    assert a.progress()[:2] == (8, 2)


def test_sfx_fires_once_per_segment_in_timeline_order() -> None:
    rec = RecSfx()
    a = TokenAnimator(sfx=rec)
    a.begin(turn(1, [
        {"t": "roll", "die": 4, "raw": 4, "modifier": None},
        {"t": "move", "frm": 1, "to": 3, "path": [2, 3]},
        {"t": "snake", "frm": 3, "to": 1},
    ]))
    run_to_end(a)
    assert rec.played == ["roll", "hop", "hop", "snake"]


def test_wheel_step_is_exposed_only_during_its_segment() -> None:
    a = TokenAnimator()
    wheel_step = {
        "t": "wheel",
        "table": [{"kind": "gold", "amount": 50}],
        "index": 0,
        "outcome": {"kind": "gold", "amount": 50},
    }
    a.begin(turn(1, [
        {"t": "move", "frm": 1, "to": 2, "path": [2]},
        wheel_step,
        {"t": "gold", "pid": "p1", "delta": 50, "total": 150},
    ]))
    assert a.wheel is None                      # during the hop
    a.update(TokenAnimator.HOP_SECONDS)
    assert a.wheel == wheel_step                # wheel segment active
    a.update(TokenAnimator.WHEEL_SECONDS)
    assert a.wheel is None                      # moved on to the gold beat
    run_to_end(a)
    assert a.wheel is None


def test_progress_is_none_during_pause_segments() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [{"t": "roll", "die": 2, "raw": 2, "modifier": None}]))
    assert a.is_playing
    assert a.progress() is None  # a roll is a non-positional pause
    run_to_end(a)


def test_win_step_plays_the_win_cue() -> None:
    rec = RecSfx()
    a = TokenAnimator(sfx=rec)
    a.begin(turn(1, [
        {"t": "move", "frm": 99, "to": 100, "path": [100]},
        {"t": "win", "pid": "p1", "name": "P1"},
    ]))
    run_to_end(a)
    assert rec.played == ["hop", "win"]


def test_buy_step_gets_a_beat_and_the_buy_cue() -> None:
    # A shop purchase commits last_turn = [{"t":"buy",...}] and doesn't move the
    # token; it must still animate (a beat) and fire the buy cue, not be silent.
    rec = RecSfx()
    a = TokenAnimator(sfx=rec)
    assert a.begin(turn(1, [{"t": "buy", "pid": "p1", "item": "reroll", "price": 30, "total": 70}])) is True
    assert a.is_playing
    run_to_end(a)
    assert rec.played == ["buy"]


def test_anchor_pins_mover_at_origin_through_the_leading_roll_pause() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [
        {"t": "roll", "die": 6, "raw": 6, "modifier": None},
        {"t": "move", "frm": 1, "to": 7, "path": [2, 3, 4, 5, 6, 7]},
    ]))
    # During the opening roll pause progress() is None, but anchor_cell holds the
    # token at its START cell (not the authoritative final cell) -> no teleport.
    assert a.progress() is None
    assert a.anchor_cell == 1
    a.update(TokenAnimator.ROLL_SECONDS)  # into the first hop
    assert a.progress()[:2] == (1, 2)
    run_to_end(a)
    assert a.anchor_cell is None  # idle -> scene snaps to authoritative pos


def test_anchor_tracks_landing_cell_during_a_trailing_pause() -> None:
    a = TokenAnimator()
    a.begin(turn(1, [
        {"t": "move", "frm": 1, "to": 5, "path": [2, 3, 4, 5]},
        {"t": "gold", "pid": "p1", "delta": 30, "total": 130},
    ]))
    a.update(TokenAnimator.HOP_SECONDS * 4 + 0.001)  # finish all hops, enter the gold beat
    assert a.progress() is None      # gold is a pause
    assert a.anchor_cell == 5        # token rests at the landing cell, not teleporting


def test_new_game_replays_when_seq_jumps_backwards() -> None:
    a = TokenAnimator()
    assert a.begin(turn(5, [{"t": "move", "frm": 1, "to": 2, "path": [2]}])) is True
    run_to_end(a)
    # A new game restarts the server seq at 1; it must replay, not be stale-gated.
    assert a.begin(turn(1, [{"t": "move", "frm": 1, "to": 2, "path": [2]}])) is True
