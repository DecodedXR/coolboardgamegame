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
from shared import protocol

_ROW_TOP = 185
_ROW_H = 46
_LIST_X = 24
_LIST_W = 432

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
        self.status = ""

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
        self.app.net.send(protocol.C_START_GAME)

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
            from client.scenes.wrong_answers import WrongAnswersScene
            self.app.go_to(WrongAnswersScene(self.app))
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
