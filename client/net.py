"""Client-side networking bridge between asyncio websockets and pygame.

Pygame's main loop is a synchronous ``while running:`` that must never block, so
the websocket lives on its own thread running a private asyncio loop. Inbound
messages are pushed onto a thread-safe ``queue.Queue`` that the pygame loop drains
once per frame; outbound messages are scheduled onto the network loop with
``run_coroutine_threadsafe``.

Usage from the pygame side::

    net = NetClient()
    net.connect(build_ws_url("localhost", 8765))   # or a full wss:// URL
    ...
    for msg in net.poll():        # called each frame; never blocks
        handle(msg)
    net.send(protocol.C_PING)
"""

from __future__ import annotations

import asyncio
import queue
import sys
from typing import Any, Optional

# Desktop-only deps. Under pygbag/Emscripten there are no OS threads and no
# CPython ``websockets`` lib, so these must never be imported in the browser —
# the browser path (see ``client.net_browser``) drives the native JS WebSocket
# instead. ``asyncio``/``queue`` are pure-Python/pygbag-patched and stay above.
if sys.platform != "emscripten":
    import threading

    import websockets

from shared import protocol
from config import (
    CONNECT_OPEN_TIMEOUT,
    CONNECT_MAX_ATTEMPTS,
    CONNECT_RETRY_BACKOFF,
    CONNECT_RETRY_BACKOFF_MAX,
)

# Synthetic message types the net layer injects so scenes can react to transport
# events through the same queue as real server messages.
EVT_CONNECTED = "_net_connected"
EVT_DISCONNECTED = "_net_disconnected"
EVT_CONNECT_FAILED = "_net_connect_failed"
EVT_CONNECTING = "_net_connecting"  # emitted before each retry; carries "attempt"


def build_ws_url(server: str, port: Optional[int] = None) -> str:
    """Normalize a user-entered server address into a websocket URL.

    Accepts either a full URL or a bare host:

    * ``ws://localhost:8765`` / ``wss://app.onrender.com`` → returned as-is
      (trailing slash trimmed).
    * ``http://`` / ``https://`` → rewritten to ``ws://`` / ``wss://`` so a
      pasted browser address works (Render serves the same host over both).
    * a scheme-less host (``localhost``, ``1.2.3.4``, ``host:9000``) → assumed
      plain ``ws://`` (the LAN case); the supplied ``port`` is appended only when
      the host doesn't already carry one.
    """
    s = (server or "").strip().rstrip("/")
    low = s.lower()
    if low.startswith(("ws://", "wss://")):
        return s
    if low.startswith("http://"):
        return "ws://" + s[len("http://"):]
    if low.startswith("https://"):
        return "wss://" + s[len("https://"):]
    if not s:
        s = "localhost"
    if ":" in s or port is None:
        return f"ws://{s}"
    return f"ws://{s}:{port}"


class NetClient:
    """Client transport with the same public surface on every platform.

    On desktop it runs the websocket on a background thread (below). Under
    pygbag/Emscripten it delegates to a single-loop :class:`~client.net_browser.BrowserNet`
    over the browser's native WebSocket — selected once here so the scenes, the
    connect screen, and the App loop are identical on both.
    """

    def __init__(self) -> None:
        if sys.platform == "emscripten":
            from client.net_browser import BrowserNet

            self._browser: Optional[Any] = BrowserNet()
            return
        self._browser = None
        self._inbox: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()

    # --- public, called from the pygame thread ---------------------------

    def connect(self, url: str) -> None:
        """Start the network thread and begin connecting (non-blocking).

        ``url`` is a full websocket URL (use :func:`build_ws_url` to make one).
        """
        if self._browser is not None:
            return self._browser.connect(url)
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        self._thread.start()

    @property
    def is_connected(self) -> bool:
        if self._browser is not None:
            return self._browser.is_connected
        return self._connected.is_set()

    def send(self, msg_type: str, **payload: Any) -> None:
        """Queue a message to the server. Safe to call before connect finishes;
        dropped if the socket isn't up yet."""
        if self._browser is not None:
            return self._browser.send(msg_type, **payload)
        if self._loop is None or self._ws is None:
            return
        data = protocol.encode(msg_type, **payload)
        asyncio.run_coroutine_threadsafe(self._do_send(data), self._loop)

    def poll(self) -> list[dict[str, Any]]:
        """Drain all pending inbound messages without blocking."""
        if self._browser is not None:
            return self._browser.poll()
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self) -> None:
        if self._browser is not None:
            return self._browser.close()
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    async def _shutdown(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        assert self._loop is not None
        self._loop.stop()

    # --- network thread internals ----------------------------------------

    def _run(self, url: str) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session(url))
        except Exception:
            pass
        finally:
            self._loop.close()

    async def _connect_with_retry(self, url: str) -> Optional[Any]:
        """Try to open the socket, retrying with linear backoff so a cloud
        instance waking from sleep (30–60s) doesn't fail the first connect.
        Returns the websocket, or ``None`` after emitting ``EVT_CONNECT_FAILED``.
        A successful LAN connect returns on the first attempt without waiting."""
        last_error = ""
        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            try:
                return await websockets.connect(url, open_timeout=CONNECT_OPEN_TIMEOUT)
            except Exception as exc:
                last_error = str(exc)
                if attempt >= CONNECT_MAX_ATTEMPTS:
                    break
                # Tell the UI we're still trying (the "waking the server…" state).
                self._inbox.put({"type": EVT_CONNECTING, "attempt": attempt})
                delay = min(CONNECT_RETRY_BACKOFF * attempt, CONNECT_RETRY_BACKOFF_MAX)
                await asyncio.sleep(delay)
        self._inbox.put({"type": EVT_CONNECT_FAILED, "error": last_error})
        return None

    async def _session(self, url: str) -> None:
        ws = await self._connect_with_retry(url)
        if ws is None:
            return
        self._ws = ws
        self._connected.set()
        self._inbox.put({"type": EVT_CONNECTED})
        try:
            async for raw in self._ws:
                try:
                    self._inbox.put(protocol.decode(raw))
                except ValueError:
                    pass  # ignore malformed frames from the server
        except Exception:
            pass
        finally:
            self._connected.clear()
            self._inbox.put({"type": EVT_DISCONNECTED})

    async def _do_send(self, data: str) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(data)
            except Exception:
                pass
