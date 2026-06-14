"""Client-side networking bridge between asyncio websockets and pygame.

Pygame's main loop is a synchronous ``while running:`` that must never block, so
the websocket lives on its own thread running a private asyncio loop. Inbound
messages are pushed onto a thread-safe ``queue.Queue`` that the pygame loop drains
once per frame; outbound messages are scheduled onto the network loop with
``run_coroutine_threadsafe``.

Usage from the pygame side::

    net = NetClient()
    net.connect("localhost", 8765)
    ...
    for msg in net.poll():        # called each frame; never blocks
        handle(msg)
    net.send(protocol.C_PING)
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Optional

import websockets

from shared import protocol

# Synthetic message types the net layer injects so scenes can react to transport
# events through the same queue as real server messages.
EVT_CONNECTED = "_net_connected"
EVT_DISCONNECTED = "_net_disconnected"
EVT_CONNECT_FAILED = "_net_connect_failed"


class NetClient:
    def __init__(self) -> None:
        self._inbox: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()

    # --- public, called from the pygame thread ---------------------------

    def connect(self, host: str, port: int) -> None:
        """Start the network thread and begin connecting (non-blocking)."""
        if self._thread is not None:
            return
        url = f"ws://{host}:{port}"
        self._thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        self._thread.start()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def send(self, msg_type: str, **payload: Any) -> None:
        """Queue a message to the server. Safe to call before connect finishes;
        dropped if the socket isn't up yet."""
        if self._loop is None or self._ws is None:
            return
        data = protocol.encode(msg_type, **payload)
        asyncio.run_coroutine_threadsafe(self._do_send(data), self._loop)

    def poll(self) -> list[dict[str, Any]]:
        """Drain all pending inbound messages without blocking."""
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self) -> None:
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

    async def _session(self, url: str) -> None:
        try:
            self._ws = await websockets.connect(url, open_timeout=8)
        except Exception as exc:
            self._inbox.put({"type": EVT_CONNECT_FAILED, "error": str(exc)})
            return
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
