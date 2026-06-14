"""Scene base class for the pygame client.

A Scene owns a slice of UI. The App pumps it each frame:
``handle_event`` (per pygame event), ``on_message`` (per inbound server/net
message), and ``draw``. Scenes trigger transitions with ``self.app.go_to(...)``.
"""

from __future__ import annotations

from typing import Any

import pygame


class Scene:
    def __init__(self, app: Any) -> None:
        self.app = app

    def on_enter(self) -> None:
        """Called once when the scene becomes active."""

    def handle_event(self, event: pygame.event.Event) -> None:
        """Handle a single pygame input event."""

    def on_message(self, msg: dict[str, Any]) -> None:
        """Handle a single message from the server / net layer."""

    def update(self, dt: float) -> None:
        """Per-frame logic (animations, timers)."""

    def draw(self, surf: pygame.Surface) -> None:
        """Render the scene."""
