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
