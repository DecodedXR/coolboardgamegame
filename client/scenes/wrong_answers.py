"""Wrong Answers Only — the in-game scene.

Renders entirely from the latest per-player ``game_state`` snapshot the server
broadcasts (stored on ``app.gamestate``), so the view always matches our role and
the current phase. The four phases:

* **prompt**  — contestants type an answer; host/spectators watch progress.
* **vote**    — contestants pick a favorite among the anonymized answers.
* **reveal**  — authorship, votes, and round scores are shown.
* **final**   — the closing scoreboard.

The *show-runner* (the human host, or the room owner in auto mode) gets an
ADVANCE button; in auto mode a countdown also drives the phases server-side.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

import pygame

from client import ui
from client.scenes.base import Scene
from shared import protocol

_MARGIN = 24

# Single portrait column: content spans the full width between the margins (no side
# scoreboard column — scores show as a compact strip in the header instead).
def _content_w(app) -> int:
    return app.width - _MARGIN * 2


class WrongAnswersScene(Scene):
    def on_enter(self) -> None:
        w, h = self.app.width, self.app.height
        cw = _content_w(self.app)
        self.answer_input = ui.TextInput(
            (_MARGIN, 250, cw, 48),
            placeholder="type your (wrong) answer", max_len=80,
        )
        self.submit_btn = ui.Button("SUBMIT", (_MARGIN, 312, 200, 48), self._submit_answer)
        self.advance_btn = ui.Button("NEXT", (w - _MARGIN - 200, h - 70, 200, 50), self._advance)
        self.return_btn = ui.Button("BACK TO LOBBY", (w - _MARGIN - 220, h - 70, 220, 50), self._return_to_lobby)
        self.leave_btn = ui.Button("LEAVE", (_MARGIN, h - 70, 120, 44), self._leave)
        self.vote_buttons: list[tuple[str, ui.Button]] = []
        self._build_vote_buttons()

    # --- derived state ----------------------------------------------------

    @property
    def gs(self) -> dict[str, Any]:
        return self.app.gamestate or {}

    @property
    def my_id(self) -> Optional[str]:
        return (self.app.you or {}).get("id")

    @property
    def phase(self) -> str:
        return self.gs.get("phase", protocol.PHASE_PROMPT)

    @property
    def role(self) -> str:
        return self.gs.get("you_role", "spectator")

    def _is_runner(self) -> bool:
        room = self.app.room or {}
        if room.get("host_mode") == protocol.HOST_HUMAN:
            return self.role == "host"
        return self.my_id is not None and self.my_id == room.get("owner_id")

    # --- actions ----------------------------------------------------------

    def _submit_answer(self) -> None:
        text = self.answer_input.text.strip()
        if text and not self.gs.get("you_submitted"):
            self.app.net.send(protocol.C_SUBMIT_ANSWER, text=text)

    def _vote(self, answer_id: str) -> None:
        if not self.gs.get("you_submitted"):
            self.app.net.send(protocol.C_SUBMIT_VOTE, answer_id=answer_id)

    def _advance(self) -> None:
        self.app.net.send(protocol.C_ADVANCE_PHASE)

    def _return_to_lobby(self) -> None:
        self.app.net.send(protocol.C_RETURN_TO_LOBBY)

    def _leave(self) -> None:
        self.app.net.send(protocol.C_LEAVE_ROOM)
        self.app.you = None
        self.app.room = None
        self.app.gamestate = None
        from client.scenes.menu import MenuScene
        self.app.go_to(MenuScene(self.app))

    # --- messages ---------------------------------------------------------

    def on_message(self, msg: dict[str, Any]) -> None:
        t = msg["type"]
        if t == protocol.S_GAME_STATE:
            self.app.gamestate = msg["game"]
            self._build_vote_buttons()
        elif t == protocol.S_ROOM_UPDATE:
            self.app.room = msg["room"]
        elif t == protocol.S_RETURN_TO_LOBBY:
            self.app.gamestate = None
            from client.scenes.lobby import LobbyScene
            self.app.go_to(LobbyScene(self.app))

    def _build_vote_buttons(self) -> None:
        self.vote_buttons = []
        if self.phase != protocol.PHASE_VOTE or self.role != "contestant":
            return
        y = 250
        for opt in self.gs.get("answers", []):
            aid = opt["answer_id"]
            rect = (_MARGIN, y, _content_w(self.app), 46)
            self.vote_buttons.append((aid, ui.Button(opt["text"], rect, lambda a=aid: self._vote(a))))
            y += 54

    # --- input ------------------------------------------------------------

    def handle_event(self, event: pygame.event.Event) -> None:
        self.leave_btn.handle(event)
        runner = self._is_runner()
        if self.phase == protocol.PHASE_FINAL:
            if runner:
                self.return_btn.handle(event)
            return
        if runner:
            self.advance_btn.handle(event)
        if self.phase == protocol.PHASE_PROMPT and self.role == "contestant" and not self.gs.get("you_submitted"):
            self.answer_input.handle(event)
            self.submit_btn.handle(event)
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self._submit_answer()
        elif self.phase == protocol.PHASE_VOTE and self.role == "contestant" and not self.gs.get("you_submitted"):
            for _aid, btn in self.vote_buttons:
                btn.handle(event)

    # --- drawing ----------------------------------------------------------

    def draw(self, surf: pygame.Surface) -> None:
        surf.fill(ui.BG)
        self._draw_header(surf)
        self._draw_scoreboard(surf)
        if self.phase == protocol.PHASE_PROMPT:
            self._draw_prompt(surf)
        elif self.phase == protocol.PHASE_VOTE:
            self._draw_vote(surf)
        elif self.phase == protocol.PHASE_REVEAL:
            self._draw_reveal(surf)
        elif self.phase == protocol.PHASE_FINAL:
            self._draw_final(surf)

        self.leave_btn.draw(surf)
        if self.phase == protocol.PHASE_FINAL:
            if self._is_runner():
                self.return_btn.draw(surf)
            else:
                ui.Label("waiting for the host to end the game...",
                         (self.app.width // 2, self.app.height - 50), 16, ui.MUTED, center=True).draw(surf)
        elif self._is_runner():
            self.advance_btn.label = self._advance_label()
            self.advance_btn.draw(surf)

    def _advance_label(self) -> str:
        if self.phase == protocol.PHASE_PROMPT:
            return "REVEAL ANSWERS"
        if self.phase == protocol.PHASE_VOTE:
            return "SHOW RESULTS"
        if self.phase == protocol.PHASE_REVEAL:
            last = self.gs.get("round", 1) >= self.gs.get("total_rounds", 1)
            return "FINAL SCORES" if last else "NEXT ROUND"
        return "NEXT"

    def _draw_header(self, surf: pygame.Surface) -> None:
        ui.Label("WRONG ANSWERS ONLY", (_MARGIN, 28), 26, ui.ACCENT).draw(surf)
        rnd = f"round {self.gs.get('round', 1)}/{self.gs.get('total_rounds', 1)}"
        ui.Label(f"{rnd}  ·  {self.phase}", (_MARGIN, 64), 18, ui.MUTED).draw(surf)
        deadline = self.gs.get("deadline")
        if deadline:
            remaining = max(0, math.ceil(deadline - time.time()))
            ui.Label(f"{remaining}s", (self.app.width - _MARGIN, 28), 30,
                     ui.GOOD if remaining > 5 else ui.ACCENT).draw(surf)
        if self.phase != protocol.PHASE_FINAL:
            # Prompt starts at y=115; _draw_scoreboard renders a compact strip at y=90.
            self._wrapped(surf, self.gs.get("prompt", ""), _MARGIN, 115,
                          _content_w(self.app), 24, ui.TEXT)

    def _draw_scoreboard(self, surf: pygame.Surface) -> None:
        if self.phase == protocol.PHASE_FINAL:
            return
        scores = self.gs.get("scores", [])
        if not scores:
            return
        # Compact horizontal strip between round info and the prompt.
        parts = [f"{r['name'][:8]}: {r['score']}" for r in scores]
        ui.Label("  ·  ".join(parts), (_MARGIN, 90), 14, ui.MUTED).draw(surf)

    def _draw_prompt(self, surf: pygame.Surface) -> None:
        if self.role == "contestant":
            if self.gs.get("you_submitted"):
                ui.Label("answer locked in:", (_MARGIN, 218), 18, ui.MUTED).draw(surf)
                self._wrapped(surf, self.gs.get("your_answer", ""), _MARGIN, 244,
                              _content_w(self.app), 22, ui.GOOD)
                self._waiting(surf, "answers")
            else:
                self.answer_input.draw(surf)
                self.submit_btn.draw(surf)
        else:
            who = "you are the HOST — run the show" if self.role == "host" else "spectating"
            ui.Label(who, (_MARGIN, 218), 18, ui.MUTED).draw(surf)
            self._waiting(surf, "answers")

    def _draw_vote(self, surf: pygame.Surface) -> None:
        if self.role == "contestant" and not self.gs.get("you_submitted"):
            if self.vote_buttons:
                ui.Label("vote for your favorite:", (_MARGIN, 218), 18, ui.MUTED).draw(surf)
                for _aid, btn in self.vote_buttons:
                    btn.draw(surf)
            else:
                ui.Label("no answers to vote on this round", (_MARGIN, 218), 18, ui.MUTED).draw(surf)
        else:
            ui.Label("votes are in" if self.gs.get("you_submitted") else "voting...",
                     (_MARGIN, 218), 18, ui.MUTED).draw(surf)
            y = 248
            for opt in self.gs.get("answers", []):
                pygame.draw.rect(surf, ui.PANEL, pygame.Rect(_MARGIN, y, _content_w(self.app), 46), border_radius=8)
                ui.Label(opt["text"][:40], (_MARGIN + 14, y + 14), 18, ui.TEXT).draw(surf)
                y += 54
            self._waiting(surf, "votes")

    def _draw_reveal(self, surf: pygame.Surface) -> None:
        ui.Label("the results:", (_MARGIN, 218), 18, ui.MUTED).draw(surf)
        results = self.gs.get("results", [])
        if not results:
            ui.Label("nobody answered this round!", (_MARGIN, 248), 20, ui.ACCENT).draw(surf)
            return
        y = 248
        width = _content_w(self.app)
        for r in results:
            pygame.draw.rect(surf, ui.PANEL, pygame.Rect(_MARGIN, y, width, 50), border_radius=8)
            ui.Label(r["text"][:40], (_MARGIN + 14, y + 8), 18, ui.TEXT).draw(surf)
            vote_word = "vote" if r["votes"] == 1 else "votes"
            ui.Label(f'— {r["author_name"]}   (+{r["points"]}, {r["votes"]} {vote_word})',
                     (_MARGIN + 14, y + 30), 14, ui.MUTED).draw(surf)
            y += 58

    def _draw_final(self, surf: pygame.Surface) -> None:
        cx = self.app.width // 2
        ui.Label("FINAL SCORES", (cx, 120), 40, ui.ACCENT, center=True).draw(surf)
        scores = self.gs.get("scores", [])
        if scores:
            winner = scores[0]
            ui.Label(f"🏆 {winner['name']} wins!", (cx, 180), 26, ui.GOOD, center=True).draw(surf)
        y = 240
        for i, row in enumerate(scores, start=1):
            ui.Label(f"{i}.  {row['name']}", (cx - 180, y), 22, ui.TEXT).draw(surf)
            ui.Label(str(row["score"]), (cx + 140, y), 22, ui.GOOD).draw(surf)
            y += 36

    # --- helpers ----------------------------------------------------------

    def _waiting(self, surf: pygame.Surface, noun: str) -> None:
        done = self.gs.get("submitted_count", 0)
        total = self.gs.get("contestant_count", 0)
        ui.Label(f"{done}/{total} {noun} in", (_MARGIN, self.app.height - 120), 18, ui.MUTED).draw(surf)

    def _wrapped(self, surf: pygame.Surface, text: str, x: int, y: int, max_w: int,
                 size: int, color: tuple[int, int, int]) -> None:
        font = ui.get_font(size)
        words = text.split()
        line = ""
        line_h = font.get_height() + 4
        for word in words:
            trial = f"{line} {word}".strip()
            if font.size(trial)[0] > max_w and line:
                ui.Label(line, (x, y), size, color).draw(surf)
                y += line_h
                line = word
            else:
                line = trial
        if line:
            ui.Label(line, (x, y), size, color).draw(surf)
