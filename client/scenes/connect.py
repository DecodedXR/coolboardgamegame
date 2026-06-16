"""Connect screen: enter server URL + display name, then connect.

The primary field is a single **server URL** (``ws://``/``wss://``, prefilled
with the baked-in default). Host/port inputs remain below as a LAN fallback,
used only when the URL field is left blank.
"""

from __future__ import annotations

from typing import Any

import pygame

from client import browser_io
from client import net
from client import ui
from client.scenes.base import Scene
from config import DEFAULT_SERVER_URL, DEFAULT_CONNECT_HOST, DEFAULT_CONNECT_PORT


class ConnectScene(Scene):
    def on_enter(self) -> None:
        cx = self.app.width // 2
        self.browser = browser_io.is_browser()
        self.name_in = ui.TextInput((cx - 180, 280, 360, 44), placeholder="your name",
                                    text=self.app.name, max_len=24)
        if self.browser:
            # In the browser the server is always the baked URL — collapse the
            # screen to just name → CONNECT (no URL / LAN fields to type).
            self.url_in = self.host_in = self.port_in = None
            self.fields = [self.name_in]
            btn_y, self.status_y = 360, 470
        else:
            default_url = self.app.server_url or DEFAULT_SERVER_URL or net.build_ws_url(
                DEFAULT_CONNECT_HOST, DEFAULT_CONNECT_PORT)
            self.url_in = ui.TextInput((cx - 180, 372, 360, 40), placeholder="ws:// or wss:// server URL",
                                       text=default_url, max_len=120)
            # Advanced LAN fallback — only consulted when the URL field is blank.
            self.host_in = ui.TextInput((cx - 180, 470, 240, 36), placeholder="host",
                                        text=DEFAULT_CONNECT_HOST)
            self.port_in = ui.TextInput((cx + 70, 470, 110, 36), placeholder="port",
                                        text=str(DEFAULT_CONNECT_PORT), max_len=5)
            self.fields = [self.name_in, self.url_in, self.host_in, self.port_in]
            btn_y, self.status_y = 560, 640
        self.connect_btn = ui.Button("CONNECT", (cx - 100, btn_y, 200, 52), self._connect)
        self.status = ""
        self.connecting = False

    def _connect(self) -> None:
        if self.connecting:
            return
        self.app.name = self.name_in.text.strip() or "Anonymous"
        if self.browser:
            url = self.app.server_url or DEFAULT_SERVER_URL
        else:
            url_text = self.url_in.text.strip()
            if url_text:
                url = net.build_ws_url(url_text)
            else:
                host = self.host_in.text.strip() or DEFAULT_CONNECT_HOST
                try:
                    port = int(self.port_in.text.strip())
                except ValueError:
                    self.status = "port must be a number"
                    return
                url = net.build_ws_url(host, port)
        self.app.server_url = url
        self.status = f"connecting to {url} ..."
        self.connecting = True
        self.app.net.connect(url)

    def handle_event(self, event: pygame.event.Event) -> None:
        for w in self.fields:
            w.handle(event)
        self.connect_btn.handle(event)

    def on_message(self, msg: dict[str, Any]) -> None:
        if msg["type"] == net.EVT_CONNECTED:
            from client.scenes.menu import MenuScene
            self.app.go_to(MenuScene(self.app))
        elif msg["type"] == net.EVT_CONNECTING:
            # A free cloud instance may be waking from sleep — keep the user posted.
            self.status = f"waking the server… (attempt {msg.get('attempt', 1)})"
        elif msg["type"] == net.EVT_CONNECT_FAILED:
            self.status = f"connection failed: {msg.get('error', '')}"
            self.connecting = False
            # The failed client's thread has finished but its `connect()` guard
            # would block any retry, so swap in a fresh one (mirrors how the App
            # recycles the client on EVT_DISCONNECTED). Without this the connect
            # screen dead-ends after one failure until the app is restarted.
            self.app.net = net.NetClient()

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        cx = self.app.width // 2
        ui.Label("THE SCUFFED", (cx, 150), 38, ui.ACCENT, center=True).draw(surf)
        ui.Label("GAMESHOW", (cx, 195), 38, ui.ACCENT, center=True).draw(surf)
        ui.Label("connect to a server", (cx, 245), 18, ui.MUTED, center=True).draw(surf)
        if not self.browser:
            ui.Label("server URL", (cx - 180, 352), 15, ui.MUTED).draw(surf)
            ui.Label("advanced — LAN host/port (if URL blank)",
                     (cx - 180, 450), 14, ui.MUTED).draw(surf)
        for w in self.fields:
            w.draw(surf)
        self.connect_btn.draw(surf)
        if self.status:
            ui.Label(self.status, (cx, self.status_y), 16, ui.MUTED, center=True).draw(surf)
