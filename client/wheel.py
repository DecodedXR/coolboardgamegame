"""Wheel-of-Names spin widget for the wheel-tile event.

When a player lands on a wheel tile the *server* picks an outcome and embeds the
whole slice ``table`` plus the chosen ``index`` in the turn timeline
(``{"t":"wheel","table":[...],"index":k,"outcome":{...}}``, see
``server/games/snakes_and_ladders.py``). The client only puts on a show: it spins
the wheel and decelerates it so the server-chosen slice comes to rest under a
fixed pointer. **The client never decides the outcome** — it is handed the index.

The spin geometry is the pure heart of this module and is unit-tested precisely by
round-tripping render-angle <-> logical-slice; the ``draw_*`` helper needs a real
Surface/font and is verified by the manual desktop smoke in the final chunk.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import pygame

from client import ui

# The pointer is fixed at the top of the wheel (12 o'clock). Angles below are
# measured counter-clockwise from the +x axis (pygame screen space), so the top
# of the wheel is +pi/2 *in wheel math*; the draw helper flips y for the screen.
POINTER_ANGLE = math.pi / 2

# Extra whole turns the wheel spins before settling — pure drama, geometry-neutral
# (any positive integer lands on the same resting slice).
SPIN_TURNS = 4

_TWO_PI = 2.0 * math.pi

# Slice fill colors, cycled around the wheel so neighbours never share one.
_SLICE_COLORS = [
    (240, 84, 120), (90, 200, 140), (120, 180, 255), (240, 200, 90),
    (200, 130, 255), (250, 150, 90), (110, 220, 220), (230, 230, 130),
]

# Short, fixed labels for each debuff so a slice reads at a glance.
_DEBUFF_LABELS = {"skip_next": "Skip!", "slip_back": "Slip", "gold_tax": "-Gold"}


def _ease_out(frac: float) -> float:
    """Cubic ease-out on a clamped ``frac`` (0..1): fast at the start, gliding to a
    stop at the end. Monotonic non-decreasing, ``f(0)=0``, ``f(1)=1``."""
    f = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)
    return 1.0 - (1.0 - f) ** 3


def rest_angle(index: int, slice_count: int, turns: int = SPIN_TURNS,
               pointer: float = POINTER_ANGLE) -> float:
    """Final wheel rotation (radians) that brings slice ``index`` to rest under the
    pointer, after ``turns`` full revolutions. Slice ``i`` spans ``[i*arc,(i+1)*arc)``
    and is centered at ``(i+0.5)*arc``; rotating the wheel by this angle puts that
    center beneath ``pointer``. Always > 0 (``turns`` >= 1)."""
    arc = _TWO_PI / slice_count
    center = (index + 0.5) * arc
    return turns * _TWO_PI + (pointer - center) % _TWO_PI


def spin_angle(index: int, slice_count: int, frac: float, turns: int = SPIN_TURNS,
               pointer: float = POINTER_ANGLE) -> float:
    """Wheel rotation at progress ``frac`` (0..1) of the spin: eases out from 0 to
    :func:`rest_angle`, so at ``frac>=1`` slice ``index`` sits under the pointer.
    Monotonic non-decreasing in ``frac`` (the wheel never visibly reverses)."""
    return _ease_out(frac) * rest_angle(index, slice_count, turns, pointer)


def slice_at_pointer(angle: float, slice_count: int,
                     pointer: float = POINTER_ANGLE) -> int:
    """Inverse of the geometry: which slice index sits under ``pointer`` when the
    wheel is rotated by ``angle``. Used to *prove* the spin lands where intended."""
    arc = _TWO_PI / slice_count
    raw = ((pointer - angle) / arc) - 0.5
    return round(raw) % slice_count


def slice_label(outcome: dict[str, Any]) -> str:
    """Short label for one wheel slice / outcome (``{"kind":...}``)."""
    kind = outcome.get("kind")
    if kind == "gold":
        return f"+{outcome.get('amount', 0)}g"
    if kind == "item":
        return str(outcome.get("item", "")).title()
    if kind == "debuff":
        return _DEBUFF_LABELS.get(outcome.get("debuff"), "Debuff")
    return str(kind or "?")


def slice_color(index: int) -> tuple[int, int, int]:
    return _SLICE_COLORS[index % len(_SLICE_COLORS)]


class Wheel:
    """Drives one wheel spin. The scene feeds it the server's wheel step via
    :meth:`begin`, ticks :meth:`update` each frame, and :meth:`draw`s it while
    :attr:`is_spinning`. The animator gates the spin's duration; this widget just
    maps elapsed time to a rotation that lands on the server's ``index``."""

    def __init__(self, duration: float = 1.6) -> None:
        self.duration = duration
        self._step: Optional[dict[str, Any]] = None
        self._t = 0.0

    def begin(self, step: dict[str, Any]) -> None:
        """Start spinning toward ``step["index"]`` over :attr:`duration` seconds."""
        self._step = step
        self._t = 0.0

    def update(self, dt: float) -> None:
        if self._step is not None:
            self._t += dt

    def reset(self) -> None:
        self._step = None
        self._t = 0.0

    @property
    def is_spinning(self) -> bool:
        return self._step is not None and self._t < self.duration

    @property
    def angle(self) -> float:
        """Current wheel rotation, easing toward the resting angle for the chosen
        slice; ``0`` when no spin is active."""
        if self._step is None:
            return 0.0
        table = self._step.get("table") or []
        n = max(1, len(table))
        frac = self._t / self.duration if self.duration > 0 else 1.0
        return spin_angle(int(self._step.get("index", 0)), n, frac)

    def draw(self, surf: "pygame.Surface", center: tuple[int, int], radius: int) -> None:
        """Render the spinning wheel and its pointer. Needs a real Surface/font, so
        it is exercised by the desktop smoke, not the headless unit tests."""
        if self._step is None:
            return
        table = self._step.get("table") or []
        n = max(1, len(table))
        arc = _TWO_PI / n
        theta = self.angle
        cx, cy = center
        for i, outcome in enumerate(table):
            a0 = i * arc + theta
            # Build a wedge polygon: center + a fan of points along the slice arc.
            pts = [(cx, cy)]
            steps = 8
            for s in range(steps + 1):
                a = a0 + arc * (s / steps)
                # y is negated: wheel math is CCW-from-+x, the screen grows down.
                pts.append((cx + radius * math.cos(a), cy - radius * math.sin(a)))
            pygame.draw.polygon(surf, slice_color(i), pts)
            pygame.draw.polygon(surf, ui.BG, pts, width=2)
            # Slice label, placed at the wedge's mid-radius / mid-angle.
            amid = a0 + arc / 2
            lx = cx + (radius * 0.6) * math.cos(amid)
            ly = cy - (radius * 0.6) * math.sin(amid)
            font = ui.get_font(14)
            img = font.render(slice_label(outcome), True, ui.BG)
            surf.blit(img, img.get_rect(center=(int(lx), int(ly))))
        # Hub + the fixed pointer at the top (12 o'clock).
        pygame.draw.circle(surf, ui.PANEL, (cx, cy), max(6, radius // 6))
        pygame.draw.polygon(
            surf, ui.TEXT,
            [(cx - 10, cy - radius - 2), (cx + 10, cy - radius - 2), (cx, cy - radius + 14)],
        )
