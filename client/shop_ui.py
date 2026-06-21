"""Shop overlay for the shop sub-state.

When the current player lands on a shop tile the server enters the ``shop``
sub-state and sends them a private ``shop`` block:
``{"stock":[{"item","price","affordable"},...]}`` (see
``server/games/snakes_and_ladders.py``). This widget renders that stock as a buy
button per item plus a SKIP button, and reports the player's choice back to the
scene via the ``on_buy(item)`` / ``on_skip()`` callbacks the scene wires to
``C_BUY_ITEM`` / ``C_SKIP_SHOP``. Buying or skipping passes the turn — the server
decides that; the widget only forwards the click.

The layout + hit-testing math is pure (no display) and unit-tested; only
:meth:`ShopUI.draw` needs a Surface and is left to the desktop smoke.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import pygame

from client import ui

# Human-readable powerup names + one-line blurbs, mirroring the catalog in
# ``server/games/snakes_and_ladders.py`` (POWERUPS). Tunable copy lives here.
ITEM_LABELS = {
    "immunity": "Immunity",
    "boost": "Boost",
    "double": "Double",
    "reroll": "Reroll",
}
ITEM_BLURBS = {
    "immunity": "block the next snake",
    "boost": "+3 to your next roll",
    "double": "double your next roll",
    "reroll": "roll twice, keep higher",
}

# A dim border for an unaffordable item row.
ACCENT_DISABLED = (90, 70, 80)

# Layout metrics (pixels). The panel is a vertical stack: a title strip, one row
# per stock item, then the SKIP row at the bottom.
_PAD = 14
_TITLE_H = 34
_ROW_H = 56
_ROW_GAP = 8


class ShopLayout:
    """Pure geometry for a shop panel: the clickable rect of each item row and of
    the SKIP button, as ``(x, y, w, h)`` tuples. No pygame needed."""

    __slots__ = ("panel", "rows", "skip")

    def __init__(self, panel: tuple[int, int, int, int], n_items: int) -> None:
        self.panel = panel
        px, py, pw, ph = panel
        inner_x = px + _PAD
        inner_w = pw - 2 * _PAD
        y = py + _PAD + _TITLE_H
        self.rows: list[tuple[int, int, int, int]] = []
        for _ in range(n_items):
            self.rows.append((inner_x, y, inner_w, _ROW_H))
            y += _ROW_H + _ROW_GAP
        # SKIP sits a gap below the last row (or below the title if no stock).
        self.skip: tuple[int, int, int, int] = (inner_x, y, inner_w, _ROW_H)


def _in(rect: tuple[int, int, int, int], pos: tuple[int, int]) -> bool:
    x, y, w, h = rect
    px, py = pos
    return x <= px < x + w and y <= py < y + h


def item_label(item: str) -> str:
    return ITEM_LABELS.get(item, item.title())


def item_blurb(item: str) -> str:
    return ITEM_BLURBS.get(item, "")


def resolve_click(panel: tuple[int, int, int, int], stock: list[dict[str, Any]],
                  pos: tuple[int, int]) -> Optional[tuple]:
    """Map a click at ``pos`` to a shop action, given the panel rect + stock list.

    Returns ``("buy", item)`` for an *affordable* item row, ``("buy_disabled",
    item)`` for an unaffordable one (so the caller can ignore it without
    re-deriving affordability), ``("skip",)`` for the SKIP button, or ``None`` for
    a click that misses everything. Pure: the gating lives in one place and is
    tested directly."""
    layout = ShopLayout(panel, len(stock))
    for rect, entry in zip(layout.rows, stock):
        if _in(rect, pos):
            item = entry.get("item")
            return ("buy", item) if entry.get("affordable") else ("buy_disabled", item)
    if _in(layout.skip, pos):
        return ("skip",)
    return None


class ShopUI:
    """Stateful wrapper the scene owns. Feed it the current ``stock`` each frame,
    forward pygame events to :meth:`handle`, and :meth:`draw` it over the board.
    Fires ``on_buy(item)`` only for affordable items; ``on_skip()`` for SKIP."""

    def __init__(self, on_buy: Callable[[str], None], on_skip: Callable[[], None]) -> None:
        self.on_buy = on_buy
        self.on_skip = on_skip
        self.stock: list[dict[str, Any]] = []

    def set_stock(self, stock: Optional[list[dict[str, Any]]]) -> None:
        self.stock = list(stock or [])

    def handle(self, event: "pygame.event.Event", panel: tuple[int, int, int, int]) -> None:
        if not (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1):
            return
        action = resolve_click(panel, self.stock, event.pos)
        if action is None:
            return
        if action[0] == "buy":
            self.on_buy(action[1])
        elif action[0] == "skip":
            self.on_skip()
        # "buy_disabled" -> intentionally ignored (can't afford it).

    def draw(self, surf: "pygame.Surface", panel: tuple[int, int, int, int]) -> None:
        """Render the panel. Needs a real Surface/font, so it is covered by the
        desktop smoke rather than the headless unit tests."""
        layout = ShopLayout(panel, len(self.stock))
        px, py = panel[0], panel[1]
        pygame.draw.rect(surf, ui.PANEL, panel, border_radius=12)
        pygame.draw.rect(surf, ui.ACCENT, panel, width=2, border_radius=12)
        title = ui.get_font(24).render("SHOP", True, ui.TEXT)
        surf.blit(title, (px + _PAD, py + _PAD))

        for rect, entry in zip(layout.rows, self.stock):
            affordable = bool(entry.get("affordable"))
            color = ui.FIELD if affordable else (30, 30, 42)
            pygame.draw.rect(surf, color, rect, border_radius=8)
            border = ui.GOOD if affordable else ACCENT_DISABLED
            pygame.draw.rect(surf, border, rect, width=2, border_radius=8)
            rx, ry, rw, rh = rect
            item = entry.get("item", "")
            name = ui.get_font(20).render(item_label(item), True,
                                          ui.TEXT if affordable else ui.MUTED)
            surf.blit(name, (rx + 12, ry + 6))
            blurb = ui.get_font(13).render(item_blurb(item), True, ui.MUTED)
            surf.blit(blurb, (rx + 12, ry + 32))
            price = ui.get_font(20).render(f"{entry.get('price', 0)}g", True,
                                           ui.GOOD if affordable else ui.MUTED)
            surf.blit(price, price.get_rect(midright=(rx + rw - 12, ry + rh // 2)))

        sx, sy, sw, sh = layout.skip
        pygame.draw.rect(surf, (60, 62, 84), layout.skip, border_radius=8)
        skip_img = ui.get_font(20).render("SKIP", True, ui.TEXT)
        surf.blit(skip_img, skip_img.get_rect(center=(sx + sw // 2, sy + sh // 2)))
