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
- **Milestone 3 — true online (cloud-hosted) ✅** The server is deployed on
  **Render** (free tier) at `wss://coolboardgamegame.onrender.com` and is the
  client's baked-in default — nobody runs a server or a tunnel. Joining is now
  just: launch the client → type a name → enter the room code. (A sleeping
  free instance takes ~30–60s to wake on the first connect; the client shows a
  "waking the server…" status and retries.)
- **Tier 4 — browser/WASM client ✅** The pygame client compiles to WebAssembly
  via **pygbag** and is hosted on **GitHub Pages** — opening a link is enough to
  play, no Python or install required.

```
shared/protocol.py    wire format (message types + JSON encode/decode), shared by both sides
server/               authoritative websockets server (rooms, host logic, broadcasting)
server/games/         pluggable minigames — wrong_answers.py (pure rules) + prompts.py
client/               pygame client (net thread + scenes: connect / menu / lobby / wrong_answers)
config.py             HOST/PORT, room sizing, and Wrong-Answers tuning (rounds, timers, scoring)
tests/                headless tests — test_server.py (end-to-end) + test_wrong_answers.py (rules)
```

## Play in your browser

Open **https://decodedxr.github.io/coolboardgamegame** in any modern browser — no
Python, no install. The page connects to the cloud server; share a room code and
play.

- **Mobile** — hold your phone portrait (upright). The layout fills the screen and
  text entry (name, room code, answers) uses the browser's native prompt dialog.
- **Desktop** — the browser client works fine; the `python -m client` desktop build
  is unchanged if you prefer it.

> The very first connect of the day wakes Render's free tier (~30–60 s); the client
> shows "waking the server…" and retries automatically.

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

## Cloud (live)

The server is hosted on **Render** at `wss://coolboardgamegame.onrender.com` and is
the client's default — to play online you don't run anything server-side, just
launch the client and join a code. The deploy is config-only (`render.yaml` +
`Dockerfile`): the server reads `HOST`/`PORT` from the environment and binds
`0.0.0.0`, so the identical `python -m server` runs on LAN and on any host that
injects `$PORT` (Render/Railway/a VPS); TLS is terminated at the platform proxy, so
the app still speaks plain `ws`. To run fully on a LAN instead, clear the URL field
on the connect screen (or set `SERVER_URL=`) and use the host/port fallback.

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
