"""Serpentine board geometry and drawing for Snakes & Ladders.

The board is a boustrophedon (snake-wise) grid: cell 1 is the bottom-left start,
numbering runs left->right along the bottom row, then right->left along the next
row up, and so on, with the finish cell in a top corner. pygame's y axis grows
*downward*, so the row index has to be inverted from the bottom edge.

:func:`cell_to_xy` is the pure heart of this module (no pygame needed) and is unit
tested precisely; the ``draw_*`` helpers and :class:`BoardLayout.cell_rect` need a
real Surface/font and are verified by the manual desktop smoke in the final chunk.
"""

from __future__ import annotations

from typing import Any

import pygame

from client import ui

# The on-board legend (kept short to fit the portrait strip under the grid).
LEGEND_TEXT = (
    "Snake-heavy board · freshly randomized each game · "
    "seeded with powerups, debuffs, shops & wheels."
)

# Glyphs marking the special tiles. Drawn small in a cell corner so the grid
# numbering stays readable; the wheel/shop/gold/debuff colors echo the legend.
_TILE_GLYPHS = {
    "wheel": ("@", (120, 180, 255)),
    "shop": ("$", (240, 200, 90)),
    "gold": ("*", (250, 220, 120)),
    "debuff": ("!", (230, 90, 110)),
}

_SNAKE_COLOR = (210, 90, 110)
_LADDER_COLOR = (110, 200, 150)
_GRID_LIGHT = (40, 42, 60)
_GRID_DARK = (30, 32, 46)
_GRID_LINE = (60, 62, 84)


def cell_to_xy(
    n: int, cols: int, cell_px: int, origin_x: int, board_bottom_y: int
) -> tuple[int, int]:
    """Pixel center of cell ``n`` (1-based) on a boustrophedon board.

    ``origin_x`` is the left edge of the grid and ``board_bottom_y`` its bottom
    edge. Row 0 is the bottom row; even rows run left->right, odd rows right->left.
    """
    idx = n - 1
    row = idx // cols
    col_in_row = idx % cols
    # Odd rows are walked in reverse, so the visual column flips.
    col = col_in_row if row % 2 == 0 else (cols - 1) - col_in_row
    cx = origin_x + col * cell_px + cell_px // 2
    # Invert the row from the bottom edge because y grows downward.
    cy = board_bottom_y - row * cell_px - cell_px // 2
    return cx, cy


class BoardLayout:
    """Fits a ``cells``-cell, ``cols``-wide grid into a pixel ``area`` and maps
    cell numbers to pixels. ``area`` is ``(x, y, w, h)``; the grid is sized to the
    smaller dimension (square cells) and centered within the area."""

    def __init__(self, cells: int, cols: int, area: tuple[int, int, int, int]) -> None:
        self.cells = cells
        self.cols = cols
        self.rows = -(-cells // cols)  # ceil division: a partial last row still counts
        x, y, w, h = area
        self.cell_px = max(1, min(w // cols, h // self.rows))
        grid_w = self.cols * self.cell_px
        grid_h = self.rows * self.cell_px
        self.origin_x = x + (w - grid_w) // 2
        self.origin_y = y + (h - grid_h) // 2
        self.board_bottom_y = self.origin_y + grid_h

    def cell_to_xy(self, n: int) -> tuple[int, int]:
        return cell_to_xy(n, self.cols, self.cell_px, self.origin_x, self.board_bottom_y)

    def cell_rect(self, n: int) -> pygame.Rect:
        cx, cy = self.cell_to_xy(n)
        half = self.cell_px // 2
        return pygame.Rect(cx - half, cy - half, self.cell_px, self.cell_px)


def _as_int_pairs(mapping: dict) -> dict[int, int]:
    """JSON turns the int cell keys of ``snakes``/``ladders`` into strings on the
    wire; normalize back to ints so geometry lookups work either way."""
    return {int(k): int(v) for k, v in mapping.items()}


def draw_board(surf: pygame.Surface, layout: BoardLayout, board: dict[str, Any]) -> None:
    """Draw the grid, cell numbers, special-tile glyphs, and snake/ladder links."""
    cells = board.get("cells", layout.cells)
    num_font = ui.get_font(max(10, layout.cell_px // 4))

    # Cells + numbers (checkerboard so the serpentine path reads at a glance).
    for n in range(1, cells + 1):
        rect = layout.cell_rect(n)
        shade = _GRID_LIGHT if (n % 2 == 0) else _GRID_DARK
        pygame.draw.rect(surf, shade, rect)
        pygame.draw.rect(surf, _GRID_LINE, rect, width=1)
        img = num_font.render(str(n), True, ui.MUTED)
        surf.blit(img, (rect.x + 3, rect.y + 2))

    # Special-tile glyphs.
    gfont = ui.get_font(max(12, layout.cell_px // 2))
    for kind in ("wheel", "shop", "gold", "debuff"):
        glyph, color = _TILE_GLYPHS[kind]
        for cell in board.get(f"{kind}_tiles", []):
            cx, cy = layout.cell_to_xy(int(cell))
            img = gfont.render(glyph, True, color)
            surf.blit(img, img.get_rect(center=(cx, cy)))

    # Snakes (head -> tail) and ladders (bottom -> top) as colored links.
    for head, tail in _as_int_pairs(board.get("snakes", {})).items():
        pygame.draw.line(
            surf, _SNAKE_COLOR, layout.cell_to_xy(head), layout.cell_to_xy(tail),
            width=max(3, layout.cell_px // 10),
        )
    for bottom, top in _as_int_pairs(board.get("ladders", {})).items():
        pygame.draw.line(
            surf, _LADDER_COLOR, layout.cell_to_xy(bottom), layout.cell_to_xy(top),
            width=max(3, layout.cell_px // 10),
        )


# A small fixed palette so each player's token has a stable, distinct color.
_TOKEN_COLORS = [
    (240, 84, 120), (90, 200, 140), (120, 180, 255), (240, 200, 90),
    (200, 130, 255), (250, 150, 90), (110, 220, 220), (230, 230, 130),
]


def token_color(index: int) -> tuple[int, int, int]:
    return _TOKEN_COLORS[index % len(_TOKEN_COLORS)]


def draw_tokens(
    surf: pygame.Surface,
    layout: BoardLayout,
    players: list[dict[str, Any]],
    overrides: dict[str, tuple[int, int]] | None = None,
) -> None:
    """Draw a token per player at its authoritative cell, unless ``overrides`` maps
    that player's id to an explicit pixel position (the animating token). Tokens
    sharing a cell are nudged apart so they don't fully overlap."""
    overrides = overrides or {}
    radius = max(4, layout.cell_px // 5)
    # Spread co-located tokens around their cell center.
    by_cell: dict[int, int] = {}
    for i, p in enumerate(players):
        pid = p.get("id")
        if pid in overrides:
            cx, cy = overrides[pid]
        else:
            pos = int(p.get("pos", 1))
            cx, cy = layout.cell_to_xy(pos)
            seen = by_cell.get(pos, 0)
            by_cell[pos] = seen + 1
            offset = (seen - 0.5) * radius
            cx += int(offset)
            cy -= int(offset)
        color = token_color(i)
        pygame.draw.circle(surf, (10, 10, 16), (cx, cy), radius + 2)
        pygame.draw.circle(surf, color, (cx, cy), radius)


def draw_legend(surf: pygame.Surface, rect: pygame.Rect, text: str = LEGEND_TEXT) -> None:
    """Word-wrap the legend into ``rect`` (a thin strip under the board)."""
    font = ui.get_font(13)
    words = text.split()
    line = ""
    y = rect.y
    line_h = font.get_height() + 2
    for word in words:
        trial = f"{line} {word}".strip()
        if font.size(trial)[0] > rect.width and line:
            surf.blit(font.render(line, True, ui.MUTED), (rect.x, y))
            y += line_h
            line = word
        else:
            line = trial
    if line:
        surf.blit(font.render(line, True, ui.MUTED), (rect.x, y))


def render_static(
    layout: BoardLayout,
    board: dict[str, Any],
    size: tuple[int, int],
    legend_rect: pygame.Rect | None = None,
) -> pygame.Surface:
    """Pre-render the *unchanging* board onto a transparent surface ONCE.

    The grid, cell numbers, tile glyphs, snake/ladder links, and (optionally) the
    legend never change mid-game, yet :func:`draw_board` re-rasterizes ~100 cell
    numbers via ``font.render`` on every call. That is invisible on desktop but
    collapses the framerate under single-threaded WASM, where text rasterization is
    far slower — so the scene draws this once and blits the result each frame,
    keeping the per-frame board cost to a single blit. Drawn at the layout's
    absolute coordinates onto a ``size``-sized SRCALPHA surface, so the caller blits
    it at ``(0, 0)``."""
    surf = pygame.Surface(size, pygame.SRCALPHA)
    draw_board(surf, layout, board)
    if legend_rect is not None:
        draw_legend(surf, legend_rect)
    return surf
