"""Guards the Tier 4 W2 async-loop conversion.

pygbag can't run a blocking ``while running:`` loop, so the App loop must be a
coroutine and the build entry must be a root ``main.py`` exposing async ``main``.
These checks are shape-only (no display needed): importing the modules merely
*defines* ``App`` — the pygame window is created in ``App.__init__``, not at
import — so they run headless in CI.
"""

from __future__ import annotations

import inspect

import main as root_main
from client.__main__ import App


def test_app_loop_is_coroutine() -> None:
    assert inspect.iscoroutinefunction(App.run_async)


def test_root_entry_main_is_coroutine() -> None:
    assert inspect.iscoroutinefunction(root_main.main)


def test_blocking_run_is_gone() -> None:
    # A future edit must not silently reintroduce a sync loop pygbag can't drive.
    assert not hasattr(App, "run")
