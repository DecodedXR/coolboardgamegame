"""Unit tests for cross-platform text input (Tier 4, W4).

The browser path can't run under real Emscripten in CI, so these simulate it by
monkeypatching :mod:`client.browser_io` — ``is_browser`` → True and ``prompt`` →
a recording fake — then assert that:

* tapping a :class:`client.ui.TextInput` opens the native prompt (honouring
  ``upper`` / ``max_len``) instead of waiting for key events,
* the desktop key-event path is untouched and never calls the prompt, and
* :class:`client.scenes.connect.ConnectScene` collapses to name → CONNECT in the
  browser, connecting to the baked server URL.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pygame
import pytest

from client import browser_io, ui
from client.scenes.connect import ConnectScene
from config import DEFAULT_SERVER_URL


def _click(x: int, y: int) -> pygame.event.Event:
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(x, y))


@pytest.fixture
def browser(monkeypatch):
    """Pretend we're running under Emscripten, with a scripted prompt()."""
    calls: list[tuple[str, str]] = []
    answer = {"value": "typed"}

    def fake_prompt(label, current=""):
        calls.append((label, current))
        return answer["value"]

    monkeypatch.setattr(browser_io, "is_browser", lambda: True)
    monkeypatch.setattr(browser_io, "prompt", fake_prompt)
    return SimpleNamespace(calls=calls, answer=answer)


# --- TextInput --------------------------------------------------------------

def test_browser_tap_opens_prompt(browser):
    field = ui.TextInput((10, 10, 100, 40), placeholder="your name")
    field.handle(_click(20, 20))
    assert field.text == "typed"
    assert browser.calls == [("your name", "")]


def test_browser_tap_outside_does_nothing(browser):
    field = ui.TextInput((10, 10, 100, 40), placeholder="your name")
    field.handle(_click(500, 500))
    assert field.text == ""
    assert browser.calls == []


def test_browser_prompt_respects_upper_and_max_len(browser):
    browser.answer["value"] = "abcdefgh"
    field = ui.TextInput((10, 10, 100, 40), upper=True, max_len=4)
    field.handle(_click(20, 20))
    assert field.text == "ABCD"


def test_desktop_keydown_still_edits_and_skips_prompt(monkeypatch):
    sentinel = []
    monkeypatch.setattr(browser_io, "is_browser", lambda: False)
    monkeypatch.setattr(browser_io, "prompt",
                        lambda *a, **k: sentinel.append(a) or "")
    field = ui.TextInput((10, 10, 100, 40))
    field.handle(_click(20, 20))  # focus
    field.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a, unicode="a"))
    assert field.text == "a"
    assert sentinel == []  # desktop path never reaches the browser prompt


# --- ConnectScene -----------------------------------------------------------

def _fake_app():
    connected: list[str] = []
    net = SimpleNamespace(connect=lambda url: connected.append(url))
    app = SimpleNamespace(width=820, name="Player", server_url="", net=net)
    return app, connected


def test_connect_scene_browser_collapses_to_name(browser):
    app, _ = _fake_app()
    scene = ConnectScene(app)
    scene.on_enter()
    assert scene.fields == [scene.name_in]
    assert scene.url_in is None and scene.host_in is None and scene.port_in is None


def test_connect_scene_browser_uses_baked_url(browser):
    app, connected = _fake_app()
    scene = ConnectScene(app)
    scene.on_enter()
    scene.name_in.text = "Noah"
    scene._connect()
    assert app.name == "Noah"
    assert connected == [DEFAULT_SERVER_URL]
    assert app.server_url == DEFAULT_SERVER_URL


# --- warm_up_server ---------------------------------------------------------
# Demand-based wake-up for the sleeping free-tier server. The browser path can't
# run under real Emscripten in CI, so we fake the pygbag-provided ``platform``
# module (its ``window.eval`` is the JS bridge) via sys.modules.

@pytest.fixture
def fake_window(monkeypatch):
    """Pretend we're in the browser with a recording ``platform.window.eval``."""
    evaluated: list[str] = []
    fake_platform = SimpleNamespace(
        window=SimpleNamespace(eval=lambda js: evaluated.append(js)))
    monkeypatch.setattr(browser_io, "is_browser", lambda: True)
    monkeypatch.setitem(sys.modules, "platform", fake_platform)
    return evaluated


def test_warm_up_server_evals_no_cors_fetch(fake_window):
    browser_io.warm_up_server("https://app.onrender.com/")
    assert len(fake_window) == 1
    js = fake_window[0]
    # A fetch at the exact URL, with no-cors so the opaque response/preflight is moot.
    assert js.startswith("fetch(")
    assert '"https://app.onrender.com/"' in js  # json-encoded → valid JS string literal
    assert "no-cors" in js


def test_warm_up_server_is_noop_off_browser(monkeypatch):
    evaluated: list[str] = []
    monkeypatch.setattr(browser_io, "is_browser", lambda: False)
    monkeypatch.setitem(sys.modules, "platform",
                        SimpleNamespace(window=SimpleNamespace(
                            eval=lambda js: evaluated.append(js))))
    browser_io.warm_up_server("https://app.onrender.com/")
    assert evaluated == []  # desktop never reaches the browser window


def test_warm_up_server_swallows_interop_errors(monkeypatch):
    def boom(js):
        raise RuntimeError("no window")

    monkeypatch.setattr(browser_io, "is_browser", lambda: True)
    monkeypatch.setitem(sys.modules, "platform",
                        SimpleNamespace(window=SimpleNamespace(eval=boom)))
    # Best-effort: a failed wake must never crash app startup.
    browser_io.warm_up_server("https://app.onrender.com/")
