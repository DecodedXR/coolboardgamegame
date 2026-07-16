# Wargame: Turn this repo into a Word Bomb game

**Mission:** Add a Bomb-Party-style word game ("Word Bomb") to this multiplayer
gameshow platform and make it the default game the lobby starts. Snakes & Ladders
stays playable via a lobby toggle. A cheaper model executes this plan blind.

**Game rules being built:** Players take turns. The current player sees a 2–3
letter prompt (e.g. `TIO`) and must submit a real English word *containing* that
substring before the timer runs out. A valid, unused dictionary word passes the
bomb to the next player with a fresh prompt. Timeout = the bomb explodes = lose a
life. 0 lives = eliminated. Last player alive wins.

---

## Recon results (verified 2026-07-16 — do not re-derive, trust these)

- Repo is a pygame-client / websockets-server party-game platform. Server game
  logic is pure-Python in `server/games/`, driven by `server/connection.py`
  (`GameServer`), rooms in `server/rooms.py`, wire format in `shared/protocol.py`,
  tunables in `config.py`. Client is scene-based: `client/scenes/{connect,menu,lobby,snakes_and_ladders}.py`,
  widgets in `client/ui.py` (`Label`, `Button`, `TextInput`).
- `GameServer` is hardcoded to `SnakesAndLaddersGame` but only relies on this
  narrow surface: `current_pid`, `seq`, `deadline`, `awaiting`, `is_over`,
  `is_current(pid)`, `is_contestant(pid)`, `advance()`, `bot_ids`,
  `bot_action(pid)`, `winner`, `public(for_pid, host_id)`. A Word Bomb game class
  exposing the same surface plugs into `_drive` / `_auto_turn` / `_turn_deadline`
  / `_on_roster_change` unchanged.
- `game.seq` + `current_pid` guard stale timers: `_drive` cancels the old timer
  and arms a new one keyed `(actor, seq)`. **Therefore: a rejected word must NOT
  bump `seq` and must NOT re-run `_drive`** (re-driving resets the countdown —
  spamming garbage words would defuse the bomb forever).
- `C_START_GAME` currently has no `game` field; `S_GAME_STARTED` has no payload;
  `LobbyScene.on_message` hardcodes the S&L scene. All three need a `game` field.
- Browser text entry: `client/ui.py` `TextInput.handle` already does the right
  thing per platform — desktop uses key events, browser opens `window.prompt` on
  tap (`client/browser_io.py`). Reuse `TextInput` as-is for word entry.
- Dictionary must live server-side only (server validates words). Put it at
  `server/games/words.txt`: the `Dockerfile` does `COPY server/ ./server/` (ships
  automatically) and `pygbag.ini` excludes `/server` (stays out of the WASM
  bundle). **No packaging config changes needed.**
- Word list source verified reachable (HTTP 200):
  primary `https://raw.githubusercontent.com/dolph/dictionary/master/enable1.txt`
  (ENABLE, public domain, 1.7 MB); fallback
  `https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt`.
- Tests are headless (fake connections, dummy video); baseline suite is the
  contract. `tests/test_server.py` shows the fake-connection pattern to copy.
- Repo commits directly on `main`. Windows machine; PowerShell is the shell.

---

## Battle plan

Work on `main`. After each step that says **COMMIT**, run `python -m pytest`
first; only commit if green. Commit messages are given per step. Do not push.

### Step 0 — Baseline

**Action:** Run `python -m pytest` from the repo root.

**Expected observation:** All tests pass (exit code 0).

**Counter-move:** If anything fails before you changed a single line, STOP.
Report the failing tests and do not proceed (abort condition A1).

### Step 1 — Dictionary asset

**Action (PowerShell):**
```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/dolph/dictionary/master/enable1.txt" -OutFile "server\games\words_raw.txt"
python -c "import re; words = sorted({w.strip().lower() for w in open('server/games/words_raw.txt', encoding='utf-8')} ); words = [w for w in words if re.fullmatch(r'[a-z]{3,15}', w)]; open('server/games/words.txt', 'w', newline='\n').write('\n'.join(words)); print(len(words))"
Remove-Item server\games\words_raw.txt
```

**Expected observation:** The python one-liner prints a count between 100,000 and
180,000. `server/games/words.txt` exists, is 1–2 MB, one lowercase a–z word per
line.

**Counter-move:** If the download fails or the count is outside that range, use
the fallback URL `https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt`
with the same filter (expected count 300,000–380,000 — also acceptable). If both
fail: abort condition A2.

**COMMIT:** `feat(word-bomb): bundle the server-side word dictionary`

### Step 2 — Protocol additions (`shared/protocol.py`)

**Action:** Add, in the matching existing sections:

```python
# with the client->server in-game messages:
C_SUBMIT_WORD = "submit_word"        # {word}            (current player, word bomb)

# with the game identifiers:
GAME_WORD_BOMB = "word_bomb"
GAMES = (GAME_WORD_BOMB, GAME_SNAKES_AND_LADDERS)

# with the awaiting sub-states:
AWAIT_WORD = "word"   # word bomb: the current player must submit a word
```

Also update the comment on `C_START_GAME` to `# {bots?:int, game?:str}` and on
`S_GAME_STARTED` to `# {game}  (clients switch to that minigame's scene)`.

**Expected observation:** `python -m pytest` still green (protocol is additive).

**Counter-move:** If a test asserts on the exact protocol constant set, read it
and extend the expectation — do not delete assertions.

### Step 3 — Config (`config.py`)

**Action:** Add a section after the S&L block:

```python
# --- Word Bomb (type-a-word-before-the-bomb-explodes) ----------------------
WB_LIVES = 2                  # lives per player; 0 lives = eliminated
WB_TURN_SECONDS = 12          # auto-host fuse: submit a valid word in this window
WB_MIN_WORDS_PER_PROMPT = 500 # a prompt substring must appear in at least this many words
WB_BOT_FAIL_CHANCE = 0.25     # chance a bot fumbles its turn and eats the explosion
```

**Expected observation:** File imports cleanly: `python -c "import config"`.

### Step 4 — The game: `server/games/word_bomb.py` (new file)

**Action:** Create the module. It must be pure (no asyncio/network imports),
mirroring the style of `server/games/snakes_and_ladders.py`. Full specification:

```python
"""Word Bomb: pure rules. <docstring in repo style>"""
from __future__ import annotations
import random
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
    """Scan every word's 2- and 3-letter substrings. Return (prompts, index):
    prompts = sorted list of substrings appearing in >= min_count words;
    index = {prompt: first pool_cap words containing it} for bot answers.
    Count each substring at most once per word (use a per-word set)."""

@lru_cache(maxsize=1)
def load_dictionary():
    """Read words.txt -> frozenset of words, then derive_prompts(words,
    config-free: caller passes min_count) ... """
```

Implementation note for `load_dictionary`: keep it config-free like the S&L
module — signature `load_dictionary(min_count: int = 500)` returning
`(words: frozenset[str], prompts: list[str], index: dict[str, list[str]])`,
`lru_cache` so the 1–2 s scan happens once per process. Read lines with
`.strip()`, not `.rstrip('\n')` — a Windows git checkout with autocrlf can hand
the file over with `\r\n`, and a dictionary of `'cat\r'` entries rejects every
word ever typed.

```python
class WordBombGame:
    def __init__(self, contestants, *, bots=(), words, prompts, index=None,
                 lives=2, bot_fail_chance=0.25, rng=None) -> None:
```

- `contestants` / `bots` are `(pid, name)` lists exactly like S&L's constructor.
- State: `self.order` (contestant pids then bot pids), `self.names` (pid→name),
  `self.bot_ids` (set of bot pids), `self.lives` (pid→int, all start at `lives`),
  `self.used` (set of accepted words), `self.phase = PHASE_PLAY`,
  `self.awaiting = AWAIT_WORD`, `self.current_idx = 0`, `self.seq = 0`,
  `self.deadline: Optional[float] = None`, `self.winner_id = None`,
  `self.feed: list[dict] = []` (event log, keep only the last 8),
  `self._event_id = 0`, `self.prompt = rng.choice(prompts)`.
- `rng = rng or random.Random()` (same seam as S&L for deterministic tests).
- Every feed append goes through one helper `_emit(**event)`: it increments
  `self._event_id`, stamps `"id": self._event_id` and `"seq": self.seq` onto
  the event, appends, and trims the feed to the last 8. **Why `id` exists:**
  rejects deliberately do NOT bump `seq` (the anti-fuse-reset rule), so two
  rejected words in one turn share a `seq` — the client dedups feed events by
  `id`, never by `seq`.

Methods (exact contract):

- `current_pid` (property): `self.order[self.current_idx]`.
- `is_over` (property): `self.phase == PHASE_GAMEOVER`.
- `is_contestant(pid)`, `is_current(pid)`: mirror S&L.
- `winner` (property): `{"id": winner_id, "name": names[winner_id]}` or `None`.
- `alive_ids()`: `[pid for pid in self.order if self.lives[pid] > 0]`.
- `submit_word(pid, word) -> Optional[str]` — returns `"accepted"`, `"rejected"`,
  or `None`:
  - `None` if `is_over`, or `not is_current(pid)` (illegal — caller sends a
    protocol error).
  - Normalize: `w = str(word or "").strip().lower()`.
  - Rejected (`_emit(kind="reject", pid=pid, name=..., word=w, reason=...,
    prompt=self.prompt)` and return `"rejected"`)
    when: prompt not in `w` (reason `"not_in_prompt"`), `w not in self.words`
    (reason `"not_a_word"`), `w in self.used` (reason `"already_used"`).
    **Do NOT change `seq`, `prompt`, or the current player on rejection.**
  - Accepted: add `w` to `used`, append feed event `{"kind": "accept", ...,
    "word": w}`, `self.seq += 1`, then `_pass_bomb()`.
- `advance() -> None` — the explosion (host NEXT / deadline timeout / absent
  human all route here): if `is_over` return. `pid = current_pid`;
  `self.lives[pid] -= 1`; feed event `{"kind": "explode", "pid", "name",
  "prompt", "seq"}`; if lives hit 0, also feed `{"kind": "eliminated", ...}`;
  `self.seq += 1`; then if `len(alive_ids()) == 1`: set
  `self.phase = PHASE_GAMEOVER`, `self.winner_id = alive_ids()[0]`,
  `self.deadline = None`; else `_pass_bomb()`.
- `_pass_bomb()`: advance `current_idx` round-robin to the next pid with
  `lives > 0` (loop `(current_idx + 1) % len(order)` until alive — at least one
  exists since gameover was checked), then `self.prompt = rng.choice(prompts)`.
- `bot_action(pid) -> dict`: if `rng.random() < bot_fail_chance` return
  `{"kind": "pass"}` (the bot fumbles; caller explodes it). Else pick candidates
  `[w for w in (self.index.get(self.prompt) or []) if w not in self.used]`; if
  empty return `{"kind": "pass"}`; else return
  `{"kind": "word", "word": rng.choice(candidates)}`.
- `public(for_pid, host_id) -> dict`: mirror the S&L shape exactly (same
  role derivation: host / contestant / spectator):

```python
{
    "name": protocol.GAME_WORD_BOMB,
    "phase": self.phase,
    "awaiting": self.awaiting,
    "prompt": self.prompt,
    "players": [{"id": pid, "name": self.names[pid], "lives": self.lives[pid],
                 "is_bot": pid in self.bot_ids, "alive": self.lives[pid] > 0}
                for pid in self.order],
    "current_pid": self.current_pid,
    "your_turn": self.is_current(for_pid),
    "your_id": for_pid,
    "you_role": role,
    "deadline": self.deadline,
    "feed": list(self.feed),
    "winner": self.winner,
    "used_count": len(self.used),
}
```

**Expected observation:** `python -c "from server.games.word_bomb import WordBombGame, load_dictionary; w,p,i = load_dictionary(); print(len(w), len(p), min(len(x) for x in p), max(len(x) for x in p))"`
prints: word count from step 1, a prompt count in the low thousands, min 2, max 3.
Runs in under ~10 seconds.

**Counter-move:** If `load_dictionary` takes over 10 s, the substring scan is
doing repeated string allocation per position — build the per-word substring set
with a comprehension over `range(len(w))` slices only (lengths 2 and 3) and use
`collections.Counter`. If still slow, fork trigger F3 (precompute to a file).

### Step 5 — Server wiring (`server/connection.py`)

**Action:** Seven surgical edits (six in `connection.py`, one in
`server/__main__.py`); touch nothing else.

1. Imports: add `WB_LIVES, WB_TURN_SECONDS, WB_MIN_WORDS_PER_PROMPT,
   WB_BOT_FAIL_CHANCE` to the `config` import; add
   `from server.games.word_bomb import WordBombGame, load_dictionary` and
   `AWAIT_WORD` (via `from shared.protocol import ...` — it's already reachable
   as `protocol.AWAIT_WORD`, use that instead of a new import).
2. Widen the games map annotation: `self.games: dict[str, Any] = {}` (update the
   nearby comment). Register the handler:
   `protocol.C_SUBMIT_WORD: GameServer._on_submit_word,` in `_dispatch`.
3. `_on_start_game`: after the not-enough-players check, select the game:

```python
game_name = msg.get("game", protocol.GAME_WORD_BOMB)
if game_name not in protocol.GAMES:
    await self._send_error(ctx.conn, protocol.ERR_BAD_MESSAGE, f"unknown game {game_name!r}")
    return
if game_name == protocol.GAME_WORD_BOMB:
    words, prompts, index = load_dictionary(WB_MIN_WORDS_PER_PROMPT)
    game = WordBombGame(contestants, bots=bots, words=words, prompts=prompts,
                        index=index, lives=WB_LIVES,
                        bot_fail_chance=WB_BOT_FAIL_CHANCE, rng=self._make_rng())
else:
    game = SnakesAndLaddersGame(<the existing kwargs, unchanged>)
```

   and send the started frame with the payload:
   `protocol.encode(protocol.S_GAME_STARTED, game=game_name)`.
4. New handler, modeled on `_on_roll_dice`:

```python
async def _on_submit_word(self, ctx, msg):
    room, player, game = self._require_game(ctx)
    if game is None:
        return await self._game_membership_error(ctx, room)
    if not isinstance(game, WordBombGame):
        return await self._send_error(ctx.conn, protocol.ERR_WRONG_SUBSTATE,
                                      "submit_word is only valid in word bomb")
    result = game.submit_word(player.id, msg.get("word"))
    if result is None:
        if game.is_over:   # a word raced the winning explosion; say so, not "bad item"
            return await self._send_error(ctx.conn, protocol.ERR_WRONG_PHASE, "the game is over")
        return await self._reject_turn(ctx, game, player.id, protocol.AWAIT_WORD)
    if result == "accepted":
        await self._after_turn_change(room, game)   # turn passed: re-arm + broadcast
    else:
        await self._broadcast_game(room, game)      # rejected: fuse keeps burning — NO _drive
```

5. Guard the four S&L-only handlers (`_on_roll_dice`, `_on_use_powerup`,
   `_on_buy_item`, `_on_skip_shop`): right after the `game is None` check, add
   `if isinstance(game, WordBombGame): return await self._send_error(ctx.conn,
   protocol.ERR_WRONG_SUBSTATE, "not available in this game")`.
6. Per-game turn budget and autoplay:
   - In `_drive`, replace the seconds line with:
     `seconds = WB_TURN_SECONDS if isinstance(game, WordBombGame) else (SAL_SHOP_SECONDS if game.awaiting == AWAIT_SHOP else SAL_ROLL_SECONDS)`
   - In `_apply_auto_action`, branch first:

```python
if isinstance(game, WordBombGame):
    if pid in game.bot_ids:
        action = game.bot_action(pid)
        if action.get("kind") == "word":
            if game.submit_word(pid, action.get("word")) == "accepted":
                return
        game.advance()          # fumble or (freak case) rejected word: bot explodes
    else:
        game.advance()          # absent human: the bomb goes off in their hands
    return
<existing S&L body unchanged>
```

   `_auto_turn` keeps using `SAL_BOT_DELAY_SECONDS` for both games (shared knob).
   Update `_apply_auto_action`'s type hint from `SnakesAndLaddersGame` to `Any`.
7. **Warm the dictionary at boot** (`server/__main__.py`): the substring scan
   takes seconds, and `load_dictionary` would otherwise run lazily INSIDE the
   first `start_game` handler — synchronously, freezing the event loop (and
   every room) mid-await. In `main()`, before `websockets.serve`, add:

```python
from config import WB_MIN_WORDS_PER_PROMPT
from server.games.word_bomb import load_dictionary
words, prompts, _ = load_dictionary(WB_MIN_WORDS_PER_PROMPT)
print(f"word bomb dictionary ready: {len(words)} words, {len(prompts)} prompts")
```

   Boot-time blocking is fine (nothing is connected yet); the `lru_cache` makes
   every later call instant. Headless tests are untouched — they construct
   `GameServer` directly and monkeypatch `load_dictionary`, never importing
   `__main__`.

**Expected observation:** `python -m pytest tests/test_server.py` — every
existing S&L server test still green (S&L is untouched: it is still constructed
with the same kwargs and `start_game` without a `game` field... note the default
is now word_bomb, see counter-move).

**Counter-move (important):** Existing `test_server.py` starts games with
`C_START_GAME` and no `game` field, and the new default is `word_bomb` — those
tests WILL now get a WordBombGame and fail. Fix the *tests*, not the default:
in `tests/test_server.py`, add `game=protocol.GAME_SNAKES_AND_LADDERS` to every
`start_game` send (or to the shared helper if the file has one — read it first;
prefer the single-helper edit). That is an expected, mechanical migration. If
failures persist beyond that, re-read your diff against this step — do not
redesign.

**COMMIT:** `feat(word-bomb): server-side word bomb game + game selection on start`

### Step 6 — Server tests (new file `tests/test_word_bomb.py` + driver coverage)

**Action:** Two test groups.

(a) Pure rules (no server): construct `WordBombGame` directly with a toy
dictionary — `words = frozenset({"cat", "cot", "coat", "catalog", "dog"})`,
`prompts = ["ca", "co"]`, `index = {"ca": ["cat", "catalog"], "co": ["cot", "coat"]}`,
`rng = random.Random(7)`, two contestants `[("p1", "A"), ("p2", "B")]`,
`lives=2`. Assert at minimum:
- constructor: `current_pid == "p1"`, prompt in prompts, everyone at 2 lives.
- accepted word containing the prompt passes the turn, bumps `seq`, adds to
  `used`, appends an `accept` feed event.
- rejected word (wrong substring / not a word / reuse of an accepted word)
  returns `"rejected"`, leaves `seq`, `prompt`, and `current_pid` unchanged, and
  appends a `reject` event with the right `reason`.
- two rejections in the same turn produce two feed events with the SAME `seq`
  but strictly increasing `id`s (the client dedups by `id`; this test pins the
  contract), and the feed never exceeds 8 entries however many events fire.
- `submit_word` by the non-current player returns `None`.
- `advance()` costs the current player a life and passes the bomb; two
  explosions for the same player eliminate them and the survivor wins
  (`is_over`, `winner["id"]`), `deadline is None`.
- eliminated players are skipped by `_pass_bomb` (3 players: eliminate the
  middle one, assert the rotation goes around them).
- `bot_action` with `bot_fail_chance=0` returns a valid unused word containing
  the prompt; with `bot_fail_chance=1` returns `{"kind": "pass"}`.
- `derive_prompts({"cat","catalog","cot"}, min_count=2)` returns prompts with
  every substring appearing in >= 2 words (e.g. `"at"` yes if in >=2, `"log"` no).
- dictionary asset guard: `server/games/words.txt` exists, > 50,000 lines, and
  a 200-line sample matches `^[a-z]{3,15}$`.

(b) Driver integration, in the fake-connection style of `tests/test_server.py`
(read that file first and reuse its helpers/pattern; copy the minimal fake conn
class if the helpers don't import cleanly): start a room with
`game=protocol.GAME_WORD_BOMB`, force determinism by monkeypatching
`server._make_rng` (the seam S&L tests use) AND injecting a small dictionary by
monkeypatching `server.connection.load_dictionary` to return the toy dict above.
Assert:
- `S_GAME_STARTED` carries `game == "word_bomb"`; first `S_GAME_STATE` has
  `name == "word_bomb"`, a `prompt`, and (auto mode) a float `deadline`.
- a valid `C_SUBMIT_WORD` broadcasts fresh state with the next `current_pid`.
- an invalid word broadcasts a `reject` feed event and **`deadline` is
  identical (same float) to before the submission** — the anti-fuse-reset test.
- `C_ROLL_DICE` during word bomb -> `ERR_WRONG_SUBSTATE` error frame.
- human-host mode: `C_ADVANCE_PHASE` from the host explodes the current player
  (lives drop in the next state).
- bots: a room of 1 human + 1 bot with `bot_fail_chance` forced to 1 (patch
  config or pass through the monkeypatched constructor path... simplest: the
  injected `load_dictionary` controls words; for fail-chance patch
  `server.connection.WB_BOT_FAIL_CHANCE` to 1.0) — advance the event loop
  (reuse the existing tests' sleep/advance idiom) until the bot explodes twice
  and the human wins.
- `C_RETURN_TO_LOBBY` from the show-runner tears down to the lobby.

**Expected observation:** `python -m pytest` fully green.

**Counter-move:** If the fake-conn idiom in `test_server.py` doesn't transfer
(e.g. its helpers are file-local), copy the smallest needed pieces into the new
file rather than importing private test internals. If the deadline-identity
assert flakes because `_broadcast_game` isn't awaited deterministically, follow
the same await/drain pattern the existing tests use for `S_GAME_STATE`.

**COMMIT:** `test(word-bomb): rules + driver coverage`

### Step 7 — Client: scene + lobby routing (`client/scenes/word_bomb.py`, `lobby.py`)

**Action:**

(a) `LobbyScene`:
- Add a game picker for the show-runner. State `self.game = protocol.GAME_WORD_BOMB`
  in `on_enter`; a `ui.Button` at rect `(24, 494, 432, 40)` labeled dynamically
  `GAME: WORD BOMB` / `GAME: SNAKES & LADDERS` that toggles between the two
  `protocol.GAMES`; draw + handle it only when `self._can_start()` (same gating
  as the bots stepper, which stays where it is).
- `_start()` sends `self.app.net.send(protocol.C_START_GAME, bots=self.bots, game=self.game)`.
- `on_message` `S_GAME_STARTED`: route by payload —

```python
elif t == protocol.S_GAME_STARTED:
    self.app.gamestate = None
    if msg.get("game") == protocol.GAME_SNAKES_AND_LADDERS:
        from client.scenes.snakes_and_ladders import SnakesAndLaddersScene
        self.app.go_to(SnakesAndLaddersScene(self.app))
    else:
        from client.scenes.word_bomb import WordBombScene
        self.app.go_to(WordBombScene(self.app))
```

(b) New `client/scenes/word_bomb.py` — read `client/scenes/snakes_and_ladders.py`
FIRST and mirror its structure for: reading `self.app.gamestate`, deriving
`my_id` / roles from the state dict, the show-runner button gating, `S_ERROR`
status line, `S_RETURN_TO_LOBBY` -> `LobbyScene`, and how that scene owns and
pumps its `Sfx` instance (init on first click, `pump()` every `update`).

**The look (build exactly this — it is the point of the scene):** a lit
cartoon bomb center-stage with the prompt letters riveted onto it as tiles, a
burning fuse whose spark crawls toward the bomb in real time, the players seated
in a ring around the bomb like a table, and the whole scene "heating up" (color,
pulse, tick rate) as the fuse runs down. Explosions shake and flash. Everything
is drawn with pygame primitives + the bundled mono font — no image assets, all
text ASCII.

Scene constants and pure helpers (module level, so Step 8 can unit-test them):

```python
CENTER = (240, 300)          # bomb center on the 480x800 canvas
BOMB_R = 78                  # bomb body radius
RING_RX, RING_RY = 165, 145  # player-seat ellipse radii
HOT = (255, 120, 60)         # the "about to blow" end of the heat ramp
SPARK = (255, 220, 120)
FLASH_TIME = 0.30            # seconds of explosion whiteout
SHAKE_TIME = 0.55            # seconds of screen shake

def _lerp(c1, c2, t): ...    # per-channel int lerp, t clamped 0..1

def press_of(remaining, total):
    """0.0 (calm) .. 1.0 (about to explode). None deadline -> 0.25 (idle simmer,
    human-host mode)."""

def heat_color(press):
    """ui.MUTED -> ui.ACCENT over press 0..0.6, ui.ACCENT -> HOT over 0.6..1."""

def fuse_points():
    """24 points of a quadratic bezier from the bomb's shoulder (252, 232)
    via control (320, 150) to the fuse tip (360, 130). Computed once at import."""

def seat_positions(n):
    """n points on the ellipse (CENTER, RING_RX, RING_RY), seat 0 at the top
    (-90 deg), clockwise. Used for 2..8 players."""

def feed_line(event) -> str:
    """accept  -> 'NAME: WORD'
       reject  -> 'NAME: WORD x <reason>'   (reasons spelled: not a word /
                   doesn't contain PROMPT / already used; ASCII letter x)
       explode -> 'BOOM! NAME loses a life'
       eliminated -> 'NAME is out'"""
```

**Pressure bands** — one number drives the whole scene. From
`press = press_of(remaining, self._turn_total)`:

| band     | press      | behavior |
|----------|------------|----------|
| calm     | < 0.40     | slow pulse (~1.2 Hz), fuse burns quietly, plain `tick` off |
| hot      | 0.40–0.75  | pulse quickens, heat colors past `ui.ACCENT`, vignette fades in, per-second `tick` at remaining <= 5 |
| critical | > 0.75     | constant micro-shake on the bomb group, prompt tiles rattle, countdown digits pop each second, `tick_hot` at remaining <= 2, rim strobes white in the final second |

Per-frame animation state on the scene: `self._shake = 0.0`, `self._flash = 0.0`,
`self._cool = 0.0` (green "phew" rim flash after an accepted word),
`self._input_flash = 0.0` (red rattle on YOUR rejected word),
`self._particles: list[dict] = []` (each `{"pos": [x, y], "vel": [vx, vy],
"life": float, "max_life": float, "color": (r, g, b)}`, population hard-capped
at 120 — drop the oldest first), `self._seen_id = 0` (highest feed-event `id`
already reacted to — dedup by `id`, never `seq`; rejects share a `seq`),
`self._turn_total: Optional[float] = None` (captured when `(current_pid, seq)`
changes: `max(0.001, deadline - time.time())` if deadline else `None`),
`self._prev_current: Optional[str] = None`, `self._last_tick = None`,
`self._celebrated = False`, `self._sudden_death = False`. `now = time.time()`
once per `draw`.

Draw order (skip everything but the title until the first `S_GAME_STATE`
arrives; every "heat" color below = `heat_color(press)` for the current
`press = press_of(remaining, self._turn_total)`):

1. **Background heat:** `surf.fill(_lerp(ui.BG, (34, 14, 22), press))` — the
   room itself slides toward ember-dark red as the fuse shortens (free: it is
   the fill color). Title `WORD BOMB` size 34 `ui.ACCENT` at (24, 34); room
   code + status line size 14 `ui.MUTED` at (24, 76).
2. **Shake offset** `(ox, oy)` applied ONLY to the bomb group (bomb, fuse, prompt
   tiles, rim pointer) — never a full-frame re-blit (WASM cost). Explosion
   shake and critical-band micro-shake sum:
   `k = self._shake / SHAKE_TIME`;
   `ox = int(math.sin(now * 73) * 12 * k)`; `oy = int(math.cos(now * 91) * 9 * k)`;
   if `press > 0.75`: `ox += int(2 * math.sin(now * 47))`,
   `oy += int(2 * math.cos(now * 53))` — the bomb never sits still once it's
   about to blow.
3. **Fuse:** with `frac = 1 - press` (fraction of fuse left), the spark sits at
   `fuse_points()[round(frac * 23)]`. Draw the unburned fuse only — connected
   2px line segments (color (120, 100, 80)) from point 0 (bomb shoulder) up to
   the spark index. The spark: filled circle radius 4 in `SPARK` plus 4 short
   (7px) radiating lines whose endpoints jitter with `math.sin(now * 40 + i)` —
   a crackling ember, no randomness needed. No deadline (human host): draw the
   whole fuse, spark parked at the tip, no crackle.
4. **Bomb body:** filled circle at `CENTER + (ox, oy)`, radius
   `BOMB_R + pulse`, fill (26, 27, 40); pulse breathes faster and deeper as
   pressure rises: `f = 1.2 + 4.8 * press` (Hz),
   `pulse = int((2 + 7 * press) * (0.5 + 0.5 * math.sin(now * math.tau * f)))`.
   Rim: same circle, `width=3`. Rim color resolution, in priority order:
   while `self._cool > 0` lerp heat -> `ui.GOOD` by `self._cool / 0.25` (the
   room exhales after a save); else in the final second (`remaining < 1`)
   strobe `_lerp(heat, (255, 255, 255), 0.5 + 0.5 * math.sin(now * math.tau * 8))`
   — an 8 Hz white flicker; else plain heat color. Glass highlight: filled
   circle radius 20, color (60, 62, 84), offset (-26, -30) from center.
5. **Countdown ring:** `pygame.draw.arc` in the rect inscribing radius
   `BOMB_R + 14`, from -90 deg sweeping `math.tau * frac` clockwise, `width=5`,
   rim color from item 4. Numeric fuse `f"{remaining:.1f}s"` centered at
   (240, 300 + BOMB_R + 34), rim color; size 26 normally, but at
   `remaining <= 5` each second **pops**:
   `size = 26 + int(8 * max(0.0, 0.3 - (remaining % 1.0)) / 0.3)` (sizes 26–34
   only — `get_font` is lru-cached per size, so the cache stays bounded).
   Human host instead: `HOST HOLDS THE DETONATOR` size 14 `ui.MUTED` there.
6. **Prompt tiles** (the star): one tile per prompt letter, 44x52 rounded rects
   (`border_radius=8`), fill `ui.FIELD`, border 2px heat color, letter size 34
   `ui.TEXT` centered; tiles laid out centered on the bomb, 48px apart, each
   bobbing independently: `dy = int(3 * math.sin(now * 3 + i * 1.1))`. In the
   critical band the bob becomes a rattle: add
   `dx = int(2 * math.sin(now * 31 + i * 2.3))` per tile. Shake offset applies.
7. **Player ring:** `seat_positions(len(players))`, players in `state["players"]`
   order. Per seat: name size 15 centered (truncate to 8 chars + `.` if longer);
   lives as filled circles radius 5, 14px apart, `ui.ACCENT`, centered under the
   name. Bots get a size-12 `bot` tag `ui.MUTED` under the lives. The **current**
   player: a `width=2` heat-color circle radius 30 behind their seat, plus a
   rim pointer — a small filled triangle on the bomb's edge (tip at radius
   `BOMB_R + 10` toward the seat's angle, 10px base), heat color, so the bomb
   visibly "faces" whoever holds it. **Eliminated** (`alive == False`): name in
   `ui.MUTED` with a 1px strikethrough line across it, no life dots.
8. **Your-turn banner:** when `your_turn` and phase `play`: `YOUR TURN - TYPE!`
   size 22 centered at (240, 560), color pulsing at 2 Hz between `ui.TEXT` and
   `ui.ACCENT` (`_lerp(TEXT, ACCENT, 0.5 + 0.5 * math.sin(now * math.tau * 2))`).
   Otherwise: `waiting for <current player name>...` size 16 `ui.MUTED` at the
   same spot.
8b. **Sudden death banner:** when exactly 2 players are alive AND both sit at
   1 life, draw `SUDDEN DEATH` size 20 centered at (240, 110), color pulsing at
   3 Hz between `ui.ACCENT` and `HOT`. (The sting cue fires once on entry —
   see event ingestion.)
9. **Feed:** newest 3 events, `feed_line()` strings, size 15 at x=24,
   y = 585/605/625, newest on top in `ui.TEXT`, each older line lerped 40%
   further toward `ui.MUTED`.
10. **Particles:** every live particle as a filled circle, radius
    `max(1, int(4 * life / max_life))`, its stored color.
11. **Input row** (drawn/handled only when `your_turn` + phase `play`; the
    widgets themselves are created ONCE in `on_enter`, never per frame):
    `ui.TextInput((24, 640, 300, 50), placeholder="type a word...", max_len=32)`
    + `ui.Button("SUBMIT", (336, 640, 120, 50), self._submit)`. `_submit` sends
    `C_SUBMIT_WORD` with `word=self.input.text` (skip if empty), then clears the
    field. Also submit on `K_RETURN`/`K_KP_ENTER` when the field has text —
    check in `handle_event` BEFORE forwarding the event to the TextInput (it
    swallows Enter to unfocus). **Time-pressure ergonomics** (this is a speed
    game; every wasted click is a lost life): (a) on the turn transition TO me,
    set `self.input.text = ""` and `self.input.focused = True` — desktop
    players type immediately, no click-to-focus tax; (b) in the browser,
    `TextInput` opens the native prompt on tap — after forwarding a
    `MOUSEBUTTONDOWN` to it, if `browser_io.is_browser()` and the field now has
    text, call `_submit()` immediately (tap → type → OK is the whole loop; no
    second tap on SUBMIT). Keep the SUBMIT button anyway — it is the desktop
    mouse path and the browser fallback. **Reject rattle:** while
    `self._input_flash > 0` (set when YOUR word bounces), draw the TextInput
    shifted by `int(4 * math.sin(now * 60) * self._input_flash / 0.3)` px on x
    and draw an extra 2px `HOT` border rect around it — an unmissable "nope".
12. **Show-runner controls** (same authority rule as the S&L scene): human-host
    mode gets `DETONATE` at (24, 706, 208, 50) sending `C_ADVANCE_PHASE`;
    `LOBBY` at (248, 706, 208, 50) sending `C_RETURN_TO_LOBBY`, always available
    to the show-runner.
13. **Heat vignette:** a second cached full-screen `SRCALPHA` Surface built ONCE
    in `on_enter` — four nested 40px border frames (`pygame.draw.rect` with
    `width=40`) in (120, 20, 30) with per-frame alphas 24/48/72/96 stepping
    toward the edges, transparent center. Per frame, when `press > 0.4`, blit it
    through `set_alpha(int(150 * (press - 0.4) / 0.6))` — the screen edges close
    in as the fuse shortens. Counter-move if `set_alpha` visibly does nothing on
    a per-pixel-alpha surface under pygbag: quantize press to 8 levels and
    re-fill the cached surface only when the level changes (still no per-frame
    allocation).
14. **Explosion flash:** one `pygame.Surface((480, 800), pygame.SRCALPHA)`
    created ONCE in `on_enter` and reused; while `self._flash > 0`, fill it
    `(255, 230, 200, int(160 * self._flash / FLASH_TIME))` and blit — only
    during the 0.3s flash.
15. **Gameover:** dim the scene with the same cached overlay filled
    (10, 10, 16, 170); `WINNER` size 26 `ui.MUTED` centered (240, 250); the
    winner's name size 44 `ui.ACCENT` centered (240, 305); confetti falling
    behind the text; show-runner keeps `LOBBY`, everyone else gets
    `waiting for the host...` size 14 `ui.MUTED`.

Event ingestion (in `on_message`, when a new `S_GAME_STATE` arrives): scan
`state["feed"]` for events with `id > self._seen_id`, then update
`self._seen_id`. Per new event:
- `explode`: `self._shake = SHAKE_TIME`; `self._flash = FLASH_TIME`; spawn 26
  particles at `CENTER` — angle `i / 26 * math.tau` (even fan, no RNG), speed
  `120 + (i * 37 % 200)` px/s, life `0.5 + (i % 5) * 0.08` s, color cycling
  `[(255, 180, 80), ui.ACCENT, ui.TEXT]`; play sfx `boom`.
- `eliminated`: this blast is bigger — extend the shake to 0.8 s, spawn 14 MORE
  particles (same fan recipe, offset angles by `math.tau / 52`), play `dirge`
  ON TOP of the boom (it layers; the catalog's amplitude headroom exists for
  exactly this).
- `accept`: play `type_ok`; set `self._cool = 0.25` (the green rim exhale).
- `reject`: play `type_bad`; if the event's `pid` is MY id, set
  `self._input_flash = 0.3` (the input rattle is personal — spectators only
  hear it).
- first time `state["winner"]` is set and not `self._celebrated`: play `win`,
  spawn 60 confetti particles (x = `(i * 61) % 480`, y = -10 - (i % 7) * 12,
  vel = (0, 60 + (i * 13) % 100), life 1.5–2.5 s, colors cycling
  `[ui.GOOD, ui.ACCENT, (255, 205, 90)]`), set `self._celebrated = True`.

Also on each state ingest (outside the feed scan, guarded by change — never
re-fire on a same-state broadcast):
- **bomb pass:** if `state["current_pid"] != self._prev_current` and the game
  isn't over: spawn a 10-particle spark trail evenly spaced along the straight
  line from the old holder's seat to the new holder's seat (life 0.3 s, no
  velocity, color `SPARK`), play `pass`; if the new holder is ME, also play
  `alarm` — you should never discover it's your turn by squinting. Then update
  `self._prev_current`.
- **sudden death:** when the 2-alive/1-life-each condition (item 8b) first
  becomes true and `self._sudden_death` is false: play `sudden_death`, set the
  flag (reset it if the condition leaves, e.g. never — lives only fall — so a
  plain latch is fine).

`update(dt)`: decay `self._shake`/`self._flash`/`self._cool`/`self._input_flash`
by `dt` (floor 0); integrate particles (`pos += vel * dt`,
`vel[1] += 220 * dt` gravity, `life -= dt`, drop dead ones, enforce the 120
cap); `self.sfx.pump()`; **countdown ticks** — when `self.app.gamestate` is
present (guard `update` the same way `draw` is guarded) and a deadline exists:
track `self._last_tick = int(max(0, remaining) * 2)` (half-second resolution);
on a boundary change play `tick` at `remaining <= 5` only on whole seconds
(`_last_tick % 2 == 0`), `tick_hot` at `remaining <= 2` on whole seconds, and
at `remaining <= 1` on every half-second boundary — the fuse audibly
accelerates into the blast. Suppress all ticks while `self._shake > 0` (never
tick over a boom).

(c) **Sound design** — extend the `SOUNDS` catalog in `client/sfx.py`
(ADDITIVE ONLY — append the entries below at the END of the dict, touch nothing
else in that file). The catalog is deliberately noise-free and loop-free
(sine/square/saw one-shots, deterministic synthesis, ~0.32 amplitude headroom
for layering) — the design works WITH that: tension comes from *rate and pitch*
of short cues, not from ambience, and dict-append order keeps the S&L cues
first in the prewarm queue (`Sfx.pump` drains FIFO; a word-bomb cue needed
before its prewarm frame just builds lazily — one small cue, already the
module's documented fallback).

The cue sheet (intent → trigger → segments):

```python
# word bomb ---------------------------------------------------------------
# the clock: dry metronome tap; its hotter sibling is higher AND shorter,
# so the acceleration reads even on phone speakers
"tick": [(1300.0, 0.03, "square")],
"tick_hot": [(1650.0, 0.025, "square")],
# bomb handed on: quick 3-note rise, airy sine so it stays out of the way
"pass": [(500.0, 0.04, "sine"), (700.0, 0.04, "sine"), (900.0, 0.05, "sine")],
# it's YOUR problem now: hard double-hit + upward kick, square = urgent
"alarm": [(880.0, 0.07, "square"), (0.0, 0.03, "sine"),
          (880.0, 0.07, "square"), (1100.0, 0.10, "square")],
# valid word: small consonant rise (matches "gold"/"buy" family in feel)
"type_ok": [(620.0, 0.05, "sine"), (930.0, 0.07, "sine")],
# bounced word: low saw shrug, short — it fires often, it must not grate
"type_bad": [(240.0, 0.08, "saw"), (160.0, 0.10, "saw")],
# the explosion: 6-step saw avalanche 180->48 Hz over ~0.67s; stepped
# segments approximate a pitch-drop sweep within the constant-freq model
"boom": [(180.0, 0.06, "saw"), (140.0, 0.07, "saw"), (110.0, 0.08, "saw"),
         (85.0, 0.10, "saw"), (65.0, 0.14, "saw"), (48.0, 0.22, "saw")],
# a player is out: three falling square notes (G4-E4-C4), layered over boom
"dirge": [(392.0, 0.12, "square"), (330.0, 0.12, "square"), (262.0, 0.20, "square")],
# two players, one life each: tritone menace sting (A3 -> D#4), saw growl
"sudden_death": [(220.0, 0.10, "saw"), (0.0, 0.05, "sine"),
                 (220.0, 0.10, "saw"), (0.0, 0.05, "sine"), (311.0, 0.18, "saw")],
```

Mixing rules (enforced in the scene, since cues have no per-play volume):
ticks are suppressed while `self._shake > 0`; `alarm` fires only on the
current-pid TRANSITION (never per broadcast — a re-broadcast of the same state
must be silent); `pass` and `alarm` may overlap by design (pass = room-wide,
alarm = personal punctuation on top); `boom` + `dirge` layering is intentional
(bass avalanche + midrange falling third). The winner reuses the existing
`win` cue — same fanfare across both games keeps the platform's sonic identity.

Performance guardrails (WASM runs this single-threaded): no per-frame Surface
allocations (both overlays — flash and vignette — are cached in `on_enter`);
`fuse_points()` computed once at import; particle population hard-capped at 120
(26 per explosion, +14 on elimination, 10 per pass, 60 confetti; dead ones
dropped and oldest evicted every update); font sizes drawn from a bounded set
(the countdown pop spans only 26–34), so `get_font`'s lru cache stays small;
everything else is circles, lines, arcs, and the already-lru-cached font
renders — the same budget the S&L scene spends.

**Expected observation:** `python -m pytest` green; `python -c "from client.scenes.word_bomb import WordBombScene"` imports.

**Counter-move:** If any coordinate collides with existing lobby widgets (rows
grow downward from y=185 at 46/row; 8 players ends at y≈553), the bots row is at
548 — if the picker at y=494 overlaps the 7th/8th player row visually, that's
pre-existing crowding matching the stepper's tradeoff; leave it (players lists
rarely fill) — do NOT redesign the lobby.

**COMMIT:** `feat(word-bomb): client scene + lobby game picker`

### Step 8 — Client tests (`tests/test_word_bomb_scene.py`)

**Action:** Read `tests/test_snakes_scene.py` first and mirror its headless
setup (dummy video driver / app stub pattern). Cover at minimum:
- Lobby routing: feeding `S_GAME_STARTED {game: "word_bomb"}` to a `LobbyScene`
  lands on `WordBombScene`; `{game: "snakes_and_ladders"}` still lands on
  `SnakesAndLaddersScene`.
- Lobby `_start()` includes `game` in the sent payload (assert on the fake net's
  captured sends, per the existing tests' idiom).
- Scene: with a synthetic `gamestate` dict (build one matching Step 4's
  `public()` shape), `draw()` runs without error on a 480×800 surface, in all
  the visual modes: mid-game with a live deadline (test a nearly-expired one so
  the critical band + strobe + vignette paths execute), human-host mode
  (`deadline: None`), sudden-death (2 alive at 1 life each), and gameover with
  a winner — and again after ingesting a synthetic `explode` feed event
  (particles + shake + flash active).
- Pure design helpers: `press_of(12, 12) == 0.0`, `press_of(0, 12) == 1.0`,
  `press_of(None, anything) == 0.25`; `heat_color(0) == ui.MUTED`,
  `heat_color(0.6) == ui.ACCENT`, `heat_color(1) == HOT`; `len(fuse_points()) == 24`;
  `seat_positions(n)` for n in 2..8 returns n points all inside the 480×800
  canvas with seat 0 topmost; `feed_line()` formats all four event kinds and
  every string is pure ASCII (`s == s.encode('ascii', 'ignore').decode()`).
- Event ingestion: feeding a state whose feed contains one new `explode` event
  sets `_shake`/`_flash` and spawns exactly 26 particles; feeding the same state
  again (same events, same `id`s) spawns nothing (the `_seen_id` guard) — and
  two `reject` events sharing one `seq` but distinct `id`s BOTH react (the
  double-reject test: this is the bug the event `id` exists to prevent). An
  `eliminated`
  event on top spawns the extra 14 and extends the shake. A state whose
  `current_pid` differs from the previous ingest spawns the 10-particle pass
  trail; re-ingesting the identical state does not (the `_prev_current` guard —
  this is the alarm-spam test, pinning the "transition, not broadcast" rule).
- Particle cap: force-spawn past 120 (several synthetic explosions in a row)
  and assert the population never exceeds 120.
- A `reject` event for MY pid sets `_input_flash`; the same event for another
  pid leaves it at 0.
- `_submit` sends `C_SUBMIT_WORD` with the typed word and clears the input;
  empty input sends nothing.
- SFX catalog: all nine new cue names (`tick`, `tick_hot`, `pass`, `alarm`,
  `type_ok`, `type_bad`, `boom`, `dirge`, `sudden_death`) exist in
  `client.sfx.SOUNDS`, every
  segment is a `(float, float, str)` with shape in `{"sine", "square", "saw"}`,
  and every cue's total duration is under 0.8 s (one-shots, no ambience — the
  catalog's contract). Read `tests/test_sfx.py` first and extend it there
  instead if it already validates the catalog.

**Expected observation:** `python -m pytest` fully green.

**Counter-move:** If `test_snakes_scene.py` uses a fixture module, reuse it; if
its app stub is file-local, copy it. Never import pygame display for real.

**COMMIT:** `test(word-bomb): scene wiring + lobby routing`

### Step 9 — Docs

**Action:** `README.md`:
- Add a Milestone/feature bullet in **Status** describing Word Bomb (mirror the
  existing voice; one bullet, 3–6 lines).
- In the repo map, extend the `server/games/` line: `snakes_and_ladders.py,
  word_bomb.py (+ words.txt dictionary)`.
- Add a short **Playing Word Bomb** subsection next to *Playing Snakes &
  Ladders*: the picker in the lobby (default game), the rules (type a word
  containing the prompt before the fuse; lives; last alive wins), human-host
  DETONATE vs auto-mode timer, bots supported.
- Update the lobby instructions line (step 5 of *Run it on a LAN*) to mention
  the `GAME:` toggle.

**Expected observation:** README renders sensibly (visual check); no test reads
README except none — safe. Note: the repo intentionally avoids em dashes in
docs (see commit `59e1e1e`) — use plain hyphens or commas.

**COMMIT:** `docs: word bomb milestone + how to play`

### Step 10 — Full verification (see "Verification runs")

Run the whole verification block below; every item must pass before declaring
victory.

---

## Fork triggers (branch conditions)

- **F1 — dictionary URL dead:** primary 404s/timeouts → dwyl fallback URL (same
  filter). Both dead → abort A2.
- **F2 — `test_server.py` starts games via one shared helper vs. many inline
  sends:** if a helper exists, add the `game=` kwarg there once; else patch each
  call site. (Determine by reading the file in Step 5's counter-move.)
- **F3 — prompt derivation too slow (>10 s after the optimization counter-move):**
  precompute once — `python -c "from server.games.word_bomb import ..."` writing
  `server/games/prompts.txt` (one prompt per line) + change `load_dictionary` to
  read it when present, deriving only as fallback. Commit the file (it is small).
- **F4 — DejaVuSansMono can't render a glyph you wanted:** the plan already
  restricts all scene text to ASCII and draws lives as circles; if any other
  glyph fails, replace with ASCII text, never a new font.
- **F5 — `_reject_turn` breaks on WordBombGame** (it reads `game.awaiting` and
  `game.is_current` — both exist, so it should work): if any attribute is
  missing, add the attribute to `WordBombGame` rather than forking the error
  helper.

## Abort conditions (stop and report; do not improvise)

- **A1:** Baseline `python -m pytest` fails before any change.
- **A2:** Both dictionary URLs unreachable — the game has no word list; stop.
- **A3:** After Step 5 + its counter-move, any pre-existing S&L test still fails
  and the fix would require changing S&L game logic or the `_drive` contract.
- **A4:** `words.txt` after filtering is under 50,000 words or contains
  non-`[a-z]` lines (corrupt source).
- **A5:** Any step requires editing `server/games/snakes_and_ladders.py`,
  `client/board_render.py`, `token_anim.py`, `wheel.py`, `shop_ui.py`, or
  `cutscene.py` — those are out of scope; a plan that needs them has gone
  wrong. (`client/sfx.py` is the one exception: Step 7c APPENDS four entries to
  its `SOUNDS` dict and changes nothing else in that file.)

## Verification runs (proof distinct from the work)

1. `python -m pytest` — entire suite green, including all pre-existing S&L,
   networking, packaging, and scene suites (regression proof) and the new
   `test_word_bomb.py` + `test_word_bomb_scene.py`.
2. `python -m compileall -q client server shared config.py main.py` — exit 0.
3. Cold-load proof:
   `python -c "import time; t=time.time(); from server.games.word_bomb import load_dictionary; w,p,i=load_dictionary(500); print(len(w),'words',len(p),'prompts', round(time.time()-t,1),'s'); assert len(p)>500; assert all(2<=len(x)<=3 for x in p)"`
   — prints counts, under 10 s.
4. Packaging guard: `python -m pytest tests/test_web_packaging.py` — green
   (words.txt lives under `/server`, already excluded from the WASM bundle).
5. Live smoke (scripted, no display): in one PowerShell,
   `$env:PORT=8799; python -m server` in the background; then run a short
   script that opens two real websocket clients (use the `websockets` library,
   already a server dep), creates a room in auto mode with
   `game="word_bomb"` and `bots=0`, joins the second client, starts, submits a
   valid word from whoever `current_pid` says is up, and asserts the next
   `game_state` shows the turn passed. Kill the server after. (This proves the
   real transport path, which the fake-conn tests bypass.) If `websockets`
   isn't installed locally, `pip install -r requirements-server.txt` first.
6. `git status` clean after the final commit; `git log --oneline -7` shows the
   five commits from steps 1, 5, 6/7, 8, 9 (exact grouping may merge 6+7 if the
   executor committed together — five to six commits acceptable).

## Red-team pass (attacks considered, fixes folded in)

- **Fuse-reset exploit** (spam invalid words to keep resetting the timer): the
  single most likely executor mistake, because every other handler calls
  `_after_turn_change`. Fixed by design in Step 5.4 (broadcast-only on reject)
  and *pinned by a dedicated test* (Step 6b deadline-identity assert).
- **Default-game flip breaks existing tests:** anticipated in Step 5's
  counter-move with the exact mechanical fix, so the executor doesn't "fix" it
  by reverting the default (which would betray the mission).
- **Executor guesses at S&L internals:** every needed interface member is listed
  in Recon; steps that need existing idioms (fake conns, scene tests, show-runner
  gating) explicitly order "read that file first and mirror it".
- **Bot with an impossible prompt** (all indexed words used): `bot_action`
  returns `pass` → explodes → game proceeds. Human in the same spot: the timer
  explodes them. No deadlock path: every state has a mover (`_drive` covers
  bot/absent/deadline/host, unchanged).
- **Human-host mode has no timer** (platform parks the turn): reframed as a
  feature — the host IS the bomb (DETONATE button), consistent with the
  platform's human-host philosophy and requiring zero driver changes.
- **Blocking `window.prompt` on mobile while the fuse burns:** server-side
  deadline keeps ticking (authoritative), so the modal can't pause the game;
  this matches Bomb Party pressure. No change needed; noted so the executor
  doesn't "fix" it.
- **Unicode/glyph crashes in the WASM font:** all scene text constrained to
  ASCII (pinned by the `feed_line` ASCII test); lives are drawn circles.
- **"Cool" turning into a WASM slideshow:** the two classic executor mistakes
  are (1) full-screen shake by re-blitting the frame through a temp Surface
  every frame, and (2) allocating overlay/particle Surfaces per frame. The
  design forbids both: shake is a sin/cos offset applied only to the bomb
  group, both overlay Surfaces (flash, vignette) are cached in `on_enter`,
  particles are plain circles with a hard 120 cap, and `fuse_points()` is
  computed at import. Randomness in the FX is deterministic (index-derived),
  so the draw-smoke tests can't flake. The vignette's `set_alpha`-on-SRCALPHA
  trick has an explicit fallback (quantized re-fill) if pygbag's SDL doesn't
  honor it.
- **Audio spam:** `S_GAME_STATE` is re-broadcast on every mutation, so any cue
  keyed to "state arrived" would machine-gun. Every cue in the design is keyed
  to a *transition* (new feed `seq`, `current_pid` change, band/second
  boundary, latched flags for sudden-death and winner) and the alarm/pass
  re-ingest test pins it. Ticks are suppressed during shake so the boom owns
  its moment; all cues are sub-0.8s one-shots inside the catalog's existing
  amplitude headroom, so worst-case layering (boom + dirge + pass) cannot clip
  into distortion.
- **The sound module's contract** (deterministic, noise-free, no loops,
  amortized prewarm) is preserved: nine appended one-shot entries, appended at
  the END of the dict so the S&L cues keep prewarm priority, and nothing else
  in `sfx.py` changes.
- **Feed dedup by turn `seq` (bug found in review, fixed by design):** rejects
  don't bump `seq` (anti-fuse-reset), so two rejects in one turn would share a
  `seq` and a seq-keyed client guard would silently swallow the second one's
  sound and rattle. Feed events therefore carry their own monotonic `id`
  (stamped by `_emit`), the client dedups only by `id`, and both a rules test
  (Step 6) and a scene ingestion test (Step 8) pin it.
- **Dictionary scan blocking the event loop (found in review):** lazy loading
  would run the multi-second substring scan synchronously inside the first
  `start_game` await, freezing every room. Fixed by warming the `lru_cache` at
  boot in `server/__main__.py` (Step 5.7), where blocking is free.
- **Two-tap/`click-to-focus` input tax (found in review):** a speed-typing game
  can't afford it. Desktop auto-focuses (and clears) the field on the turn
  transition to you; the browser auto-submits when the native prompt returns
  text (Step 7, item 11). The SUBMIT button remains as the universal fallback.
- **Client/server clock skew** shifts the *displayed* fuse (remaining =
  server-stamped deadline minus local `time.time()`); the S&L countdown already
  lives with the identical exposure, the server's deadline stays authoritative
  regardless of what the client draws, and `press_of`/`remaining` clamp to
  0..1 / >= 0 so a skewed clock degrades to a cosmetically early/late fuse,
  never a crash or a stuck scene. Accepted platform-wide; do not "fix" it here.
- **Animation state desync from server state:** all motion (pulse, bob, spark,
  heat) is derived per-frame from `time.time()` and the authoritative
  `deadline`/`seq` — the scene stores no simulation of its own beyond decaying
  FX timers and particles, so a missed frame or a mid-turn reconnect can't
  drift the visuals from the truth.
- **Dictionary licensing:** ENABLE is public domain (safe to commit); fallback
  dwyl is also public-domain-declared.
- **Memory blowup from the prompt index:** capped at 200 words/prompt
  (`_BOT_POOL_CAP`), a few MB worst case on a free-tier instance.
- **Race: two movers per room:** untouched — `_drive`'s cancel+`(actor, seq)`
  guard is reused verbatim; rejection deliberately leaves `seq` alone so the
  armed timer stays valid for the still-current actor.
- **Load-bearing asymmetry `seq` on accept vs reject** could confuse the
  executor: stated three times (recon, Step 4 contract, Step 6 tests).

## Self-score

```
Score: 8/8
  1 expected observations  — per step, concrete and checkable
  2 counter-moves          — per step, including the pre-identified test migration
  3 fork triggers          — F1–F5
  4 recon flags            — all checked during planning (URLs live, interface
                             surface confirmed, Dockerfile/pygbag verified)
  5 abort conditions       — A1–A5
  6 verification runs      — 6 proofs, incl. a real-socket smoke distinct from unit tests
  7 red-team pass          — done; fixes folded into steps 5/6 and the ASCII/circle rules
  8 blind executability    — exact paths, names, signatures, payload shapes, rects,
                             colors, animation math, pressure bands, particle counts,
                             a 9-cue sound design with exact waveform segments and
                             mixing rules, commit messages; no unstated decisions remain
Blockers: none
Verdict: SHIP
```
