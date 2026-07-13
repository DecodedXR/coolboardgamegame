# coolboardgamegame

An online-multiplayer party-game platform in the spirit of **Magic the Noah** -
scuffed homebrew gameshows (word games, trivia, board games) run by a host for a
handful of contestants. Built with **pygame** clients talking to a small
**websockets** server.

Think *"Jackbox meets a Discord gameshow."* One player can be the **host** (reveals
prompts, judges, torments contestants) or the room can run **host-less / automated**
- it's a per-room toggle.

## Status

- **Milestone 1 - networking plumbing ✅** The multiplayer spine: lobby, rooms,
  live state broadcast, ready-up, the human/auto **host toggle**, **host handoff**,
  and graceful disconnect with a grace period.
- **Milestone 2 - networked Snakes & Ladders ✅** A turn-based, server-authoritative
  board game plugged in behind `start_game` - and **snake-heavy**, so winning is
  brutal. Roll the dice to move; landing on a special tile spins a Wheel-of-Names
  for a powerup / debuff / gold outcome, opens a shop, grants gold, or drops a
  debuff on you. A gold economy and held powerups (immunity / boost / double /
  reroll) ride on top. The board is freshly randomized each game. Server-side
  **bot players** fill empty seats so you can play solo against the computer.
  Driven either by a **human host** (manual *next* control) or the **auto host**
  (per-turn countdown timers). The server resolves each turn into an ordered
  timeline that clients replay as animation (token hops, snake slides, wheel
  spins, cutscenes, procedural SFX).
- **Milestone 3 - true online (cloud-hosted) ✅** The server is deployed on
  **Render** (free tier) at `wss://coolboardgamegame.onrender.com` and is the
  client's baked-in default - nobody runs a server or a tunnel. Joining is now
  just: launch the client → type a name → enter the room code. (A sleeping
  free instance takes ~30–60s to wake on the first connect; the client shows a
  "waking the server…" status and retries.)
- **Tier 4 - browser/WASM client ✅** The pygame client compiles to WebAssembly
  via **pygbag** and is hosted on **GitHub Pages** - opening a link is enough to
  play, no Python or install required.

```
shared/protocol.py    wire format (message types + JSON encode/decode), shared by both sides
server/               authoritative websockets server (rooms, host logic, broadcasting)
server/games/         pluggable minigames - snakes_and_ladders.py (pure rules + turn timeline)
client/               pygame client (net thread + scenes: connect / menu / lobby / snakes_and_ladders)
client/board_render.py, token_anim.py, wheel.py, shop_ui.py, cutscene.py, sfx.py  - board components
config.py             HOST/PORT, room sizing, and Snakes & Ladders tuning (board, tiles, economy, timers)
tests/                headless tests - test_server.py (end-to-end) + test_snakes_and_ladders.py (rules)
```

## Play in your browser

Open **https://decodedxr.github.io/coolboardgamegame** in any modern browser - no
Python, no install. The page connects to the cloud server; share a room code and
play.

- **Mobile** - hold your phone portrait (upright). The layout fills the screen and
  text entry (name, room code, answers) uses the browser's native prompt dialog.
- **Desktop** - the browser client works fine; the `python -m client` desktop build
  is unchanged if you prefer it.

> The very first connect of the day wakes Render's free tier (~30–60 s); the client
> shows "waking the server…" and retries automatically.

### Playing Snakes & Ladders

One player starts the game from the lobby (the **host** in human mode, the
**owner** in auto mode), optionally seating a few **bots** with the `- bots N +`
stepper to fill out the board - you need at least two players total (humans + bots).
On your turn, click **ROLL** to move along the serpentine board. Land on a snake
and you slide *down* (there are far more snakes than ladders - winning is meant to
hurt); land on a special tile to spin a wheel for a random outcome, gain gold, take
a debuff, or open a **shop** to buy a powerup. Powerups you hold (immunity / boost /
double / reroll) can be armed *before* you roll. Reach the final cell exactly to win
(overshooting bounces back). In **auto** mode a per-turn timer keeps things moving
and bots take their own turns; in **human** mode the host can force the current turn
along with **NEXT**. When someone wins, the host returns everyone to the lobby to
play again.

## Setup

Requires Python 3.10+. On Python 3.14 use `pygame-ce` (a drop-in for `pygame`
with up-to-date wheels - it still imports as `pygame`); `requirements.txt` already
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
   host clicks a player to **pass the host role**; the starter can seat **bots** with
   the `- bots N +` stepper; the host (HUMAN) or owner (AUTO) clicks **START GAME** to
   launch Snakes & Ladders (see *Playing Snakes & Ladders* above).

## Cloud (live)

The server is hosted on **Render** at `wss://coolboardgamegame.onrender.com` and is
the client's default - to play online you don't run anything server-side, just
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
disconnect) plus the Snakes & Ladders turn driver end to end (auto + human host,
bots taking turns, deadlines, disconnect-during-turn, shop buys, return-to-lobby).
`test_snakes_and_ladders.py` covers the pure game rules directly (board gen, the
turn timeline, bounce, transports, the economy), and the `test_board_render` /
`test_token_anim` / `test_wheel` / `test_shop_ui` / `test_cutscene` /
`test_snakes_scene` suites pin the client components and scene wiring. Everything
runs without a display or real sockets, in a few seconds.
