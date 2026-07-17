"""Word Bomb: pure-rules unit tests + headless driver-integration tests.

Group (a) constructs :class:`WordBombGame` directly with a toy dictionary and
pins the rule contract (accept/reject, the anti-fuse-reset ``seq`` asymmetry, the
feed ``id`` dedup, elimination/win, bot policy). Group (b) drives the real
:class:`GameServer` over fake connections (reusing ``tests.test_server``'s
helpers) with a small injected dictionary, proving the wire path — including the
load-bearing "a rejected word must not reset the fuse" test.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import server.connection as connection
from server.connection import GameServer
from server.games.word_bomb import WordBombGame, derive_prompts
from shared import protocol
from tests.test_server import FakeConn, open_conn, settle, _conn_for, _wait  # noqa: F401


# --- (a) pure rules -------------------------------------------------------

PURE_WORDS = frozenset({"cat", "cot", "coat", "catalog", "dog"})
PURE_PROMPTS = ["ca", "co"]
PURE_INDEX = {"ca": ["cat", "catalog"], "co": ["cot", "coat"]}


def _game(contestants=None, **over) -> WordBombGame:
    contestants = contestants or [("p1", "A"), ("p2", "B")]
    kw = dict(bots=(), words=PURE_WORDS, prompts=list(PURE_PROMPTS),
              index={k: list(v) for k, v in PURE_INDEX.items()},
              lives=2, bot_fail_chance=0.25, rng=random.Random(7))
    kw.update(over)
    return WordBombGame(contestants, **kw)


def test_constructor_seats_players_at_full_lives() -> None:
    g = _game()
    assert g.current_pid == "p1"
    assert g.prompt in PURE_PROMPTS
    assert all(g.lives[pid] == 2 for pid in g.order)
    assert g.awaiting == protocol.AWAIT_WORD
    assert g.phase == protocol.PHASE_PLAY


def test_accepted_word_passes_the_turn_and_bumps_seq() -> None:
    g = _game()
    g.prompt = "ca"
    assert g.submit_word("p1", "cat") == "accepted"
    assert g.seq == 1
    assert "cat" in g.used
    assert g.current_pid == "p2"
    assert any(e["kind"] == "accept" and e["word"] == "cat" for e in g.feed)


def test_rejected_word_leaves_the_turn_untouched() -> None:
    for word, reason, setup in [
        ("dog", "not_in_prompt", lambda g: None),
        ("caz", "not_a_word", lambda g: None),          # contains "ca", not a real word
        ("cat", "already_used", lambda g: g.used.add("cat")),
    ]:
        g = _game()
        g.prompt = "ca"
        setup(g)
        assert g.submit_word("p1", word) == "rejected"
        assert g.seq == 0                     # anti-fuse-reset: seq untouched
        assert g.prompt == "ca"               # prompt untouched
        assert g.current_pid == "p1"          # still the same player's turn
        assert g.feed[-1]["kind"] == "reject" and g.feed[-1]["reason"] == reason


def test_two_rejects_share_a_seq_but_have_distinct_ids() -> None:
    g = _game()
    g.prompt = "ca"
    g.submit_word("p1", "dog")
    g.submit_word("p1", "xyz")
    e1, e2 = g.feed[-2], g.feed[-1]
    assert e1["seq"] == e2["seq"]             # same turn -> same seq
    assert e2["id"] > e1["id"]               # but strictly increasing ids


def test_feed_is_capped_at_eight_however_many_events_fire() -> None:
    g = _game()
    g.prompt = "ca"
    for _ in range(20):
        assert g.submit_word("p1", "dog") == "rejected"
    assert g.seq == 0
    assert len(g.feed) <= 8
    ids = [e["id"] for e in g.feed]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)   # strictly increasing
    assert all(e["seq"] == 0 for e in g.feed)


def test_submit_by_the_non_current_player_returns_none() -> None:
    g = _game()
    assert g.submit_word("p2", "cot") is None   # p1 is up, not p2


def test_advance_costs_a_life_eliminates_and_crowns_the_survivor() -> None:
    g = _game()
    g.advance()                              # p1 -> 1 life, bomb to p2
    assert g.lives["p1"] == 1 and g.current_pid == "p2"
    assert g.feed[-1]["kind"] == "explode"
    g.advance()                              # p2 -> 1 life, bomb to p1
    g.advance()                              # p1 -> 0: eliminated, p2 wins
    assert g.is_over and g.winner["id"] == "p2"
    assert g.deadline is None
    assert any(e["kind"] == "eliminated" and e["pid"] == "p1" for e in g.feed)


def test_pass_bomb_skips_eliminated_players() -> None:
    g = _game(contestants=[("p1", "A"), ("p2", "B"), ("p3", "C")])
    g.lives["p2"] = 0                        # middle player is out
    g.current_idx = 0                        # bomb on p1
    g._pass_bomb()
    assert g.current_pid == "p3"             # skips the eliminated p2
    g._pass_bomb()
    assert g.current_pid == "p1"             # wraps around, still skipping p2


def test_bot_action_plays_a_word_or_fumbles_by_chance() -> None:
    g = _game(bot_fail_chance=0.0)
    g.prompt = "ca"
    act = g.bot_action("p2")
    assert act["kind"] == "word"
    assert "ca" in act["word"] and act["word"] not in g.used
    g2 = _game(bot_fail_chance=1.0)
    assert g2.bot_action("p2") == {"kind": "pass"}


def test_derive_prompts_keeps_only_frequent_substrings() -> None:
    prompts, index = derive_prompts({"cat", "catalog", "cot"}, min_count=2)
    words = ["cat", "catalog", "cot"]
    assert "at" in prompts and "ca" in prompts     # each in >= 2 words
    assert "log" not in prompts                    # only in "catalog"
    for p in prompts:
        assert sum(1 for w in words if p in w) >= 2
        assert p in index and index[p]             # every prompt has bot candidates


def test_dictionary_asset_is_present_and_clean() -> None:
    path = Path(__file__).parent.parent / "server" / "games" / "words.txt"
    assert path.is_file(), "words.txt must ship under server/games/"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 50000, f"only {len(lines)} words (corrupt source? abort A4)"
    for w in lines[:200]:
        assert re.fullmatch(r"[a-z]{3,15}", w), f"non-clean dictionary line: {w!r}"


# --- (b) driver integration ----------------------------------------------

DRIVER_WORDS = frozenset({"cat", "cot", "coat", "catalog", "dog",
                          "cab", "cog", "cap", "con"})
DRIVER_PROMPTS = ["ca", "co"]
DRIVER_INDEX = {"ca": ["cat", "catalog", "cab", "cap"],
                "co": ["cot", "coat", "cog", "con"]}


def _toy_load(*_a, **_k):
    """Stand-in for load_dictionary: a tiny, deterministic word set."""
    return DRIVER_WORDS, list(DRIVER_PROMPTS), {k: list(v) for k, v in DRIVER_INDEX.items()}


async def _wb_room(server, conns, names, monkeypatch, *, host_mode=protocol.HOST_AUTO,
                   bots=0):
    """Create a room, join the rest, and start a Word Bomb game with the toy dict
    and a determinized RNG. Returns the room code."""
    monkeypatch.setattr(connection, "load_dictionary", _toy_load)
    monkeypatch.setattr(server, "_make_rng", lambda: random.Random(0))
    first, *rest = conns
    await first.push(protocol.C_CREATE_ROOM, name=names[0], host_mode=host_mode)
    code = first.last(protocol.S_ROOM_CREATED)["code"]
    for conn, name in zip(rest, names[1:]):
        await conn.push(protocol.C_JOIN_ROOM, code=code, name=name)
    await first.push(protocol.C_START_GAME, game=protocol.GAME_WORD_BOMB, bots=bots)
    return code


def _valid_word(prompt: str, used: set) -> str:
    for w in DRIVER_INDEX[prompt]:
        if w not in used:
            return w
    raise AssertionError(f"no unused word left for prompt {prompt!r}")


async def test_word_bomb_starts_and_broadcasts_state(monkeypatch) -> None:
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _wb_room(server, [a, b], ["A", "B"], monkeypatch)

    assert a.last(protocol.S_GAME_STARTED)["game"] == "word_bomb"
    g = a.game()
    assert g["name"] == "word_bomb"
    assert g["prompt"] in DRIVER_PROMPTS
    assert isinstance(g["deadline"], float)   # auto mode arms a fuse

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()


async def test_word_bomb_valid_submit_passes_the_bomb(monkeypatch) -> None:
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _wb_room(server, [a, b], ["A", "B"], monkeypatch)

    cur = a.game()["current_pid"]
    cur_conn = _conn_for([a, b], cur)
    word = _valid_word(a.game()["prompt"], set())
    await cur_conn.push(protocol.C_SUBMIT_WORD, word=word)

    g = a.game()
    assert g["current_pid"] != cur         # the bomb passed on
    assert g["used_count"] == 1
    assert g["feed"][-1]["kind"] == "accept"

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()


async def test_word_bomb_reject_does_not_reset_the_fuse(monkeypatch) -> None:
    # THE anti-fuse-reset test: a rejected word broadcasts a reject event but leaves
    # the deadline (fuse) at the exact same float — no _drive, no re-armed timer.
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _wb_room(server, [a, b], ["A", "B"], monkeypatch)

    cur = a.game()["current_pid"]
    cur_conn = _conn_for([a, b], cur)
    deadline_before = a.game()["deadline"]
    await cur_conn.push(protocol.C_SUBMIT_WORD, word="zzzzz")   # contains no prompt

    g = a.game()
    assert g["feed"][-1]["kind"] == "reject"
    assert g["deadline"] == deadline_before      # same fuse, not reset
    assert g["current_pid"] == cur               # still their turn

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()


async def test_roll_dice_is_rejected_during_word_bomb(monkeypatch) -> None:
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _wb_room(server, [a, b], ["A", "B"], monkeypatch)

    await a.push(protocol.C_ROLL_DICE)
    assert a.last(protocol.S_ERROR)["code"] == protocol.ERR_WRONG_SUBSTATE

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop()


async def test_word_bomb_human_host_detonate_costs_a_life(monkeypatch) -> None:
    server = GameServer()
    a, ta = await open_conn(server)   # human host
    b, tb = await open_conn(server)
    c, tc = await open_conn(server)
    await _wb_room(server, [a, b, c], ["Host", "B", "C"], monkeypatch,
                   host_mode=protocol.HOST_HUMAN)

    g = a.game()
    assert g["you_role"] == "host"
    assert g["deadline"] is None              # human-host parks (no fuse)
    cur = g["current_pid"]
    lives_before = next(p["lives"] for p in g["players"] if p["id"] == cur)

    await a.push(protocol.C_ADVANCE_PHASE)    # the host detonates
    g = a.game()
    cur_player = next(p for p in g["players"] if p["id"] == cur)
    assert cur_player["lives"] == lives_before - 1
    assert any(e["kind"] == "explode" for e in g["feed"])

    await a.push(protocol.C_RETURN_TO_LOBBY)
    await a.drop(); await b.drop(); await c.drop()


async def test_word_bomb_bot_fumbles_until_human_wins(monkeypatch) -> None:
    monkeypatch.setattr(connection, "SAL_BOT_DELAY_SECONDS", 0.01)  # bot autoplays fast
    monkeypatch.setattr(connection, "WB_BOT_FAIL_CHANCE", 1.0)      # the bot always fumbles
    server = GameServer()
    a, ta = await open_conn(server)
    await _wb_room(server, [a], ["Solo"], monkeypatch, bots=1)      # 1 human + 1 bot

    human = a.game()["your_id"]
    assert a.game()["current_pid"] == human   # the human is up first
    used: set = set()
    for _ in range(12):
        g = a.game()
        if g["winner"]:
            break
        if g["your_turn"] and g["phase"] == protocol.PHASE_PLAY:
            word = _valid_word(g["prompt"], used)   # never reuse (a reused word rejects)
            used.add(word)
            await a.push(protocol.C_SUBMIT_WORD, word=word)
        # the bot fumbles (fail-chance 1) and explodes; wait for the bomb to return
        await _wait(lambda: a.game()["current_pid"] == human or a.game()["winner"], tries=80)

    assert a.game()["winner"]["id"] == human

    await a.drop(); await ta


async def test_word_bomb_return_to_lobby_tears_down(monkeypatch) -> None:
    server = GameServer()
    a, ta = await open_conn(server)
    b, tb = await open_conn(server)
    await _wb_room(server, [a, b], ["A", "B"], monkeypatch)
    assert a.room()["in_game"] is True

    await a.push(protocol.C_RETURN_TO_LOBBY)
    assert protocol.S_RETURN_TO_LOBBY in a.types() and protocol.S_RETURN_TO_LOBBY in b.types()
    assert a.room()["in_game"] is False

    await a.drop(); await b.drop()
