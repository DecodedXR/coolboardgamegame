"""Procedurally-synthesized sound effects — no bundled audio assets, no numpy.

Each cue is a short list of ``(frequency_hz, duration_s, shape)`` tone segments
that :func:`_render_wav` turns into a 16-bit mono WAV; :meth:`Sfx._sound` writes
that to a real ``.wav`` file and loads it via ``pygame.mixer.Sound(path)``.
pygbag's SDL_mixer silently yields a *soundless* clip when a Sound is built from
a Python file object (``BytesIO``) — only a real file path decodes in the
browser — so the file detour is what makes audio actually play under WASM (it is
a no-op difference on desktop). Synthesis is pure stdlib, so it is unit-testable
without an audio device.

Audio is *best effort*. The mixer is initialised lazily on the first user gesture
(:meth:`Sfx.init`, called on the first click to satisfy browser autoplay rules),
and every mixer call is guarded: if audio is unavailable (common under headless
CI and sometimes in the browser), :class:`Sfx` silently degrades to a no-op
rather than crashing the game.
"""

from __future__ import annotations

import array
import math
import os
import struct
from typing import Iterable

import pygame

_SAMPLE_RATE = 22050
_AMPLITUDE = 0.32  # headroom so layered cues don't clip
_MAX_INT16 = 32767

# Tone segment = (frequency Hz, duration s, wave shape[, envelope]). Envelope is
# "flat" (default: triangular edge-fade) or "decay" (percussive: sharp click that
# falls off like a real clock tick). A frequency of 0 is a silent rest.
Segment = tuple  # 3- or 4-tuple; see above

# The cue catalog, keyed by the names the animator fires. Kept small, punchy, and
# deterministic (sine/square/saw only — no noise — so synthesis is reproducible).
SOUNDS: dict[str, list[Segment]] = {
    "roll": [(420.0, 0.05, "square"), (300.0, 0.05, "square"), (220.0, 0.06, "square")],
    "hop": [(660.0, 0.06, "sine")],
    "snake": [(520.0, 0.10, "saw"), (300.0, 0.12, "saw"), (170.0, 0.16, "saw")],
    "ladder": [(300.0, 0.09, "square"), (450.0, 0.09, "square"), (640.0, 0.12, "square")],
    "wheel": [(700.0, 0.04, "square"), (560.0, 0.04, "square"),
              (700.0, 0.04, "square"), (560.0, 0.04, "square"), (820.0, 0.10, "square")],
    "gold": [(880.0, 0.07, "sine"), (1175.0, 0.12, "sine")],
    "debuff": [(200.0, 0.10, "saw"), (150.0, 0.16, "saw")],
    "buy": [(520.0, 0.07, "sine"), (780.0, 0.10, "sine")],
    "win": [(523.0, 0.10, "square"), (659.0, 0.10, "square"),
            (784.0, 0.10, "square"), (1047.0, 0.20, "square")],
    # word bomb ---------------------------------------------------------------
    # the clock: a crisp click (short bright transient) + a woody decaying body,
    # both percussive so they read as a real *tick*, not a sustained 8-bit buzz.
    # The scene swells their volume as the fuse runs down (see tick_volume()).
    "tick":     [(2600.0, 0.009, "square", "decay"), (900.0, 0.05, "sine", "decay")],
    # its hotter sibling: higher + a touch shorter, so the acceleration still
    # reads on phone speakers even before the volume swell.
    "tick_hot": [(3100.0, 0.009, "square", "decay"), (1150.0, 0.045, "sine", "decay")],
    # bomb handed on: quick 3-note rise, airy sine so it stays out of the way
    "pass": [(500.0, 0.04, "sine"), (700.0, 0.04, "sine"), (900.0, 0.05, "sine")],
    # it's YOUR problem now: hard double-hit + upward kick, square = urgent
    "alarm": [(880.0, 0.07, "square"), (0.0, 0.03, "sine"),
              (880.0, 0.07, "square"), (1100.0, 0.10, "square")],
    # valid word: small consonant rise (matches "gold"/"buy" family in feel)
    "type_ok": [(620.0, 0.05, "sine"), (930.0, 0.07, "sine")],
    # bounced word: low saw shrug, short — it fires often, it must not grate
    "type_bad": [(240.0, 0.08, "saw"), (160.0, 0.10, "saw")],
    # the explosion: 6-step saw avalanche 180->48 Hz over ~0.67s; stepped
    # segments approximate a pitch-drop sweep within the constant-freq model
    "boom": [(180.0, 0.06, "saw"), (140.0, 0.07, "saw"), (110.0, 0.08, "saw"),
             (85.0, 0.10, "saw"), (65.0, 0.14, "saw"), (48.0, 0.22, "saw")],
    # a player is out: three falling square notes (G4-E4-C4), layered over boom
    "dirge": [(392.0, 0.12, "square"), (330.0, 0.12, "square"), (262.0, 0.20, "square")],
    # two players, one life each: tritone menace sting (A3 -> D#4), saw growl
    "sudden_death": [(220.0, 0.10, "saw"), (0.0, 0.05, "sine"),
                     (220.0, 0.10, "saw"), (0.0, 0.05, "sine"), (311.0, 0.18, "saw")],
}

# Linear fade applied to each segment's edges so abrupt starts/stops don't click.
_FADE_SECONDS = 0.006


def _make_cache_dir() -> str:
    """A writable dir to stage cue ``.wav`` files (see module docstring for why a
    file is needed). ``tempfile`` first; pygbag's stdlib is trimmed, so fall back
    to a cwd-relative dir if it's missing."""
    try:
        import tempfile
        return tempfile.mkdtemp(prefix="cbgg_sfx_")
    except Exception:
        path = os.path.join(os.getcwd(), ".sfx_cache")
        os.makedirs(path, exist_ok=True)
        return path


def _sample(shape: str, phase: float) -> float:
    """One sample of a unit-amplitude waveform; ``phase`` is in cycles (0..1 repeats)."""
    frac = phase - math.floor(phase)
    if shape == "square":
        return 1.0 if frac < 0.5 else -1.0
    if shape == "saw":
        return 2.0 * frac - 1.0
    # default: sine
    return math.sin(2.0 * math.pi * frac)


def _render_wav(segments: Iterable[Segment], sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Render tone segments to WAV file bytes (16-bit signed mono)."""
    samples = array.array("h")
    fade_n = max(1, int(_FADE_SECONDS * sample_rate))
    attack_n = max(1, int(0.0012 * sample_rate))   # ~1.2ms attack for "decay"
    for seg in segments:
        freq, dur, shape = seg[0], seg[1], seg[2]
        env_kind = seg[3] if len(seg) > 3 else "flat"
        n = int(dur * sample_rate)
        for i in range(n):
            if freq <= 0.0:  # a rest
                samples.append(0)
                continue
            value = _sample(shape, freq * i / sample_rate)
            if env_kind == "decay":
                # percussive: fast attack, exponential fall-off -> a real "tick",
                # not a sustained buzz. exp(-5) ~= 0.007 by the segment's end.
                env = min(1.0, (i + 1) / attack_n) * math.exp(-5.0 * i / n)
            else:
                # triangular edge fade so abrupt starts/stops don't click.
                env = min(1.0, (i + 1) / fade_n, (n - i) / fade_n)
            samples.append(int(value * env * _AMPLITUDE * _MAX_INT16))

    # Hand-rolled 44-byte RIFF/WAVE header (PCM, mono, 16-bit): pygbag's WASM
    # CPython ships without the `wave` module, and this is all wave.open() wrote.
    pcm = samples.tobytes()
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", len(pcm),
    )
    return header + pcm


class Sfx:
    """A guarded sound player. Construct once; :meth:`init` on the first click;
    :meth:`play` named cues. Silent and exception-free if audio is unavailable."""

    def __init__(self) -> None:
        self._ok = False
        self._cache: dict[str, "pygame.mixer.Sound"] = {}
        self._pending: list[str] = []  # cues queued for amortized synthesis
        self._dir: str | None = None   # staging dir for cue .wav files

    def init(self) -> bool:
        """Bring audio up, returning whether it is available. Idempotent once
        successful, but *retries* after a failure: under browser autoplay policy
        the first click's ``mixer.init()`` can fail while a later click succeeds,
        so an early failure must not permanently silence the session. On success,
        *queue* every cue for amortized prewarm (one per :meth:`pump`) rather than
        synthesizing all of them now — see :meth:`pump`."""
        if self._ok:
            return True
        try:
            pygame.mixer.init()
        except Exception:  # no audio device / browser restriction -> silent, retry later
            return False
        if self._dir is None:
            self._dir = _make_cache_dir()
        self._ok = True
        # Queue in catalog order; pump() drains from the front so "roll" (the first
        # cue fired every turn) is prewarmed first, not left to play()'s synchronous
        # lazy synth -- the frame stall pump() exists to spread out.
        self._pending = list(SOUNDS)
        return True

    def _sound(self, name: str) -> "pygame.mixer.Sound":
        """Build (once) and return the Sound for ``name``, staging its WAV to a
        real file first — see the module docstring for why BytesIO won't do."""
        path = os.path.join(self._dir or ".", name + ".wav")
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(_render_wav(SOUNDS[name]))
        return pygame.mixer.Sound(path)

    def pump(self) -> None:
        """Synthesize **at most one** queued cue, off the animation hot path. Call
        once per frame (the scene does, from its ``update``): the nine cues build
        over nine frames instead of all-at-once.

        Why not build them all in :meth:`init`? The pure-Python synth loop costs
        ~25ms for the full set on desktop but **10-50x that in single-threaded
        pygbag/WASM** — and there it runs inside one frame with no yield to the
        browser, freezing the tab for up to a second-plus right after the first
        click. Drip-feeding keeps every frame cheap. A cue still missing when it is
        first needed is built lazily by :meth:`play` (one small cue, not nine)."""
        if not self._ok or not self._pending:
            return
        name = self._pending.pop(0)  # FIFO: catalog order, "roll" first
        if name in self._cache:
            return
        try:
            self._cache[name] = self._sound(name)
        except Exception:  # one bad cue is ignored; play() will retry it lazily
            pass

    def play(self, name: str, volume: float = 1.0) -> None:
        """Play a named cue, or do nothing if audio is unavailable / name unknown.
        ``volume`` (0..1) scales this playback — the word-bomb tick uses it to swell
        as the fuse runs down. Never raises: a mixer failure mid-game flips playback
        to silent."""
        if not self._ok:
            return
        spec = SOUNDS.get(name)
        if spec is None:
            return
        try:
            sound = self._cache.get(name)
            if sound is None:
                sound = self._sound(name)
                self._cache[name] = sound
            sound.set_volume(max(0.0, min(1.0, volume)))
            sound.play()
        except Exception:
            self._ok = False  # something broke; degrade to silence for the rest of the run
