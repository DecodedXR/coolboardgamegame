"""Unit tests for procedural sound effects.

Two things matter and neither needs a real audio device:

* ``_render_wav`` builds a valid in-memory WAV from tone segments (no numpy, no
  bundled assets), and
* :class:`client.sfx.Sfx` *degrades to silence* when the mixer is unavailable
  (the browser/headless case) — ``play`` must be a no-op that never even tries to
  build a Sound, and never raises.
"""

from __future__ import annotations

import ast
import inspect

import pygame

from client import sfx
from client.token_anim import TokenAnimator


def test_render_wav_returns_valid_wav_bytes() -> None:
    data = sfx._render_wav([(440.0, 0.05, "sine")])
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert len(data) > 44  # RIFF/WAVE header is 44 bytes; there must be samples too


def test_render_wav_more_segments_make_a_longer_clip() -> None:
    short = sfx._render_wav([(440.0, 0.05, "sine")])
    longer = sfx._render_wav([(440.0, 0.05, "sine"), (660.0, 0.05, "square")])
    assert len(longer) > len(short)


def test_decay_envelope_falls_off() -> None:
    import array
    data = sfx._render_wav([(880.0, 0.06, "sine", "decay")])
    pcm = array.array("h"); pcm.frombytes(data[44:])   # skip 44-byte header
    n = len(pcm)
    head = max(abs(s) for s in pcm[: n // 5])          # first 20%
    tail = max(abs(s) for s in pcm[-n // 5:])          # last 20%
    assert head > tail * 4, "decay segment should be far quieter at its tail"


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


def test_init_does_not_synthesize_in_frame_but_pump_amortizes_it(monkeypatch) -> None:
    # WASM is single-threaded: building all cues at once blocks (freezes) a frame.
    # init() must only queue them; pump() builds at most ONE per call.
    class FakeSound:
        def play(self):
            pass

        def set_volume(self, volume):
            pass

    built: list[int] = []
    monkeypatch.setattr(pygame.mixer, "init", lambda *a, **k: None)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: built.append(1) or FakeSound())
    s = sfx.Sfx()
    assert s.init() is True
    assert built == []                       # nothing synthesised inside init()
    s.pump()
    assert len(built) == 1                    # one cue per pump
    for _ in range(len(sfx.SOUNDS) + 5):      # drain the queue (extra pumps are no-ops)
        s.pump()
    assert len(built) == len(sfx.SOUNDS)      # every cue eventually built, none twice
    # a later play() of an already-built cue is a cache hit -> no further synthesis
    before = len(built)
    s.play("snake")
    assert len(built) == before


def test_play_builds_a_missing_cue_lazily(monkeypatch) -> None:
    # If a cue is played before pump() got to it, play() synthesises that one cue.
    class FakeSound:
        def play(self):
            pass

        def set_volume(self, volume):
            pass

    built: list[int] = []
    monkeypatch.setattr(pygame.mixer, "init", lambda *a, **k: None)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: built.append(1) or FakeSound())
    s = sfx.Sfx()
    s.init()
    s.play("win")                             # not pumped yet -> built on demand
    assert len(built) == 1


def test_pump_prewarms_cues_in_catalog_order_roll_first(monkeypatch) -> None:
    # The first cue fired every turn is "roll" (the dice). pump() drains the queue
    # with list.pop() (from the END), so init() must queue the cues REVERSED for the
    # pops to come out in catalog order -- building "roll" FIRST. Otherwise roll is
    # built last and routinely misses the prewarm, falling back to play()'s
    # synchronous lazy synth (the very frame-freeze that pump() exists to avoid).
    class FakeSound:
        def play(self) -> None:
            pass

        def set_volume(self, volume) -> None:
            pass

    monkeypatch.setattr(pygame.mixer, "init", lambda *a, **k: None)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: FakeSound())
    s = sfx.Sfx()
    assert s.init() is True
    s.pump()
    # _cache preserves insertion order, so its keys ARE the prewarm order so far.
    assert list(s._cache) == ["roll"]            # the very first cue built is roll
    for _ in range(len(sfx.SOUNDS)):
        s.pump()
    assert list(s._cache) == list(sfx.SOUNDS)    # full prewarm follows catalog order


def test_pump_skips_a_cue_already_built_lazily_by_play(monkeypatch) -> None:
    # play() can synthesise a cue on demand before pump() drains it from the queue
    # (it stays in _pending until popped). When pump() later pops that name it must
    # take the cache-hit branch: NO second synth (don't waste the once-per-frame synth
    # budget pump() exists to ration) and the SAME Sound object is kept (don't swap out
    # an instance the game may already be holding/looping).
    class FakeSound:
        def play(self) -> None:
            pass

        def set_volume(self, volume) -> None:
            pass

    built: list[int] = []
    monkeypatch.setattr(pygame.mixer, "init", lambda *a, **k: None)
    monkeypatch.setattr(pygame.mixer, "Sound", lambda *a, **k: built.append(1) or FakeSound())
    s = sfx.Sfx()
    assert s.init() is True
    s.play("roll")                         # lazily built + cached, but still queued in _pending
    assert len(built) == 1
    first = s._cache["roll"]

    for _ in range(len(sfx.SOUNDS) + 5):   # drain the whole queue, incl. the queued "roll"
        s.pump()

    # roll was popped from _pending but NOT re-synthesised, and every OTHER cue built once.
    assert len(built) == len(sfx.SOUNDS)   # 1 (lazy roll) + the remaining cues, roll not twice
    assert s._cache["roll"] is first       # same object: pump() did not overwrite the cache


def _animation_step_tokens() -> list[str]:
    """Every ``t`` value the animator's ``_build`` branches on, read straight from
    its source via AST. Deriving this (instead of listing it) means a newly added
    ``elif t == "<new>":`` branch is picked up automatically -- so if that branch
    fires a cue with no ``sfx.SOUNDS`` spec, the guard below catches it without the
    test needing a manual edit."""
    fn = ast.parse(inspect.getsource(TokenAnimator._build).strip()).body[0]
    tokens: list[str] = []
    for node in ast.walk(fn):
        # Match the chain's `t == "<literal>"` comparisons (t = s.get("t")).
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "t":
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    tokens.append(comp.value)
    return tokens


def _emitted_animation_cues() -> set[str]:
    """The set of cue names the animator can actually fire, derived by running
    ``_build`` over a step of every branch token. Each step carries a superset of
    the fields any branch reads (``frm``/``to``/``path``), and ``debuff`` is fed in
    both shapes so its slip-back slide *and* its generic-pause sub-branch are both
    exercised."""
    steps: list[dict] = []
    for tok in _animation_step_tokens():
        base = {"t": tok, "frm": 1, "to": 2, "path": [2]}
        if tok == "debuff":
            steps.append(dict(base, debuff="slip_back"))  # slide sub-branch
            steps.append({"t": "debuff"})                 # generic-pause sub-branch
        else:
            steps.append(base)
    segs = TokenAnimator()._build(steps)
    return {seg.sfx for seg in segs if seg.sfx}


def test_every_animation_sound_has_a_spec() -> None:
    # DERIVE the required cues from what the animator emits rather than hard-coding
    # them: a new animation cue with no matching SOUNDS spec must fail this test.
    cues = _emitted_animation_cues()
    assert cues, "derived no animation cues -- the extraction is broken, not a real check"
    missing = sorted(name for name in cues if name not in sfx.SOUNDS)
    assert not missing, f"animation cues with no sfx.SOUNDS spec: {missing}"
