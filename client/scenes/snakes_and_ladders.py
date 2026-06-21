"""Snakes & Ladders — the in-game scene.

This is the *orchestrator*: it composes the standalone components built in the
earlier chunks (``board_render``, ``token_anim``, ``wheel``, ``shop_ui``,
``cutscene``, ``sfx``) into the playable board, driven entirely by the per-player
``game_state`` snapshots the server broadcasts (stored on ``app.gamestate``).

The server is authoritative: it resolves each turn into an ordered, serializable
``last_turn`` timeline with a monotonic ``seq``, and this scene merely *replays*
it as animation (token hops, snake slides, dice/wheel spins, cutscenes, SFX). The
client never decides an outcome — every player action (roll, use a powerup, buy,
skip the shop, the host's force-advance) just forwards a message and waits for the
next broadcast. Input is **locked while an animation is playing** so a player can't
act on a board that hasn't finished settling.

Per the codebase convention, the *decision* logic (when ROLL is offered, which
item buttons to show, where the animating token sits, the deadline countdown) is
factored into pure module-level helpers that are unit-tested headless; only
:meth:`SnakesAndLaddersScene.draw` needs a real Surface/font and is covered by the
manual desktop smoke.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

import pygame

from client import ui
from client import board_render
from client.board_render import BoardLayout
from client.cutscene import Cutscene, event_text, turn_text
from client.scenes.base import Scene
from client.sfx import Sfx
from client.shop_ui import ShopUI
from client.token_anim import TokenAnimator
from client.wheel import Wheel
from shared import protocol

_MARGIN = 24

# ``awaiting`` sub-state values within PHASE_PLAY (the wire contract; mirrors the
# server's ``AWAIT_ROLL`` / ``AWAIT_SHOP`` in ``server/games/snakes_and_ladders.py``).
AWAIT_ROLL = "roll"
AWAIT_SHOP = "shop"

# Vertical budget (portrait 480x800): header, then a square board, a thin legend
# strip, and a controls band — all comfortably under 800 (see the plan's A6).
_BOARD_TOP = 124
_LEGEND_H = 38


# --- pure decision helpers (unit-tested headless) -------------------------

def is_runner(room: dict[str, Any], my_id: Optional[str], role: str) -> bool:
    """Whether *we* are the show-runner who may force-advance / end the game: the
    human host in human-host mode, else the room owner in auto mode."""
    if room.get("host_mode") == protocol.HOST_HUMAN:
        return role == "host"
    return my_id is not None and my_id == room.get("owner_id")


def can_roll(gs: dict[str, Any], animating: bool) -> bool:
    """ROLL is offered only on our own roll-turn while nothing is animating."""
    return (
        bool(gs.get("your_turn"))
        and gs.get("phase") == protocol.PHASE_PLAY
        and gs.get("awaiting") == AWAIT_ROLL
        and not animating
    )


def my_player(gs: dict[str, Any]) -> dict[str, Any]:
    """Our own player row from the broadcast, or ``{}`` if we're a spectator/host."""
    me = gs.get("your_id")
    for p in gs.get("players", []):
        if p.get("id") == me:
            return p
    return {}


def usable_items(gs: dict[str, Any]) -> list[str]:
    """The held powerups we may arm right now: only pre-roll on our own turn. The
    server removes an item from our hand the instant it's armed, so the held list
    *is* the usable list."""
    if not (gs.get("your_turn") and gs.get("awaiting") == AWAIT_ROLL):
        return []
    return list(my_player(gs).get("items", []))


def countdown_seconds(deadline: Optional[float], now: float) -> Optional[int]:
    """Whole seconds left on the turn deadline (auto mode), or ``None`` when no
    deadline is set (bot turn / human-host park). Floors at ``0`` and never goes
    negative. The caller formats the label and picks its colour from the number, so
    nothing has to re-parse a formatted string."""
    if not deadline:
        return None
    return max(0, math.ceil(deadline - now))


def animating_override(
    animator: TokenAnimator, layout: BoardLayout
) -> Optional[tuple[str, tuple[int, int]]]:
    """Where to draw the *animating* token this frame as ``(mover_pid, (x, y))``,
    or ``None`` when nothing is animating. During a move/slide segment the token
    is lerped between the two cells' pixel centers; during a pause beat it rests at
    the animator's ``anchor_cell`` (NOT ``players[*].pos``, which the server has
    already advanced to the turn's final cell)."""
    if not animator.is_playing:
        return None
    mover = animator.mover
    if mover is None:
        return None
    prog = animator.progress()
    if prog is not None:
        frm, to, frac = prog
        fx, fy = layout.cell_to_xy(frm)
        tx, ty = layout.cell_to_xy(to)
        return mover, (round(fx + (tx - fx) * frac), round(fy + (ty - fy) * frac))
    anchor = animator.anchor_cell
    if anchor is None:
        return None
    return mover, layout.cell_to_xy(anchor)


# --- the scene ------------------------------------------------------------

class SnakesAndLaddersScene(Scene):
    def on_enter(self) -> None:
        w, h = self.app.width, self.app.height
        # Audio + the animation/overlay components. The Sfx mixer stays down until
        # the first click (browser autoplay rules); the animator fires cues through
        # it as the timeline replays.
        self.sfx = Sfx()
        self.animator = TokenAnimator(self.sfx)
        self.wheel = Wheel(duration=TokenAnimator.WHEEL_SECONDS)
        self.cutscene = Cutscene()
        self.shop = ShopUI(self._buy, self._skip_shop)

        # Static board (cells/cols/snakes/ladders/tile sets) is sent on every
        # broadcast but never changes mid-game, so cache the first one + its layout.
        self.board: Optional[dict[str, Any]] = None
        self.layout: Optional[BoardLayout] = None

        # Cutscene/animation bookkeeping.
        self._last_current: Optional[str] = None
        self._wheel_step: Optional[dict[str, Any]] = None

        # Persistent controls. Item buttons are rebuilt per frame from the hand.
        self.roll_btn = ui.Button("ROLL", (w // 2 - 90, 648, 180, 54), self._roll)
        self.leave_btn = ui.Button("LEAVE", (_MARGIN, h - 64, 120, 44), self._leave)
        self.next_btn = ui.Button("NEXT", (w - _MARGIN - 150, h - 64, 150, 44), self._advance)
        self.back_btn = ui.Button("BACK TO LOBBY", (w - _MARGIN - 220, h - 64, 220, 48),
                                   self._return_to_lobby)
        self._item_buttons: list[tuple[str, ui.Button]] = []

    # --- derived state ----------------------------------------------------

    @property
    def gs(self) -> dict[str, Any]:
        return self.app.gamestate or {}

    @property
    def my_id(self) -> Optional[str]:
        return (self.app.you or {}).get("id")

    @property
    def phase(self) -> str:
        return self.gs.get("phase", protocol.PHASE_PLAY)

    @property
    def role(self) -> str:
        return self.gs.get("you_role", "spectator")

    def _is_runner(self) -> bool:
        return is_runner(self.app.room or {}, self.my_id, self.role)

    def _name_of(self, pid: Optional[str]) -> str:
        for p in self.gs.get("players", []):
            if p.get("id") == pid:
                return p.get("name", "")
        return ""

    # --- actions (forward-only; the server decides everything) ------------

    def _roll(self) -> None:
        if can_roll(self.gs, self.animator.is_playing):
            self.app.net.send(protocol.C_ROLL_DICE)

    def _use(self, item: str) -> None:
        self.app.net.send(protocol.C_USE_POWERUP, item=item)

    def _buy(self, item: str) -> None:
        self.app.net.send(protocol.C_BUY_ITEM, item=item)

    def _skip_shop(self) -> None:
        self.app.net.send(protocol.C_SKIP_SHOP)

    def _advance(self) -> None:
        self.app.net.send(protocol.C_ADVANCE_PHASE)

    def _return_to_lobby(self) -> None:
        self.app.net.send(protocol.C_RETURN_TO_LOBBY)

    def _leave(self) -> None:
        self.app.net.send(protocol.C_LEAVE_ROOM)
        self.app.you = None
        self.app.room = None
        self.app.gamestate = None
        self.animator.reset()
        from client.scenes.menu import MenuScene
        self.app.go_to(MenuScene(self.app))

    # --- messages ---------------------------------------------------------

    def on_message(self, msg: dict[str, Any]) -> None:
        t = msg["type"]
        if t == protocol.S_GAME_STATE:
            self._ingest_state(msg["game"])
        elif t == protocol.S_ROOM_UPDATE:
            self.app.room = msg["room"]
        elif t == protocol.S_RETURN_TO_LOBBY:
            self.app.gamestate = None
            self.animator.reset()
            from client.scenes.lobby import LobbyScene
            self.app.go_to(LobbyScene(self.app))

    def _ingest_state(self, game: dict[str, Any]) -> None:
        self.app.gamestate = game
        if self.board is None and game.get("board"):
            self.board = game["board"]
            self._build_layout()

        # A new turn (seq bump) starts the replay; the animator ignores a re-fed
        # same-seq snapshot, so this is safe to call on every broadcast.
        started = self.animator.begin(game.get("last_turn"))
        event_banner = False
        if started:
            ev = event_text(game.get("last_turn"))
            if ev:
                text, kind = ev
                if kind == "win":
                    self.cutscene.show_persistent(text, kind="win")
                else:
                    self.cutscene.show(text, kind=kind)
                event_banner = True

        # Announce the new actor when the authoritative turn passes — but never
        # clobber a notable banner just raised *this* ingest. A skip almost always
        # advances current_pid, so without this guard the "Bob skipped!" banner
        # would be instantly overwritten by "Carol's turn" and never seen; a win
        # banner must persist for the same reason.
        cur = game.get("current_pid")
        if cur != self._last_current:
            self._last_current = cur
            if cur is not None and not game.get("winner") and not event_banner:
                self.cutscene.show(turn_text(self._name_of(cur)))

        # Private shop stock is present only while it's our shop sub-state.
        self.shop.set_stock((game.get("shop") or {}).get("stock"))

    def _build_layout(self) -> None:
        size = self.app.width - _MARGIN * 2
        self.layout = BoardLayout(
            int(self.board["cells"]), int(self.board["cols"]),
            (_MARGIN, _BOARD_TOP, size, size),
        )

    # --- per-frame update -------------------------------------------------

    def update(self, dt: float) -> None:
        self.animator.update(dt)
        self.wheel.update(dt)
        self.cutscene.update(dt)

        # Hand the wheel its spin when the animator reaches a wheel beat, and let it
        # go once that beat passes. ``animator.wheel`` is the same dict instance for
        # the whole beat, so identity tells us when a *new* wheel step begins.
        wstep = self.animator.wheel
        if wstep is not None and wstep is not self._wheel_step:
            self._wheel_step = wstep
            self.wheel.begin(wstep)
        elif wstep is None and self._wheel_step is not None:
            self._wheel_step = None
            self.wheel.reset()

        self._sync_item_buttons()

    def _sync_item_buttons(self) -> None:
        """Rebuild one small button per held powerup, laid out as a row above ROLL.
        Hidden (empty) whenever it isn't our pre-roll turn."""
        items = usable_items(self.gs) if not self.animator.is_playing else []
        if [i for i, _ in self._item_buttons] == items:
            return
        self._item_buttons = []
        bw, gap = 104, 8
        total = len(items) * bw + (len(items) - 1) * gap if items else 0
        x = (self.app.width - total) // 2
        for item in items:
            from client.shop_ui import item_label
            self._item_buttons.append(
                (item, ui.Button(item_label(item), (x, 596, bw, 40),
                                 lambda it=item: self._use(it)))
            )
            x += bw + gap

    # --- input ------------------------------------------------------------

    def handle_event(self, event: pygame.event.Event) -> None:
        # The first click is the browser gesture that lets the mixer come up.
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.sfx.init()

        self.leave_btn.handle(event)

        # Lock every control (the runner's NEXT/BACK included) while the board is
        # still settling, so nobody acts on — or tears down, mid-win-animation —
        # a board that hasn't finished replaying. Only LEAVE (handled above) stays
        # live, since a player must always be able to quit.
        if self.animator.is_playing:
            return

        if self.phase == protocol.PHASE_GAMEOVER:
            if self._is_runner():
                self.back_btn.handle(event)
            return

        if self._is_runner():
            self.next_btn.handle(event)

        gs = self.gs
        if gs.get("your_turn"):
            if gs.get("awaiting") == AWAIT_SHOP:
                self.shop.handle(event, self._shop_panel())
            elif gs.get("awaiting") == AWAIT_ROLL:
                self.roll_btn.handle(event)
                for _item, btn in self._item_buttons:
                    btn.handle(event)

    # --- drawing (needs a real Surface/font -> covered by the desktop smoke) --

    def _shop_panel(self) -> tuple[int, int, int, int]:
        size = self.app.width - _MARGIN * 2
        return (_MARGIN, _BOARD_TOP, size, size)

    def draw(self, surf: pygame.Surface) -> None:  # pragma: no cover - desktop smoke
        surf.fill(ui.BG)
        self._draw_header(surf)
        if self.layout is not None and self.board is not None:
            board_render.draw_board(surf, self.layout, self.board)
            override = animating_override(self.animator, self.layout)
            overrides = {override[0]: override[1]} if override else None
            board_render.draw_tokens(surf, self.layout, self.gs.get("players", []), overrides)
            self._draw_legend(surf)
            # Wheel spin overlays the board center.
            if self.wheel.is_spinning:
                size = self.app.width - _MARGIN * 2
                self.wheel.draw(surf, (self.app.width // 2, _BOARD_TOP + size // 2), 130)
        self._draw_hud(surf)
        # Shop overlay (current player's private stock) sits over the board.
        if self.gs.get("your_turn") and self.gs.get("awaiting") == AWAIT_SHOP \
                and not self.animator.is_playing:
            self.shop.draw(surf, self._shop_panel())
        # The between-turns / win cutscene banner.
        if self.cutscene.is_active:
            self.cutscene.draw(surf, (_MARGIN, _BOARD_TOP, self.app.width - _MARGIN * 2,
                                      self.app.width - _MARGIN * 2))
        self._draw_controls(surf)

    def _draw_header(self, surf: pygame.Surface) -> None:  # pragma: no cover
        ui.Label("SNAKES & LADDERS", (_MARGIN, 24), 24, ui.ACCENT).draw(surf)
        gs = self.gs
        cur = gs.get("current_pid")
        if gs.get("winner"):
            sub = f"{gs['winner'].get('name', '?')} wins!"
        elif gs.get("your_turn"):
            sub = "your turn" + ("  ·  shopping" if gs.get("awaiting") == AWAIT_SHOP else "")
        else:
            sub = f"{self._name_of(cur)}'s turn" if cur else "..."
        ui.Label(sub, (_MARGIN, 60), 18, ui.MUTED).draw(surf)
        remaining = countdown_seconds(gs.get("deadline"), time.time())
        if remaining is not None:
            ui.Label(f"{remaining}s", (self.app.width - _MARGIN - 4, 24), 28,
                     ui.GOOD if remaining > 5 else ui.ACCENT).draw(surf)
        me = my_player(gs)
        if me:
            ui.Label(f"gold: {me.get('gold', 0)}", (_MARGIN, 90), 16, ui.GOOD).draw(surf)

    def _draw_legend(self, surf: pygame.Surface) -> None:  # pragma: no cover
        size = self.app.width - _MARGIN * 2
        rect = pygame.Rect(_MARGIN, _BOARD_TOP + size + 6, size, _LEGEND_H)
        board_render.draw_legend(surf, rect)

    def _draw_hud(self, surf: pygame.Surface) -> None:  # pragma: no cover
        # Usable powerup buttons (pre-roll, our turn).
        for _item, btn in self._item_buttons:
            btn.draw(surf)
        if can_roll(self.gs, self.animator.is_playing):
            self.roll_btn.draw(surf)

    def _draw_controls(self, surf: pygame.Surface) -> None:  # pragma: no cover
        self.leave_btn.draw(surf)
        if self.phase == protocol.PHASE_GAMEOVER:
            if self._is_runner():
                self.back_btn.draw(surf)
            else:
                ui.Label("waiting for the host to end the game...",
                         (self.app.width // 2, self.app.height - 44), 15, ui.MUTED,
                         center=True).draw(surf)
        elif self._is_runner():
            self.next_btn.draw(surf)
