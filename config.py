"""Shared configuration defaults.

The server reads ``HOST``/``PORT`` from the environment so the exact same code
runs locally (LAN testing) and on a cloud host (Render/Railway set ``$PORT``).
"""

from __future__ import annotations

import os

# Server bind address. 0.0.0.0 makes it reachable from other machines on the LAN.
SERVER_HOST = os.environ.get("HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT", "8765"))

# Default server the client tries first. A full ``ws://``/``wss://`` URL is the
# primary form (Milestone 3 bakes the deployed ``wss://...onrender.com`` URL in
# here); when blank, the connect screen falls back to the host/port below. The
# host/port remain the LAN-testing path.
DEFAULT_SERVER_URL = os.environ.get("SERVER_URL", "")
DEFAULT_CONNECT_HOST = os.environ.get("CONNECT_HOST", "localhost")
DEFAULT_CONNECT_PORT = int(os.environ.get("CONNECT_PORT", str(SERVER_PORT)))

# Cold-start tolerance. A sleeping free-tier cloud instance (e.g. Render) can
# take 30–60s to wake, so the initial connect uses a generous per-attempt
# timeout and retries with linear backoff before giving up. LAN connects still
# succeed on the first attempt and never wait.
CONNECT_OPEN_TIMEOUT = 12.0       # seconds per websockets.connect attempt
CONNECT_MAX_ATTEMPTS = 6          # total attempts before surfacing failure
CONNECT_RETRY_BACKOFF = 2.0       # base seconds between attempts (grows linearly)
CONNECT_RETRY_BACKOFF_MAX = 8.0   # cap on the inter-attempt delay

# Room sizing and lifecycle.
MAX_PLAYERS_PER_ROOM = 8
ROOM_CODE_LENGTH = 4

# How long a disconnected player's slot is held before being dropped, in seconds.
# (Reconnection-by-token is a future milestone; the grace period is designed in now.)
DISCONNECT_GRACE_SECONDS = 20.0

# --- Wrong Answers Only (first minigame) ----------------------------------
# Need at least two contestants so there's always someone else to vote for.
WAO_MIN_CONTESTANTS = 2
WAO_TOTAL_ROUNDS = 3
WAO_MAX_ANSWER_LEN = 80
WAO_POINTS_PER_VOTE = 100
# Auto-host phase budgets, in seconds (ignored in human-host mode, which is
# advanced manually by the host).
WAO_ANSWER_SECONDS = 60.0
WAO_VOTE_SECONDS = 30.0
WAO_REVEAL_SECONDS = 12.0
