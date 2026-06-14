"""Pluggable minigame modules.

Each game keeps its rules as a plain, asyncio-free object (mirroring
:mod:`server.rooms`) so the logic is unit-testable; the connection layer owns the
sockets and any timers. ``start_game`` in :mod:`server.connection` is the seam
where a module from here plugs in.
"""
