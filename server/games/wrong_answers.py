"""Wrong Answers Only — the first real minigame (Quiplash-style).

Flow per round:  **prompt** (everyone types an answer) → **vote** (everyone
picks their favorite of the *anonymized* answers, never their own) → **reveal**
(authorship + votes + scores).  After the configured number of rounds the game
ends on a **final** scoreboard.

Like :mod:`server.rooms`, this class is pure game state — no asyncio, no sockets.
The connection layer drives it (records submissions, decides when to
:meth:`advance`, and in auto-host mode runs the phase timers) and serializes a
*per-player* view with :meth:`public` so a contestant never sees others' answers
during the prompt phase nor the authorship during voting.

Two things keep voting honest:

* **Anonymized answer ids.** When the vote phase opens, each answer gets a fresh
  random id (:meth:`advance` from the prompt phase). Votes reference that id, so
  the wire never carries "answer X was written by player Y" until reveal.
* **No self-votes.** A contestant's own answer is omitted from their ballot and
  rejected server-side.
"""

from __future__ import annotations

import random
import uuid
from typing import Any, Optional

from shared import protocol


class WrongAnswersGame:
    def __init__(
        self,
        contestants: list[tuple[str, str]],
        prompts: list[str],
        *,
        total_rounds: int,
        points_per_vote: int,
        max_answer_len: int,
    ) -> None:
        if len(prompts) < total_rounds:
            raise ValueError("need at least one prompt per round")
        # Insertion order = lobby order; used only for stable scoreboard ties.
        self.names: dict[str, str] = {cid: name for cid, name in contestants}
        self.contestant_ids: list[str] = [cid for cid, _ in contestants]
        self.prompts = prompts
        self.total_rounds = total_rounds
        self.points_per_vote = points_per_vote
        self.max_answer_len = max_answer_len

        self.round = 1
        self.phase = protocol.PHASE_PROMPT
        self.scores: dict[str, int] = {cid: 0 for cid in self.contestant_ids}
        # Set by the connection layer in auto mode so clients can show a
        # countdown; ``None`` in human mode (host advances manually).
        self.deadline: Optional[float] = None
        self._reset_round()

    # --- round lifecycle --------------------------------------------------

    def _reset_round(self) -> None:
        self.answers: dict[str, str] = {}        # author_id -> text
        self.votes: dict[str, str] = {}          # voter_id  -> answer_id
        self.ans_id: dict[str, str] = {}         # author_id -> anonymized answer_id
        self.author_of: dict[str, str] = {}      # answer_id -> author_id
        self.tally: dict[str, int] = {}          # author_id -> votes this round
        self.results: list[dict[str, Any]] = []  # built at reveal

    @property
    def current_prompt(self) -> str:
        return self.prompts[self.round - 1]

    def is_contestant(self, pid: Optional[str]) -> bool:
        return pid in self.scores

    @property
    def is_over(self) -> bool:
        return self.phase == protocol.PHASE_FINAL

    # --- submissions ------------------------------------------------------

    def submit_answer(self, pid: str, text: Any) -> bool:
        if self.phase != protocol.PHASE_PROMPT or not self.is_contestant(pid):
            return False
        cleaned = str(text or "").strip()[: self.max_answer_len]
        if not cleaned:
            return False
        self.answers[pid] = cleaned
        return True

    def submit_vote(self, voter: str, answer_id: Any) -> bool:
        if self.phase != protocol.PHASE_VOTE or not self.is_contestant(voter):
            return False
        author = self.author_of.get(str(answer_id))
        if author is None or author == voter:  # unknown answer, or voting for self
            return False
        self.votes[voter] = str(answer_id)
        return True

    def _can_vote(self, pid: str) -> bool:
        """True if there is at least one answer this player is allowed to vote for."""
        return any(author != pid for author in self.answers)

    def all_submitted(self, connected_ids: set[str]) -> bool:
        """Whether every still-connected contestant has acted for this phase.

        Used by the connection layer to advance early (and in auto mode to skip
        the rest of the timer). Disconnected contestants are not waited on.
        """
        active = [c for c in self.contestant_ids if c in connected_ids]
        if not active:
            return True
        if self.phase == protocol.PHASE_PROMPT:
            return all(c in self.answers for c in active)
        if self.phase == protocol.PHASE_VOTE:
            return all(c in self.votes for c in active if self._can_vote(c))
        return False

    # --- phase transitions ------------------------------------------------

    def advance(self) -> str:
        """Move to the next phase, scoring/round-rolling as needed. Idempotent
        at the final phase. Returns the new phase."""
        if self.phase == protocol.PHASE_PROMPT:
            self._open_voting()
        elif self.phase == protocol.PHASE_VOTE:
            self._score_round()
            self._build_results()
            self.phase = protocol.PHASE_REVEAL
        elif self.phase == protocol.PHASE_REVEAL:
            if self.round >= self.total_rounds:
                self.phase = protocol.PHASE_FINAL
            else:
                self.round += 1
                self._reset_round()
                self.phase = protocol.PHASE_PROMPT
        # PHASE_FINAL: terminal, no-op.
        return self.phase

    def _open_voting(self) -> None:
        # Assign anonymized ids in shuffled order so neither id nor position
        # leaks authorship.
        authors = list(self.answers)
        random.shuffle(authors)
        for author in authors:
            aid = uuid.uuid4().hex[:8]
            self.ans_id[author] = aid
            self.author_of[aid] = author
        if not self.answers:
            # Nobody answered — skip voting, reveal an empty round.
            self._build_results()
            self.phase = protocol.PHASE_REVEAL
        else:
            self.phase = protocol.PHASE_VOTE

    def _score_round(self) -> None:
        for answer_id in self.votes.values():
            author = self.author_of.get(answer_id)
            if author is not None:
                self.tally[author] = self.tally.get(author, 0) + 1
        for author, votes in self.tally.items():
            self.scores[author] += votes * self.points_per_vote

    def _build_results(self) -> None:
        results = [
            {
                "author_id": author,
                "author_name": self.names[author],
                "text": text,
                "votes": self.tally.get(author, 0),
                "points": self.tally.get(author, 0) * self.points_per_vote,
            }
            for author, text in self.answers.items()
        ]
        # Highest-voted first; stable by name for ties.
        results.sort(key=lambda r: (-r["votes"], r["author_name"]))
        self.results = results

    # --- serialization ----------------------------------------------------

    def scoreboard(self) -> list[dict[str, Any]]:
        rows = [
            {"id": cid, "name": self.names[cid], "score": self.scores[cid]}
            for cid in self.contestant_ids
        ]
        rows.sort(key=lambda r: (-r["score"], r["name"]))
        return rows

    def public(self, for_pid: Optional[str], host_id: Optional[str]) -> dict[str, Any]:
        """Per-player view. ``host_id`` marks the human host (a spectator of play
        who runs the show); everyone in ``self.scores`` is a contestant."""
        if for_pid == host_id and host_id is not None:
            role = "host"
        elif self.is_contestant(for_pid):
            role = "contestant"
        else:
            role = "spectator"

        data: dict[str, Any] = {
            "name": protocol.GAME_WRONG_ANSWERS,
            "phase": self.phase,
            "round": self.round,
            "total_rounds": self.total_rounds,
            "prompt": self.current_prompt,
            "deadline": self.deadline,
            "you_role": role,
            "contestant_count": len(self.contestant_ids),
            "scores": self.scoreboard(),
        }

        if self.phase == protocol.PHASE_PROMPT:
            data["submitted_count"] = len(self.answers)
            data["you_submitted"] = for_pid in self.answers
            data["your_answer"] = self.answers.get(for_pid or "", "")
        elif self.phase == protocol.PHASE_VOTE:
            # Ballot: every answer except the viewer's own, ordered by anon id
            # (which is random) so position reveals nothing.
            options = [
                {"answer_id": self.ans_id[author], "text": text}
                for author, text in self.answers.items()
                if author != for_pid
            ]
            options.sort(key=lambda o: o["answer_id"])
            data["answers"] = options
            data["submitted_count"] = len(self.votes)
            data["you_submitted"] = for_pid in self.votes
            data["your_vote"] = self.votes.get(for_pid or "")
        elif self.phase in (protocol.PHASE_REVEAL, protocol.PHASE_FINAL):
            data["results"] = self.results

        return data
