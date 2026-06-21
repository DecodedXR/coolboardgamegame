"""Procedurally-synthesized sound effects — no bundled audio assets, no numpy.

Each cue is a short list of ``(frequency_hz, duration_s, shape)`` tone segments
that :func:`_render_wav` turns into an in-memory 16-bit mono WAV; pygame then
loads it via ``pygame.mixer.Sound(BytesIO(...))``. Synthesis is pure stdlib
(``array`` + ``wave``), so it is unit-testable without an audio device.

Audio is *best effort*. The mixer is initialised lazily on the first user gesture
(:meth:`Sfx.init`, called on the first click to satisfy browser autoplay rules),
and every mixer call is guarded: if audio is unavailable (common under headless
CI and sometimes in the browser), :class:`Sfx` silently degrades to a no-op
rather than crashing the game.
"""

from __future__ import annotations

import array
import io
import math
import wave
from typing import Iterable

import pygame

_SAMPLE_RATE = 22050
_AMPLITUDE = 0.32  # headroom so layered cues don't clip
_MAX_INT16 = 32767

# Tone segment = (frequency in Hz, duration in seconds, wave shape). A frequency
# of 0 is a silent rest. Shapes are intentionally cheap to compute per sample.
Segment = tuple[float, float, str]

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
}

# Linear fade applied to each segment's edges so abrupt starts/stops don't click.
_FADE_SECONDS = 0.006


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
    for freq, dur, shape in segments:
        n = int(dur * sample_rate)
        for i in range(n):
            if freq <= 0.0:  # a rest
                samples.append(0)
                continue
            value = _sample(shape, freq * i / sample_rate)
            # Triangular fade in/out at the segment edges.
            env = min(1.0, (i + 1) / fade_n, (n - i) / fade_n)
            samples.append(int(value * env * _AMPLITUDE * _MAX_INT16))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return buf.getvalue()


class Sfx:
    """A guarded sound player. Construct once; :meth:`init` on the first click;
    :meth:`play` named cues. Silent and exception-free if audio is unavailable."""

    def __init__(self) -> None:
        self._ok = False
        self._cache: dict[str, "pygame.mixer.Sound"] = {}

    def init(self) -> bool:
        """Bring audio up, returning whether it is available. Idempotent once
        successful, but *retries* after a failure: under browser autoplay policy
        the first click's ``mixer.init()`` can fail while a later click succeeds,
        so an early failure must not permanently silence the session. On success,
        prewarm every cue off the animation hot path (see :meth:`_prewarm`)."""
        if self._ok:
            return True
        try:
            pygame.mixer.init()
        except Exception:  # no audio device / browser restriction -> silent, retry later
            return False
        self._ok = True
        self._prewarm()
        return True

    def _prewarm(self) -> None:
        """Render and cache every cue now — on the first click, where a one-time
        stall is unnoticeable and the mixer is up — so :meth:`play` never runs the
        pure-Python synth loop inside a frame (single-threaded pygbag/WASM would
        hitch right as a slide/win animation starts). A failure for one cue is
        ignored; ``play`` will rebuild it lazily."""
        for name, spec in SOUNDS.items():
            try:
                self._cache[name] = pygame.mixer.Sound(io.BytesIO(_render_wav(spec)))
            except Exception:
                pass

    def play(self, name: str) -> None:
        """Play a named cue, or do nothing if audio is unavailable / name unknown.
        Never raises: a mixer failure mid-game just flips playback to silent."""
        if not self._ok:
            return
        spec = SOUNDS.get(name)
        if spec is None:
            return
        try:
            sound = self._cache.get(name)
            if sound is None:
                # Pass the WAV as a file object (not buffer=): pygame then reads the
                # RIFF header and resamples our 22050 Hz mono clip to the mixer's
                # actual format. buffer= would treat the header bytes as audio and
                # assume the wrong rate/channels.
                sound = pygame.mixer.Sound(io.BytesIO(_render_wav(spec)))
                self._cache[name] = sound
            sound.play()
        except Exception:
            self._ok = False  # something broke; degrade to silence for the rest of the run
