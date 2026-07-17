"""Word Bomb: pure rules — no asyncio, no sockets.

A Bomb-Party-style word game. The current player must submit a real English word
*containing* the prompt substring before the fuse runs out; a valid, unused word
passes the bomb on with a fresh prompt. A timeout (the connection layer's
deadline / a host DETONATE) explodes the bomb: the current player loses a life,
and at 0 lives they are eliminated. Last player alive wins.

Like :class:`~server.games.snakes_and_ladders.SnakesAndLaddersGame`, this class is
pure game state: the connection layer drives it (records submissions, runs
auto/bot timers) and serializes a per-player view with :meth:`public`, exposing
the same narrow surface the driver relies on (``current_pid``, ``seq``,
``deadline``, ``awaiting``, ``is_over``, ``is_current``, ``is_contestant``,
``advance``, ``bot_ids``, ``bot_action``, ``winner``, ``public``).

**The load-bearing asymmetry:** an *accepted* word bumps ``seq`` and passes the
bomb; a *rejected* word does NOT touch ``seq``, ``prompt``, or the current player.
The driver arms a stale-timer guard keyed ``(actor, seq)``, so re-driving on a
reject would reset the countdown — spamming garbage words would defuse the bomb
forever. Because two rejects in one turn share a ``seq``, every feed event also
carries its own monotonic ``id`` (stamped by :meth:`_emit`); the client dedups
feed events by ``id``, never by ``seq``.
"""

from __future__ import annotations

import random
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from shared import protocol

AWAIT_WORD = protocol.AWAIT_WORD
PHASE_PLAY = protocol.PHASE_PLAY
PHASE_GAMEOVER = protocol.PHASE_GAMEOVER

_WORDS_PATH = Path(__file__).parent / "words.txt"
_BOT_POOL_CAP = 200   # per-prompt candidate words kept for bots


def derive_prompts(words, min_count, pool_cap=_BOT_POOL_CAP):
    """Scan every word's 2- and 3-letter substrings. Return ``(prompts, index,
    counts)``:

    ``prompts`` = sorted list of substrings appearing in >= ``min_count`` words;
    ``index`` = ``{prompt: first pool_cap words containing it}`` for bot answers;
    ``counts`` = ``{prompt: total words containing it}`` (surviving prompts only).
    Each substring is counted at most once per word (a per-word set), so a word
    like ``"banana"`` doesn't triple-count ``"an"``.
    """
    counts: Counter = Counter()
    index: dict[str, list[str]] = {}
    for w in words:
        seen = set()
        n = len(w)
        for i in range(n):
            if i + 2 <= n:
                seen.add(w[i:i + 2])
            if i + 3 <= n:
                seen.add(w[i:i + 3])
        for sub in seen:
            counts[sub] += 1
            bucket = index.get(sub)
            if bucket is None:
                index[sub] = [w]
            elif len(bucket) < pool_cap:
                bucket.append(w)
    prompts = sorted(sub for sub, c in counts.items() if c >= min_count)
    index = {sub: index[sub] for sub in prompts}
    surviving_counts = {sub: counts[sub] for sub in prompts}
    return prompts, index, surviving_counts


@lru_cache(maxsize=1)
def load_dictionary(min_count: int = 500):
    """Read ``words.txt`` and derive the prompt table once per process.

    Returns ``(words: frozenset[str], prompts: list[str], index: dict[str,
    list[str]], counts: dict[str, int])``. ``lru_cache`` amortizes the one-time
    substring scan (the driver warms it at boot). Lines are read with ``.strip()``
    — a Windows checkout with autocrlf can hand the file over with ``\\r\\n``, and a
    dictionary of ``'cat\\r'`` entries would reject every word ever typed.
    """
    raw = _WORDS_PATH.read_text(encoding="utf-8")
    words = frozenset(w for w in (line.strip() for line in raw.splitlines()) if w)
    prompts, index, counts = derive_prompts(words, min_count)
    return words, prompts, index, counts


class WordBombGame:
    def __init__(self, contestants, *, bots=(), words, prompts, index=None,
                 counts=None, fuse_start=20.0, fuse_step=0.5, fuse_floor=7.0,
                 lives=2, bot_fail_chance=0.25, rng=None) -> None:
        contestants = list(contestants)
        bots = list(bots)
        self.contestant_ids: list[str] = [pid for pid, _ in contestants]
        self.bot_ids: set[str] = {pid for pid, _ in bots}
        self.order: list[str] = self.contestant_ids + [pid for pid, _ in bots]
        self.names: dict[str, str] = {pid: name for pid, name in contestants + bots}

        self.rng = rng if rng is not None else random.Random()
        self.words = words
        self.prompts = prompts
        self.index = index or {}
        self.counts = counts or {}
        self.fuse_start = fuse_start
        self.fuse_step = fuse_step
        self.fuse_floor = fuse_floor
        self.fuse_seconds = fuse_start
        self.bot_fail_chance = bot_fail_chance

        self.lives: dict[str, int] = {pid: lives for pid in self.order}
        self.used: set[str] = set()
        self.phase = PHASE_PLAY
        self.awaiting = AWAIT_WORD
        self.current_idx = 0
        self.seq = 0
        self.deadline: Optional[float] = None
        self.winner_id: Optional[str] = None
        self.feed: list[dict] = []   # event log; only the last 8 kept
        self._event_id = 0
        self.prompt = self.rng.choice(prompts)
        self.options = 0
        self._count_options()

    def _count_options(self) -> None:
        base = self.counts.get(self.prompt, 0)
        self.options = max(0, base - sum(1 for w in self.used if self.prompt in w))

    # --- contract methods used by the connection layer --------------------

    @property
    def current_pid(self) -> str:
        return self.order[self.current_idx]

    @property
    def is_over(self) -> bool:
        return self.phase == PHASE_GAMEOVER

    def is_contestant(self, pid: Optional[str]) -> bool:
        return pid in self.contestant_ids

    def is_current(self, pid: Optional[str]) -> bool:
        return pid == self.current_pid and not self.is_over

    @property
    def winner(self) -> Optional[dict[str, str]]:
        if self.winner_id is None:
            return None
        return {"id": self.winner_id, "name": self.names[self.winner_id]}

    def alive_ids(self) -> list[str]:
        return [pid for pid in self.order if self.lives[pid] > 0]

    # --- feed ------------------------------------------------------------

    def _emit(self, **event) -> None:
        """Stamp a monotonic ``id`` and the current ``seq`` onto a feed event,
        append it, and trim to the last 8. The ``id`` is what the client dedups on:
        rejects share a ``seq`` (the anti-fuse-reset rule leaves ``seq`` alone), so
        a ``seq``-keyed dedup would silently swallow a second reject's reaction."""
        self._event_id += 1
        event["id"] = self._event_id
        event["seq"] = self.seq
        self.feed.append(event)
        del self.feed[:-8]

    # --- player actions ---------------------------------------------------

    def submit_word(self, pid: str, word: Any) -> Optional[str]:
        """Submit a word. Returns ``"accepted"``, ``"rejected"``, or ``None`` (an
        illegal submission — not the current player / game over — the caller turns
        into a protocol error). A reject deliberately leaves ``seq``, ``prompt``,
        and the current player untouched (the anti-fuse-reset rule)."""
        if self.is_over or not self.is_current(pid):
            return None
        w = str(word or "").strip().lower()
        name = self.names[pid]
        if self.prompt not in w:
            self._emit(kind="reject", pid=pid, name=name, word=w,
                       reason="not_in_prompt", prompt=self.prompt)
            return "rejected"
        if w not in self.words:
            self._emit(kind="reject", pid=pid, name=name, word=w,
                       reason="not_a_word", prompt=self.prompt)
            return "rejected"
        if w in self.used:
            self._emit(kind="reject", pid=pid, name=name, word=w,
                       reason="already_used", prompt=self.prompt)
            return "rejected"
        self.used.add(w)
        self._emit(kind="accept", pid=pid, name=name, word=w, prompt=self.prompt)
        self.seq += 1
        self.fuse_seconds = max(self.fuse_floor, self.fuse_seconds - self.fuse_step)
        self._pass_bomb()
        return "accepted"

    def advance(self) -> None:
        """The explosion — host DETONATE / deadline timeout / absent human all route
        here. The current player loses a life (eliminated at 0); the last player
        alive wins."""
        if self.is_over:
            return
        pid = self.current_pid
        self.lives[pid] -= 1
        self._emit(kind="explode", pid=pid, name=self.names[pid], prompt=self.prompt)
        if self.lives[pid] <= 0:
            self._emit(kind="eliminated", pid=pid, name=self.names[pid])
        self.seq += 1
        self.fuse_seconds = self.fuse_start
        if len(self.alive_ids()) == 1:
            self.phase = PHASE_GAMEOVER
            self.winner_id = self.alive_ids()[0]
            self.deadline = None
        else:
            self._pass_bomb()

    def _pass_bomb(self) -> None:
        """Advance the bomb round-robin to the next still-alive player and pick a
        fresh prompt. At least one alive player exists (gameover was checked)."""
        n = len(self.order)
        idx = self.current_idx
        for _ in range(n):
            idx = (idx + 1) % n
            if self.lives[self.order[idx]] > 0:
                break
        self.current_idx = idx
        self.prompt = self.rng.choice(self.prompts)
        self._count_options()

    def bot_action(self, pid: str) -> dict[str, Any]:
        """Pure: what a trivial bot would do now. Fumbles at ``bot_fail_chance``
        (the caller then explodes it); otherwise plays a random unused indexed word
        containing the prompt, or passes if it can't find one."""
        if self.rng.random() < self.bot_fail_chance:
            return {"kind": "pass"}
        candidates = [w for w in (self.index.get(self.prompt) or []) if w not in self.used]
        if not candidates:
            return {"kind": "pass"}
        return {"kind": "word", "word": self.rng.choice(candidates)}

    # --- serialization ----------------------------------------------------

    def public(self, for_pid: Optional[str], host_id: Optional[str]) -> dict[str, Any]:
        """Per-player view, mirroring the S&L role derivation (host / contestant /
        spectator). Nothing is secret in word bomb, so every player sees the same
        prompt, feed, and roster."""
        if host_id is not None and for_pid == host_id:
            role = "host"
        elif self.is_contestant(for_pid):
            role = "contestant"
        else:
            role = "spectator"

        return {
            "name": protocol.GAME_WORD_BOMB,
            "phase": self.phase,
            "awaiting": self.awaiting,
            "prompt": self.prompt,
            "players": [
                {"id": pid, "name": self.names[pid], "lives": self.lives[pid],
                 "is_bot": pid in self.bot_ids, "alive": self.lives[pid] > 0}
                for pid in self.order
            ],
            "current_pid": self.current_pid,
            "your_turn": self.is_current(for_pid),
            "your_id": for_pid,
            "you_role": role,
            "deadline": self.deadline,
            "feed": list(self.feed),
            "winner": self.winner,
            "used_count": len(self.used),
            "options": self.options,
        }
