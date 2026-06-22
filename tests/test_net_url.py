"""Unit tests for the client's server-URL normalization (Milestone 3, W1).

These exercise :func:`client.net.build_ws_url` in isolation — no pygame, no
sockets — proving that ``ws`` vs ``wss`` is chosen correctly from full URLs,
http(s) pastes, and bare LAN host:port input.
"""

from __future__ import annotations

import pytest

from client.net import build_ws_url, NetClient, server_health_url


@pytest.mark.parametrize("server, port, expected", [
    # Full websocket URLs pass through untouched (trailing slash trimmed).
    ("ws://localhost:8765", None, "ws://localhost:8765"),
    ("wss://app.onrender.com", None, "wss://app.onrender.com"),
    ("wss://app.onrender.com/", None, "wss://app.onrender.com"),
    ("  wss://app.onrender.com  ", None, "wss://app.onrender.com"),
    # http(s) pastes map onto the matching ws scheme.
    ("https://app.onrender.com", None, "wss://app.onrender.com"),
    ("http://localhost:8765", None, "ws://localhost:8765"),
    # Scheme-less LAN host: plain ws, supplied port appended.
    ("localhost", 8765, "ws://localhost:8765"),
    ("192.168.1.50", 8765, "ws://192.168.1.50:8765"),
    # Scheme-less host that already carries a port keeps it (port arg ignored).
    ("192.168.1.50:9000", 8765, "ws://192.168.1.50:9000"),
    # Bare host with no port given.
    ("example.com", None, "ws://example.com"),
    # Empty / blank input falls back to localhost.
    ("", 8765, "ws://localhost:8765"),
    ("   ", 8765, "ws://localhost:8765"),
])
def test_build_ws_url(server, port, expected):
    assert build_ws_url(server, port) == expected


def test_case_insensitive_scheme():
    assert build_ws_url("WSS://App.Onrender.com") == "WSS://App.Onrender.com"
    assert build_ws_url("HTTPS://app.onrender.com") == "wss://app.onrender.com"


def test_baked_default_server_url_is_secure_and_normalized():
    """The Milestone-3 baked-in default (config.DEFAULT_SERVER_URL) must be a
    real ``wss://`` URL that build_ws_url passes through unchanged — guards
    against a typo'd scheme (e.g. plain ``ws://`` or ``https://``) silently
    shipping in the client default."""
    from config import DEFAULT_SERVER_URL

    assert DEFAULT_SERVER_URL.startswith("wss://")  # TLS to the cloud server
    assert build_ws_url(DEFAULT_SERVER_URL) == DEFAULT_SERVER_URL


@pytest.mark.parametrize("ws_url, expected", [
    # ws/wss map onto the matching http scheme, landing on the root.
    ("wss://app.onrender.com", "https://app.onrender.com/"),
    ("ws://localhost:8765", "http://localhost:8765/"),
    # Trailing slash / path / query are all dropped — the probe targets "/".
    ("wss://app.onrender.com/", "https://app.onrender.com/"),
    ("wss://app.onrender.com/ws?token=x", "https://app.onrender.com/"),
    ("  wss://app.onrender.com  ", "https://app.onrender.com/"),
    # Already-http(s) input keeps its scheme.
    ("https://app.onrender.com", "https://app.onrender.com/"),
    ("http://localhost:8765", "http://localhost:8765/"),
    # Scheme-less LAN host assumes http (and never sleeps anyway).
    ("localhost:8765", "http://localhost:8765/"),
])
def test_server_health_url(ws_url, expected):
    assert server_health_url(ws_url) == expected


def test_health_url_of_baked_default_is_https_root():
    """The wake-up probe for the shipped default must be the HTTPS root of the
    same host — exactly the endpoint server.__main__.health_check answers 200 on,
    so a stray path or a downgraded scheme can't silently miss the wake."""
    from config import DEFAULT_SERVER_URL

    url = server_health_url(DEFAULT_SERVER_URL)
    assert url == "https://" + DEFAULT_SERVER_URL[len("wss://"):].rstrip("/") + "/"
    assert url.startswith("https://") and url.endswith("/")


def test_connect_is_single_use():
    """connect() must no-op once a thread has been spawned. This is *why* the
    connect screen recycles the NetClient after EVT_CONNECT_FAILED — reusing a
    finished client would silently do nothing and dead-end the retry."""
    c = NetClient()
    c._thread = object()  # stand in for an already-spawned (possibly dead) thread
    c.connect("ws://localhost:8765")
    assert c._thread is not None and not hasattr(c._thread, "start")  # unchanged
