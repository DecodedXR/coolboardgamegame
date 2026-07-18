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
from typing import Any, Callable, Optional

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

# ``awaiting`` sub-state values within PHASE_PLAY. Single-sourced in the protocol
# (shared with the server authority) and aliased here so the scene reads tersely.
AWAIT_ROLL = protocol.AWAIT_ROLL
AWAIT_SHOP = protocol.AWAIT_SHOP

# Vertical budget (portrait 480x800): header, then a square board, a thin legend
# strip, and a controls band — all comfortably under 800 (see the plan's A6).
_BOARD_TOP = 124
_LEGEND_H = 38

# Powerup-row layout: the pre-roll item buttons are fixed-size and wrap into
# centered rows that stack UPWARD from a bottom anchor (just below the legend and
# above ROLL). A common <=4-item hand is a single row at the anchor; larger hands
# add rows above it so the row never overflows the 480px canvas. The anchor is the
# long-standing single-row position; collision-safety with ROLL comes from growing
# upward (the bottom row never moves down), not from coupling to ROLL's y.
_ITEM_BTN_W = 104
_ITEM_BTN_H = 40
_ITEM_GAP = 8
_ITEM_ROW_BOTTOM_Y = 596


# --- pure decision helpers (unit-tested headless) -------------------------

def is_runner(room: dict[str, Any], my_id: Optional[str]) -> bool:
    """Whether *we* are the show-runner who may force-advance / end the game: the
    room owner."""
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


def header_subtitle(gs: dict[str, Any], name_of: Callable[[Optional[str]], str]) -> str:
    """The header's one-line status under the title, in priority order: winner >
    your-turn (+ shopping) > whose-turn > idle. Pure so every branch is testable
    without the scene; ``name_of`` resolves a pid to a display name (only the
    whose-turn branch needs it, so the helper stays display-only) and is invoked
    lazily — exactly as the original draw code did — so a missing ``current_pid``
    short-circuits to ``"..."`` without ever calling it."""
    cur = gs.get("current_pid")
    if gs.get("winner"):
        return f"{gs['winner'].get('name', '?')} wins!"
    if gs.get("your_turn"):
        return "your turn" + ("  ·  shopping" if gs.get("awaiting") == AWAIT_SHOP else "")
    return f"{name_of(cur)}'s turn" if cur else "..."


# --- the scene ------------------------------------------------------------

class SnakesAndLaddersScene(Scene):
    def on_enter(self) -> None:
        w, h = self.app.width, self.app.height
        # Audio + the animation/overlay components. init() now: by scene time the
        # player has clicked through the menus (autoplay unlocked), and spectators
        # may never click here; per-event init() calls below remain as retries.
        self.sfx = Sfx()
        self.sfx.init()
        self.animator = TokenAnimator(self.sfx)
        self.wheel = Wheel()
        self.cutscene = Cutscene()
        self.shop = ShopUI(self._buy, self._skip_shop)

        # Static board (cells/cols/snakes/ladders/tile sets) is sent on every
        # broadcast but never changes mid-game, so cache the first one + its layout.
        self.board: Optional[dict[str, Any]] = None
        self.layout: Optional[BoardLayout] = None
        # The static board (grid/numbers/glyphs/links/legend) is rasterized ONCE to
        # this surface and blitted each frame; re-rendering ~100 cell numbers per
        # frame is invisible on desktop but freezes the tab under single-threaded
        # WASM. Built lazily on first draw, so it stays off the headless test path.
        self._board_surf: Optional[pygame.Surface] = None

        # Cutscene/animation bookkeeping.
        self._last_current: Optional[str] = None
        # Turn replays waiting their turn. A fresh broadcast must never clobber an
        # in-flight replay (bots roll faster than a turn animates), so new turns
        # queue here and begin only once the animator is idle (see _drain_pending).
        # _seen_seq is the latest turn seq already queued, so a re-fed snapshot is
        # not enqueued twice; any *different* seq (including a new game's reset to 1)
        # does enqueue, mirroring the animator's own seq gate.
        self._pending: list[dict[str, Any]] = []
        self._seen_seq = 0

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
    def _busy(self) -> bool:
        """The board is settling: a replay is in-flight OR a just-arrived turn is
        queued to start. ROLL and the other controls stay locked until this clears
        (the queue can be non-empty for a frame before update() drains it), so no
        one acts on a board that is about to move."""
        return self.animator.is_playing or bool(self._pending)

    @property
    def my_id(self) -> Optional[str]:
        return (self.app.you or {}).get("id")

    @property
    def phase(self) -> str:
        return self.gs.get("phase", protocol.PHASE_PLAY)

    def _is_runner(self) -> bool:
        return is_runner(self.app.room or {}, self.my_id)

    def _name_of(self, pid: Optional[str]) -> str:
        for p in self.gs.get("players", []):
            if p.get("id") == pid:
                return p.get("name", "")
        return ""

    # --- actions (forward-only; the server decides everything) ------------

    def _roll(self) -> None:
        if can_roll(self.gs, self._busy):
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
        self._reset_animation()
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
            self._reset_animation()
            from client.scenes.lobby import LobbyScene
            self.app.go_to(LobbyScene(self.app))

    def _ingest_state(self, game: dict[str, Any]) -> None:
        self.app.gamestate = game
        if self.board is None and game.get("board"):
            self.board = game["board"]
            self._build_layout()

        # Queue a genuinely-new turn instead of replaying it now: a fresh broadcast
        # must never clobber an in-flight replay (bots roll ~1.2s apart but a turn
        # can animate for ~3s). _drain_pending starts the next queued turn the moment
        # the animator is idle. current_pid/winner ride along so that turn's
        # post-turn banner ("X's turn" / skip / win) fires at its START, tracking the
        # animation instead of jumping ahead to the latest snapshot.
        lt = game.get("last_turn")
        if lt is not None and lt.get("seq", 0) != self._seen_seq:
            self._seen_seq = lt.get("seq", 0)
            self._pending.append({
                "last_turn": lt,
                "current_pid": game.get("current_pid"),
                "winner": bool(game.get("winner")),
            })
        self._drain_pending()

        # Nothing to replay (game start, or a re-fed snapshot): announce the actor
        # right away so the very first "X's turn" still shows. While a replay is
        # queued or playing, _busy is set and the announce defers to the turn START
        # in _drain_pending — which ran just above, so for a zero-segment turn (begin
        # returned but is_playing is already False) _last_current is already set and
        # this fallback is a de-duped no-op, not a double banner.
        if not self._busy:
            self._announce(game.get("current_pid"), bool(game.get("winner")), None)

        # Private shop stock is present only while it's our shop sub-state.
        self.shop.set_stock((game.get("shop") or {}).get("stock"))

    def _reset_animation(self) -> None:
        """Stop the current replay and drop any queued turns — used when leaving the
        scene so a stale backlog can't restart an animation on a torn-down board. The
        _pending queue is scene state paralleling the animator's in-flight state, so
        the two are cleared together; the wheel overlay is downstream of the animator,
        so it clears with it."""
        self.animator.reset()
        self.wheel.reset()
        self._pending.clear()

    def _drain_pending(self) -> None:
        """Start the next queued turn once the animator goes idle. If more than one
        is waiting we have fallen behind (a turn animates slower than bots roll), so
        snap past the stale ones and replay only the newest: every non-animating
        token already sits at its authoritative position, so only the unseen replays
        are dropped, never game state."""
        if self.animator.is_playing or not self._pending:
            return
        record = self._pending[-1]
        self._pending.clear()
        self.animator.begin(record["last_turn"])
        self._announce(record["current_pid"], record["winner"], record["last_turn"])

    def _announce(self, current_pid: Optional[str], winner: bool,
                  last_turn: Optional[dict[str, Any]]) -> None:
        """Raise the turn-START banner: a notable event from the just-finished turn
        (a win persists; a skip names the skipped player) outranks the routine
        "X's turn", so a "Bob skipped!" / win banner is never instantly clobbered by
        "Carol's turn". The actor announce is de-duped on _last_current so a re-fed
        current_pid does not re-banner."""
        event_banner = False
        if last_turn is not None:
            ev = event_text(last_turn)
            if ev:
                text, kind = ev
                if kind == "win":
                    self.cutscene.show_persistent(text, kind="win")
                else:
                    self.cutscene.show(text, kind=kind)
                event_banner = True
        if current_pid != self._last_current:
            self._last_current = current_pid
            if current_pid is not None and not winner and not event_banner:
                self.cutscene.show(turn_text(self._name_of(current_pid)))

    def _build_layout(self) -> None:
        size = self.app.width - _MARGIN * 2
        self.layout = BoardLayout(
            int(self.board["cells"]), int(self.board["cols"]),
            (_MARGIN, _BOARD_TOP, size, size),
        )

    def _legend_rect(self) -> pygame.Rect:
        size = self.app.width - _MARGIN * 2
        return pygame.Rect(_MARGIN, _BOARD_TOP + size + 6, size, _LEGEND_H)

    def _ensure_board_surface(self) -> None:
        """Rasterize the static board (grid/numbers/glyphs/links/legend) once and
        cache it; the board never changes mid-game, so redoing it every frame is
        wasted work that freezes the tab under WASM — see ``_board_surf``. Lazy
        (first draw) to stay off the headless test path, which never draws."""
        if self._board_surf is None and self.layout is not None and self.board is not None:
            self._board_surf = board_render.render_static(
                self.layout, self.board,
                (self.app.width, self.app.height), self._legend_rect())

    # --- per-frame update -------------------------------------------------

    def update(self, dt: float) -> None:
        # Drip-feed audio synthesis (one cue/frame) so the mixer warm-up never
        # blocks a frame — critical under single-threaded WASM, where building all
        # cues at once froze the tab right after the first click. No-op until the
        # first click brings the mixer up, and once all cues are cached.
        self.sfx.pump()
        # Start the next queued turn the instant the animator frees up, so a backlog
        # of bot turns replays in order (snapping past stale ones when behind).
        self._drain_pending()
        self.animator.update(dt)
        self.cutscene.update(dt)

        # Drive the wheel straight from the animator's wheel-beat progress, so the
        # spin tracks the authoritative timeline: a long/stalled frame advances it to
        # its true position instead of a parallel widget clock flashing the un-spun
        # wheel for a frame and vanishing.
        wprog = self.animator.wheel_progress
        if wprog is not None:
            self.wheel.drive(self.animator.wheel, wprog)
        elif self.wheel.is_visible:
            self.wheel.reset()

        self._sync_item_buttons()

    def _sync_item_buttons(self) -> None:
        """Rebuild one small button per held powerup, wrapped into centered rows
        above ROLL. Hidden (empty) whenever it isn't our pre-roll turn.

        A single centered row of 5+ powerups overflows the 480px canvas off both
        edges (``5*104 + 4*8 = 552 > 480`` -> first/last buttons clipped), and the
        server hands out powerups without a cap, so the row WRAPS: at most
        ``per_row`` fixed-size buttons sit on one row and the rest stack onto
        further rows growing UPWARD from the bottom anchor (the <=4-item case is
        unchanged; downward would collide with ROLL). Each row is centered
        independently, so every button stays fully on-canvas."""
        items = usable_items(self.gs) if not self._busy else []
        if [i for i, _ in self._item_buttons] == items:
            return
        self._item_buttons = []
        if not items:
            return
        from client.shop_ui import item_label
        bw, bh, gap = _ITEM_BTN_W, _ITEM_BTN_H, _ITEM_GAP
        # Most fixed-size buttons that fit one centered row (4 at 480px); the rest
        # wrap onto further rows stacked upward. Build rows top-to-bottom so the
        # flat button list stays in held-item order.
        per_row = max(1, (self.app.width + gap) // (bw + gap))
        rows = [items[i:i + per_row] for i in range(0, len(items), per_row)]
        top_y = _ITEM_ROW_BOTTOM_Y - (len(rows) - 1) * (bh + gap)
        for r, row_items in enumerate(rows):
            y = top_y + r * (bh + gap)
            row_w = len(row_items) * bw + (len(row_items) - 1) * gap
            x = (self.app.width - row_w) // 2
            for item in row_items:
                self._item_buttons.append(
                    (item, ui.Button(item_label(item), (x, y, bw, bh),
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
        # still settling — animating OR a just-arrived turn still queued — so nobody
        # acts on (or tears down, mid-win-animation) a board that hasn't finished
        # replaying. Only LEAVE (handled above) stays live, since a player must
        # always be able to quit.
        if self._busy:
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
            self._ensure_board_surface()
            if self._board_surf is not None:
                surf.blit(self._board_surf, (0, 0))  # static grid + legend, drawn once
            override = animating_override(self.animator, self.layout)
            overrides = {override[0]: override[1]} if override else None
            board_render.draw_tokens(surf, self.layout, self.gs.get("players", []), overrides)
            # Wheel spin overlays the board center.
            if self.wheel.is_visible:
                size = self.app.width - _MARGIN * 2
                self.wheel.draw(surf, (self.app.width // 2, _BOARD_TOP + size // 2), 130)
        self._draw_hud(surf)
        # Shop overlay (current player's private stock) sits over the board.
        if self.gs.get("your_turn") and self.gs.get("awaiting") == AWAIT_SHOP \
                and not self._busy:
            self.shop.draw(surf, self._shop_panel())
        # The between-turns / win cutscene banner.
        if self.cutscene.is_active:
            self.cutscene.draw(surf, (_MARGIN, _BOARD_TOP, self.app.width - _MARGIN * 2,
                                      self.app.width - _MARGIN * 2))
        self._draw_controls(surf)

    def _draw_header(self, surf: pygame.Surface) -> None:  # pragma: no cover
        ui.Label("SNAKES & LADDERS", (_MARGIN, 24), 24, ui.ACCENT).draw(surf)
        gs = self.gs
        sub = header_subtitle(gs, self._name_of)
        ui.Label(sub, (_MARGIN, 60), 18, ui.MUTED).draw(surf)
        remaining = countdown_seconds(gs.get("deadline"), time.time())
        if remaining is not None:
            txt = f"{remaining}s"
            tw = ui.get_font(28).size(txt)[0]  # right-align so multi-digit values don't clip the edge
            ui.Label(txt, (self.app.width - _MARGIN - tw, 24), 28,
                     ui.GOOD if remaining > 5 else ui.ACCENT).draw(surf)
        me = my_player(gs)
        if me:
            ui.Label(f"gold: {me.get('gold', 0)}", (_MARGIN, 90), 16, ui.GOOD).draw(surf)

    def _draw_hud(self, surf: pygame.Surface) -> None:  # pragma: no cover
        # Usable powerup buttons (pre-roll, our turn).
        for _item, btn in self._item_buttons:
            btn.draw(surf)
        if can_roll(self.gs, self._busy):
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
