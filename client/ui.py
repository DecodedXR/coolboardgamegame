"""Minimal immediate-ish-mode widgets for the pygame client.

Deliberately tiny: a Button, a single-line TextInput, and a Label. Scenes own
their widget instances, forward pygame events to them, and call ``draw`` each
frame. Good enough for lobby UI without pulling in a GUI dependency.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Callable, Optional

import pygame

from client import browser_io

# Palette — a moody gameshow look.
BG = (18, 18, 28)
PANEL = (32, 33, 48)
ACCENT = (240, 84, 120)
ACCENT_DIM = (120, 50, 64)
TEXT = (236, 236, 244)
MUTED = (150, 152, 172)
GOOD = (90, 200, 140)
FIELD = (44, 46, 64)


# A bundled monospace TTF, not a system font: browser/Emscripten system fonts are
# unreliable and vary per device, so we ship one and render identically everywhere.
_FONT_PATH = Path(__file__).parent / "assets" / "DejaVuSansMono.ttf"


@functools.lru_cache(maxsize=64)
def get_font(size: int) -> pygame.font.Font:
    # Cached: this is called for every Label/Button/TextInput every frame.
    return pygame.font.Font(str(_FONT_PATH), size)


class Label:
    def __init__(self, text: str, pos: tuple[int, int], size: int = 22,
                 color: tuple[int, int, int] = TEXT, center: bool = False) -> None:
        self.text = text
        self.pos = pos
        self.size = size
        self.color = color
        self.center = center

    def draw(self, surf: pygame.Surface) -> None:
        font = get_font(self.size)
        img = font.render(self.text, True, self.color)
        rect = img.get_rect()
        if self.center:
            rect.center = self.pos
        else:
            rect.topleft = self.pos
        surf.blit(img, rect)


class Button:
    def __init__(self, label: str, rect: tuple[int, int, int, int],
                 on_click: Callable[[], None], *, enabled: bool = True) -> None:
        self.label = label
        self.rect = pygame.Rect(rect)
        self.on_click = on_click
        self.enabled = enabled
        self._hover = False

    def handle(self, event: pygame.event.Event) -> None:
        if not self.enabled:
            return
        if event.type == pygame.MOUSEMOTION:
            self._hover = self.rect.collidepoint(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.rect.collidepoint(event.pos):
                self.on_click()

    def draw(self, surf: pygame.Surface) -> None:
        if not self.enabled:
            color = ACCENT_DIM
        elif self._hover:
            color = ACCENT
        else:
            color = (200, 70, 100)
        pygame.draw.rect(surf, color, self.rect, border_radius=8)
        font = get_font(20)
        img = font.render(self.label, True, TEXT if self.enabled else MUTED)
        surf.blit(img, img.get_rect(center=self.rect.center))


class TextInput:
    def __init__(self, rect: tuple[int, int, int, int], *, placeholder: str = "",
                 text: str = "", max_len: int = 32, upper: bool = False) -> None:
        self.rect = pygame.Rect(rect)
        self.placeholder = placeholder
        self.text = text
        self.max_len = max_len
        self.upper = upper
        self.focused = False

    def handle(self, event: pygame.event.Event) -> None:
        if browser_io.is_browser():
            # The phone soft keyboard can't focus the canvas, so key events never
            # arrive. Tapping the field opens the browser's native prompt instead.
            if (event.type == pygame.MOUSEBUTTONDOWN and event.button == 1
                    and self.rect.collidepoint(event.pos)):
                value = browser_io.prompt(self.placeholder or "enter text", self.text)
                if self.upper:
                    value = value.upper()
                self.text = value[: self.max_len]
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self.focused = self.rect.collidepoint(event.pos)
        elif event.type == pygame.KEYDOWN and self.focused:
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_TAB):
                self.focused = False
            elif event.unicode and event.unicode.isprintable() and len(self.text) < self.max_len:
                ch = event.unicode.upper() if self.upper else event.unicode
                self.text += ch

    def draw(self, surf: pygame.Surface) -> None:
        pygame.draw.rect(surf, FIELD, self.rect, border_radius=6)
        border = ACCENT if self.focused else (70, 72, 96)
        pygame.draw.rect(surf, border, self.rect, width=2, border_radius=6)
        font = get_font(20)
        if self.text:
            img = font.render(self.text, True, TEXT)
        else:
            img = font.render(self.placeholder, True, MUTED)
        surf.blit(img, (self.rect.x + 10, self.rect.centery - img.get_height() // 2))
