"""pygbag / browser entry point. Boots the same App as ``python -m client``.

pygbag requires the build's entry file to be a root ``main.py`` exposing an async
``main()`` ending in ``asyncio.run(main())`` (its patched asyncio drives the
browser loop). Desktop users can still run ``python main.py`` or
``python -m client`` — both boot the identical async App loop.
"""

from __future__ import annotations

import asyncio

# pygbag decides which WASM packages to bundle by scanning THIS entry file's
# imports. pygame is otherwise only imported deep inside client/, which the
# scanner never sees, so without this line pygbag ships no pygame-ce and the app
# dies on the first `pygame.init()` with "module 'pygame' has no attribute 'init'".
# Desktop ignores the unused import.
import pygame  # noqa: F401

from client.__main__ import App


async def main() -> None:
    await App().run_async()


if __name__ == "__main__":
    asyncio.run(main())
