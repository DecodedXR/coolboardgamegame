"""Unit tests for procedural sound effects.

Two things matter and neither needs a real audio device:

* ``_render_wav`` builds a valid in-memory WAV from tone segments (no numpy, no
  bundled assets), and
* :class:`client.sfx.Sfx` *degrades to silence* when the mixer is unavailable
  (the browser/headless case) — ``play`` must be a no-op that never even tries to
  build a Sound, and never raises.
"""

from __future__ import annotations

import pygame

from client import sfx


def test_render_wav_returns_valid_wav_bytes() -> None:
    data = sfx._render_wav([(440.0, 0.05, "sine")])
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert len(data) > 44  # RIFF/WAVE header is 44 bytes; there must be samples too


def test_render_wav_more_segments_make_a_longer_clip() -> None:
    short = sfx._render_wav([(440.0, 0.05, "sine")])
    longer = sfx._render_wav([(440.0, 0.05, "sine"), (660.0, 0.05, "square")])
    assert len(longer) > len(short)


def test_render_wav_supports_a_silent_rest() -> None:
    # freq 0 = a rest; still produces frames, just silence.
    data = sfx._render_wav([(0.0, 0.02, "sine")])
    assert data[:4] == b"RIFF"
    assert len(data) > 44


def test_sfx_is_silent_when_the_mixer_cannot_init(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("no audio device")

    built: list[int] = []
    monkeypatch.setattr(pygame.mixer, "init", boom)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: built.append(1))

    s = sfx.Sfx()
    assert s.init() is False
    s.play("roll")         # must be a silent no-op...
    s.play("does-not-exist")
    assert built == []     # ...and must never even try to construct a Sound


def test_play_before_init_is_a_silent_no_op(monkeypatch) -> None:
    built: list[int] = []
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: built.append(1))
    s = sfx.Sfx()
    s.play("roll")  # never initialised -> nothing happens
    assert built == []


def test_init_is_idempotent(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(pygame.mixer, "init", lambda *a, **k: calls.append(1))
    s = sfx.Sfx()
    assert s.init() is True
    assert s.init() is True
    assert calls == [1]  # the mixer is initialised at most once on the success path


def test_init_retries_after_a_failure(monkeypatch) -> None:
    # Browser autoplay: the first gesture's mixer.init() can fail while a later one
    # succeeds. A failed init must NOT permanently latch the session silent.
    state = {"calls": 0}

    def flaky(*a, **k):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("audio context not ready yet")

    monkeypatch.setattr(pygame.mixer, "init", flaky)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: object())  # prewarm: no real audio
    s = sfx.Sfx()
    assert s.init() is False  # first gesture: not ready
    assert s.init() is True   # later gesture: retried and succeeded
    assert state["calls"] == 2


def test_init_prewarms_every_cue_off_the_hot_path(monkeypatch) -> None:
    class FakeSound:
        def play(self):
            pass

    built: list[int] = []
    monkeypatch.setattr(pygame.mixer, "init", lambda *a, **k: None)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: built.append(1) or FakeSound())
    s = sfx.Sfx()
    assert s.init() is True
    assert len(built) == len(sfx.SOUNDS)  # every cue synthesised up-front at init
    # a later play() is a cache hit -> no further synthesis in-frame
    before = len(built)
    s.play("snake")
    assert len(built) == before


def test_every_animation_sound_has_a_spec() -> None:
    for name in ("roll", "hop", "snake", "ladder", "wheel", "gold", "debuff", "buy", "win"):
        assert name in sfx.SOUNDS
