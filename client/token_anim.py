"""Turn-timeline animator: replays a server ``last_turn`` as token movement.

The server resolves each turn into an ordered, serializable ``last_turn["steps"]``
timeline with a monotonic ``seq``; the client *replays* it (it never decides an
outcome). This animator turns those steps into a queue of timed segments:

* **move** — one segment per cell hop along the move ``path`` (follows the
  serpentine bends),
* **slide** — a single longer glide for a snake / ladder / slip-back,
* **pause** — a non-positional beat for the dice roll, a wheel spin (the wheel
  step is exposed via :attr:`wheel` for the scene to hand off to the wheel widget),
  a gold/debuff/buy/win cue, etc.

It runs purely in *cell space* plus fractional progress, so it needs no display
and no pixel layout — the scene places the moving token via :meth:`progress`
``(from_cell, to_cell, frac)`` during a hop/slide and at :attr:`anchor_cell`
during a pause beat (mapping cells to pixels via ``board_render``), and once
:attr:`is_playing` is false snaps every token to the authoritative
``players[*].pos``. A turn animates exactly once: :meth:`begin` ignores a re-fed
``last_turn`` with the same ``seq``, and treats a ``seq`` that jumps *backwards*
as a new game (the server restarts ``seq`` at 1 per game).
"""

from __future__ import annotations

from typing import Any, Optional


class _NullSfx:
    """Default sound sink: swallows every cue (audio is optional)."""

    def play(self, name: str) -> None:  # pragma: no cover - trivial
        pass


class _Seg:
    """One timed step of the animation. ``move``/``slide`` carry cell endpoints
    the scene interpolates between; ``pause`` is non-positional."""

    __slots__ = ("kind", "frm", "to", "sfx", "duration", "wheel")

    def __init__(self, kind, *, frm=None, to=None, sfx=None, duration=0.0, wheel=None):
        self.kind = kind
        self.frm = frm
        self.to = to
        self.sfx = sfx
        self.duration = duration
        self.wheel = wheel


class TokenAnimator:
    # Segment durations (seconds). Tuned for readable, not sluggish, playback.
    HOP_SECONDS = 0.18
    SLIDE_SECONDS = 0.5
    ROLL_SECONDS = 0.4
    WHEEL_SECONDS = 1.6
    TILE_SECONDS = 0.45

    def __init__(self, sfx: Optional[Any] = None) -> None:
        self._sfx = sfx if sfx is not None else _NullSfx()
        self._played_seq = 0
        self._queue: list[_Seg] = []
        self._i = 0
        self._t = 0.0
        self._mover: Optional[str] = None
        self._wheel: Optional[dict[str, Any]] = None
        self._anchor: Optional[int] = None

    # --- public state -----------------------------------------------------

    @property
    def is_playing(self) -> bool:
        return self._i < len(self._queue)

    @property
    def mover(self) -> Optional[str]:
        """Id of the token this turn is animating — set for the *whole* replay,
        including non-positional pause beats (where :meth:`progress` is ``None``
        and the token rests at :attr:`anchor_cell`). ``None`` once idle."""
        return self._mover if self.is_playing else None

    @property
    def wheel(self) -> Optional[dict[str, Any]]:
        """The wheel step dict while a wheel segment is active, else ``None`` —
        the seam the scene uses to drive the Wheel-of-Names widget (Chunk 4)."""
        return self._wheel if self.is_playing else None

    @property
    def anchor_cell(self) -> Optional[int]:
        """The mover's resting cell while ``is_playing`` but :meth:`progress` is
        ``None`` (a pause beat such as the opening dice roll): the start cell
        before the first hop, then each segment's landing cell as the replay
        proceeds. The scene draws the mover here during pauses — it must NOT fall
        back to ``players[*].pos`` mid-replay, because the server has already
        advanced that to the turn's *final* cell. ``None`` once idle (then it is
        correct to snap to ``players[*].pos``)."""
        return self._anchor if self.is_playing else None

    def progress(self) -> Optional[tuple[int, int, float]]:
        """``(from_cell, to_cell, frac)`` of the active move/slide segment (``frac``
        in 0..1), or ``None`` during a pause or when idle. The scene lerps between
        the two cells' pixel centers by ``frac`` to place the moving token."""
        if not self.is_playing:
            return None
        seg = self._queue[self._i]
        if seg.kind not in ("move", "slide"):
            return None
        frac = min(1.0, self._t / seg.duration) if seg.duration > 0 else 1.0
        return (seg.frm, seg.to, frac)

    # --- driving ----------------------------------------------------------

    def begin(self, last_turn: Optional[dict[str, Any]]) -> bool:
        """Start animating ``last_turn`` if its ``seq`` is newer than the last one
        played (so re-feeding the same snapshot every frame is a no-op). Returns
        whether a new turn was started."""
        if not last_turn:
            return False
        seq = last_turn.get("seq", 0)
        # Ignore the same turn re-fed every frame. A seq that jumps *backwards*
        # means a new game started (the server restarts seq at 1), so play it.
        if seq == self._played_seq:
            return False
        self._played_seq = seq
        self._mover = last_turn.get("pid")
        self._queue = self._build(last_turn.get("steps") or [])
        self._i = 0
        self._t = 0.0
        self._wheel = None
        # The mover's resting cell before the first hop is that hop's start cell.
        # Exposed via anchor_cell so the scene can pin the token through the opening
        # roll pause instead of using players[*].pos (already the FINAL cell).
        self._anchor = next((s.frm for s in self._queue if s.kind in ("move", "slide")), None)
        if self.is_playing:
            self._enter_segment()
        else:
            self._mover = None  # nothing animatable in this turn
            self._anchor = None
        return True

    def update(self, dt: float) -> None:
        """Advance the animation clock, crossing into later segments as needed."""
        if not self.is_playing:
            return
        self._t += dt
        # Cross as many segment boundaries as this frame's dt spans. is_playing is
        # checked before indexing so running off the end can't IndexError.
        while self.is_playing and self._t >= self._queue[self._i].duration:
            finished = self._queue[self._i]
            self._t -= finished.duration
            self._i += 1
            if finished.kind in ("move", "slide"):
                self._anchor = finished.to  # the token now rests at this segment's end cell
            if self.is_playing:
                self._enter_segment()
            else:
                self._mover = None
                self._wheel = None

    def reset(self) -> None:
        """Stop any in-progress animation (e.g. when leaving the scene). Does not
        rewind ``seq`` — a finished turn stays finished."""
        self._queue = []
        self._i = 0
        self._t = 0.0
        self._mover = None
        self._wheel = None
        self._anchor = None

    # --- internals --------------------------------------------------------

    def _enter_segment(self) -> None:
        seg = self._queue[self._i]
        self._wheel = seg.wheel
        if seg.sfx:
            self._sfx.play(seg.sfx)

    def _build(self, steps: list[dict[str, Any]]) -> list[_Seg]:
        """Translate the server timeline into timed animation segments. Steps that
        are pure labels (``tile``) or have no visible token effect in this chunk
        (``shop_enter``/``shop_skip``/``buy``) produce no segment."""
        segs: list[_Seg] = []
        for s in steps:
            t = s.get("t")
            if t == "roll":
                segs.append(_Seg("pause", sfx="roll", duration=self.ROLL_SECONDS))
            elif t == "move":
                prev = s.get("frm")
                for cell in s.get("path") or []:
                    segs.append(_Seg("move", frm=prev, to=cell, sfx="hop",
                                     duration=self.HOP_SECONDS))
                    prev = cell
            elif t == "snake":
                segs.append(_Seg("slide", frm=s["frm"], to=s["to"], sfx="snake",
                                 duration=self.SLIDE_SECONDS))
            elif t == "ladder":
                segs.append(_Seg("slide", frm=s["frm"], to=s["to"], sfx="ladder",
                                 duration=self.SLIDE_SECONDS))
            elif t == "immunity_used":
                segs.append(_Seg("pause", sfx="buy", duration=self.TILE_SECONDS))
            elif t == "wheel":
                segs.append(_Seg("pause", sfx="wheel", duration=self.WHEEL_SECONDS, wheel=s))
            elif t == "gold":
                segs.append(_Seg("pause", sfx="gold", duration=self.TILE_SECONDS))
            elif t == "item":
                segs.append(_Seg("pause", sfx="buy", duration=self.TILE_SECONDS))
            elif t == "debuff":
                # slip-back carries cell endpoints -> animate it as a backward slide.
                if s.get("debuff") == "slip_back" and "frm" in s and "to" in s:
                    segs.append(_Seg("slide", frm=s["frm"], to=s["to"], sfx="debuff",
                                     duration=self.SLIDE_SECONDS))
                else:
                    segs.append(_Seg("pause", sfx="debuff", duration=self.TILE_SECONDS))
            elif t == "buy":
                # A shop purchase ends the turn; give it a beat + the buy cue so
                # spending gold isn't silent (the token doesn't move on a buy).
                segs.append(_Seg("pause", sfx="buy", duration=self.TILE_SECONDS))
            elif t == "win":
                segs.append(_Seg("pause", sfx="win", duration=self.TILE_SECONDS))
            elif t == "skipped":
                segs.append(_Seg("pause", duration=self.TILE_SECONDS))
            # tile / shop_enter / shop_skip -> no animation segment
        return segs
