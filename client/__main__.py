"""Pygame client entrypoint:  ``python -m client``

Owns the window, the :class:`NetClient`, and shared session state (your identity,
the current room). Runs the classic pygame loop: pump input events, drain inbound
network messages, update, draw. Both event streams are forwarded to the active
:class:`Scene`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pygame

from client.net import NetClient, EVT_DISCONNECTED, build_ws_url
from client.scenes.base import Scene
from client.scenes.connect import ConnectScene
from config import DEFAULT_SERVER_URL, DEFAULT_CONNECT_HOST, DEFAULT_CONNECT_PORT

# Portrait canvas — a phone held upright fills it without rotating, and the same
# layout serves the desktop window (one layout to maintain, not a per-platform fork).
WIDTH, HEIGHT = 480, 800
FPS = 60


class App:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("The Scuffed Gameshow")
        self.surface = pygame.display.set_mode((WIDTH, HEIGHT))
        self.clock = pygame.time.Clock()
        self.width, self.height = WIDTH, HEIGHT

        self.net = NetClient()
        self.running = True

        # Session state, shared across scenes.
        self.name = "Player"
        self.server_url = DEFAULT_SERVER_URL or build_ws_url(
            DEFAULT_CONNECT_HOST, DEFAULT_CONNECT_PORT)
        self.you: Optional[dict[str, Any]] = None
        self.room: Optional[dict[str, Any]] = None
        self.gamestate: Optional[dict[str, Any]] = None

        self.scene: Scene = ConnectScene(self)
        self.scene.on_enter()

    def go_to(self, scene: Scene) -> None:
        self.scene = scene
        self.scene.on_enter()

    def _on_global_message(self, msg: dict[str, Any]) -> None:
        # Losing the connection always kicks back to the connect screen.
        if msg["type"] == EVT_DISCONNECTED and not isinstance(self.scene, ConnectScene):
            self.you = None
            self.room = None
            self.gamestate = None
            self.net = NetClient()  # fresh client for the next attempt
            self.go_to(ConnectScene(self))

    async def run_async(self) -> None:
        while self.running:
            dt = self.clock.tick(FPS) / 1000.0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                else:
                    self.scene.handle_event(event)
            for msg in self.net.poll():
                self._on_global_message(msg)
                self.scene.on_message(msg)
            self.scene.update(dt)
            self.scene.draw(self.surface)
            pygame.display.flip()
            # Yield to the event loop each frame. Required under pygbag/Emscripten
            # (the browser drives the loop); a harmless no-op cost on desktop.
            await asyncio.sleep(0)

        self.net.close()
        pygame.quit()


def main() -> None:
    asyncio.run(App().run_async())


if __name__ == "__main__":
    main()
