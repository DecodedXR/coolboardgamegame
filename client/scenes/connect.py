"""Connect screen: enter server address + display name, then connect."""

from __future__ import annotations

from typing import Any

import pygame

from client import net
from client import ui
from client.scenes.base import Scene
from config import DEFAULT_CONNECT_HOST, DEFAULT_CONNECT_PORT


class ConnectScene(Scene):
    def on_enter(self) -> None:
        cx = self.app.width // 2
        self.host_in = ui.TextInput((cx - 180, 200, 240, 40), placeholder="server host",
                                    text=DEFAULT_CONNECT_HOST)
        self.port_in = ui.TextInput((cx + 70, 200, 110, 40), placeholder="port",
                                    text=str(DEFAULT_CONNECT_PORT), max_len=5)
        self.name_in = ui.TextInput((cx - 180, 270, 360, 40), placeholder="your name",
                                    text=self.app.name, max_len=24)
        self.connect_btn = ui.Button("CONNECT", (cx - 100, 340, 200, 48), self._connect)
        self.status = ""
        self.connecting = False

    def _connect(self) -> None:
        if self.connecting:
            return
        host = self.host_in.text.strip() or DEFAULT_CONNECT_HOST
        try:
            port = int(self.port_in.text.strip())
        except ValueError:
            self.status = "port must be a number"
            return
        self.app.name = self.name_in.text.strip() or "Anonymous"
        self.app.server_host, self.app.server_port = host, port
        self.status = f"connecting to {host}:{port} ..."
        self.connecting = True
        self.app.net.connect(host, port)

    def handle_event(self, event: pygame.event.Event) -> None:
        for w in (self.host_in, self.port_in, self.name_in):
            w.handle(event)
        self.connect_btn.handle(event)

    def on_message(self, msg: dict[str, Any]) -> None:
        if msg["type"] == net.EVT_CONNECTED:
            from client.scenes.menu import MenuScene
            self.app.go_to(MenuScene(self.app))
        elif msg["type"] == net.EVT_CONNECT_FAILED:
            self.status = f"connection failed: {msg.get('error', '')}"
            self.connecting = False

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        ui.Label("THE SCUFFED GAMESHOW", (self.app.width // 2, 110), 40, ui.ACCENT, center=True).draw(surf)
        ui.Label("connect to a server", (self.app.width // 2, 155), 20, ui.MUTED, center=True).draw(surf)
        for w in (self.host_in, self.port_in, self.name_in):
            w.draw(surf)
        self.connect_btn.draw(surf)
        if self.status:
            ui.Label(self.status, (self.app.width // 2, 410), 18, ui.MUTED, center=True).draw(surf)
