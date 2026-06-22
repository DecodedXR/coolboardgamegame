"""Unit tests for the pure Snakes & Ladders game logic (no server, no sockets).

The engine is authoritative and deterministic: a seeded ``random.Random`` drives
board generation and dice, and every turn resolves into an ordered ``last_turn``
timeline the client later replays as animation. These tests pin the parts the
plan flags as easy to get wrong — ``roll()`` step ordering, the exact-finish
bounce, transport (snake/ladder) gating, the shop sub-state, powerups/debuffs,
turn skipping, and ``seq`` monotonicity.
"""

from __future__ import annotations

import random

import pytest

from server.games.snakes_and_ladders import (
    DEBUFFS,
    DEFAULT_PRICES,
    POWERUPS,
    WHEEL_OUTCOMES,
    SnakesAndLaddersGame,
)
from shared import protocol


def make_game(humans=2, bots=0, seed=1234, **kw):
    contestants = [(f"p{i}", f"P{i}") for i in range(humans)]
    bot_seats = [(f"b{i}", f"Bot{i}") for i in range(bots)]
    return SnakesAndLaddersGame(contestants, bots=bot_seats, rng=random.Random(seed), **kw)


class FixedRng:
    """Scripted RNG for deterministic turn tests.

    ``ints`` feeds ``randint`` (dice, possibly two for a reroll); ``ranges`` feeds
    ``randrange`` (wheel index, random-debuff pick). Board generation uses the
    real seeded RNG at construction time, so tests swap this in *after* building
    and overriding the board for a controlled scenario.
    """

    def __init__(self, ints=(), ranges=()):
        self._ints = list(ints)
        self._ranges = list(ranges)

    def randint(self, a, b):
        return self._ints.pop(0)

    def randrange(self, n):
        return self._ranges.pop(0)

    def shuffle(self, seq):  # pragma: no cover - only used during construction
        pass


def test_await_substates_are_single_sourced_in_protocol():
    # The "roll"/"shop" awaiting sub-state values are a wire contract shared by the
    # server (authority) and the client (replay UI), so they live ONCE in
    # shared.protocol; both layers must reference that single source rather than
    # re-declaring the literals (which previously sat in both modules and could drift).
    from client.scenes import snakes_and_ladders as client_sal  # local: pulls pygame
    from server.games import snakes_and_ladders as server_sal

    assert (protocol.AWAIT_ROLL, protocol.AWAIT_SHOP) == ("roll", "shop")
    assert (server_sal.AWAIT_ROLL, server_sal.AWAIT_SHOP) \
        == (protocol.AWAIT_ROLL, protocol.AWAIT_SHOP)
    assert (client_sal.AWAIT_ROLL, client_sal.AWAIT_SHOP) \
        == (protocol.AWAIT_ROLL, protocol.AWAIT_SHOP)


def clear_board(g):
    """Strip every special off the board so a scenario can place its own."""
    g.snakes = {}
    g.ladders = {}
    g.wheel_tiles = []
    g.shop_tiles = []
    g.gold_tiles = []
    g.debuff_tiles = []


def step_kinds(g):
    return [s["t"] for s in g.last_turn["steps"]]


# --- board generation -----------------------------------------------------


def test_board_invariants():
    g = make_game()
    assert len(g.snakes) == 14 and len(g.ladders) == 4
    assert len(g.snakes) > len(g.ladders)  # snake-heavy
    # Snakes go down (head > tail); ladders go up (bottom < top).
    for head, tail in g.snakes.items():
        assert head > tail
    for bottom, top in g.ladders.items():
        assert bottom < top
    # Every special sits on its own distinct cell (no chaining possible).
    cells = (
        list(g.snakes) + list(g.snakes.values())
        + list(g.ladders) + list(g.ladders.values())
        + g.wheel_tiles + g.shop_tiles + g.gold_tiles + g.debuff_tiles
    )
    assert len(cells) == len(set(cells))
    # Nothing on the start (1) or finish (last) cell.
    assert 1 not in cells and g.cells not in cells
    assert all(2 <= c <= g.cells - 1 for c in cells)
    assert len(g.wheel_tiles) == 5 and len(g.shop_tiles) == 4
    assert len(g.gold_tiles) == 6 and len(g.debuff_tiles) == 5


def test_board_deterministic_per_seed():
    a, b = make_game(seed=7), make_game(seed=7)
    assert (a.snakes, a.ladders, a.wheel_tiles, a.shop_tiles, a.gold_tiles, a.debuff_tiles) == (
        b.snakes, b.ladders, b.wheel_tiles, b.shop_tiles, b.gold_tiles, b.debuff_tiles
    )
    # A different seed yields a different board (collisions are astronomically rare).
    c = make_game(seed=8)
    assert (c.snakes, c.ladders) != (a.snakes, a.ladders)


def test_board_too_small_rejected():
    with pytest.raises(ValueError):
        make_game(cells=10, snake_count=14)


def test_needs_two_players():
    with pytest.raises(ValueError):
        SnakesAndLaddersGame([("solo", "Solo")], rng=random.Random(0))


# --- dice / movement ------------------------------------------------------


def test_simple_move_advances_and_passes_turn():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[3])
    assert g.roll("p0") is True
    assert g.pos["p0"] == 13
    assert step_kinds(g) == ["roll", "move"]
    roll = g.last_turn["steps"][0]
    assert roll["die"] == 3 and roll["raw"] == 3 and roll["modifier"] is None
    assert g.last_turn["steps"][1]["path"] == [11, 12, 13]
    assert g.last_turn["ended"] is True
    assert g.current_pid == "p1"  # turn passed
    assert g.seq == 1


def test_dice_sequence_is_deterministic_per_seed():
    # Same seed -> identical board AND identical dice/positions across a playout;
    # a different seed diverges. (Replay safety depends on this.)
    def playout(seed):
        g = make_game(seed=seed)
        log = []
        for _ in range(40):
            cur = g.current_pid
            if g.awaiting == "shop":
                g.skip_shop(cur)
            else:
                g.roll(cur)
            log.append((cur, g.last_turn["seq"], g.pos[cur]))
            if g.is_over:
                break
        return log

    assert playout(99) == playout(99)
    assert playout(99) != playout(100)


# --- transport: snakes & ladders -----------------------------------------


def test_snake_slides_down():
    g = make_game()
    clear_board(g)
    g.snakes = {15: 4}
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[5])
    g.roll("p0")
    assert g.pos["p0"] == 4
    assert step_kinds(g) == ["roll", "move", "snake"]
    assert g.last_turn["steps"][2] == {"t": "snake", "frm": 15, "to": 4}


def test_ladder_climbs_up():
    g = make_game()
    clear_board(g)
    g.ladders = {12: 30}
    g.pos["p0"] = 9
    g.rng = FixedRng(ints=[3])
    g.roll("p0")
    assert g.pos["p0"] == 30
    assert step_kinds(g) == ["roll", "move", "ladder"]


# --- exact-finish bounce --------------------------------------------------


def test_exact_finish_bounces_back():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 98
    g.rng = FixedRng(ints=[5])  # 98 + 5 = 103 -> overshoot 3 -> bounce to 97
    g.roll("p0")
    assert g.pos["p0"] == 97
    move = g.last_turn["steps"][1]
    assert move["to"] == 97
    assert move["path"] == [99, 100, 99, 98, 97]
    assert not g.is_over


def test_landing_exactly_on_last_wins():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 95
    g.rng = FixedRng(ints=[5])  # exactly 100
    g.roll("p0")
    assert g.pos["p0"] == 100
    assert g.is_over and g.phase == protocol.PHASE_GAMEOVER
    assert g.winner == {"id": "p0", "name": "P0"}
    assert "win" in step_kinds(g)


def test_bounce_into_snake_then_slides():
    g = make_game()
    clear_board(g)
    g.snakes = {97: 5}
    g.pos["p0"] = 98
    g.rng = FixedRng(ints=[5])  # bounce to 97, which is a snake head
    g.roll("p0")
    assert g.pos["p0"] == 5
    assert step_kinds(g) == ["roll", "move", "snake"]


# --- tiles: gold / wheel / debuff ----------------------------------------


def test_gold_tile_pays_out():
    g = make_game()
    clear_board(g)
    g.gold_tiles = [14]
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[4])
    g.roll("p0")
    assert g.gold["p0"] == 100 + g.gold_tile_amount
    assert step_kinds(g) == ["roll", "move", "tile", "gold"]
    assert g.last_turn["steps"][2] == {"t": "tile", "kind": "gold"}


def test_wheel_tile_canonical_step_order():
    g = make_game()
    clear_board(g)
    g.wheel_tiles = [14]
    g.pos["p0"] = 10
    gold_idx = next(i for i, o in enumerate(WHEEL_OUTCOMES) if o["kind"] == "gold")
    g.rng = FixedRng(ints=[4], ranges=[gold_idx])
    g.roll("p0")
    assert step_kinds(g) == ["roll", "move", "tile", "wheel", "gold"]
    wheel = g.last_turn["steps"][3]
    assert wheel["index"] == gold_idx
    assert wheel["table"] == WHEEL_OUTCOMES
    assert wheel["outcome"] == WHEEL_OUTCOMES[gold_idx]
    assert g.gold["p0"] == 100 + WHEEL_OUTCOMES[gold_idx]["amount"]


def test_wheel_can_grant_an_item():
    g = make_game()
    clear_board(g)
    g.wheel_tiles = [14]
    g.pos["p0"] = 10
    item_idx = next(i for i, o in enumerate(WHEEL_OUTCOMES) if o["kind"] == "item")
    g.rng = FixedRng(ints=[4], ranges=[item_idx])
    g.roll("p0")
    assert WHEEL_OUTCOMES[item_idx]["item"] in g.items["p0"]
    assert step_kinds(g)[-1] == "item"


def test_debuff_tile_slip_back():
    g = make_game()
    clear_board(g)
    g.debuff_tiles = [20]
    g.pos["p0"] = 16
    g.rng = FixedRng(ints=[4], ranges=[DEBUFFS.index("slip_back")])
    g.roll("p0")
    assert g.pos["p0"] == 20 - g.slip_back  # 14
    last = g.last_turn["steps"][-1]
    assert last["debuff"] == "slip_back" and last["to"] == 14


def test_debuff_tile_gold_tax():
    g = make_game()
    clear_board(g)
    g.debuff_tiles = [20]
    g.pos["p0"] = 16
    g.rng = FixedRng(ints=[4], ranges=[DEBUFFS.index("gold_tax")])
    g.roll("p0")
    assert g.gold["p0"] == 100 - g.gold_tax


def test_debuff_tile_skip_next_marks_skip():
    g = make_game()
    clear_board(g)
    g.debuff_tiles = [20]
    g.pos["p0"] = 16
    g.rng = FixedRng(ints=[4], ranges=[DEBUFFS.index("skip_next")])
    g.roll("p0")
    assert g.skips["p0"] == 1
    assert g.last_turn["steps"][-1]["debuff"] == "skip_next"


def test_gold_tax_floors_at_zero():
    g = make_game()
    clear_board(g)
    g.debuff_tiles = [20]
    g.pos["p0"] = 16
    g.gold["p0"] = 10
    g.rng = FixedRng(ints=[4], ranges=[DEBUFFS.index("gold_tax")])
    g.roll("p0")
    assert g.gold["p0"] == 0


# --- shop sub-state -------------------------------------------------------


def enter_shop(g, pid="p0", at=20, frm=16):
    clear_board(g)
    g.shop_tiles = [at]
    g.pos[pid] = frm
    g.rng = FixedRng(ints=[at - frm])
    g.roll(pid)


def test_shop_enter_pauses_turn():
    g = make_game()
    enter_shop(g)
    assert g.awaiting == "shop"
    assert g.current_pid == "p0"  # turn did NOT pass
    assert g.last_turn["ended"] is False
    assert step_kinds(g) == ["roll", "move", "tile", "shop_enter"]


def test_shop_stock_is_secret_to_others():
    g = make_game()
    enter_shop(g)
    me = g.public("p0", host_id=None)
    other = g.public("p1", host_id=None)
    assert "shop" in me and me["shop"]["stock"]
    assert "shop" not in other


def test_buy_spends_gold_and_passes_turn():
    g = make_game()
    enter_shop(g)
    before_seq = g.seq
    assert g.buy_item("p0", "boost") is True
    assert g.gold["p0"] == 100 - DEFAULT_PRICES["boost"]
    assert "boost" in g.items["p0"]
    assert g.awaiting == "roll" and g.current_pid == "p1"
    assert g.seq == before_seq + 1
    assert g.last_turn["steps"][0]["t"] == "buy"


def test_buy_rejects_unaffordable_unknown_and_out_of_substate():
    g = make_game()
    enter_shop(g)
    g.gold["p0"] = 5
    assert g.buy_item("p0", "double") is False  # can't afford
    assert g.buy_item("p0", "banana") is False  # unknown item
    assert g.gold["p0"] == 5 and g.awaiting == "shop"  # nothing happened
    # A successful skip ends the sub-state; further shop actions are rejected.
    assert g.skip_shop("p0") is True
    assert g.buy_item("p0", "boost") is False  # no longer shopping
    assert g.skip_shop("p0") is False


def test_skip_shop_passes_turn():
    g = make_game()
    enter_shop(g)
    assert g.skip_shop("p0") is True
    assert g.awaiting == "roll" and g.current_pid == "p1"
    assert g.last_turn["steps"][0]["t"] == "shop_skip"


# --- powerups -------------------------------------------------------------


def test_boost_adds_to_roll():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 10
    g.items["p0"] = ["boost"]
    assert g.use_powerup("p0", "boost") is True
    assert g.items["p0"] == []  # consumed at use
    g.rng = FixedRng(ints=[4])
    g.roll("p0")
    assert g.pos["p0"] == 10 + 4 + g.boost_bonus  # 17
    assert g.last_turn["steps"][0]["modifier"] == "boost"


def test_double_doubles_roll():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 10
    g.items["p0"] = ["double"]
    g.use_powerup("p0", "double")
    g.rng = FixedRng(ints=[4])
    g.roll("p0")
    assert g.pos["p0"] == 10 + 8


def test_boost_and_double_stack_boost_then_double():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 10
    g.items["p0"] = ["boost", "double"]
    g.use_powerup("p0", "boost")
    g.use_powerup("p0", "double")
    g.rng = FixedRng(ints=[4])
    g.roll("p0")
    # (4 + 3) * 2 = 14
    assert g.last_turn["steps"][0]["die"] == 14
    assert g.pos["p0"] == 24


def test_reroll_keeps_higher_of_two_dice():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 10
    g.items["p0"] = ["reroll"]
    g.use_powerup("p0", "reroll")
    g.rng = FixedRng(ints=[2, 5])  # keep the 5
    g.roll("p0")
    assert g.pos["p0"] == 15
    assert g.last_turn["steps"][0]["raw"] == 5
    assert "reroll" in g.last_turn["steps"][0]["modifier"]


def test_immunity_blocks_one_snake_and_is_consumed():
    g = make_game()
    clear_board(g)
    g.snakes = {15: 4}
    g.pos["p0"] = 10
    g.items["p0"] = ["immunity"]
    g.use_powerup("p0", "immunity")
    assert g.items["p0"] == []
    g.rng = FixedRng(ints=[5])
    g.roll("p0")
    assert g.pos["p0"] == 15  # snake blocked, stayed on the head
    assert step_kinds(g) == ["roll", "move", "immunity_used"]


def test_powerup_catalogs_agree():
    # POWERUPS is the canonical name list; prices and arming flags must match it.
    assert set(POWERUPS) == set(DEFAULT_PRICES)
    assert set(POWERUPS) == set(SnakesAndLaddersGame._PENDING_FLAG)


def test_use_powerup_requires_holding_it_and_your_turn():
    g = make_game()
    clear_board(g)
    assert g.use_powerup("p0", "boost") is False  # not held
    assert g.use_powerup("p1", "boost") is False  # not p1's turn
    g.items["p0"] = ["boost"]
    assert g.use_powerup("p0", "boost") is True
    assert g.use_powerup("p0", "boost") is False  # already armed this turn


# --- turn skipping --------------------------------------------------------


def test_skip_next_skips_that_players_turn():
    g = make_game(humans=3)
    clear_board(g)
    g.skips["p1"] = 1
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[3])
    g.roll("p0")  # p0 -> would be p1, but p1 is skipped -> p2
    assert g.current_pid == "p2"
    assert g.skips["p1"] == 0
    assert {"t": "skipped", "pid": "p1", "name": "P1"} in g.last_turn["steps"]


def test_two_player_skip_returns_to_mover():
    g = make_game(humans=2)
    clear_board(g)
    g.skips["p1"] = 1
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[3])
    g.roll("p0")  # only other player is skipped -> p0 goes again
    assert g.current_pid == "p0"
    assert g.skips["p1"] == 0


# --- turn / substate guards ----------------------------------------------


def test_rejects_out_of_turn_and_wrong_substate():
    g = make_game()
    clear_board(g)
    assert g.roll("p1") is False  # not p1's turn
    assert g.buy_item("p0", "boost") is False  # awaiting roll, not shop
    assert g.skip_shop("p0") is False
    # And rolling / arming a powerup while shopping is rejected (wrong sub-state).
    enter_shop(g)
    g.items["p0"] = ["boost"]
    assert g.roll("p0") is False
    assert g.use_powerup("p0", "boost") is False
    assert g.items["p0"] == ["boost"]  # nothing consumed


def test_unhashable_item_is_rejected_not_crashed():
    # The wire delivers ``item`` as any JSON value; a list/dict must be rejected
    # with False, never raise TypeError out of dict.get(unhashable_key).
    g = make_game()
    clear_board(g)
    g.items["p0"] = ["boost"]
    assert g.use_powerup("p0", ["boost"]) is False
    assert g.use_powerup("p0", {"x": 1}) is False
    assert g.items["p0"] == ["boost"]  # not consumed
    enter_shop(g)
    assert g.buy_item("p0", ["boost"]) is False
    assert g.buy_item("p0", {"x": 1}) is False
    assert g.awaiting == "shop" and g.current_pid == "p0"  # untouched


def test_wheel_step_does_not_alias_module_catalog():
    g = make_game()
    clear_board(g)
    g.wheel_tiles = [14]
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[4], ranges=[0])  # index 0 = a gold outcome
    g.roll("p0")
    wheel = g.last_turn["steps"][3]
    assert wheel["t"] == "wheel"
    assert wheel["table"] is not WHEEL_OUTCOMES
    assert wheel["outcome"] is not WHEEL_OUTCOMES[0]
    # Mutating the emitted step must not corrupt the shared catalog.
    wheel["table"][0]["amount"] = 999999
    wheel["outcome"]["kind"] = "HACKED"
    assert WHEEL_OUTCOMES[0] == {"kind": "gold", "amount": 50}


def test_bounce_lands_on_a_tile_and_fires_it():
    # The spec's #1 risk: a tile must fire on the *post-bounce* landing cell.
    g = make_game()
    clear_board(g)
    g.gold_tiles = [97]
    g.pos["p0"] = 98
    g.rng = FixedRng(ints=[5])  # 98 + 5 -> bounce to 97 (a gold tile)
    g.roll("p0")
    assert g.pos["p0"] == 97
    assert step_kinds(g) == ["roll", "move", "tile", "gold"]
    assert g.gold["p0"] == 100 + g.gold_tile_amount


def test_tile_does_not_chain_after_a_transport():
    # Force an overlap board-gen would never produce (snake tail is also a wheel
    # tile); the engine must NOT fire the tile after the snake (no chaining).
    g = make_game()
    clear_board(g)
    g.snakes = {15: 8}
    g.wheel_tiles = [8]
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[5], ranges=[0])
    g.roll("p0")
    assert g.pos["p0"] == 8
    assert step_kinds(g) == ["roll", "move", "snake"]  # no tile/wheel step


# --- bots -----------------------------------------------------------------


def test_bot_action_rolls_then_shops():
    g = make_game(humans=1, bots=1)
    g.current_pid = "b0"
    g.awaiting = "roll"
    assert g.bot_action("b0") == {"kind": "roll"}
    g.awaiting = "shop"
    g.gold["b0"] = 100
    act = g.bot_action("b0")
    assert act["kind"] == "buy" and act["item"] in DEFAULT_PRICES
    g.gold["b0"] = 5  # can't afford anything
    assert g.bot_action("b0") == {"kind": "skip"}


def test_bot_action_noop_when_not_actor():
    g = make_game(humans=1, bots=1)
    assert g.bot_action("b0")["kind"] == "noop"  # p0 is current


def test_bots_are_players_but_not_contestants():
    g = make_game(humans=2, bots=1)
    assert g.is_contestant("p0") is True
    assert g.is_contestant("b0") is False
    assert "b0" in g.order and "b0" in g.bot_ids


# --- serialization & contract --------------------------------------------


def test_public_roles_and_player_rows():
    g = make_game(humans=2, bots=1)
    assert g.public("p0", host_id="hostX")["you_role"] == "contestant"
    assert g.public("hostX", host_id="hostX")["you_role"] == "host"
    assert g.public("nobody", host_id="hostX")["you_role"] == "spectator"
    view = g.public("p0", host_id=None)
    assert view["name"] == protocol.GAME_SNAKES_AND_LADDERS
    rows = {r["id"]: r for r in view["players"]}
    assert set(rows) == {"p0", "p1", "b0"}
    assert rows["b0"]["is_bot"] is True and rows["p0"]["is_bot"] is False
    for r in rows.values():
        assert set(r) >= {"id", "name", "pos", "gold", "items", "is_bot", "finished"}
    assert view["your_turn"] is True  # p0 is current
    assert g.public("p1", host_id=None)["your_turn"] is False


def test_all_submitted_is_always_false():
    g = make_game()
    assert g.all_submitted(set()) is False
    assert g.all_submitted({"p0", "p1"}) is False


def test_seq_is_monotonic_across_actions():
    g = make_game(humans=3)
    clear_board(g)
    seqs = []
    g.pos["p0"] = 5
    g.rng = FixedRng(ints=[3])
    g.roll("p0")
    seqs.append(g.seq)
    g.pos["p1"] = 5
    g.rng = FixedRng(ints=[3])
    g.roll("p1")
    seqs.append(g.seq)
    assert seqs == sorted(set(seqs)) and seqs[0] < seqs[1]
    assert g.last_turn["seq"] == g.seq


def test_advance_force_resolves_current_actor():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 10
    g.rng = FixedRng(ints=[3])
    phase = g.advance()  # host force-advance: auto-roll the current actor
    assert phase == protocol.PHASE_PLAY
    assert g.current_pid == "p1"  # a turn was resolved
    # In the shop sub-state, advance auto-skips.
    enter_shop(g, pid="p1", at=20, frm=16)
    g.advance()
    assert g.awaiting == "roll" and g.current_pid == "p0"


def test_terminal_state_is_a_no_op():
    g = make_game()
    clear_board(g)
    g.pos["p0"] = 95
    g.rng = FixedRng(ints=[5])
    g.roll("p0")  # win
    assert g.is_over
    seq_at_win = g.seq
    assert g.roll("p1") is False
    assert g.buy_item("p0", "boost") is False
    assert g.use_powerup("p0", "boost") is False
    assert g.advance() == protocol.PHASE_GAMEOVER
    assert g.seq == seq_at_win  # nothing mutated after game over
