"""Guard the W5 web-packaging contracts so they can't silently regress.

Covers four things that are easy to break and hard to notice until you run a
browser build:
  1. Bundled TTF exists — browser/Emscripten system fonts are unreliable.
  2. main.py imports pygame at the top — pygbag scans the entry file to decide
     which WASM packages to bundle; missing import → no pygame-ce → crash.
  3. Canvas is portrait (HEIGHT > WIDTH) — the layouts were rewritten for 480×800.
  4. pygbag.ini excludes server/, tests/, spike/ — without scoping, the whole
     repo (server code, test runner, spike build artifacts) ends up in the bundle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_CLIENT = _REPO / "client"


def test_bundled_font_exists():
    font = _CLIENT / "assets" / "DejaVuSansMono.ttf"
    assert font.is_file(), f"bundled TTF missing at {font}"


def test_main_py_imports_pygame():
    text = (_REPO / "main.py").read_text()
    assert "import pygame" in text, (
        "main.py must import pygame at the top so pygbag bundles pygame-ce into the WASM build"
    )


def test_canvas_is_portrait():
    text = (_CLIENT / "__main__.py").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("WIDTH, HEIGHT"):
            _, values = stripped.split("=", 1)
            w, h = [int(v.strip()) for v in values.split(",")]
            assert h > w, f"canvas must be portrait (HEIGHT > WIDTH) for mobile layout, got {w}×{h}"
            return
    pytest.fail("WIDTH, HEIGHT not found in client/__main__.py")


def test_pygbag_ini_excludes_server_tests_spike():
    text = (_REPO / "pygbag.ini").read_text()
    for path in ("/server", "/tests", "/spike"):
        assert path in text, f"pygbag.ini must exclude {path!r} to keep it out of the WASM bundle"
