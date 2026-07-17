"""Word Bomb — the in-game scene.

A lit cartoon bomb center-stage: the prompt letters are riveted onto it as tiles,
a burning fuse's spark crawls toward the bomb in real time, and the players sit in
a ring around it like a table. The whole scene "heats up" (color, pulse, tick
rate) as the fuse runs down; explosions shake and flash. Everything is drawn with
pygame primitives + the bundled mono font — no image assets, all text ASCII.

The server is authoritative (it stamps the deadline and resolves each turn); this
scene stores no simulation of its own beyond decaying FX timers and particles, so
all motion is derived per-frame from ``time.time()`` and the authoritative
``deadline``/feed. Feed events carry a monotonic ``id`` and the scene dedups on it
(never ``seq`` — rejects deliberately share a ``seq``), so a re-broadcast of the
same state stays silent and still.

The *decision* logic (pressure curve, heat ramp, fuse/seat geometry, feed text) is
factored into pure module-level helpers that are unit-tested headless; only
:meth:`WordBombScene.draw` needs a real Surface/font.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

import pygame

from client import ui
from client import browser_io
from client.scenes.base import Scene
from client.sfx import Sfx
from shared import protocol

CENTER = (240, 300)          # bomb center on the 480x800 canvas
BOMB_R = 78                  # bomb body radius
RING_RX, RING_RY = 165, 145  # player-seat ellipse radii
HOT = (255, 120, 60)         # the "about to blow" end of the heat ramp
SPARK = (255, 220, 120)
FLASH_TIME = 0.30            # seconds of explosion whiteout
SHAKE_TIME = 0.55            # seconds of screen shake

_PARTICLE_CAP = 120


# --- pure design helpers (unit-tested headless) ---------------------------

def _clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def _lerp(c1, c2, t):
    """Per-channel int lerp between two colors, ``t`` clamped 0..1."""
    t = _clamp01(t)
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def press_of(remaining: Optional[float], total: Optional[float]) -> float:
    """0.0 (calm) .. 1.0 (about to explode). A ``None`` deadline (human-host mode,
    no fuse) reads as an idle simmer of 0.25."""
    if remaining is None or not total:
        return 0.25
    return _clamp01(1 - remaining / total)


def heat_color(press: float):
    """``ui.MUTED`` -> ``ui.ACCENT`` over press 0..0.6, then ``ui.ACCENT`` -> ``HOT``
    over 0.6..1 — the room glows hotter as the fuse shortens."""
    if press <= 0.6:
        return _lerp(ui.MUTED, ui.ACCENT, press / 0.6)
    return _lerp(ui.ACCENT, HOT, (press - 0.6) / 0.4)


def _bezier(p0, p1, p2, t):
    mt = 1 - t
    return (mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
            mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1])


# 24 points of a quadratic bezier from the bomb's shoulder to the fuse tip,
# computed once at import (never per frame).
_FUSE_POINTS = [_bezier((252, 232), (320, 150), (360, 130), i / 23) for i in range(24)]


def fuse_points():
    return _FUSE_POINTS


def seat_positions(n: int):
    """``n`` points on the seat ellipse (CENTER, RING_RX, RING_RY), seat 0 at the
    top (-90 deg), going clockwise. Used for 2..8 players."""
    cx, cy = CENTER
    pts = []
    for i in range(n):
        ang = -math.pi / 2 + (i / n) * math.tau
        pts.append((round(cx + RING_RX * math.cos(ang)),
                    round(cy + RING_RY * math.sin(ang))))
    return pts


def feed_line(event: dict) -> str:
    """One feed event as an ASCII status line."""
    kind = event.get("kind")
    name = event.get("name", "")
    if kind == "accept":
        return f"{name}: {event.get('word', '')}"
    if kind == "reject":
        prompt = (event.get("prompt") or "").upper()
        reason = {
            "not_a_word": "not a word",
            "not_in_prompt": f"doesn't contain {prompt}",
            "already_used": "already used",
        }.get(event.get("reason"), event.get("reason") or "")
        return f"{name}: {event.get('word', '')} x {reason}"
    if kind == "explode":
        return f"BOOM! {name} loses a life"
    if kind == "eliminated":
        return f"{name} is out"
    return ""


def fmt_options(n: int) -> str:
    """804 -> '804 words', 19983 -> '20k words'."""
    if n >= 10_000:
        return f"{round(n / 1000)}k words"
    return f"{n} words"


def tail_that_fits(text: str, max_w: int, measure) -> str:
    """Longest tail of ``text`` whose ``measure(tail)`` fits ``max_w`` — long
    words keep the end (where the typing happens) on screen."""
    while text and measure(text) > max_w:
        text = text[1:]
    return text


# --- the scene ------------------------------------------------------------

class WordBombScene(Scene):
    def on_enter(self) -> None:
        w, h = self.app.width, self.app.height
        self.sfx = Sfx()
        self.status = ""

        self.typed = ""          # my in-progress word (your-turn only)
        self.live_typing = ""    # the current player's in-progress word (theirs)

        # Input widgets, created ONCE (never per frame).
        self.detonate_btn = ui.Button("DETONATE", (24, 706, 208, 50), self._detonate)
        self.lobby_btn = ui.Button("LOBBY", (248, 706, 208, 50), self._return_to_lobby)

        # Per-frame FX state (decaying timers + particles). The scene simulates
        # nothing else — all steady motion is derived from time.time() + the server.
        self._shake = 0.0
        self._flash = 0.0
        self._cool = 0.0            # green "phew" rim flash after an accepted word
        self._input_flash = 0.0     # red rattle on YOUR rejected word
        self._particles: list[dict] = []
        self._seen_id = 0           # highest feed-event id already reacted to (dedup by id)
        self._turn_total: Optional[float] = None
        self._prev_current: Optional[str] = None
        self._last_tick: Optional[int] = None
        self._celebrated = False
        self._sudden_death = False

        # Two cached full-screen overlays, built ONCE (no per-frame Surface alloc).
        self._flash_surf = pygame.Surface((w, h), pygame.SRCALPHA)
        self._vignette = pygame.Surface((w, h), pygame.SRCALPHA)
        for i, alpha in enumerate((96, 72, 48, 24)):
            inset = i * 40
            pygame.draw.rect(self._vignette, (120, 20, 30, alpha),
                             pygame.Rect(inset, inset, w - 2 * inset, h - 2 * inset), width=40)

    # --- derived state ----------------------------------------------------

    @property
    def gs(self) -> dict[str, Any]:
        return self.app.gamestate or {}

    @property
    def my_id(self) -> Optional[str]:
        return (self.app.you or {}).get("id")

    def _is_runner(self) -> bool:
        room = self.app.room or {}
        if room.get("host_mode") == protocol.HOST_HUMAN:
            return self.gs.get("you_role") == "host"
        return self.my_id is not None and self.my_id == room.get("owner_id")

    def _name_of(self, pid: Optional[str]) -> str:
        for p in self.gs.get("players", []):
            if p.get("id") == pid:
                return p.get("name", "")
        return ""

    def _is_sudden_death(self, gs: dict[str, Any]) -> bool:
        alive = [p for p in gs.get("players", []) if p.get("alive")]
        return len(alive) == 2 and all(p.get("lives") == 1 for p in alive)

    # --- actions (forward-only) -------------------------------------------

    def _submit(self) -> None:
        if not self.typed:
            return
        self.app.net.send(protocol.C_SUBMIT_WORD, word=self.typed)
        self.typed = ""
        self.app.net.send(protocol.C_TYPING, text="")   # clears everyone's view

    def _detonate(self) -> None:
        self.app.net.send(protocol.C_ADVANCE_PHASE)

    def _return_to_lobby(self) -> None:
        self.app.net.send(protocol.C_RETURN_TO_LOBBY)

    # --- messages ---------------------------------------------------------

    def on_message(self, msg: dict[str, Any]) -> None:
        t = msg["type"]
        if t == protocol.S_GAME_STATE:
            self._ingest_state(msg["game"])
        elif t == protocol.S_ROOM_UPDATE:
            self.app.room = msg["room"]
        elif t == protocol.S_RETURN_TO_LOBBY:
            self.app.gamestate = None
            from client.scenes.lobby import LobbyScene
            self.app.go_to(LobbyScene(self.app))
        elif t == protocol.S_TYPING:
            if msg.get("pid") == self.gs.get("current_pid") and msg.get("pid") != self.my_id:
                self.live_typing = str(msg.get("text") or "")[:32]
        elif t == protocol.S_ERROR:
            self.status = msg.get("message", "error")

    def _ingest_state(self, state: dict[str, Any]) -> None:
        self.app.gamestate = state

        # Feed scan: react to every event newer than the last seen id (dedup by id,
        # NEVER by seq — two rejects in a turn share a seq but carry distinct ids).
        feed = state.get("feed", [])
        for ev in feed:
            if ev.get("id", 0) > self._seen_id:
                self._react_to_event(ev)
        if feed:
            self._seen_id = max(self._seen_id, max(ev.get("id", 0) for ev in feed))

        over = bool(state.get("winner"))

        # Bomb pass / turn boundary: guarded by a real current_pid CHANGE so a
        # re-broadcast of the same state is silent (the alarm/pass anti-spam rule).
        cur = state.get("current_pid")
        if cur != self._prev_current:
            if self._prev_current is not None and not over:
                self._spawn_pass_trail(self._prev_current, cur, state)
                self.sfx.play("pass")
                if cur == self.my_id:
                    self.sfx.play("alarm")
            if cur == self.my_id and not over:
                # Turn transition to me: clear so desktop can type at once.
                self.typed = ""
            self.live_typing = ""   # any bomb pass invalidates the old typer's text
            deadline = state.get("deadline")
            self._turn_total = max(0.001, deadline - time.time()) if deadline else None
            self._prev_current = cur

        # Sudden death: latch once when the 2-alive/1-life-each condition first holds.
        if self._is_sudden_death(state) and not self._sudden_death:
            self.sfx.play("sudden_death")
            self._sudden_death = True

        # Winner: fire the fanfare + confetti exactly once.
        if state.get("winner") and not self._celebrated:
            self.sfx.play("win")
            self._spawn_confetti()
            self._celebrated = True

    # --- FX spawning ------------------------------------------------------

    def _add_particle(self, pos, vel, life: float, color) -> None:
        self._particles.append({"pos": [pos[0], pos[1]], "vel": [vel[0], vel[1]],
                                "life": life, "max_life": life, "color": color})
        if len(self._particles) > _PARTICLE_CAP:   # drop the oldest first
            del self._particles[:len(self._particles) - _PARTICLE_CAP]

    def _spawn_explosion(self, count: int, angle_offset: float) -> None:
        colors = [(255, 180, 80), ui.ACCENT, ui.TEXT]
        for i in range(count):
            ang = i / 26 * math.tau + angle_offset     # even fan, no RNG
            speed = 120 + (i * 37 % 200)
            life = 0.5 + (i % 5) * 0.08
            self._add_particle(CENTER, (math.cos(ang) * speed, math.sin(ang) * speed),
                               life, colors[i % 3])

    def _spawn_pass_trail(self, from_pid, to_pid, state) -> None:
        players = state.get("players", [])
        seats = seat_positions(len(players))
        idx = {p.get("id"): i for i, p in enumerate(players)}
        if from_pid not in idx or to_pid not in idx:
            return
        fx, fy = seats[idx[from_pid]]
        tx, ty = seats[idx[to_pid]]
        for i in range(10):
            t = i / 9
            self._add_particle((fx + (tx - fx) * t, fy + (ty - fy) * t), (0, 0), 0.3, SPARK)

    def _spawn_confetti(self) -> None:
        colors = [ui.GOOD, ui.ACCENT, (255, 205, 90)]
        for i in range(60):
            self._add_particle(((i * 61) % 480, -10 - (i % 7) * 12),
                               (0, 60 + (i * 13) % 100), 1.5 + (i % 11) / 10, colors[i % 3])

    def _react_to_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "explode":
            self._shake = SHAKE_TIME
            self._flash = FLASH_TIME
            self._spawn_explosion(26, 0.0)
            self.sfx.play("boom")
        elif kind == "eliminated":
            self._shake = 0.8                          # a bigger blast
            self._spawn_explosion(14, math.tau / 52)
            self.sfx.play("dirge")                     # layers on top of the boom
        elif kind == "accept":
            self.sfx.play("type_ok")
            self._cool = 0.25                          # the green rim exhale
        elif kind == "reject":
            self.sfx.play("type_bad")
            if ev.get("pid") == self.my_id:
                self._input_flash = 0.3                # the rattle is personal

    # --- per-frame update -------------------------------------------------

    def update(self, dt: float) -> None:
        self._shake = max(0.0, self._shake - dt)
        self._flash = max(0.0, self._flash - dt)
        self._cool = max(0.0, self._cool - dt)
        self._input_flash = max(0.0, self._input_flash - dt)

        alive: list[dict] = []
        for p in self._particles:
            p["pos"][0] += p["vel"][0] * dt
            p["pos"][1] += p["vel"][1] * dt
            p["vel"][1] += 220 * dt                    # gravity
            p["life"] -= dt
            if p["life"] > 0:
                alive.append(p)
        self._particles = alive[-_PARTICLE_CAP:]

        self.sfx.pump()

        if self.app.gamestate:
            self._tick_countdown()

    def _tick_countdown(self) -> None:
        deadline = self.gs.get("deadline")
        if not deadline:
            self._last_tick = None
            return
        remaining = max(0.0, deadline - time.time())
        bucket = int(remaining * 2)                    # half-second resolution
        if bucket == self._last_tick:
            return
        self._last_tick = bucket
        if self._shake > 0:
            return                                     # never tick over a boom
        whole = bucket % 2 == 0
        if remaining <= 1:
            self.sfx.play("tick_hot")                  # every half-second boundary
        elif remaining <= 2 and whole:
            self.sfx.play("tick_hot")
        elif remaining <= 5 and whole:
            self.sfx.play("tick")

    # --- input ------------------------------------------------------------

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.sfx.init()

        gs = self.gs
        over = gs.get("phase") == protocol.PHASE_GAMEOVER

        # Show-runner controls: DETONATE only in human-host mode (no auto fuse).
        if self._is_runner():
            if not over and gs.get("deadline") is None:
                self.detonate_btn.handle(event)
            self.lobby_btn.handle(event)

        # Typing, live only on our own play-turn. No focus, no box: keys go
        # straight onto the bomb, and every edit is relayed to the room.
        if gs.get("your_turn") and gs.get("phase") == protocol.PHASE_PLAY:
            if event.type == pygame.KEYDOWN:
                before = self.typed
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self._submit()
                    return
                if event.key == pygame.K_BACKSPACE:
                    self.typed = self.typed[:-1]
                elif event.unicode and event.unicode.isprintable() and len(self.typed) < 32:
                    self.typed += event.unicode
                if self.typed != before:
                    self.app.net.send(protocol.C_TYPING, text=self.typed)
            # Browser touch fallback: a phone can't type into the canvas, so
            # tapping the bomb opens the native prompt, then submits at once.
            elif (browser_io.is_browser() and event.type == pygame.MOUSEBUTTONDOWN
                    and event.button == 1 and getattr(event, "pos", None)
                    and math.hypot(event.pos[0] - CENTER[0], event.pos[1] - CENTER[1]) <= BOMB_R + 16):
                self.typed = browser_io.prompt("type a word...", self.typed)[:32]
                self._submit()

    # --- drawing (needs a real Surface/font) ------------------------------

    def draw(self, surf: pygame.Surface) -> None:
        now = time.time()
        gs = self.gs
        deadline = gs.get("deadline")
        remaining = None if not deadline else max(0.0, deadline - now)
        press = press_of(remaining, self._turn_total)
        heat = heat_color(press)

        # 1. background heat + title
        surf.fill(_lerp(ui.BG, (34, 14, 22), press))
        ui.Label("WORD BOMB", (24, 34), 34, ui.ACCENT).draw(surf)
        room = (self.app.room or {}).get("code", "????")
        ui.Label(f"room {room}   {self.status}", (24, 76), 14, ui.MUTED).draw(surf)
        if not gs:
            return                                     # nothing until the first state

        over = gs.get("phase") == protocol.PHASE_GAMEOVER

        # 2. shake offset (bomb group only — explosion + critical micro-shake)
        k = self._shake / SHAKE_TIME
        ox = int(math.sin(now * 73) * 12 * k)
        oy = int(math.cos(now * 91) * 9 * k)
        if press > 0.75:
            ox += int(2 * math.sin(now * 47))
            oy += int(2 * math.cos(now * 53))

        frac = 1 - press

        # rim color resolution (used by bomb rim + countdown ring)
        if self._cool > 0:
            rim = _lerp(heat, ui.GOOD, self._cool / 0.25)
        elif remaining is not None and remaining < 1:
            rim = _lerp(heat, (255, 255, 255), 0.5 + 0.5 * math.sin(now * math.tau * 8))
        else:
            rim = heat

        self._draw_fuse(surf, frac, deadline, ox, oy, now)
        self._draw_bomb(surf, press, rim, ox, oy, now)
        self._draw_countdown(surf, frac, rim, remaining, deadline)
        self._draw_prompt_tiles(surf, gs.get("prompt", ""), heat, press, ox, oy, now)
        if gs.get("phase") == protocol.PHASE_PLAY and gs.get("options") is not None:
            ui.Label(fmt_options(gs.get("options", 0)), (240, 196 + oy), 14, ui.MUTED, center=True).draw(surf)
        if gs.get("phase") == protocol.PHASE_PLAY:
            self._draw_typed(surf, gs, now, ox, oy)
        self._draw_players(surf, gs, heat, ox, oy)

        # 8. your-turn / waiting banner
        if gs.get("phase") == protocol.PHASE_PLAY:
            if gs.get("your_turn"):
                col = _lerp(ui.TEXT, ui.ACCENT, 0.5 + 0.5 * math.sin(now * math.tau * 2))
                ui.Label("YOUR TURN - TYPE!", (240, 560), 22, col, center=True).draw(surf)
            else:
                ui.Label(f"waiting for {self._name_of(gs.get('current_pid'))}...",
                         (240, 560), 16, ui.MUTED, center=True).draw(surf)

        # 8b. sudden death
        if self._is_sudden_death(gs):
            col = _lerp(ui.ACCENT, HOT, 0.5 + 0.5 * math.sin(now * math.tau * 3))
            ui.Label("SUDDEN DEATH", (240, 110), 20, col, center=True).draw(surf)

        # 9. feed (newest 3, newest on top, older fades toward MUTED)
        ys = [585, 605, 625]
        for i, ev in enumerate(gs.get("feed", [])[-3:][::-1]):
            ui.Label(feed_line(ev), (24, ys[i]), 15, _lerp(ui.TEXT, ui.MUTED, 0.4 * i)).draw(surf)

        # 10. particles (skipped while the gameover overlay redraws them on top)
        if not over:
            self._draw_particles(surf)

        # 12. show-runner controls
        if self._is_runner():
            if not over and gs.get("deadline") is None:
                self.detonate_btn.draw(surf)
            self.lobby_btn.draw(surf)

        # 13. heat vignette (edges close in as the fuse shortens)
        if press > 0.4:
            self._vignette.set_alpha(int(150 * (press - 0.4) / 0.6))
            surf.blit(self._vignette, (0, 0))

        # 14. explosion flash
        if self._flash > 0:
            self._flash_surf.fill((255, 230, 200, int(160 * self._flash / FLASH_TIME)))
            surf.blit(self._flash_surf, (0, 0))

        # 15. gameover
        if over and gs.get("winner"):
            self._flash_surf.fill((10, 10, 16, 170))
            surf.blit(self._flash_surf, (0, 0))
            self._draw_particles(surf)                 # confetti behind the text
            ui.Label("WINNER", (240, 250), 26, ui.MUTED, center=True).draw(surf)
            ui.Label(gs["winner"].get("name", ""), (240, 305), 44, ui.ACCENT, center=True).draw(surf)
            if not self._is_runner():
                ui.Label("waiting for the host...", (240, 730), 14, ui.MUTED, center=True).draw(surf)

    def _draw_particles(self, surf: pygame.Surface) -> None:
        for p in self._particles:
            r = max(1, int(4 * p["life"] / p["max_life"]))
            pygame.draw.circle(surf, p["color"], (int(p["pos"][0]), int(p["pos"][1])), r)

    def _draw_fuse(self, surf, frac, deadline, ox, oy, now) -> None:
        pts = fuse_points()
        spark_idx = 23 if not deadline else max(0, min(23, round(frac * 23)))
        for i in range(spark_idx):
            a = (pts[i][0] + ox, pts[i][1] + oy)
            b = (pts[i + 1][0] + ox, pts[i + 1][1] + oy)
            pygame.draw.line(surf, (120, 100, 80), a, b, 2)
        sx, sy = pts[spark_idx][0] + ox, pts[spark_idx][1] + oy
        pygame.draw.circle(surf, SPARK, (int(sx), int(sy)), 4)
        if deadline:                                   # crackling ember (no RNG)
            for i in range(4):
                ang = i / 4 * math.tau
                j = 0.5 + 0.5 * math.sin(now * 40 + i)
                pygame.draw.line(surf, SPARK, (int(sx), int(sy)),
                                 (int(sx + math.cos(ang) * 7 * j), int(sy + math.sin(ang) * 7 * j)), 1)

    def _draw_bomb(self, surf, press, rim, ox, oy, now) -> None:
        cx, cy = CENTER[0] + ox, CENTER[1] + oy
        f = 1.2 + 4.8 * press
        pulse = int((2 + 7 * press) * (0.5 + 0.5 * math.sin(now * math.tau * f)))
        pygame.draw.circle(surf, (26, 27, 40), (cx, cy), BOMB_R + pulse)
        pygame.draw.circle(surf, rim, (cx, cy), BOMB_R + pulse, width=3)
        pygame.draw.circle(surf, (60, 62, 84), (cx - 26, cy - 30), 20)  # glass highlight

    def _draw_countdown(self, surf, frac, rim, remaining, deadline) -> None:
        cx, cy = CENTER
        r = BOMB_R + 14
        rect = pygame.Rect(cx - r, cy - r, 2 * r, 2 * r)
        pygame.draw.arc(surf, rim, rect, math.pi / 2 - math.tau * frac, math.pi / 2, 5)
        if deadline is not None and remaining is not None:
            size = 26
            if remaining <= 5:
                size = 26 + int(8 * max(0.0, 0.3 - (remaining % 1.0)) / 0.3)
            ui.Label(f"{remaining:.1f}s", (240, 300 + BOMB_R + 34), size, rim, center=True).draw(surf)
        else:
            ui.Label("HOST HOLDS THE DETONATOR", (240, 300 + BOMB_R + 34), 14,
                     ui.MUTED, center=True).draw(surf)

    def _draw_prompt_tiles(self, surf, prompt, heat, press, ox, oy, now) -> None:
        prompt = (prompt or "").upper()
        if not prompt:
            return
        tw, th, gap = 44, 52, 48
        start_x = CENTER[0] - (len(prompt) - 1) * gap // 2
        for i, ch in enumerate(prompt):
            dy = int(3 * math.sin(now * 3 + i * 1.1))
            dx = int(2 * math.sin(now * 31 + i * 2.3)) if press > 0.75 else 0
            cx = start_x + i * gap + ox + dx
            cy = CENTER[1] + oy + dy
            rect = pygame.Rect(cx - tw // 2, cy - th // 2, tw, th)
            pygame.draw.rect(surf, ui.FIELD, rect, border_radius=8)
            pygame.draw.rect(surf, heat, rect, width=2, border_radius=8)
            ui.Label(ch, (cx, cy), 34, ui.TEXT, center=True).draw(surf)

    def _draw_players(self, surf, gs, heat, ox, oy) -> None:
        players = gs.get("players", [])
        if not players:
            return
        seats = seat_positions(len(players))
        cur = gs.get("current_pid")
        for i, p in enumerate(players):
            sx, sy = seats[i]
            name = p.get("name", "")
            if len(name) > 8:
                name = name[:8] + "."
            alive = p.get("alive", True)
            if p.get("id") == cur:
                pygame.draw.circle(surf, heat, (sx, sy), 30, width=2)
                self._draw_rim_pointer(surf, (sx, sy), heat, ox, oy)
            ui.Label(name, (sx, sy - 18), 15, ui.TEXT if alive else ui.MUTED, center=True).draw(surf)
            if not alive:
                wpx = ui.get_font(15).size(name)[0]
                pygame.draw.line(surf, ui.MUTED, (sx - wpx // 2, sy - 14), (sx + wpx // 2, sy - 14), 1)
            else:
                lives = p.get("lives", 0)
                for L in range(lives):
                    lx = sx - (lives - 1) * 7 + L * 14
                    pygame.draw.circle(surf, ui.ACCENT, (lx, sy + 4), 5)
            if p.get("is_bot"):
                ui.Label("bot", (sx, sy + 18), 12, ui.MUTED, center=True).draw(surf)

    def _draw_rim_pointer(self, surf, seat, heat, ox, oy) -> None:
        sx, sy = seat
        cx, cy = CENTER[0] + ox, CENTER[1] + oy
        ang = math.atan2(sy - cy, sx - cx)
        perp = ang + math.pi / 2
        tip = (cx + math.cos(ang) * (BOMB_R + 10), cy + math.sin(ang) * (BOMB_R + 10))
        b1 = (cx + math.cos(ang) * BOMB_R + math.cos(perp) * 5,
              cy + math.sin(ang) * BOMB_R + math.sin(perp) * 5)
        b2 = (cx + math.cos(ang) * BOMB_R - math.cos(perp) * 5,
              cy + math.sin(ang) * BOMB_R - math.sin(perp) * 5)
        pygame.draw.polygon(surf, heat, [tip, b1, b2])

    def _draw_typed(self, surf, gs, now, ox, oy) -> None:
        mine = bool(gs.get("your_turn"))
        text = self.typed if mine else self.live_typing
        if not mine and not text:
            return
        shown = tail_that_fits(text, 400, lambda s: ui.get_font(20).size(s)[0])
        if mine and (now % 1.0) < 0.6:
            shown += "_"                       # blinking caret: type right here
        if self._input_flash > 0:              # the reject rattle, now on the bomb
            ox += int(4 * math.sin(now * 60) * self._input_flash / 0.3)
            color = HOT
        else:
            color = ui.GOOD if (gs.get("prompt") or "").lower() in text.lower() else ui.TEXT
        ui.Label(shown, (CENTER[0] + ox, CENTER[1] + 48 + oy), 20, color, center=True).draw(surf)
