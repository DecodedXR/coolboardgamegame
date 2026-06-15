"""WebSocket transport for the Tier 4 W1 spike — one tiny surface, two backends.

The whole point of W1 is to prove that a pygbag/WASM client can talk to our real
``wss://`` server with **no** Python ``websockets`` lib and **no** threads, by
driving the browser's native JS ``WebSocket``. The hard, de-risked part is how
Python receives inbound frames from JS. We avoid handing Python callbacks to JS
(the fragile ``create_proxy`` path) entirely:

    * A few lines of JS own the socket and push every inbound frame into a plain
      array on ``window``; a JS ``__cbgg_drain`` returns + clears it.
    * Python only ever *reads* that array (via ``drain``) and *calls* a JS
      ``send`` — never the reverse.

This maps 1:1 onto the existing ``NetClient.poll()`` queue-drain model
(``client/net.py``), so the Tier 4 W3 transport is nearly a copy of the browser
backend here.

``poll()`` returns a list of raw JSON strings (decode is the caller's job, using
``shared.protocol``); ``send(text)`` takes an already-encoded frame; ``state`` is
one of ``"connecting" | "open" | "closed" | "error"``. Both backends share that
surface so ``spike/main.py`` is platform-agnostic.
"""

from __future__ import annotations

import json
import sys

# Separator used to pack the JS inbox array into one string for the trip back to
# Python. Our server only ever sends single-line JSON, which can never contain a
# NUL, so splitting on it is lossless.
_SEP = "\x00"


class BrowserWS:
    """Native-``WebSocket`` backend, used when running under pygbag/Emscripten.

    The JS globals are named ``cbgg_*`` (NOT ``__cbgg_*``): a leading double
    underscore on an identifier written inside a class body is name-mangled by
    Python (``platform.window.__x`` becomes ``platform.window._BrowserWS__x``),
    which would look up the wrong — undefined — JS property and crash the app.
    """

    def __init__(self) -> None:
        self._connected = False  # whether the JS shim has been installed yet
        self._error = False      # eval/interop failed at install time

    def connect(self, url: str) -> None:
        import platform  # pygbag-provided in the browser; exposes the JS window

        # Install the shim once. URL is JSON-quoted so it is safely embedded.
        js = """
        window.cbgg_inbox = [];
        window.cbgg_state = "connecting";
        try {
            // Detach + close any prior socket first, so a late onclose/onerror
            // from a dropped connection can't clobber the new socket's state
            // (which would make the main loop's reconnect logic flap).
            var prev = window.cbgg_ws;
            if (prev) {
                prev.onopen = prev.onmessage = prev.onclose = prev.onerror = null;
                try { prev.close(); } catch (e) {}
            }
            var ws = new WebSocket(%s);
            window.cbgg_ws = ws;
            ws.onopen    = function()  { window.cbgg_state = "open"; };
            ws.onmessage = function(e) { window.cbgg_inbox.push(e.data); };
            ws.onclose   = function()  { window.cbgg_state = "closed"; };
            ws.onerror   = function()  { window.cbgg_state = "error"; };
            window.cbgg_send  = function(t) { if (ws.readyState === 1) ws.send(t); };
            window.cbgg_drain = function() {
                var a = window.cbgg_inbox; window.cbgg_inbox = [];
                return a.join("\\u0000");
            };
        } catch (err) { window.cbgg_state = "error"; }
        """ % json.dumps(url)
        try:
            platform.window.eval(js)
            self._connected = True
            self._error = False
        except Exception as exc:  # eval/window interop unavailable
            print("BrowserWS.connect error:", exc)
            self._error = True

    def reconnect(self, url: str) -> None:
        """Re-open after a drop. The shim is idempotent — just rebuild it."""
        self.connect(url)

    @property
    def state(self) -> str:
        if self._error:
            return "error"
        if not self._connected:
            return "connecting"
        import platform

        try:
            return str(platform.window.cbgg_state)
        except Exception as exc:  # surface, don't black-screen the HUD
            print("BrowserWS.state error:", exc)
            return "error"

    def poll(self) -> list[str]:
        if not self._connected:
            return []
        import platform

        try:
            packed = platform.window.cbgg_drain()
        except Exception as exc:
            print("BrowserWS.poll error:", exc)
            return []
        text = str(packed) if packed is not None else ""
        return text.split(_SEP) if text else []

    def send(self, text: str) -> None:
        if not self._connected:
            return
        import platform

        try:
            platform.window.cbgg_send(text)
        except Exception as exc:
            print("BrowserWS.send error:", exc)

    @staticmethod
    def prompt(label: str, current: str = "") -> str:
        """Native browser text entry — the mobile soft-keyboard workaround."""
        import platform

        result = platform.window.prompt(label, current)
        return str(result) if result is not None else current


class DesktopWS:
    """Threaded ``websockets`` backend so ``python -m spike.main`` runs off a
    browser for a quick local sanity check. Mirrors :class:`BrowserWS`'s surface
    exactly (raw strings in/out, non-blocking ``poll``). Never imported under
    Emscripten, where threads and ``websockets`` are unavailable."""

    def __init__(self) -> None:
        import queue

        self._inbox: "queue.Queue[str]" = queue.Queue()
        self._state = "connecting"
        self._loop = None
        self._ws = None
        self._thread = None

    def connect(self, url: str) -> None:
        import threading

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        self._thread.start()

    def reconnect(self, url: str) -> None:
        # Restart the worker once the previous attempt has finished (e.g. a
        # cold-start open-timeout). Lets `python -m spike.main` recover instead of
        # latching on "error"; the main loop paces the retries.
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = None
        self._ws = None
        self._loop = None
        self._state = "connecting"
        self.connect(url)

    @property
    def state(self) -> str:
        return self._state

    def poll(self) -> list[str]:
        import queue

        out: list[str] = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    def send(self, text: str) -> None:
        import asyncio

        if self._loop is None or self._ws is None:
            return
        asyncio.run_coroutine_threadsafe(self._ws.send(text), self._loop)

    @staticmethod
    def prompt(label: str, current: str = "") -> str:
        # No DOM off-browser; the browser backend is where prompt() is proven.
        return current or "(prompt is browser-only)"

    def _run(self, url: str) -> None:
        import asyncio

        import websockets

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def session() -> None:
            try:
                self._ws = await websockets.connect(url, open_timeout=12.0)
            except Exception:
                self._state = "error"
                return
            self._state = "open"
            try:
                async for raw in self._ws:
                    self._inbox.put(raw if isinstance(raw, str) else raw.decode())
            except Exception:
                pass
            finally:
                self._state = "closed"

        try:
            self._loop.run_until_complete(session())
        finally:
            self._loop.close()


def make_bridge():
    """Pick the backend for the current platform."""
    if sys.platform == "emscripten":
        return BrowserWS()
    return DesktopWS()
