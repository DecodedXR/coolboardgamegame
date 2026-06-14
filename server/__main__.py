"""Server entrypoint:  ``python -m server``

Binds a websockets endpoint and hands every connection to :class:`GameServer`.
``HOST``/``PORT`` come from the environment (see :mod:`config`) so the identical
command runs on LAN and on a cloud host that injects ``$PORT``.
"""

from __future__ import annotations

import asyncio

import websockets

from config import SERVER_HOST, SERVER_PORT
from server.connection import GameServer


async def main() -> None:
    game = GameServer()

    async def handler(conn):  # websockets passes the connection object
        await game.serve_connection(conn)

    async with websockets.serve(handler, SERVER_HOST, SERVER_PORT):
        print(f"gameshow server listening on ws://{SERVER_HOST}:{SERVER_PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nserver stopped")
