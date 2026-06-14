"""Shared configuration defaults.

The server reads ``HOST``/``PORT`` from the environment so the exact same code
runs locally (LAN testing) and on a cloud host (Render/Railway set ``$PORT``).
"""

from __future__ import annotations

import os

# Server bind address. 0.0.0.0 makes it reachable from other machines on the LAN.
SERVER_HOST = os.environ.get("HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT", "8765"))

# Default address the client tries first (overridable in the connect screen).
DEFAULT_CONNECT_HOST = os.environ.get("CONNECT_HOST", "localhost")
DEFAULT_CONNECT_PORT = int(os.environ.get("CONNECT_PORT", str(SERVER_PORT)))

# Room sizing and lifecycle.
MAX_PLAYERS_PER_ROOM = 8
ROOM_CODE_LENGTH = 4

# How long a disconnected player's slot is held before being dropped, in seconds.
# (Reconnection-by-token is a future milestone; the grace period is designed in now.)
DISCONNECT_GRACE_SECONDS = 20.0
