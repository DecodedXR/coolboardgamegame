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
# primary form: Milestone 3 baked the deployed Render URL in here so the friend's
# flow is just launch → name → join code (no address to type, no ngrok). Override
# with ``SERVER_URL`` (e.g. set it empty for LAN) and the connect screen falls back
# to the host/port below — the LAN-testing path.
DEFAULT_SERVER_URL = os.environ.get("SERVER_URL", "wss://coolboardgamegame.onrender.com")
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

# --- Snakes & Ladders (snake-heavy board game) ----------------------------
# These are the runtime tunables; the pure ``SnakesAndLaddersGame`` mirrors them
# as constructor defaults so it stays import-free of config (the connection layer
# passes these through, so config is the single source of truth at runtime).
SAL_MIN_CONTESTANTS = 2       # humans + bots must total at least this to start
SAL_BOARD_CELLS = 100         # serpentine board, cell 1 = start, last = finish
SAL_BOARD_COLS = 10           # grid width (rows = cells / cols)
SAL_SNAKE_COUNT = 14          # deliberately snake-heavy: winning is brutal
SAL_LADDER_COUNT = 4
SAL_SHOP_TILES = 4            # land here -> shop sub-state (buy a powerup)
SAL_WHEEL_TILES = 5          # land here -> spin a Wheel-of-Names for a random outcome
SAL_GOLD_TILES = 6           # land here -> gain gold
SAL_DEBUFF_TILES = 5         # land here -> a random debuff is applied
SAL_STARTING_GOLD = 100
SAL_DICE_SIDES = 6
SAL_EXACT_FINISH = True       # overshooting the last cell bounces back

# Item shop prices (gold).
SAL_PRICE_IMMUNITY = 40       # block the next snake this turn
SAL_PRICE_BOOST = 30          # +SAL_BOOST_BONUS to the next roll
SAL_PRICE_DOUBLE = 60         # double the next roll
SAL_PRICE_REROLL = 30         # roll twice, keep the higher die

# Effect magnitudes.
SAL_BOOST_BONUS = 3
SAL_GOLD_TILE_AMOUNT = 30     # gold gained on a gold tile
SAL_SLIP_BACK = 6             # cells slid backwards by the slip-back debuff
SAL_GOLD_TAX = 25            # gold lost to the gold-tax debuff

# Auto-host / bot turn budgets, in seconds.
SAL_ROLL_SECONDS = 30
SAL_SHOP_SECONDS = 20
SAL_BOT_DELAY_SECONDS = 1.2

# --- Word Bomb (type-a-word-before-the-bomb-explodes) ----------------------
WB_LIVES = 2                  # lives per player; 0 lives = eliminated
WB_TURN_SECONDS = 12          # auto-host fuse: submit a valid word in this window
WB_MIN_WORDS_PER_PROMPT = 500 # a prompt substring must appear in at least this many words
WB_BOT_FAIL_CHANCE = 0.25     # chance a bot fumbles its turn and eats the explosion

assert SAL_SNAKE_COUNT > SAL_LADDER_COUNT, "board must be snake-heavy (more snakes than ladders)"
# Every special occupies a distinct cell; cells 1 and last are reserved (no specials).
_SAL_SPECIAL_CELLS = (
    2 * SAL_SNAKE_COUNT + 2 * SAL_LADDER_COUNT
    + SAL_SHOP_TILES + SAL_WHEEL_TILES + SAL_GOLD_TILES + SAL_DEBUFF_TILES
)
assert _SAL_SPECIAL_CELLS <= SAL_BOARD_CELLS - 2, "too many specials to fit on the board"
