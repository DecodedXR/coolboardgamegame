"""Server entrypoint:  ``python -m server``

Binds a websockets endpoint and hands every connection to :class:`GameServer`.
``HOST``/``PORT`` come from the environment (see :mod:`config`) so the identical
command runs on LAN and on a cloud host that injects ``$PORT``.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus

import websockets

from config import SERVER_HOST, SERVER_PORT
from server.connection import GameServer


def health_check(connection, request):
    """``process_request`` hook: answer plain HTTP probes with 200.

    A raw WebSocket server replies ``426 Upgrade Required`` to any non-upgrade
    request, which managed hosts (Render) and keep-warm pingers read as
    *unhealthy*. Genuine clients send ``Upgrade: websocket`` (the upgrade lands on
    path ``/``, so we key off the header, not the path) — those return ``None`` so
    the normal handshake proceeds.
    """
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None  # real client → proceed with the WS handshake
    return connection.respond(HTTPStatus.OK, "ok\n")


async def main() -> None:
    # Warm the word-bomb dictionary's lru_cache before serving: the substring scan
    # takes a couple of seconds, and running it lazily inside the first start_game
    # handler would block the event loop mid-await (freezing every room). At boot,
    # nothing is connected yet, so blocking here is free.
    from config import WB_MIN_WORDS_PER_PROMPT
    from server.games.word_bomb import load_dictionary
    words, prompts, _, _ = load_dictionary(WB_MIN_WORDS_PER_PROMPT)
    print(f"word bomb dictionary ready: {len(words)} words, {len(prompts)} prompts")

    game = GameServer()

    async def handler(conn):  # websockets passes the connection object
        await game.serve_connection(conn)

    async with websockets.serve(
        handler, SERVER_HOST, SERVER_PORT, process_request=health_check
    ):
        print(f"gameshow server listening on ws://{SERVER_HOST}:{SERVER_PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nserver stopped")
