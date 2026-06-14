"""Placeholder in-game screen.

Milestone 1 ends here: ``start_game`` lands every client on this screen, proving
the full lobby->game handoff. The next milestone replaces this with the first
real minigame module (Wrong Answers Only), driven by the room's host mode.
"""

from __future__ import annotations

from typing import Any

import pygame

from client import ui
from client.scenes.base import Scene
from shared import protocol


class IngameStubScene(Scene):
    def on_enter(self) -> None:
        w, h = self.app.width, self.app.height
        self.back_btn = ui.Button("BACK TO LOBBY", (w // 2 - 120, h - 110, 240, 46), self._back)

    def _back(self) -> None:
        from client.scenes.lobby import LobbyScene
        self.app.go_to(LobbyScene(self.app))

    def handle_event(self, event: pygame.event.Event) -> None:
        self.back_btn.handle(event)

    def on_message(self, msg: dict[str, Any]) -> None:
        if msg["type"] == protocol.S_ROOM_UPDATE:
            self.app.room = msg["room"]

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        cx, cy = self.app.width // 2, self.app.height // 2
        room = self.app.room or {}
        ui.Label("GAME STARTED", (cx, cy - 80), 48, ui.ACCENT, center=True).draw(surf)
        ui.Label(f"room {room.get('code', '????')} · host mode: {room.get('host_mode', '?')}",
                 (cx, cy - 20), 22, ui.MUTED, center=True).draw(surf)
        ui.Label("(minigame plugs in here next milestone)", (cx, cy + 20), 18, ui.MUTED, center=True).draw(surf)
        self.back_btn.draw(surf)
