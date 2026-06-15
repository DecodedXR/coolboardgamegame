"""Unit tests for the browser net transport (Tier 4, W3).

The browser path can't run under real Emscripten in CI, so these drive
:class:`client.net_browser.BrowserNet` with a scriptable **FakeBridge** and assert
that the bridge's ``state`` transitions + raw frames are translated into the exact
``poll() -> list[dict]`` / synthetic-event contract the scenes consume. A final
guard test proves the desktop ``NetClient`` is undisturbed by the platform branch.
"""

from __future__ import annotations

import pytest

from shared import protocol
from client import net_browser
from client.net_browser import BrowserNet
from client.net import (
    EVT_CONNECTED,
    EVT_CONNECTING,
    EVT_CONNECT_FAILED,
    EVT_DISCONNECTED,
)
from config import CONNECT_MAX_ATTEMPTS


class FakeBridge:
    """Stand-in for :class:`BrowserBridge` with a settable ``state`` and a queue of
    raw inbound frames, recording the calls ``BrowserNet`` makes."""

    def __init__(self) -> None:
        self.state = "connecting"
        self.frames: list[str] = []
        self.sent: list[str] = []
        self.connect_calls: list[str] = []
        self.reconnect_calls: list[str] = []
        self.closed = False

    def connect(self, url: str) -> None:
        self.connect_calls.append(url)

    def reconnect(self, url: str) -> None:
        self.reconnect_calls.append(url)

    def poll(self) -> list[str]:
        out, self.frames = self.frames, []
        return out

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        self.closed = True


def _types(msgs: list[dict]) -> list[str]:
    return [m["type"] for m in msgs]


def test_open_emits_connected_once():
    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.connect("wss://example.test")
    assert bridge.connect_calls == ["wss://example.test"]

    bridge.state = "connecting"
    assert net.poll() == []
    assert not net.is_connected

    bridge.state = "open"
    assert _types(net.poll()) == [EVT_CONNECTED]
    assert net.is_connected
    # Staying open emits nothing further.
    assert net.poll() == []


def test_frames_are_decoded_and_malformed_dropped():
    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.connect("wss://example.test")
    bridge.state = "open"
    net.poll()  # consume EVT_CONNECTED

    bridge.frames = [
        protocol.encode(protocol.S_ROOM_UPDATE, room={"code": "ABCD"}),
        "not json{",            # malformed → silently dropped
        protocol.encode(protocol.S_PONG),
    ]
    msgs = net.poll()
    assert _types(msgs) == [protocol.S_ROOM_UPDATE, protocol.S_PONG]
    assert msgs[0]["room"] == {"code": "ABCD"}


def test_drop_after_open_emits_disconnected_once():
    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.connect("wss://example.test")
    bridge.state = "open"
    net.poll()  # EVT_CONNECTED

    bridge.state = "closed"
    assert _types(net.poll()) == [EVT_DISCONNECTED]
    assert not net.is_connected
    # No repeat once already disconnected.
    assert net.poll() == []


def test_cold_start_retries_then_fails(monkeypatch):
    """Never reaching ``open`` drives EVT_CONNECTING retries paced by the backoff,
    then a single EVT_CONNECT_FAILED at the attempt cap."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(net_browser.time, "monotonic", lambda: clock["t"])

    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.connect("wss://example.test")
    bridge.state = "error"

    seen: list[str] = []
    # Step well past each backoff window so every poll is allowed to retry.
    for _ in range(CONNECT_MAX_ATTEMPTS + 2):
        seen.extend(_types(net.poll()))
        clock["t"] += 1000.0

    connecting = [t for t in seen if t == EVT_CONNECTING]
    failed = [t for t in seen if t == EVT_CONNECT_FAILED]
    assert len(connecting) == CONNECT_MAX_ATTEMPTS - 1  # attempts 1..N-1
    assert len(failed) == 1                             # exactly one terminal failure
    assert len(bridge.reconnect_calls) == CONNECT_MAX_ATTEMPTS - 1


def test_backoff_gates_retries(monkeypatch):
    """Within a backoff window, extra polls don't fire another retry."""
    clock = {"t": 0.0}
    monkeypatch.setattr(net_browser.time, "monotonic", lambda: clock["t"])

    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.connect("wss://example.test")
    bridge.state = "error"

    assert _types(net.poll()) == [EVT_CONNECTING]  # first retry fires immediately
    assert net.poll() == []                        # same frame/time: gated
    assert net.poll() == []
    assert len(bridge.reconnect_calls) == 1


def test_send_encodes_through_bridge():
    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.connect("wss://example.test")
    net.send(protocol.C_PING)
    assert bridge.sent == [protocol.encode(protocol.C_PING)]


def test_close_delegates_to_bridge():
    bridge = FakeBridge()
    net = BrowserNet(bridge=bridge)
    net.close()
    assert bridge.closed


def test_desktop_netclient_unaffected_by_branch():
    """On a non-Emscripten platform the desktop transport is selected and keeps its
    threaded internals — the platform branch must not disturb it."""
    import sys

    assert sys.platform != "emscripten"  # the test runner is desktop CPython
    from client.net import NetClient

    c = NetClient()
    assert c._browser is None
    assert hasattr(c, "_inbox") and hasattr(c, "_thread")
