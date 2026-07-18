"""Lobby: live player list, ready-up, and owner-only controls.

All authority flags are derived from the latest ``room`` snapshot (not the stale
``you`` from join time), so badges and buttons update the instant a broadcast
arrives — including when room ownership passes to or away from us.
"""

from __future__ import annotations

from typing import Any, Optional

import pygame

from client import ui
from client.scenes.base import Scene
from config import MAX_PLAYERS_PER_ROOM
from shared import protocol

_ROW_TOP = 185
_ROW_H = 46
_LIST_X = 24
_LIST_W = 432

# Bots stepper (show-runner only): "- bots N +" above the control bar.
_BOTS_Y = 548
_BOTS_BTN = 36
# Bottom control bar: a 2x2 grid of buttons spanning the content width.
_CTRL_X2 = 248          # left edge of the right-hand column
_CTRL_W = 208
_CTRL_Y1 = 600
_CTRL_Y2 = 658

# Bot difficulty cycle. Duplicated as three strings on purpose: the server owns
# the (base, span) presets and validates the choice, so the client never imports
# WB_BOT_DIFFICULTIES (cheaper than a new shared-protocol surface).
_DIFFICULTIES = ("easy", "medium", "hard")


class LobbyScene(Scene):
    def on_enter(self) -> None:
        self.ready_btn = ui.Button("READY", (_LIST_X, _CTRL_Y1, _CTRL_W, 50), self._toggle_ready)
        self.start_btn = ui.Button("START GAME", (_LIST_X, _CTRL_Y2, _CTRL_W, 50), self._start)
        self.leave_btn = ui.Button("LEAVE", (_CTRL_X2, _CTRL_Y2, _CTRL_W, 50), self._leave)
        # The show-runner can seat bots to fill out a Snakes & Ladders game (solo
        # vs. bots, or topping up a short room). Bots aren't room players — the
        # server creates them from this count and clamps it to the free seats.
        self.bots = 0
        self.bots_minus = ui.Button("-", (_LIST_X + 152, _BOTS_Y, _BOTS_BTN, _BOTS_BTN), self._bots_dec)
        self.bots_plus = ui.Button("+", (_LIST_X + 244, _BOTS_Y, _BOTS_BTN, _BOTS_BTN), self._bots_inc)
        # Bot difficulty (Word Bomb): scales how hard bots crack under pressure.
        # On the stepper row, right of the bots +/- (right edge x=456).
        self.difficulty = "medium"
        self.diff_btn = ui.Button("BOTS: MEDIUM", (_LIST_X + 296, _BOTS_Y, 136, _BOTS_BTN), self._cycle_difficulty)
        # The show-runner picks which minigame the lobby launches. Word Bomb is the
        # default; the button toggles between the two ``protocol.GAMES``.
        self.game = protocol.GAME_WORD_BOMB
        self.game_btn = ui.Button(self._game_label(), (_LIST_X, 494, _LIST_W, 40), self._toggle_game)
        self.status = ""

    # --- game picker ------------------------------------------------------

    def _game_label(self) -> str:
        return "GAME: WORD BOMB" if self.game == protocol.GAME_WORD_BOMB else "GAME: SNAKES & LADDERS"

    def _toggle_game(self) -> None:
        self.game = (protocol.GAME_SNAKES_AND_LADDERS
                     if self.game == protocol.GAME_WORD_BOMB
                     else protocol.GAME_WORD_BOMB)

    # --- bots stepper -----------------------------------------------------

    def _max_bots(self) -> int:
        """Free seats the server could actually fill with bots: the room caps total
        players (humans + bots) at ``MAX_PLAYERS_PER_ROOM``, so never offer more
        than the open seats — otherwise the on-screen count lies (the server would
        silently clamp the excess away)."""
        return max(0, MAX_PLAYERS_PER_ROOM - len(self.room.get("players", [])))

    def _bots_inc(self) -> None:
        self.bots = min(self.bots + 1, self._max_bots())

    def _bots_dec(self) -> None:
        self.bots = max(0, self.bots - 1)

    def _cycle_difficulty(self) -> None:
        i = _DIFFICULTIES.index(self.difficulty)
        self.difficulty = _DIFFICULTIES[(i + 1) % len(_DIFFICULTIES)]

    def _show_stepper(self) -> bool:
        """Show the bots stepper only to the show-runner, and only when there is a
        free seat to fill (also keeps it clear of the 'pass host' hint, which only
        reaches the stepper's row at a full 8-player roster)."""
        return self._can_start() and self._max_bots() > 0

    # --- derived authority -----------------------------------------------

    @property
    def room(self) -> dict[str, Any]:
        return self.app.room or {}

    @property
    def my_id(self) -> Optional[str]:
        return (self.app.you or {}).get("id")

    @property
    def is_owner(self) -> bool:
        return self.my_id is not None and self.my_id == self.room.get("owner_id")

    @property
    def me(self) -> dict[str, Any]:
        for p in self.room.get("players", []):
            if p["id"] == self.my_id:
                return p
        return {}

    # --- actions ----------------------------------------------------------

    def _toggle_ready(self) -> None:
        self.app.net.send(protocol.C_SET_READY, ready=not self.me.get("ready", False))

    def _start(self) -> None:
        # Clamp to the seats still open at click time (the roster may have grown
        # since the count was dialed in), so the request matches what the server
        # can seat.
        self.bots = min(self.bots, self._max_bots())
        self.app.net.send(protocol.C_START_GAME, bots=self.bots, game=self.game,
                          bot_difficulty=self.difficulty)

    def _leave(self) -> None:
        self.app.net.send(protocol.C_LEAVE_ROOM)
        self.app.you = None
        self.app.room = None
        from client.scenes.menu import MenuScene
        self.app.go_to(MenuScene(self.app))

    def _can_start(self) -> bool:
        return self.is_owner

    def _row_rects(self) -> list[tuple[dict[str, Any], pygame.Rect]]:
        rows = []
        for i, p in enumerate(self.room.get("players", [])):
            rows.append((p, pygame.Rect(_LIST_X, _ROW_TOP + i * _ROW_H, _LIST_W, _ROW_H - 6)))
        return rows

    # --- pygame plumbing --------------------------------------------------

    def handle_event(self, event: pygame.event.Event) -> None:
        self.ready_btn.handle(event)
        self.leave_btn.handle(event)
        self.start_btn.enabled = self._can_start()
        self.start_btn.handle(event)
        # Only the show-runner seats bots and picks their difficulty, and only while
        # a seat is open.
        if self._show_stepper():
            self.bots_minus.handle(event)
            self.bots_plus.handle(event)
            self.diff_btn.handle(event)
        # Only the show-runner picks the game.
        if self._can_start():
            self.game_btn.handle(event)

    def on_message(self, msg: dict[str, Any]) -> None:
        t = msg["type"]
        if t == protocol.S_ROOM_UPDATE:
            self.app.room = msg["room"]
        elif t == protocol.S_GAME_STARTED:
            self.app.gamestate = None
            if msg.get("game") == protocol.GAME_SNAKES_AND_LADDERS:
                from client.scenes.snakes_and_ladders import SnakesAndLaddersScene
                self.app.go_to(SnakesAndLaddersScene(self.app))
            else:
                from client.scenes.word_bomb import WordBombScene
                self.app.go_to(WordBombScene(self.app))
        elif t == protocol.S_ERROR:
            self.status = msg.get("message", "error")

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        room = self.room
        code = room.get("code", "????")
        ui.Label(f"ROOM {code}", (_LIST_X, 56), 40, ui.ACCENT).draw(surf)
        ui.Label("PLAYERS", (_LIST_X, 150), 18, ui.MUTED).draw(surf)

        for p, rect in self._row_rects():
            pygame.draw.rect(surf, ui.PANEL, rect, border_radius=8)
            name = p["name"]
            tags = []
            if p["id"] == room.get("owner_id"):
                tags.append("owner")
            if not p["connected"]:
                tags.append("offline")
            label = name + ("   [" + " ".join(tags) + "]" if tags else "")
            color = ui.TEXT if p["connected"] else ui.MUTED
            ui.Label(label, (rect.x + 14, rect.centery - 10), 20, color).draw(surf)
            if p["ready"]:
                ui.Label("READY", (rect.right - 80, rect.centery - 9), 18, ui.GOOD).draw(surf)

        # Bots stepper + difficulty (runner only, and only while a seat is open to fill).
        if self._show_stepper():
            self.bots = min(self.bots, self._max_bots())
            ui.Label("bots", (_LIST_X, _BOTS_Y + 8), 18, ui.MUTED).draw(surf)
            self.bots_minus.draw(surf)
            ui.Label(str(self.bots), (_LIST_X + 206, _BOTS_Y + 8), 20, ui.TEXT).draw(surf)
            self.bots_plus.draw(surf)
            self.diff_btn.label = f"BOTS: {self.difficulty.upper()}"
            self.diff_btn.draw(surf)

        # Game picker (runner only).
        if self._can_start():
            self.game_btn.label = self._game_label()
            self.game_btn.draw(surf)

        # Bottom control bar. The freed top-right slot is left empty.
        self.ready_btn.label = "UNREADY" if self.me.get("ready") else "READY"
        self.ready_btn.draw(surf)
        self.start_btn.enabled = self._can_start()
        self.start_btn.draw(surf)
        self.leave_btn.draw(surf)
        if not self.start_btn.enabled:
            ui.Label("only the owner can start", (_LIST_X, _CTRL_Y2 + 56), 14, ui.MUTED).draw(surf)
        if self.status:
            ui.Label(self.status, (_LIST_X, _CTRL_Y2 + 78), 14, ui.ACCENT).draw(surf)
