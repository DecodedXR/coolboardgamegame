"""Unit tests for turn-transition cutscenes (pure text + fade envelope, no display).

The banner text is derived from the authoritative turn data and the fade envelope
is a pure function of elapsed time, so both are pinned headless; only the actual
rendering is left to the desktop smoke.
"""

from __future__ import annotations

from client.cutscene import Cutscene, event_text, turn_text


def test_turn_text_announces_the_actor() -> None:
    assert turn_text("Alice") == "Alice's turn"


def test_event_text_none_when_nothing_notable() -> None:
    assert event_text(None) is None
    assert event_text({"name": "Alice", "steps": []}) is None
    assert event_text({"name": "Alice", "steps": [
        {"t": "roll", "die": 3}, {"t": "move", "frm": 1, "to": 4, "path": [2, 3, 4]},
    ]}) is None


def test_event_text_reports_a_win() -> None:
    text, kind = event_text({"name": "Bob", "steps": [
        {"t": "move", "frm": 99, "to": 100, "path": [100]},
        {"t": "win", "pid": "p2", "name": "Bob"},
    ]})
    assert text == "Bob wins!" and kind == "win"


def test_event_text_reports_a_skip_using_the_skipped_players_name() -> None:
    # The skipped player differs from the turn's mover; the banner must name the
    # skipped player (carried on the step), not last_turn["name"].
    text, kind = event_text({"name": "Alice", "steps": [
        {"t": "roll", "die": 5},
        {"t": "skipped", "pid": "p2", "name": "Bob"},
    ]})
    assert text == "Bob skipped!" and kind == "skip"


def test_event_text_prefers_win_over_skip() -> None:
    text, kind = event_text({"name": "Alice", "steps": [
        {"t": "skipped", "pid": "p2", "name": "Bob"},
        {"t": "win", "pid": "p1", "name": "Alice"},
    ]})
    assert kind == "win" and text == "Alice wins!"


def test_cutscene_is_inactive_until_shown() -> None:
    c = Cutscene()
    assert not c.is_active
    assert c.alpha() == 0.0


def test_cutscene_lifecycle_fades_in_holds_then_expires() -> None:
    c = Cutscene(fade=0.3, hold=1.0)  # total 1.6s
    c.show("Alice's turn")
    assert c.is_active
    assert c.alpha() == 0.0           # opacity ramps from 0
    c.update(0.3)                     # end of fade-in
    assert abs(c.alpha() - 1.0) < 1e-9
    c.update(0.5)                     # mid-hold, still fully opaque
    assert abs(c.alpha() - 1.0) < 1e-9
    c.update(1.0)                     # past the total duration
    assert not c.is_active
    assert c.alpha() == 0.0


def test_cutscene_alpha_peaks_in_the_middle_and_is_symmetric() -> None:
    c = Cutscene(fade=0.3, hold=1.0)
    c.show("hi")
    samples = []
    for _ in range(16):
        samples.append(c.alpha())
        c.update(0.1)
    assert max(samples) > 0.99        # reaches full opacity
    assert samples[0] < 0.5           # starts dim
    # The last on-screen sample is on the fade-out ramp (dim again).
    active = [a for a in samples if a > 0]
    assert active[-1] < 1.0


def test_show_replaces_the_previous_banner() -> None:
    c = Cutscene(fade=0.3, hold=1.0)
    c.show("first")
    c.update(0.8)
    c.show("second")                  # restart
    assert c.text == "second"
    assert c.alpha() == 0.0           # clock reset -> back to the fade-in start


def test_persistent_win_banner_holds_at_full_opacity() -> None:
    c = Cutscene(fade=0.3, hold=1.0)
    c.show_persistent("Alice wins!", kind="win")
    c.update(0.3)
    assert abs(c.alpha() - 1.0) < 1e-9
    c.update(100.0)                   # would have long expired a timed banner
    assert c.is_active                # ...but a persistent banner stays
    assert abs(c.alpha() - 1.0) < 1e-9
    c.reset()
    assert not c.is_active


def test_reset_clears_an_active_banner() -> None:
    c = Cutscene()
    c.show("x")
    c.reset()
    assert not c.is_active
    assert c.text == ""
