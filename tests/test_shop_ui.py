"""Unit tests for the shop overlay (pure layout + hit-testing, no display).

The widget turns the server's private ``shop.stock`` into clickable rows and maps
a click back to a buy/skip action. Affordability gating is the part with real
consequences (spending gold you don't have), so it is pinned directly rather than
left to the desktop smoke.
"""

from __future__ import annotations

from client.shop_ui import (
    ShopLayout,
    ShopUI,
    item_blurb,
    item_label,
    resolve_click,
)

PANEL = (40, 100, 400, 600)

STOCK = [
    {"item": "immunity", "price": 40, "affordable": True},
    {"item": "boost", "price": 30, "affordable": True},
    {"item": "double", "price": 60, "affordable": False},
    {"item": "reroll", "price": 30, "affordable": True},
]


def _center(rect):
    x, y, w, h = rect
    return (x + w // 2, y + h // 2)


def test_layout_has_one_row_per_item_plus_a_skip_row() -> None:
    lay = ShopLayout(PANEL, len(STOCK))
    assert len(lay.rows) == len(STOCK)
    # Rows are inside the panel and strictly stacked top-to-bottom (no overlap).
    px, py, pw, ph = PANEL
    prev_bottom = py
    for rx, ry, rw, rh in lay.rows:
        assert rx >= px and rx + rw <= px + pw
        assert ry >= prev_bottom
        prev_bottom = ry + rh
    # SKIP sits below the last item row.
    assert lay.skip[1] >= prev_bottom


def test_layout_with_no_stock_still_offers_skip() -> None:
    lay = ShopLayout(PANEL, 0)
    assert lay.rows == []
    assert lay.skip[2] > 0 and lay.skip[3] > 0


def test_click_on_an_affordable_row_buys_that_item() -> None:
    lay = ShopLayout(PANEL, len(STOCK))
    assert resolve_click(PANEL, STOCK, _center(lay.rows[1])) == ("buy", "boost")


def test_click_on_an_unaffordable_row_is_reported_disabled_not_bought() -> None:
    lay = ShopLayout(PANEL, len(STOCK))
    # Row index 2 ("double") is not affordable -> must NOT resolve to a buy.
    assert resolve_click(PANEL, STOCK, _center(lay.rows[2])) == ("buy_disabled", "double")


def test_click_on_skip_returns_skip() -> None:
    lay = ShopLayout(PANEL, len(STOCK))
    assert resolve_click(PANEL, STOCK, _center(lay.skip)) == ("skip",)


def test_click_in_empty_space_returns_none() -> None:
    # The panel's title strip (top-left corner) is not a button.
    assert resolve_click(PANEL, STOCK, (PANEL[0] + 1, PANEL[1] + 1)) is None


def test_shopui_fires_on_buy_only_for_affordable_items() -> None:
    bought: list[str] = []
    skipped: list[bool] = []
    ui_ = ShopUI(on_buy=bought.append, on_skip=lambda: skipped.append(True))
    ui_.set_stock(STOCK)
    lay = ShopLayout(PANEL, len(STOCK))

    # Affordable -> on_buy fires.
    ui_.handle(_FakeClick(_center(lay.rows[0])), PANEL)
    assert bought == ["immunity"]

    # Unaffordable -> nothing happens (no gold wasted on a buy that'd be rejected).
    ui_.handle(_FakeClick(_center(lay.rows[2])), PANEL)
    assert bought == ["immunity"]

    # SKIP -> on_skip fires.
    ui_.handle(_FakeClick(_center(lay.skip)), PANEL)
    assert skipped == [True]


def test_shopui_ignores_non_left_clicks_and_other_events() -> None:
    bought: list[str] = []
    ui_ = ShopUI(on_buy=bought.append, on_skip=lambda: None)
    ui_.set_stock(STOCK)
    lay = ShopLayout(PANEL, len(STOCK))
    ui_.handle(_FakeClick(_center(lay.rows[0]), button=3), PANEL)   # right-click
    ui_.handle(_FakeMotion(_center(lay.rows[0])), PANEL)            # mouse move
    assert bought == []


def test_item_labels_and_blurbs_cover_the_powerup_catalog() -> None:
    from server.games.snakes_and_ladders import POWERUPS

    for item in POWERUPS:
        assert item_label(item) and item_label(item) != item  # a friendly name
        assert item_blurb(item)                                # a non-empty blurb


# --- tiny pygame-event stand-ins (avoid constructing real events headless) ----

import pygame


class _FakeClick:
    def __init__(self, pos, button: int = 1) -> None:
        self.type = pygame.MOUSEBUTTONDOWN
        self.button = button
        self.pos = pos


class _FakeMotion:
    def __init__(self, pos) -> None:
        self.type = pygame.MOUSEMOTION
        self.pos = pos
