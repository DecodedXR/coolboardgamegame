"""Main menu: host a new game (pick host mode) or join one by code."""

from __future__ import annotations

from typing import Any

import pygame

from client import ui
from client.scenes.base import Scene
from shared import protocol


class MenuScene(Scene):
    def on_enter(self) -> None:
        cx = self.app.width // 2
        self.host_mode = protocol.HOST_HUMAN
        self.mode_btn = ui.Button("HOST MODE: HUMAN", (cx - 160, 175, 320, 40), self._toggle_mode)
        self.host_btn = ui.Button("HOST A GAME", (cx - 160, 225, 320, 48), self._host)
        self.code_in = ui.TextInput((cx - 160, 320, 200, 44), placeholder="ROOM CODE", upper=True, max_len=6)
        self.join_btn = ui.Button("JOIN", (cx + 50, 320, 110, 44), self._join)
        self.status = ""

    def _toggle_mode(self) -> None:
        self.host_mode = protocol.HOST_AUTO if self.host_mode == protocol.HOST_HUMAN else protocol.HOST_HUMAN
        self.mode_btn.label = f"HOST MODE: {self.host_mode.upper()}"

    def _host(self) -> None:
        self.app.net.send(protocol.C_CREATE_ROOM, name=self.app.name, host_mode=self.host_mode)

    def _join(self) -> None:
        code = self.code_in.text.strip().upper()
        if not code:
            self.status = "enter a room code"
            return
        self.app.net.send(protocol.C_JOIN_ROOM, code=code, name=self.app.name)

    def handle_event(self, event: pygame.event.Event) -> None:
        for w in (self.mode_btn, self.host_btn, self.code_in, self.join_btn):
            w.handle(event)

    def on_message(self, msg: dict[str, Any]) -> None:
        t = msg["type"]
        if t in (protocol.S_ROOM_CREATED, protocol.S_ROOM_JOINED):
            self.app.you = msg["you"]
            self.app.room = msg["room"]
            from client.scenes.lobby import LobbyScene
            self.app.go_to(LobbyScene(self.app))
        elif t == protocol.S_ERROR:
            self.status = msg.get("message", "error")

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        ui.Label(f"hi {self.app.name}", (self.app.width // 2, 90), 22, ui.MUTED, center=True).draw(surf)
        ui.Label("HOST OR JOIN", (self.app.width // 2, 130), 34, ui.ACCENT, center=True).draw(surf)
        for w in (self.mode_btn, self.host_btn, self.code_in, self.join_btn):
            w.draw(surf)
        ui.Label("— or —", (self.app.width // 2, 295), 18, ui.MUTED, center=True).draw(surf)
        if self.status:
            ui.Label(self.status, (self.app.width // 2, 390), 18, ui.ACCENT, center=True).draw(surf)
