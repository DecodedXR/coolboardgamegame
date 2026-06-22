"""Browser-only platform primitives (Tier 4, W4).

Two tiny helpers that behave differently under pygbag/Emscripten. They live in
their own module so the widget layer (:mod:`client.ui`) can use them without
importing the net layer, and so tests can monkeypatch them to simulate the
browser while running on desktop CPython.
"""

from __future__ import annotations

import sys


def is_browser() -> bool:
    """True when running under pygbag/Emscripten (the browser/WASM build)."""
    return sys.platform == "emscripten"


def prompt(label: str, current: str = "") -> str:
    """Native browser text entry — the mobile soft-keyboard workaround.

    The pygame canvas can't focus a phone's soft keyboard, so under the browser
    we capture text via the JS ``window.prompt`` dialog instead of key events.
    Returns ``current`` if the user cancels (JS ``null``) or interop fails.
    """
    import platform  # pygbag-provided in the browser; exposes the JS window

    try:
        result = platform.window.prompt(label, current)
    except Exception as exc:
        print("browser_io.prompt error:", exc)
        return current
    return str(result) if result is not None else current


def warm_up_server(http_url: str) -> None:
    """Fire-and-forget GET to wake a sleeping free-tier server (browser-only).

    The game server runs on Render's free tier, which spins down after ~15 min
    idle — a 30-60s cold start on the next connect. Kicking off this request as
    the app loads wakes the instance while the player is still on the connect
    screen, so it's usually warm by the time they hit Connect. Demand-based, so
    the server is up only when someone's actually here (no 24/7 pinger burning
    free instance-hours).

    ``no-cors`` because we only want the side effect: the opaque response is
    never read, which also avoids a CORS preflight. Best-effort — any interop
    failure is swallowed, since the connect path already retries through a server
    that's still waking (see ``config.CONNECT_MAX_ATTEMPTS``)."""
    if not is_browser():
        return
    import json
    import platform  # pygbag-provided in the browser; exposes the JS window

    try:
        platform.window.eval(f"fetch({json.dumps(http_url)}, {{mode: 'no-cors'}})")
    except Exception as exc:
        print("browser_io.warm_up_server error:", exc)
