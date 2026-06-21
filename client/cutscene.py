"""Turn-transition cutscenes: the brief banners between turns.

A cutscene is a short, non-blocking banner that fades in, holds, and fades out —
"Alice's turn", "Bob skipped!", or the win banner. The scene shows one when the
authoritative ``current_pid`` changes or a turn's timeline contains a notable
event, then keeps ticking it while it renders the board underneath.

The banner *text* derivation and the fade *envelope* are pure (no display) and
unit-tested; only :meth:`Cutscene.draw` needs a Surface and is left to the desktop
smoke. The win banner deliberately persists (the game is over), so it is shown
with no timeout.
"""

from __future__ import annotations

from typing import Any, Optional

import pygame

from client import ui

# Banner kinds -> accent color. ``turn`` is the routine "X's turn" announcement;
# ``skip`` and ``win`` are louder.
_KIND_COLORS = {
    "turn": ui.ACCENT,
    "skip": (230, 90, 110),
    "win": (250, 210, 90),
}

# Default timings (seconds). A turn/skip banner fades in, holds, fades out; a win
# banner holds indefinitely (``duration=None``) since the game has ended.
FADE_SECONDS = 0.3
HOLD_SECONDS = 1.1


def turn_text(name: str) -> str:
    """The routine between-turns announcement for the player about to act."""
    return f"{name}'s turn"


def event_text(last_turn: Optional[dict[str, Any]]) -> Optional[tuple[str, str]]:
    """Derive a notable banner ``(text, kind)`` from a completed turn's timeline, or
    ``None`` if nothing in it warrants one. A win outranks a skip (it ends the
    game). The skipped player's own ``name`` is carried on the ``skipped`` step
    (it is a *different* player than the turn's mover)."""
    if not last_turn:
        return None
    steps = last_turn.get("steps") or []
    for step in steps:
        if step.get("t") == "win":
            return (f"{step.get('name', last_turn.get('name', '?'))} wins!", "win")
    for step in steps:
        if step.get("t") == "skipped":
            return (f"{step.get('name', '?')} skipped!", "skip")
    return None


class Cutscene:
    """A single, replaceable banner with a fade envelope. :meth:`show` (re)starts
    it, :meth:`update` advances its clock, :meth:`alpha` gives the current opacity
    (0..1), and :attr:`is_active` says whether to draw it at all."""

    def __init__(self, fade: float = FADE_SECONDS, hold: float = HOLD_SECONDS) -> None:
        self.fade = fade
        self.hold = hold
        self.text = ""
        self.kind = "turn"
        self._t = 0.0
        # None => no timeout (win banner holds until the scene is left).
        self._duration: Optional[float] = 0.0

    def show(self, text: str, kind: str = "turn",
             duration: Optional[float] = None) -> None:
        """Start (or replace) the banner. ``duration`` defaults to fade+hold+fade;
        pass ``duration=None`` explicitly via :meth:`show_persistent` for a banner
        that never times out."""
        self.text = text
        self.kind = kind
        self._t = 0.0
        self._duration = duration if duration is not None else 2 * self.fade + self.hold

    def show_persistent(self, text: str, kind: str = "win") -> None:
        """Show a banner that holds at full opacity until reset (the win banner)."""
        self.text = text
        self.kind = kind
        self._t = 0.0
        self._duration = None

    def update(self, dt: float) -> None:
        if self.is_active:
            self._t += dt

    def reset(self) -> None:
        self.text = ""
        self._t = 0.0
        self._duration = 0.0

    @property
    def is_active(self) -> bool:
        """Whether the banner is still on screen. A persistent (``duration=None``)
        banner is active once shown and until :meth:`reset`."""
        if not self.text:
            return False
        if self._duration is None:
            return True
        return self._t < self._duration

    def alpha(self) -> float:
        """Current opacity 0..1: a trapezoid that ramps up over ``fade``, holds at
        1, then ramps down over the final ``fade`` (a persistent banner skips the
        ramp-down and stays at 1)."""
        if not self.is_active:
            return 0.0
        if self._duration is None:
            # Persistent: just the fade-in, then hold at full.
            return min(1.0, self._t / self.fade) if self.fade > 0 else 1.0
        into = self._t
        out = self._duration - self._t
        if self.fade <= 0:
            return 1.0
        return max(0.0, min(1.0, into / self.fade, out / self.fade))

    def draw(self, surf: "pygame.Surface", area: tuple[int, int, int, int]) -> None:
        """Render the banner centered in ``area`` at the current opacity. Needs a
        real Surface/font, so it is exercised by the desktop smoke."""
        if not self.is_active:
            return
        a = self.alpha()
        x, y, w, h = area
        color = _KIND_COLORS.get(self.kind, ui.ACCENT)
        banner_h = 64
        banner = pygame.Surface((w, banner_h), pygame.SRCALPHA)
        banner.fill((*ui.PANEL, int(220 * a)))
        pygame.draw.rect(banner, (*color, int(255 * a)), banner.get_rect(), width=3)
        img = ui.get_font(30).render(self.text, True, color)
        img.set_alpha(int(255 * a))
        banner.blit(img, img.get_rect(center=(w // 2, banner_h // 2)))
        surf.blit(banner, (x, y + (h - banner_h) // 2))
