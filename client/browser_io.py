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


def lock_canvas_aspect(width: int, height: int) -> None:
    """Stop pygbag's template from stretching the canvas (browser-only).

    The stock template CSS is ``width:100%; height:100%``, which smears a
    portrait 480x800 game across a landscape monitor. Override it with the
    largest centered box at the game's aspect ratio; SDL maps mouse coords
    through the CSS box, so input stays aligned.
    """
    if not is_browser():
        return
    import platform  # pygbag-provided in the browser; exposes the JS window

    ratio = width / height
    try:
        style = platform.window.canvas.style
        style.width = f"min(100vw, calc(100vh * {ratio}))"
        style.height = f"min(100vh, calc(100vw / {ratio}))"
        style.margin = "auto"  # template sets absolute + 0 insets -> centers both axes
    except Exception as exc:
        print("browser_io.lock_canvas_aspect error:", exc)


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
    that's still waking (see ``config.CONNECT_MAX_ATTEMPTS``).

    The ``.catch`` is load-bearing: ``fetch`` rejects asynchronously (e.g. the
    server is unreachable, or it's a ws:// endpoint that isn't an HTTP server),
    and Python's ``try/except`` can't see an async JS rejection — unhandled, it
    reaches pygbag's rejection handler and crashes the app with "Failed to
    fetch". Swallowing it in JS is the only place that works."""
    if not is_browser():
        return
    import json
    import platform  # pygbag-provided in the browser; exposes the JS window

    try:
        platform.window.eval(
            f"fetch({json.dumps(http_url)}, {{mode: 'no-cors'}}).catch(() => {{}})")
    except Exception as exc:
        print("browser_io.warm_up_server error:", exc)
