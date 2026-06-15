"""Tier 4 W1 de-risk spike — async pygbag app + browser-WebSocket bridge.

Run in the browser sim (the real acceptance):

    pip install pygbag
    pygbag spike/main.py        # then open http://localhost:8000

Expect the status line to go ``connecting -> open`` against the live Render
server (allow ~30-60s for a cold start), SPACE / a tap to bump the ping count and
refresh the "last pong" line, and ``P`` / a tap on the name box to pop a native
``prompt()`` whose text renders back.

Also runs off-browser for a quick local sanity check (uses the threaded
``websockets`` backend instead of the JS bridge):

    python -m spike.main

The pygbag-required shape is here: a single ``async def main()`` loop that does
``await asyncio.sleep(0)`` every frame, with no threads on the browser path.
"""

from __future__ import annotations

import asyncio
import json
import time

import pygame

# main.py runs at the top level under pygbag (cwd = spike/), but as a package
# module under ``python -m spike.main`` — support both import roots.
try:
    from ws_bridge import make_bridge
except ImportError:  # pragma: no cover - exercised by `python -m spike.main`
    from spike.ws_bridge import make_bridge

# Reuse the real protocol + baked server URL when they're importable (desktop
# run). The pygbag spike build may not package repo-root modules — that scoping
# is Tier 4 W5 — so fall back to the handful of constants the spike needs.
try:
    from shared import protocol  # type: ignore
    from config import DEFAULT_SERVER_URL  # type: ignore
except ImportError:  # pragma: no cover - exercised inside the pygbag build
    DEFAULT_SERVER_URL = "wss://coolboardgamegame.onrender.com"

    class protocol:  # type: ignore
        C_PING = "ping"
        S_PONG = "pong"

        @staticmethod
        def encode(msg_type, **payload):
            payload["type"] = msg_type
            return json.dumps(payload)

        @staticmethod
        def decode(raw):
            return json.loads(raw)


WIDTH, HEIGHT = 480, 640  # portrait-ish, friendly to a phone viewport (W5 refines)
FPS = 60
BG = (18, 18, 28)
FG = (235, 235, 245)
ACCENT = (120, 200, 160)
DIM = (140, 140, 160)
RECONNECT_AFTER = 3.0  # seconds before re-opening a dropped/failed socket


def _draw(surface, font, big, lines) -> None:
    surface.fill(BG)
    big_surf = big.render("W1 spike: WS bridge", True, FG)
    surface.blit(big_surf, (20, 20))
    y = 80
    for text, color in lines:
        surface.blit(font.render(text, True, color), (20, y))
        y += 34
    hint = font.render("SPACE/tap: ping    P: prompt()", True, DIM)
    surface.blit(hint, (20, HEIGHT - 40))


async def main() -> None:
    pygame.init()
    pygame.display.set_caption("W1 spike")
    surface = pygame.display.set_mode((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas,menlo,monospace", 22)
    big = pygame.font.SysFont("consolas,menlo,monospace", 30, bold=True)

    url = DEFAULT_SERVER_URL or "wss://coolboardgamegame.onrender.com"
    bridge = make_bridge()
    bridge.connect(url)

    pings_sent = 0
    pongs_recv = 0
    last_pong_at: float | None = None
    name = ""
    reconnect_at: float | None = None
    running = True

    def send_ping() -> None:
        nonlocal pings_sent
        bridge.send(protocol.encode(protocol.C_PING))
        pings_sent += 1

    while running:
        clock.tick(FPS)
        now = time.monotonic()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_SPACE:
                    send_ping()
                elif event.key == pygame.K_p:
                    name = bridge.prompt("Your name", name)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                # Touch taps arrive as mouse events. Top third taps the name box;
                # anywhere else pings — enough to exercise both on a phone.
                if event.pos[1] < HEIGHT // 3:
                    name = bridge.prompt("Your name", name)
                else:
                    send_ping()

        # Drain inbound frames (raw JSON strings) and count pongs.
        for raw in bridge.poll():
            try:
                msg = protocol.decode(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if msg.get("type") == protocol.S_PONG:
                pongs_recv += 1
                last_pong_at = now

        state = bridge.state

        # Spike-grade cold-start handling: if the socket failed or dropped, wait a
        # few seconds and re-open. Full backoff is a Tier 4 W3 concern.
        if state in ("error", "closed"):
            if reconnect_at is None:
                reconnect_at = now + RECONNECT_AFTER
            elif now >= reconnect_at:
                bridge.reconnect(url)
                reconnect_at = None
        else:
            reconnect_at = None

        if state == "connecting":
            state_color = DIM
            state_text = "state: connecting (waking the server, ~30-60s)..."
        elif state == "open":
            state_color = ACCENT
            state_text = "state: open"
        else:
            state_color = (220, 120, 120)
            state_text = f"state: {state} (retrying...)"

        if last_pong_at is None:
            pong_text = "last pong: (none yet)"
        else:
            pong_text = f"last pong: {now - last_pong_at:0.1f}s ago"

        lines = [
            (state_text, state_color),
            (f"server: {url}", DIM),
            (f"pings sent: {pings_sent}", FG),
            (f"pongs recv: {pongs_recv}", FG),
            (pong_text, FG),
            (f"prompt() name: {name or '(unset)'}", FG),
        ]
        _draw(surface, font, big, lines)
        pygame.display.flip()

        await asyncio.sleep(0)  # yield to the browser event loop every frame

    pygame.quit()


if __name__ == "__main__":
    asyncio.run(main())
