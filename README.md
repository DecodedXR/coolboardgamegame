# coolboardgamegame

An online-multiplayer party-game platform in the spirit of **Magic the Noah** —
scuffed homebrew gameshows (word games, trivia, board games) run by a host for a
handful of contestants. Built with **pygame** clients talking to a small
**websockets** server.

Think *"Jackbox meets a Discord gameshow."* One player can be the **host** (reveals
prompts, judges, torments contestants) or the room can run **host-less / automated**
— it's a per-room toggle.

## Status — Milestone 1: networking plumbing ✅

The multiplayer spine is built and tested: lobby, rooms, live state broadcast,
ready-up, the human/auto **host toggle**, **host handoff**, graceful disconnect
with a grace period, and a `start_game` that lands everyone on a stub in-game
screen. No real minigame yet — that's next.

```
shared/protocol.py   wire format (message types + JSON encode/decode), shared by both sides
server/              authoritative websockets server (rooms, host logic, broadcasting)
client/              pygame client (net thread + scenes: connect / menu / lobby / in-game stub)
config.py            HOST/PORT etc. (env-overridable, cloud-ready)
tests/test_server.py headless end-to-end tests (no pygame, no real sockets)
```

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
   clicks **START GAME**.

## Cloud later

The server reads `HOST`/`PORT` from the environment and binds `0.0.0.0`, so it runs
unchanged on Render/Railway/a VPS (which inject `$PORT`). Going "true online" is then
just pointing the client's connect screen at the deployed URL — no code changes.

## Tests

```bash
python -m pytest
```

Drives the server through fake connections to assert the full broadcast behavior
(create/join, ready, host toggle, host handoff, start_game, grace-period disconnect)
in well under a second.
