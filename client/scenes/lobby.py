"""Lobby: live player list, ready-up, and host-only controls.

All authority flags are derived from the latest ``room`` snapshot (not the stale
``you`` from join time), so badges and buttons update the instant a broadcast
arrives — including when the host role is transferred to or away from us.
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


class LobbyScene(Scene):
    def on_enter(self) -> None:
        self.ready_btn = ui.Button("READY", (_LIST_X, _CTRL_Y1, _CTRL_W, 50), self._toggle_ready)
        self.mode_btn = ui.Button("HOST MODE", (_CTRL_X2, _CTRL_Y1, _CTRL_W, 50), self._toggle_mode)
        self.start_btn = ui.Button("START GAME", (_LIST_X, _CTRL_Y2, _CTRL_W, 50), self._start)
        self.leave_btn = ui.Button("LEAVE", (_CTRL_X2, _CTRL_Y2, _CTRL_W, 50), self._leave)
        # The show-runner can seat bots to fill out a Snakes & Ladders game (solo
        # vs. bots, or topping up a short room). Bots aren't room players — the
        # server creates them from this count and clamps it to the free seats.
        self.bots = 0
        self.bots_minus = ui.Button("-", (_LIST_X + 152, _BOTS_Y, _BOTS_BTN, _BOTS_BTN), self._bots_dec)
        self.bots_plus = ui.Button("+", (_LIST_X + 244, _BOTS_Y, _BOTS_BTN, _BOTS_BTN), self._bots_inc)
        self.status = ""

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
    def is_host(self) -> bool:
        return self.my_id is not None and self.my_id == self.room.get("host_id")

    @property
    def me(self) -> dict[str, Any]:
        for p in self.room.get("players", []):
            if p["id"] == self.my_id:
                return p
        return {}

    # --- actions ----------------------------------------------------------

    def _toggle_ready(self) -> None:
        self.app.net.send(protocol.C_SET_READY, ready=not self.me.get("ready", False))

    def _toggle_mode(self) -> None:
        if not self.is_owner:
            return
        new = protocol.HOST_AUTO if self.room.get("host_mode") == protocol.HOST_HUMAN else protocol.HOST_HUMAN
        self.app.net.send(protocol.C_SET_HOST_MODE, mode=new)

    def _start(self) -> None:
        # Clamp to the seats still open at click time (the roster may have grown
        # since the count was dialed in), so the request matches what the server
        # can seat.
        self.bots = min(self.bots, self._max_bots())
        self.app.net.send(protocol.C_START_GAME, bots=self.bots)

    def _leave(self) -> None:
        self.app.net.send(protocol.C_LEAVE_ROOM)
        self.app.you = None
        self.app.room = None
        from client.scenes.menu import MenuScene
        self.app.go_to(MenuScene(self.app))

    def _can_start(self) -> bool:
        if self.room.get("host_mode") == protocol.HOST_HUMAN:
            return self.is_host
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
        if self.is_owner:
            self.mode_btn.handle(event)
        self.start_btn.enabled = self._can_start()
        self.start_btn.handle(event)
        # Only the show-runner seats bots, and only while a seat is open.
        if self._show_stepper():
            self.bots_minus.handle(event)
            self.bots_plus.handle(event)
        # In human-host mode, the host clicks a player row to hand off the host role.
        if self.is_host and event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for p, rect in self._row_rects():
                if rect.collidepoint(event.pos) and p["id"] != self.my_id:
                    self.app.net.send(protocol.C_TRANSFER_HOST, target_id=p["id"])

    def on_message(self, msg: dict[str, Any]) -> None:
        t = msg["type"]
        if t == protocol.S_ROOM_UPDATE:
            self.app.room = msg["room"]
        elif t == protocol.S_GAME_STARTED:
            self.app.gamestate = None
            from client.scenes.snakes_and_ladders import SnakesAndLaddersScene
            self.app.go_to(SnakesAndLaddersScene(self.app))
        elif t == protocol.S_ERROR:
            self.status = msg.get("message", "error")

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        room = self.room
        code = room.get("code", "????")
        mode = room.get("host_mode", "?")
        ui.Label(f"ROOM {code}", (_LIST_X, 56), 40, ui.ACCENT).draw(surf)
        ui.Label(f"host mode: {mode}", (_LIST_X, 106), 18, ui.MUTED).draw(surf)
        ui.Label("PLAYERS", (_LIST_X, 150), 18, ui.MUTED).draw(surf)

        for p, rect in self._row_rects():
            pygame.draw.rect(surf, ui.PANEL, rect, border_radius=8)
            name = p["name"]
            tags = []
            if p["id"] == room.get("owner_id"):
                tags.append("owner")
            if p["id"] == room.get("host_id"):
                tags.append("HOST")
            if not p["connected"]:
                tags.append("offline")
            label = name + ("   [" + " ".join(tags) + "]" if tags else "")
            color = ui.TEXT if p["connected"] else ui.MUTED
            ui.Label(label, (rect.x + 14, rect.centery - 10), 20, color).draw(surf)
            if p["ready"]:
                ui.Label("READY", (rect.right - 80, rect.centery - 9), 18, ui.GOOD).draw(surf)

        if self.is_host:
            ui.Label("click a player to pass host", (_LIST_X, _ROW_TOP + len(room.get("players", [])) * _ROW_H + 6),
                     16, ui.MUTED).draw(surf)

        # Bots stepper (runner only, and only while a seat is open to fill).
        if self._show_stepper():
            self.bots = min(self.bots, self._max_bots())
            ui.Label("bots", (_LIST_X, _BOTS_Y + 8), 18, ui.MUTED).draw(surf)
            self.bots_minus.draw(surf)
            ui.Label(str(self.bots), (_LIST_X + 206, _BOTS_Y + 8), 20, ui.TEXT).draw(surf)
            self.bots_plus.draw(surf)

        # Bottom control bar (2x2 grid).
        self.ready_btn.label = "UNREADY" if self.me.get("ready") else "READY"
        self.ready_btn.draw(surf)
        if self.is_owner:
            self.mode_btn.label = f"MODE: {mode.upper()}"
            self.mode_btn.draw(surf)
        self.start_btn.enabled = self._can_start()
        self.start_btn.draw(surf)
        self.leave_btn.draw(surf)
        if not self.start_btn.enabled:
            who = "host" if mode == protocol.HOST_HUMAN else "owner"
            ui.Label(f"only the {who} can start", (_LIST_X, _CTRL_Y2 + 56), 14, ui.MUTED).draw(surf)
        if self.status:
            ui.Label(self.status, (_LIST_X, _CTRL_Y2 + 78), 14, ui.ACCENT).draw(surf)
