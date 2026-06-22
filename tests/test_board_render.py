"""Unit tests for the serpentine board geometry (no display, no pygame.init).

Only the pure cell->pixel math and the layout arithmetic are exercised here; the
drawing functions need a real Surface/font and are verified by the manual desktop
smoke in Chunk 5. The math is the part that is easy to get wrong (boustrophedon
row flips + pygame's y-grows-down inversion), so it is pinned precisely.
"""

from __future__ import annotations

import pygame

from client import board_render
from client.board_render import BoardLayout, LEGEND_TEXT, cell_to_xy

# A 10x10 board, 40px cells, origin at (0,0), bottom edge at y=400. With these
# numbers every cell center is a round number, so the asserts read like a diagram.
K = dict(cols=10, cell_px=40, origin_x=0, board_bottom_y=400)


def test_cell_one_is_bottom_left() -> None:
    assert cell_to_xy(1, **K) == (20, 380)


def test_first_row_runs_left_to_right() -> None:
    assert cell_to_xy(10, **K) == (380, 380)


def test_serpentine_second_row_reverses() -> None:
    # Row 1 runs right->left, so cell 11 sits directly ABOVE cell 10 (same column),
    # and cell 20 ends up above cell 1.
    assert cell_to_xy(11, **K) == (380, 340)
    assert cell_to_xy(20, **K) == (20, 340)


def test_bottom_origin_inversion_higher_cells_are_higher() -> None:
    # y grows downward, so a higher cell number must have a SMALLER y.
    y1 = cell_to_xy(1, **K)[1]
    y11 = cell_to_xy(11, **K)[1]
    y21 = cell_to_xy(21, **K)[1]
    assert y1 > y11 > y21


def test_last_cell_is_top_left_on_a_ten_by_ten() -> None:
    # Standard snakes & ladders: 100 lands top-left, 91 top-right (row 9 is odd).
    assert cell_to_xy(100, **K) == (20, 20)
    assert cell_to_xy(91, **K) == (380, 20)


def test_layout_fills_and_centers_a_square_area() -> None:
    lay = BoardLayout(cells=100, cols=10, area=(0, 0, 480, 480))
    assert lay.rows == 10
    assert lay.cell_px == 48
    assert lay.origin_x == 0
    assert lay.origin_y == 0
    assert lay.cell_to_xy(1) == (24, 480 - 24)


def test_layout_uses_the_smaller_dimension_and_centers() -> None:
    # A tall area is width-limited: 48px cells, 10 rows = 480 tall, centered in 800.
    lay = BoardLayout(cells=100, cols=10, area=(0, 0, 480, 800))
    assert lay.cell_px == 48
    assert lay.origin_y == (800 - 480) // 2


def test_layout_rounds_rows_up_for_a_partial_last_row() -> None:
    lay = BoardLayout(cells=95, cols=10, area=(0, 0, 480, 480))
    assert lay.rows == 10  # ceil(95/10)


def test_legend_describes_the_snake_heavy_board() -> None:
    assert "snake" in LEGEND_TEXT.lower()


# --- render_static: bake the unchanging board once ----------------------------
# draw_board re-rasterizes ~100 cell numbers via font.render on every call; redoing
# that each frame freezes the tab under single-threaded WASM. render_static draws
# the static board (+ optional legend) into one surface so the scene blits it. We
# stub the font/display-dependent draw_* helpers so the contract is checked headless.

def test_render_static_bakes_board_and_legend_once(monkeypatch) -> None:
    calls = {"board": 0, "legend": []}
    monkeypatch.setattr(board_render, "draw_board",
                        lambda s, l, b: calls.__setitem__("board", calls["board"] + 1))
    monkeypatch.setattr(board_render, "draw_legend",
                        lambda s, r: calls["legend"].append(r))
    layout = BoardLayout(cells=100, cols=10, area=(0, 0, 480, 480))
    rect = pygame.Rect(24, 562, 432, 38)
    surf = board_render.render_static(layout, {"cells": 100, "cols": 10}, (480, 800), rect)
    assert surf.get_size() == (480, 800)
    assert calls["board"] == 1          # board rasterized exactly once
    assert calls["legend"] == [rect]    # legend baked in at the given rect


def test_render_static_skips_legend_when_no_rect(monkeypatch) -> None:
    legend_calls = []
    monkeypatch.setattr(board_render, "draw_board", lambda s, l, b: None)
    monkeypatch.setattr(board_render, "draw_legend", lambda s, r: legend_calls.append(r))
    layout = BoardLayout(cells=100, cols=10, area=(0, 0, 480, 480))
    surf = board_render.render_static(layout, {"cells": 100, "cols": 10}, (480, 800))
    assert surf.get_size() == (480, 800)
    assert legend_calls == []
