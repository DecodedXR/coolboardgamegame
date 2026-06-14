# coolboardgamegame

An online-multiplayer party-game platform in the spirit of **Magic the Noah** —
scuffed homebrew gameshows (word games, trivia, board games) run by a host for a
handful of contestants. Built with **pygame** clients talking to a small
**websockets** server.

Think *"Jackbox meets a Discord gameshow."* One player can be the **host** (reveals
prompts, judges, torments contestants) or the room can run **host-less / automated**
— it's a per-room toggle.

## Status

- **Milestone 1 — networking plumbing ✅** The multiplayer spine: lobby, rooms,
  live state broadcast, ready-up, the human/auto **host toggle**, **host handoff**,
  and graceful disconnect with a grace period.
- **Milestone 2 — first minigame: Wrong Answers Only ✅** A Quiplash-style round
  loop plugged in behind `start_game`: **prompt → answers → vote → score**, across
  several rounds, ending on a scoreboard. Answers are anonymized for voting (you
  can't see who wrote what, and can't vote for your own). Driven either by a
  **human host** (manual *reveal / next* control) or the **auto host** (per-phase
  countdown timers that also fast-forward once everyone has acted).

```
shared/protocol.py    wire format (message types + JSON encode/decode), shared by both sides
server/               authoritative websockets server (rooms, host logic, broadcasting)
server/games/         pluggable minigames — wrong_answers.py (pure rules) + prompts.py
client/               pygame client (net thread + scenes: connect / menu / lobby / wrong_answers)
config.py             HOST/PORT, room sizing, and Wrong-Answers tuning (rounds, timers, scoring)
tests/                headless tests — test_server.py (end-to-end) + test_wrong_answers.py (rules)
```

### Playing Wrong Answers Only

One player starts the game from the lobby (the **host** in human mode, the
**owner** in auto mode); you need at least two *contestants* (the human host
doesn't play, just runs the show). Each round shows a prompt; contestants type the
funniest wrong answer, then vote on the anonymized answers. Each vote your answer
gets is worth points. In **human** mode the host clicks through *reveal answers →
show results → next round*; in **auto** mode timers do it (and skip ahead the
moment everyone's answered or voted). After the last round, the host returns
everyone to the lobby to play again.

## Setup

Requires Python 3.10+. On Python 3.14 use `pygame-ce` (a drop-in for `pygame`
with up-to-date wheels — it still imports as `pygame`); `requirements.txt` already
pins it.

```bash
python -m pip install -r requirements.txt
```

## Run it on a LAN

1. **Start the server** (on the machine that will host):

   ```bash
   python -m server          # binds 0.0.0.0:8765
   # custom port:  PORT=8799 python -m server   (Windows PowerShell: $env:PORT=8799; python -m server)
   ```

2. **Launch a client** on each player's machine:

   ```bash
   python -m client
   ```

3. On the **connect screen**, enter the server's address:
   - same machine as the server → `localhost`
   - another machine on the LAN → the server machine's LAN IP (e.g. `192.168.1.42`), port `8765`

4. One player picks a **host mode** (HUMAN or AUTO) and clicks **HOST A GAME** to get
   a room **code**. Everyone else types that code and clicks **JOIN**.

5. In the lobby: toggle **READY**; the **owner** can flip host mode; in HUMAN mode the
   host clicks a player to **pass the host role**; the host (HUMAN) or owner (AUTO)
   clicks **START GAME** to launch Wrong Answers Only (see *Playing Wrong Answers
   Only* above).

## Cloud later

The server reads `HOST`/`PORT` from the environment and binds `0.0.0.0`, so it runs
unchanged on Render/Railway/a VPS (which inject `$PORT`). Going "true online" is then
just pointing the client's connect screen at the deployed URL — no code changes.

## Tests

```bash
python -m pytest
```

`test_server.py` drives the server through fake connections to assert the full
broadcast behavior (create/join, ready, host toggle, host handoff, grace-period
disconnect) plus the Wrong Answers Only flow end to end (auto + human host,
anonymized voting, scoring, return-to-lobby). `test_wrong_answers.py` covers the
pure game rules directly. Everything runs without pygame or real sockets, in well
under a second.
