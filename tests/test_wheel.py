"""Unit tests for the Wheel-of-Names spin geometry (pure, no display).

The server hands the client a slice ``table`` and the winning ``index``; the
client's only job is to spin and *land that index* under a fixed pointer. The
geometry is pinned here by round-tripping the render angle back through the
inverse map (``slice_at_pointer``) — a non-tautological property: a wrong rotation
formula lands the pointer on the wrong slice and the round-trip fails.
"""

from __future__ import annotations

import math

from client import wheel
from client.wheel import Wheel, rest_angle, slice_at_pointer, slice_label, spin_angle


def test_resting_spin_lands_the_chosen_index_for_every_slice() -> None:
    # The defining property: at the end of the spin, the chosen slice is under the
    # pointer. Checked for several slice counts incl. the engine's 8-outcome table.
    for n in (3, 4, 6, 8, 12):
        for index in range(n):
            angle = spin_angle(index, n, 1.0)
            assert slice_at_pointer(angle, n) == index, (n, index)


def test_overshooting_frac_still_rests_on_the_chosen_index() -> None:
    # frac is clamped, so a late/over-long frame can't drift the wheel off-target.
    assert spin_angle(5, 8, 2.0) == rest_angle(5, 8)
    assert slice_at_pointer(spin_angle(5, 8, 9.9), 8) == 5


def test_spin_is_monotonic_non_decreasing_so_it_never_visibly_reverses() -> None:
    prev = -1.0
    for i in range(101):
        a = spin_angle(3, 8, i / 100.0)
        assert a >= prev - 1e-9, f"reversed at frac={i/100.0}"
        prev = a


def test_spin_starts_at_zero_and_makes_several_turns_before_resting() -> None:
    assert spin_angle(2, 8, 0.0) == 0.0
    # rest_angle includes the whole-turn drama: well over a couple revolutions.
    assert rest_angle(0, 8) >= 2 * 2 * math.pi


def test_negative_frac_is_clamped_to_the_start() -> None:
    assert spin_angle(1, 8, -0.5) == 0.0


def test_slice_label_renders_each_outcome_kind() -> None:
    assert slice_label({"kind": "gold", "amount": 50}) == "+50g"
    assert slice_label({"kind": "item", "item": "boost"}) == "Boost"
    assert slice_label({"kind": "debuff", "debuff": "slip_back"}) == "Slip"
    assert slice_label({"kind": "debuff", "debuff": "skip_next"}) == "Skip!"
    # Unknown debuff degrades to a generic label rather than raising.
    assert slice_label({"kind": "debuff", "debuff": "???"}) == "Debuff"


def test_slice_labels_cover_the_servers_whole_wheel_table() -> None:
    # Guard against an outcome kind the label map forgot: every real slice must
    # produce a non-empty, non-"?" label.
    from server.games.snakes_and_ladders import WHEEL_OUTCOMES

    for outcome in WHEEL_OUTCOMES:
        label = slice_label(outcome)
        assert label and label != "?", outcome


def test_wheel_widget_spins_then_settles_on_the_chosen_index() -> None:
    step = {
        "t": "wheel",
        "table": [{"kind": "gold", "amount": 50}] * 8,
        "index": 6,
        "outcome": {"kind": "gold", "amount": 50},
    }
    w = Wheel(duration=1.6)
    assert not w.is_spinning            # idle before begin
    w.begin(step)
    assert w.is_spinning
    assert w.angle == 0.0               # starts unrotated
    w.update(0.8)
    assert w.is_spinning                # mid-spin
    w.update(1.0)                       # past the duration
    assert not w.is_spinning
    # Settled: the widget's final angle puts slice 6 under the pointer.
    assert slice_at_pointer(w.angle, 8) == 6


def test_wheel_reset_clears_the_spin() -> None:
    w = Wheel()
    w.begin({"table": [{"kind": "gold", "amount": 1}] * 4, "index": 2})
    w.reset()
    assert not w.is_spinning
    assert w.angle == 0.0


def test_pointer_constant_points_up() -> None:
    # Sanity: the fixed pointer is at the top (12 o'clock) in wheel math.
    assert wheel.POINTER_ANGLE == math.pi / 2
