"""Browser/WASM net transport for :class:`client.net.NetClient` (Tier 4, W3).

Under pygbag/Emscripten there are **no threads and no CPython ``websockets`` lib**,
so the desktop transport can't run. Instead the browser drives its **native JS
``WebSocket``**, which connects straight to our real ``wss://`` server. This module
holds the two pieces that give ``NetClient`` a browser backend behind its existing
public surface:

* :class:`BrowserBridge` — the proven W1 JS-shim (ported from
  ``spike/ws_bridge.py``). It owns the socket from a few lines of JS and exposes a
  ``state`` string + raw inbound frames, never handing Python callbacks to JS.
* :class:`BrowserNet` — translates that ``state`` machine + raw frames into the
  exact ``poll() -> list[dict]`` / synthetic-event contract the scenes consume
  (``EVT_CONNECTED`` / ``EVT_CONNECTING`` / ``EVT_CONNECT_FAILED`` /
  ``EVT_DISCONNECTED``), reusing the Milestone-3 cold-start retry UX. The bridge is
  injectable so the translation is unit-testable off-Emscripten.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from shared import protocol
from config import (
    CONNECT_MAX_ATTEMPTS,
    CONNECT_RETRY_BACKOFF,
    CONNECT_RETRY_BACKOFF_MAX,
)

# Imported here so there is a single definition shared with the desktop path.
from client.net import (
    EVT_CONNECTED,
    EVT_CONNECTING,
    EVT_CONNECT_FAILED,
    EVT_DISCONNECTED,
)

# Separator used to pack the JS inbox array into one string for the trip back to
# Python. Our server only ever sends single-line JSON, which can never contain a
# NUL, so splitting on it is lossless.
_SEP = "\x00"


class BrowserBridge:
    """Native-``WebSocket`` shim, used when running under pygbag/Emscripten.

    The JS globals are named ``cbgg_*`` (NOT ``__cbgg_*``): a leading double
    underscore on an identifier written inside a class body is name-mangled by
    Python (``platform.window.__x`` becomes ``platform.window._BrowserBridge__x``),
    which would look up the wrong — undefined — JS property and crash the app.
    """

    def __init__(self) -> None:
        self._installed = False  # whether the JS shim has been installed yet
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
            // (which would make the reconnect logic flap).
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
            self._installed = True
            self._error = False
        except Exception as exc:  # eval/window interop unavailable
            print("BrowserBridge.connect error:", exc)
            self._error = True

    def reconnect(self, url: str) -> None:
        """Re-open after a drop. The shim is idempotent — just rebuild it."""
        self.connect(url)

    @property
    def state(self) -> str:
        if self._error:
            return "error"
        if not self._installed:
            return "connecting"
        import platform

        try:
            return str(platform.window.cbgg_state)
        except Exception as exc:  # surface, don't crash the loop
            print("BrowserBridge.state error:", exc)
            return "error"

    def poll(self) -> list[str]:
        if not self._installed:
            return []
        import platform

        try:
            packed = platform.window.cbgg_drain()
        except Exception as exc:
            print("BrowserBridge.poll error:", exc)
            return []
        text = str(packed) if packed is not None else ""
        return text.split(_SEP) if text else []

    def send(self, text: str) -> None:
        if not self._installed:
            return
        import platform

        try:
            platform.window.cbgg_send(text)
        except Exception as exc:
            print("BrowserBridge.send error:", exc)

    def close(self) -> None:
        if not self._installed:
            return
        import platform

        try:
            platform.window.eval(
                "if (window.cbgg_ws) { try { window.cbgg_ws.close(); } catch (e) {} }"
            )
        except Exception as exc:
            print("BrowserBridge.close error:", exc)

    @staticmethod
    def prompt(label: str, current: str = "") -> str:
        """Native browser text entry — the mobile soft-keyboard workaround (W4)."""
        import platform

        result = platform.window.prompt(label, current)
        return str(result) if result is not None else current


class BrowserNet:
    """Browser backend for :class:`client.net.NetClient`.

    Mirrors ``NetClient``'s public surface (``connect`` / ``poll`` / ``send`` /
    ``close`` / ``is_connected``) but, having no thread to push events, derives the
    synthetic transport events from the bridge's ``state`` transitions each time
    ``poll()`` is called (once per frame). ``poll()`` returns **decoded protocol
    dicts**, same as the desktop path.

    ``bridge`` is injectable so the translation can be unit-tested with a fake
    off-Emscripten; it defaults to a real :class:`BrowserBridge`.
    """

    def __init__(self, bridge: Optional[Any] = None) -> None:
        self._bridge = bridge if bridge is not None else BrowserBridge()
        self._url = ""
        self._connected = False     # have we ever reached "open"?
        self._opened = False        # latched once open, so we emit CONNECTED once
        self._failed = False        # CONNECT_FAILED already surfaced — stay quiet
        self._attempt = 0           # cold-start retry counter
        self._next_retry_at = 0.0   # monotonic time gate for the next reconnect

    # --- public surface (delegated to from NetClient) --------------------

    def connect(self, url: str) -> None:
        self._url = url
        self._connected = False
        self._opened = False
        self._failed = False
        self._attempt = 0
        self._next_retry_at = 0.0
        self._bridge.connect(url)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def send(self, msg_type: str, **payload: Any) -> None:
        self._bridge.send(protocol.encode(msg_type, **payload))

    def poll(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        state = self._bridge.state

        if not self._opened and state == "open":
            self._opened = True
            self._connected = True
            out.append({"type": EVT_CONNECTED})
        elif self._opened and state in ("closed", "error"):
            # An established connection dropped: kick back to the connect screen
            # (the App recycles the client, mirroring the desktop path).
            if self._connected:
                self._connected = False
                out.append({"type": EVT_DISCONNECTED})
        elif not self._opened and state in ("closed", "error") and not self._failed:
            # Never opened — the server may be a free instance waking from sleep
            # (30–60s). Retry by re-opening, paced by monotonic time (no thread).
            out.extend(self._retry())

        # Drain + decode any inbound frames the shim collected this frame.
        for raw in self._bridge.poll():
            try:
                out.append(protocol.decode(raw))
            except ValueError:
                pass  # ignore malformed frames from the server
        return out

    def close(self) -> None:
        self._bridge.close()

    # --- internals -------------------------------------------------------

    def _retry(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now < self._next_retry_at:
            return []  # still waiting out the backoff between attempts
        self._attempt += 1
        if self._attempt >= CONNECT_MAX_ATTEMPTS:
            self._failed = True
            return [{"type": EVT_CONNECT_FAILED, "error": "could not reach server"}]
        delay = min(CONNECT_RETRY_BACKOFF * self._attempt, CONNECT_RETRY_BACKOFF_MAX)
        self._next_retry_at = now + delay
        self._bridge.reconnect(self._url)
        return [{"type": EVT_CONNECTING, "attempt": self._attempt}]
