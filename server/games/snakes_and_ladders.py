"""Snakes & Ladders — a snake-heavy, server-authoritative board game.

This class is pure game state: no asyncio, no sockets. The connection layer
drives it (records the current player's action, runs auto/bot timers) and
serializes a per-player view with :meth:`public`.

**The core trick that makes a networked, *animated* board game testable:** the
server computes each turn's *entire* resolution up front as an ordered,
serializable timeline (``last_turn["steps"]``) with a monotonic ``seq``. The
client merely *replays* that timeline as animation (token hops, snake slides,
dice, wheel spins, shop) — it never decides an outcome. Authority lives here,
where it can be unit-tested headless; animation lives in the client.

**Phases collapse to two** — :data:`~shared.protocol.PHASE_PLAY` and
:data:`~shared.protocol.PHASE_GAMEOVER`. The shop is a *sub-state*
(``awaiting == "shop"``) on the same player's turn rather than a separate phase,
which removes "the current player changed but the phase didn't" races when bots
and timers are involved.

``roll()`` resolves a turn in a deliberately fixed order — see the heavy comments
there; it is the part most easily gotten wrong.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from shared import protocol

# --- catalogs (intrinsic game rules; deployment tunables live in config.py) ---

# Powerups are *held* and spent by choice before a roll; they modify that one
# roll and do not pass the turn.
POWERUPS = ("immunity", "boost", "double", "reroll")

# Debuffs are applied automatically when triggered (debuff tile / wheel).
DEBUFFS = ("skip_next", "slip_back", "gold_tax")

# Default shop / wheel-grant prices, mirrored by ``SAL_PRICE_*`` in config.py.
DEFAULT_PRICES = {"immunity": 40, "boost": 30, "double": 60, "reroll": 30}

# The Wheel-of-Names slice table: equal-weight outcomes the server picks from by
# index (the client renders these slices and decelerates the spin to the chosen
# index — it never decides the outcome). Kept intentionally small and serializable.
WHEEL_OUTCOMES = [
    {"kind": "gold", "amount": 50},
    {"kind": "item", "item": "boost"},
    {"kind": "debuff", "debuff": "slip_back"},
    {"kind": "gold", "amount": 25},
    {"kind": "item", "item": "immunity"},
    {"kind": "debuff", "debuff": "skip_next"},
    {"kind": "item", "item": "double"},
    {"kind": "debuff", "debuff": "gold_tax"},
]

# ``awaiting`` sub-states within PHASE_PLAY; single-sourced in the protocol so this
# authority and the client's replay UI never drift.
AWAIT_ROLL = protocol.AWAIT_ROLL
AWAIT_SHOP = protocol.AWAIT_SHOP


class SnakesAndLaddersGame:
    def __init__(
        self,
        contestants: list[tuple[str, str]],
        *,
        bots: tuple = (),
        cells: int = 100,
        cols: int = 10,
        snake_count: int = 14,
        ladder_count: int = 4,
        shop_count: int = 4,
        wheel_count: int = 5,
        gold_count: int = 6,
        debuff_count: int = 5,
        starting_gold: int = 100,
        dice_sides: int = 6,
        exact_finish: bool = True,
        prices: Optional[dict[str, int]] = None,
        boost_bonus: int = 3,
        gold_tile_amount: int = 30,
        slip_back: int = 6,
        gold_tax: int = 25,
        rng: Optional[random.Random] = None,
    ) -> None:
        contestants = list(contestants)
        bots = list(bots)
        # Bots are players too (they take turns) but they are NOT contestants —
        # only humans are. They live solely in this object, never in room.players.
        self.contestant_ids: list[str] = [pid for pid, _ in contestants]
        self.bot_ids: set[str] = {pid for pid, _ in bots}
        self.order: list[str] = self.contestant_ids + [pid for pid, _ in bots]
        if len(self.order) < 2:
            raise ValueError("Snakes & Ladders needs at least 2 players")
        self.names: dict[str, str] = {pid: name for pid, name in contestants + bots}

        self.rng = rng if rng is not None else random.Random()
        self.cells = cells
        self.cols = cols
        self.dice_sides = dice_sides
        self.exact_finish = exact_finish
        self.prices = dict(prices) if prices else dict(DEFAULT_PRICES)
        self.boost_bonus = boost_bonus
        self.gold_tile_amount = gold_tile_amount
        self.slip_back = slip_back
        self.gold_tax = gold_tax

        # Per-player state, keyed by id (survives a room slot expiring).
        self.pos: dict[str, int] = {pid: 1 for pid in self.order}
        self.gold: dict[str, int] = {pid: starting_gold for pid in self.order}
        self.items: dict[str, list[str]] = {pid: [] for pid in self.order}
        self.skips: dict[str, int] = {pid: 0 for pid in self.order}

        self.phase = protocol.PHASE_PLAY
        self.awaiting = AWAIT_ROLL
        self.current_pid = self.order[0]
        # Set by the connection layer to a per-turn deadline so clients can show a
        # countdown; ``None`` on a bot/absent turn (auto-played after a short delay).
        self.deadline: Optional[float] = None
        self.seq = 0
        self.last_turn: Optional[dict[str, Any]] = None
        self.winner_id: Optional[str] = None
        self._reset_pending()

        self._generate_board(
            snake_count, ladder_count, shop_count, wheel_count, gold_count, debuff_count
        )

    # --- board generation -------------------------------------------------

    def _generate_board(
        self, snake_count, ladder_count, shop_count, wheel_count, gold_count, debuff_count
    ) -> None:
        """Freshly randomize the board. Every special occupies a *distinct* cell
        drawn from 2..cells-1 (cells 1 and ``cells`` are reserved), so no snake
        tail / ladder top is ever itself a special — that disjointness is what
        guarantees ``roll()`` never chains transports or tiles."""
        usable = list(range(2, self.cells))  # exclude start (1) and finish (cells)
        needed = (
            2 * snake_count + 2 * ladder_count
            + shop_count + wheel_count + gold_count + debuff_count
        )
        if needed > len(usable):
            raise ValueError(
                f"board too small: need {needed} special cells, have {len(usable)}"
            )
        self.rng.shuffle(usable)
        cells = iter(usable)

        self.snakes: dict[int, int] = {}
        for _ in range(snake_count):
            a, b = next(cells), next(cells)
            self.snakes[max(a, b)] = min(a, b)  # head (high) -> tail (low)
        self.ladders: dict[int, int] = {}
        for _ in range(ladder_count):
            a, b = next(cells), next(cells)
            self.ladders[min(a, b)] = max(a, b)  # bottom (low) -> top (high)
        self.wheel_tiles: list[int] = [next(cells) for _ in range(wheel_count)]
        self.shop_tiles: list[int] = [next(cells) for _ in range(shop_count)]
        self.gold_tiles: list[int] = [next(cells) for _ in range(gold_count)]
        self.debuff_tiles: list[int] = [next(cells) for _ in range(debuff_count)]

    # --- pending pre-roll modifiers --------------------------------------

    def _reset_pending(self) -> None:
        self._p_immunity = False
        self._p_boost = False
        self._p_double = False
        self._p_reroll = False

    _PENDING_FLAG = {
        "immunity": "_p_immunity",
        "boost": "_p_boost",
        "double": "_p_double",
        "reroll": "_p_reroll",
    }

    # --- contract methods used by the connection layer --------------------

    @property
    def is_over(self) -> bool:
        return self.phase == protocol.PHASE_GAMEOVER

    def is_contestant(self, pid: Optional[str]) -> bool:
        """Only *humans* in the game are contestants (bots are not)."""
        return pid in self.contestant_ids

    def is_current(self, pid: Optional[str]) -> bool:
        return pid == self.current_pid and not self.is_over

    def all_submitted(self, connected_ids: set[str]) -> bool:
        """Always False: this is a turn game, so the connection layer never
        "fast-forwards because everyone acted" — it drives turns with per-actor
        deadlines and bot delays instead. Present to satisfy the game contract."""
        return False

    @property
    def winner(self) -> Optional[dict[str, str]]:
        if self.winner_id is None:
            return None
        return {"id": self.winner_id, "name": self.names[self.winner_id]}

    # --- player actions ---------------------------------------------------

    def use_powerup(self, pid: str, item: Any) -> bool:
        """Arm a held powerup for the upcoming roll. Pre-roll only; consumes the
        item immediately and does NOT pass the turn."""
        if self.is_over or pid != self.current_pid or self.awaiting != AWAIT_ROLL:
            return False
        if not isinstance(item, str):  # wire input may be any JSON value; reject, don't crash
            return False
        flag = self._PENDING_FLAG.get(item)
        if flag is None or item not in self.items[pid]:
            return False
        if getattr(self, flag):  # already armed this turn
            return False
        setattr(self, flag, True)
        self.items[pid].remove(item)
        return True

    def roll(self, pid: str) -> bool:
        """Resolve the current player's whole turn into ``last_turn``.

        Canonical step order (easy to get wrong — do not reorder):
          1. roll the die (reroll keeps the higher of two; then boost +N, then double x2)
          2. move along a path; if exact-finish overshoots the last cell, bounce back
          3. at most ONE transport at the landing cell — snake XOR ladder (immunity
             blocks a snake). Board-gen guarantees the destination isn't special, so
             nothing chains.
          4. tile at the (post-transport) cell: wheel / gold / debuff / shop-enter
          5. win if the cell is exactly the last cell
          6. pass the turn to the next non-skipped player (unless shopping / game over)
        """
        if self.is_over or pid != self.current_pid or self.awaiting != AWAIT_ROLL:
            return False
        steps: list[dict[str, Any]] = []

        # 1. dice (+ modifiers)
        raw = self.rng.randint(1, self.dice_sides)
        mods: list[str] = []
        if self._p_reroll:
            raw = max(raw, self.rng.randint(1, self.dice_sides))
            mods.append("reroll")
        die = raw
        if self._p_boost:
            die += self.boost_bonus
            mods.append("boost")
        if self._p_double:
            die *= 2
            mods.append("double")
        steps.append({"t": "roll", "raw": raw, "die": die, "modifier": "+".join(mods) or None})

        # 2. move (with exact-finish bounce)
        frm = self.pos[pid]
        last = self.cells
        raw_target = frm + die
        if raw_target > last and self.exact_finish:
            overshoot = raw_target - last
            to = max(1, last - overshoot)  # bounce: reflect off the final cell
            path = list(range(frm + 1, last + 1)) + list(range(last - 1, to - 1, -1))
        else:
            to = min(raw_target, last)
            path = list(range(frm + 1, to + 1))
        self.pos[pid] = to
        steps.append({"t": "move", "frm": frm, "to": to, "path": path})

        # 3. transport (at most one; immunity blocks a snake)
        landing = to
        transported = False
        if landing in self.snakes:
            if self._p_immunity:
                steps.append({"t": "immunity_used", "at": landing})
            else:
                tail = self.snakes[landing]
                self.pos[pid] = tail
                steps.append({"t": "snake", "frm": landing, "to": tail})
                landing = tail
                transported = True
        elif landing in self.ladders:
            top = self.ladders[landing]
            self.pos[pid] = top
            steps.append({"t": "ladder", "frm": landing, "to": top})
            landing = top
            transported = True

        # 4. tile effect. Board-gen guarantees a transport destination is never a
        #    special cell, so a tile can only fire when no transport happened.
        #    Gating on ``not transported`` *enforces* "at most one transport, no
        #    chaining" locally instead of trusting that board-gen invariant from afar.
        entered_shop = False
        if not transported and landing != last:
            if landing in self.wheel_tiles:
                steps.append({"t": "tile", "kind": "wheel"})
                self._spin_wheel(pid, steps)
            elif landing in self.gold_tiles:
                steps.append({"t": "tile", "kind": "gold"})
                self._grant_gold(pid, self.gold_tile_amount, steps)
            elif landing in self.debuff_tiles:
                steps.append({"t": "tile", "kind": "debuff"})
                self._apply_debuff(pid, DEBUFFS[self.rng.randrange(len(DEBUFFS))], steps)
            elif landing in self.shop_tiles:
                steps.append({"t": "tile", "kind": "shop"})
                steps.append({"t": "shop_enter", "pid": pid})
                self.awaiting = AWAIT_SHOP
                entered_shop = True

        # 5. win
        if landing == last:
            self.winner_id = pid
            self.phase = protocol.PHASE_GAMEOVER
            steps.append({"t": "win", "pid": pid, "name": self.names[pid]})

        self._reset_pending()  # modifiers are spent on the roll they armed

        # 6. pass the turn unless the player is now shopping or the game is over
        if not entered_shop and not self.is_over:
            self._advance_turn(steps)
        self._commit_turn(pid, steps, ended=not entered_shop)
        return True

    def buy_item(self, pid: str, item: Any) -> bool:
        """Buy a powerup in the shop sub-state; spends gold and passes the turn."""
        if self.is_over or pid != self.current_pid or self.awaiting != AWAIT_SHOP:
            return False
        if not isinstance(item, str):  # wire input may be any JSON value; reject, don't crash
            return False
        price = self.prices.get(item)
        if price is None or self.gold[pid] < price:
            return False
        self.gold[pid] -= price
        self.items[pid].append(item)
        steps = [{"t": "buy", "pid": pid, "item": item, "price": price, "total": self.gold[pid]}]
        self._advance_turn(steps)
        self._commit_turn(pid, steps, ended=True)
        return True

    def skip_shop(self, pid: str) -> bool:
        """Leave the shop without buying; passes the turn."""
        if self.is_over or pid != self.current_pid or self.awaiting != AWAIT_SHOP:
            return False
        steps = [{"t": "shop_skip", "pid": pid}]
        self._advance_turn(steps)
        self._commit_turn(pid, steps, ended=True)
        return True

    def advance(self) -> str:
        """Host force-advance / auto-timeout: resolve the current actor's pending
        action (roll, or skip the shop). Idempotent once the game is over."""
        if self.is_over:
            return self.phase
        if self.awaiting == AWAIT_SHOP:
            self.skip_shop(self.current_pid)
        else:
            self.roll(self.current_pid)
        return self.phase

    def bot_action(self, pid: str) -> dict[str, Any]:
        """Pure: what a trivial bot would do right now (no mutation). The driver
        translates this into a roll/buy/skip call."""
        if not self.is_current(pid):
            return {"kind": "noop"}
        if self.awaiting == AWAIT_SHOP:
            affordable = sorted(
                (it for it, pr in self.prices.items() if self.gold[pid] >= pr),
                key=lambda it: self.prices[it],
            )
            if affordable:
                return {"kind": "buy", "item": affordable[0]}
            return {"kind": "skip"}
        return {"kind": "roll"}

    # --- turn resolution helpers -----------------------------------------

    def _spin_wheel(self, pid: str, steps: list[dict[str, Any]]) -> None:
        index = self.rng.randrange(len(WHEEL_OUTCOMES))
        outcome = WHEEL_OUTCOMES[index]
        # Embed *copies*, never the module-global table/dicts: the server is
        # long-lived, so a consumer that mutates a step must not corrupt the
        # shared catalog (and therefore every future game's wheel).
        steps.append({
            "t": "wheel",
            "table": [dict(o) for o in WHEEL_OUTCOMES],
            "index": index,
            "outcome": dict(outcome),
        })
        kind = outcome["kind"]
        if kind == "gold":
            self._grant_gold(pid, outcome["amount"], steps)
        elif kind == "item":
            self.items[pid].append(outcome["item"])
            steps.append({"t": "item", "pid": pid, "item": outcome["item"]})
        elif kind == "debuff":
            self._apply_debuff(pid, outcome["debuff"], steps)

    def _grant_gold(self, pid: str, amount: int, steps: list[dict[str, Any]]) -> None:
        self.gold[pid] += amount
        steps.append({"t": "gold", "pid": pid, "delta": amount, "total": self.gold[pid]})

    def _apply_debuff(self, pid: str, debuff: str, steps: list[dict[str, Any]]) -> None:
        if debuff == "skip_next":
            self.skips[pid] += 1
            steps.append({"t": "debuff", "pid": pid, "debuff": "skip_next"})
        elif debuff == "slip_back":
            before = self.pos[pid]
            self.pos[pid] = max(1, before - self.slip_back)
            steps.append(
                {"t": "debuff", "pid": pid, "debuff": "slip_back", "frm": before, "to": self.pos[pid]}
            )
        elif debuff == "gold_tax":
            before = self.gold[pid]
            self.gold[pid] = max(0, before - self.gold_tax)
            steps.append(
                {"t": "debuff", "pid": pid, "debuff": "gold_tax",
                 "delta": self.gold[pid] - before, "total": self.gold[pid]}
            )

    def _advance_turn(self, steps: list[dict[str, Any]]) -> None:
        """Move ``current_pid`` to the next player who isn't skipped, consuming one
        ``skip_next`` per skipped player and logging it. If every *other* player is
        skipped this round, the mover takes another turn."""
        n = len(self.order)
        idx = self.order.index(self.current_pid)
        for offset in range(1, n):
            cand = self.order[(idx + offset) % n]
            if self.skips.get(cand, 0) > 0:
                self.skips[cand] -= 1
                steps.append({"t": "skipped", "pid": cand, "name": self.names[cand]})
                continue
            self.current_pid = cand
            break
        else:
            self.current_pid = self.order[idx]  # everyone else skipped -> go again
        self.awaiting = AWAIT_ROLL
        self._reset_pending()

    def _commit_turn(self, pid: str, steps: list[dict[str, Any]], ended: bool) -> None:
        self.seq += 1
        self.last_turn = {
            "seq": self.seq,
            "pid": pid,
            "name": self.names[pid],
            "ended": ended,
            "steps": steps,
        }

    # --- serialization ----------------------------------------------------

    def public(self, for_pid: Optional[str]) -> dict[str, Any]:
        """Per-player view (contestant / spectator). The only secret is the shop
        stock, sent solely to the current player while they are shopping."""
        role = "contestant" if self.is_contestant(for_pid) else "spectator"

        data: dict[str, Any] = {
            "name": protocol.GAME_SNAKES_AND_LADDERS,
            "phase": self.phase,
            "awaiting": self.awaiting,
            # snakes/ladders are int-keyed (the plan's shape); JSON encoding turns
            # those keys into strings on the wire, so the client looks them up by
            # str(cell). Every step in the timeline carries cells as ints.
            "board": {
                "cells": self.cells,
                "cols": self.cols,
                "snakes": dict(self.snakes),
                "ladders": dict(self.ladders),
                "wheel_tiles": list(self.wheel_tiles),
                "shop_tiles": list(self.shop_tiles),
                "gold_tiles": list(self.gold_tiles),
                "debuff_tiles": list(self.debuff_tiles),
            },
            "players": [
                {
                    "id": pid,
                    "name": self.names[pid],
                    "pos": self.pos[pid],
                    "gold": self.gold[pid],
                    "items": list(self.items[pid]),
                    "is_bot": pid in self.bot_ids,
                    "finished": pid == self.winner_id,
                }
                for pid in self.order
            ],
            "current_pid": self.current_pid,
            "your_turn": self.is_current(for_pid),
            "your_id": for_pid,
            "you_role": role,
            "deadline": self.deadline,
            "last_turn": self.last_turn,
            "winner": self.winner,
        }

        if self.is_current(for_pid):
            if self.awaiting == AWAIT_SHOP:
                data["shop"] = {
                    "stock": [
                        {"item": it, "price": pr, "affordable": self.gold[for_pid] >= pr}
                        for it, pr in self.prices.items()
                    ]
                }
            else:
                data["your_armed"] = [
                    name for name, flag in self._PENDING_FLAG.items() if getattr(self, flag)
                ]

        return data


# The powerup catalog is spelled in three places that must agree: POWERUPS (the
# canonical names), _PENDING_FLAG (name -> arming flag), and DEFAULT_PRICES (name
# -> shop price). Pin them at import so adding/renaming a powerup can't silently
# drift one out of sync.
assert set(POWERUPS) == set(DEFAULT_PRICES) == set(SnakesAndLaddersGame._PENDING_FLAG), (
    "powerup catalogs out of sync: POWERUPS / DEFAULT_PRICES / _PENDING_FLAG"
)
