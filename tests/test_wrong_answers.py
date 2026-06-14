"""Unit tests for the pure Wrong Answers Only game logic (no server, no sockets)."""

from __future__ import annotations

import pytest

from server.games.wrong_answers import WrongAnswersGame
from shared import protocol


def make_game(n_contestants=3, total_rounds=2):
    contestants = [(f"p{i}", f"Player{i}") for i in range(n_contestants)]
    prompts = [f"prompt {i}" for i in range(total_rounds)]
    return WrongAnswersGame(
        contestants, prompts, total_rounds=total_rounds, points_per_vote=100, max_answer_len=80
    )


def test_full_round_scores_by_votes():
    g = make_game(n_contestants=3, total_rounds=1)
    assert g.phase == protocol.PHASE_PROMPT
    g.submit_answer("p0", "aaa")
    g.submit_answer("p1", "bbb")
    g.submit_answer("p2", "ccc")

    g.advance()  # -> vote, anon ids assigned
    assert g.phase == protocol.PHASE_VOTE

    # p1 and p2 both vote for p0's answer; p0 votes for p1's.
    p0_aid = g.ans_id["p0"]
    p1_aid = g.ans_id["p1"]
    assert g.submit_vote("p1", p0_aid)
    assert g.submit_vote("p2", p0_aid)
    assert g.submit_vote("p0", p1_aid)

    g.advance()  # -> reveal, scores tallied
    assert g.phase == protocol.PHASE_REVEAL
    assert g.scores["p0"] == 200  # two votes
    assert g.scores["p1"] == 100  # one vote
    assert g.scores["p2"] == 0

    g.advance()  # single round -> final
    assert g.phase == protocol.PHASE_FINAL
    assert g.is_over
    assert g.scoreboard()[0]["id"] == "p0"


def test_cannot_vote_for_own_answer():
    g = make_game(n_contestants=2, total_rounds=1)
    g.submit_answer("p0", "x")
    g.submit_answer("p1", "y")
    g.advance()
    assert g.submit_vote("p0", g.ans_id["p0"]) is False
    assert g.submit_vote("p0", g.ans_id["p1"]) is True


def test_answer_ids_do_not_leak_authorship():
    g = make_game(n_contestants=3, total_rounds=1)
    for pid in ("p0", "p1", "p2"):
        g.submit_answer(pid, f"ans-{pid}")
    g.advance()
    # Anonymized ids must differ from the author ids the client also knows.
    for author, aid in g.ans_id.items():
        assert aid != author
    # And the vote-phase ballot for a player never contains their own answer.
    view = g.public("p0", host_id=None)
    texts = {o["text"] for o in view["answers"]}
    assert "ans-p0" not in texts
    assert texts == {"ans-p1", "ans-p2"}


def test_submit_rejected_in_wrong_phase():
    g = make_game()
    g.submit_answer("p0", "a")
    g.submit_answer("p1", "b")
    g.advance()  # now in vote phase
    assert g.phase == protocol.PHASE_VOTE
    assert g.submit_answer("p0", "late") is False


def test_empty_answer_rejected():
    g = make_game()
    assert g.submit_answer("p0", "   ") is False
    assert g.submit_answer("not-a-player", "hi") is False


def test_all_submitted_ignores_disconnected():
    g = make_game(n_contestants=3, total_rounds=1)
    g.submit_answer("p0", "a")
    g.submit_answer("p1", "b")
    # p2 hasn't answered, but if only p0/p1 are connected the phase is complete.
    assert g.all_submitted({"p0", "p1"}) is True
    assert g.all_submitted({"p0", "p1", "p2"}) is False


def test_no_answers_skips_voting():
    g = make_game(n_contestants=2, total_rounds=1)
    g.advance()  # nobody answered -> straight to reveal
    assert g.phase == protocol.PHASE_REVEAL
    assert g.results == []


def test_per_player_view_role_and_hidden_answers():
    g = make_game(n_contestants=2, total_rounds=1)
    g.submit_answer("p0", "mine")
    # During prompt, a player only sees their own answer, never others'.
    view = g.public("p0", host_id="hostX")
    assert view["you_role"] == "contestant"
    assert view["your_answer"] == "mine"
    assert "answers" not in view
    # The designated host is reported as host.
    assert g.public("hostX", host_id="hostX")["you_role"] == "host"


def test_round_rolls_over_and_resets():
    g = make_game(n_contestants=2, total_rounds=2)
    g.submit_answer("p0", "a")
    g.submit_answer("p1", "b")
    g.advance(); g.advance()  # vote -> reveal
    assert g.round == 1
    g.advance()  # reveal -> next round prompt
    assert g.round == 2
    assert g.phase == protocol.PHASE_PROMPT
    assert g.answers == {}  # round state reset
    assert g.current_prompt == "prompt 1"
