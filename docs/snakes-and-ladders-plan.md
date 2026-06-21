# Replace Quiplash with Networked Snakes & Ladders (snake-heavy)

> **Multi-session build.** This plan is split into 5 self-contained **session chunks**
> (below). Each chunk leaves `python -m pytest tests/` green and ends with a commit to
> `main`, so work can stop/resume between sessions. Each new session: read **Part A
> (Design Reference)** + the next unchecked chunk in **Part B**, implement it, get tests
> green, run `/code-review high`, fix, commit to `main` (push only when asked), tick the
> box.

## Progress tracker
- [x] **Chunk 1 — Game engine (pure logic) + tests** (additive; biggest) — DONE: `server/games/snakes_and_ladders.py` + `tests/test_snakes_and_ladders.py` (45 tests), `SAL_*`/protocol constants added. Suite 105 green.
- [x] **Chunk 2 — Server turn-driver + integration tests; retire quiplash server-side** — DONE: rewrote `server/connection.py` game-driving section per A5 (single `_drive` funnel + `_auto_turn`/`_turn_deadline` carrying a `(pid, seq)` single-resolution guard, four `C_*` handlers, generalized `_reject_turn`, `_clamp_bots`, `_make_rng` seam; `_on_roster_change` auto-plays an absent current actor without resetting a present one's deadline). Deleted `server/games/wrong_answers.py` + `prompts.py` + `tests/test_wrong_answers.py`; rewrote the WAO in-game section of `tests/test_server.py` into S&L driver tests. Suite 109 green. WAO protocol/config constants + client scene kept until Chunk 5.
- [x] **Chunk 3 — Client foundation: board render + audio + animation timeline** — DONE: `client/board_render.py` (serpentine `cell_to_xy`/`BoardLayout` + grid/snake/ladder/glyph/token/legend drawing), `client/sfx.py` (procedural-WAV synth, lazy/guarded mixer that degrades to silent + retries after autoplay-block), `client/token_anim.py` (`seq`-gated turn-timeline player in cell space). Headless tests `tests/test_{board_render,sfx,token_anim}.py` (+32). Suite 141 green.
- [ ] **Chunk 4 — Client components: wheel + shop UI + cutscene**
- [ ] **Chunk 5 — Scene + go-live: wire, route lobby, retire quiplash client-side, docs**

---

## Context

`coolboardgamegame` is a Jackbox-style **multiplayer party-game framework**: a Pygame
client (compiled to WebAssembly via pygbag, deployed to GitHub Pages) talking to an
authoritative websocket server. It ships exactly **one** game today — "Wrong Answers
Only" (quiplash-style, internally `wrong_answers`).

Goal: **remove quiplash and replace it with a Snakes & Ladders game with far too many
snakes** (winning is brutally hard), plus a powerup/debuff system, gold economy,
"Wheel of Names"-style event wheels, shop tiles, sound effects, an animated turn
transition, and an on-screen legend. The board is freshly randomized each game.

**Confirmed decisions (from clarifying Q&A):**
- **Networked multiplayer**, server-authoritative, same room/lobby/server framework as
  quiplash. Players join from separate devices; the server owns board/turn state and
  broadcasts it; clients render & animate.
- **Dice move; wheels are events.** Roll dice to move; landing on a *wheel tile* spins a
  Wheel-of-Names for a random powerup / debuff / gold outcome.
- **AI = server-side bot players** that fill seats and auto-take turns (enables solo vs.
  bot). The bot "AI" is trivial; the work is the plumbing.
- **2–8 players** (framework cap 8), humans + bots.

**Assumptions** (the user's prompt had ~140 truncated lines I couldn't read; these are
defaults, all tunable in `config.py`): 100-cell 10×10 serpentine board; 14 snakes vs 4
ladders; exact-finish (overshoot bounces back); powerups held & used by choice
(immunity/boost/double/reroll), debuffs auto-applied (skip-turn/slip-back/gold-tax), shop
sells powerups for gold; **audio synthesized procedurally** (no bundled assets), silent
if the mixer is unavailable.

**Landing:** no `gh` CLI in this repo and CI only builds/deploys (doesn't run tests), so
the autopilot PR/auto-merge gate doesn't apply. `python -m pytest tests/` is the gate.
Per project workflow: verify (tests + adversarial review), **commit to `main` locally**,
**push only when asked**.

---

# PART A — Design Reference (shared by all chunks)

### A1. Architecture mirror

| Layer | Quiplash (today) | Snakes & Ladders (new) |
|---|---|---|
| Pure server logic | `server/games/wrong_answers.py` | `server/games/snakes_and_ladders.py` |
| Client scene | `client/scenes/wrong_answers.py` | `client/scenes/snakes_and_ladders.py` + `client/{board_render,token_anim,wheel,shop_ui,cutscene,sfx}.py` |
| Wire contract | `shared/protocol.py` | edit same |
| Tunables | `config.py` (`WAO_*`) | edit same (`SAL_*`) |
| Connection driver | `server/connection.py` | rewrite game-driving section |
| Launch routing | `client/scenes/lobby.py` | edit same |

**Core idea (makes a networked, animated board game testable):** the server is
authoritative and computes each turn's full resolution as an **ordered, serializable
timeline** (`last_turn.steps`) with a monotonic `seq`. Clients *replay* it as animation
(token hops, snake slides, dice, wheel spin, cutscenes, SFX), keyed off `seq` so each
turn animates exactly once. Authority = server (pure, unit-testable); animation = client.

### A2. Server game class — `server/games/snakes_and_ladders.py`

Pure state, no asyncio/sockets (like `WrongAnswersGame`). Inject a seedable
`random.Random` for deterministic tests.

```python
class SnakesAndLaddersGame:
    def __init__(self, contestants, *, bots=(), cells, snake_count, ladder_count,
                 shop_tiles, wheel_tiles, gold_tiles, debuff_tiles,
                 starting_gold, dice_sides, exact_finish, rng=None): ...
    # contract used by connection.py:
    .phase            # PHASE_PLAY | PHASE_GAMEOVER
    .deadline         # Optional[float], set by connection layer (auto mode)
    .is_over          # property -> phase == PHASE_GAMEOVER
    def is_contestant(self, pid) -> bool
    def all_submitted(self, connected_ids) -> bool   # ALWAYS False (turn game; documents contract)
    def advance(self) -> str                          # host force-advance: auto-resolve current actor
    def public(self, for_pid, host_id) -> dict
    # game-specific:
    .awaiting         # "roll" | "shop"  (sub-state within PHASE_PLAY)
    .current_pid; .bot_ids
    def is_current(self, pid) -> bool
    def roll(self, pid) -> bool          # resolves the whole turn into last_turn
    def use_powerup(self, pid, item) -> bool   # pre-roll; modifies upcoming roll; does NOT pass turn
    def buy_item(self, pid, item) -> bool      # shop sub-state; spends gold; passes turn
    def skip_shop(self, pid) -> bool           # shop sub-state; passes turn
    def bot_action(self, pid) -> dict          # pure: {"kind":"roll"|"buy"|"skip"|"use","item"?}
```

**Phases collapsed (key simplification):** only `PHASE_PLAY` and `PHASE_GAMEOVER`. Shop is
a *sub-state* (`awaiting=="shop"`) on the same player's turn, **not** a separate phase —
removes "current player changed but phase didn't" races with bots/timers.

**`roll()` canonical step order (the explicitly-easy-to-get-wrong part — comment heavily):**
1. `roll` — raw die (+ pre-used powerup: boost `+3`, double `×2`).
2. `move` — advance along a `path`; if `exact_finish` and overshoot last cell, **bounce**:
   `target = last - (target - last)`.
3. **At most one transport** at the landing cell — snake (head→tail) XOR ladder
   (bottom→top), immunity-gated. **No chaining** (board-gen guarantees endpoints aren't
   themselves specials).
4. `tile` at the *post-transport* cell: `wheel` (server picks outcome index from rng) /
   `gold` / `debuff` / `shop_enter` (sets `awaiting="shop"`, keeps `current_pid`).
5. `win` if landed exactly on the last cell → `PHASE_GAMEOVER`.
6. Advance to next non-skipped player (consume one `skip_next`), unless in shop sub-state.

**`last_turn` shape** (in `public()`, replayed by client):
```python
{ "seq":7, "pid":"p2", "name":"Bob", "ended":True,
  "steps":[
    {"t":"roll","die":4,"raw":4,"modifier":None},
    {"t":"move","frm":12,"to":16,"path":[13,14,15,16]},
    {"t":"snake","frm":16,"to":6},                         # or "ladder"/"immunity_used"
    {"t":"tile","kind":"wheel"},
    {"t":"wheel","table":[...],"index":2,"outcome":{"kind":"gold","amount":50}},
    {"t":"gold","pid":"p2","delta":50,"total":130},
    {"t":"win"} | {"t":"shop_enter"} | {"t":"skipped","pid":...},
  ]}
```

**`public(for_pid, host_id)` shape:**
```python
{ "name":"snakes_and_ladders", "phase":..., "awaiting":...,
  "board":{"cells":100,"cols":10,"snakes":{16:6,...},"ladders":{3:21,...},
           "wheel_tiles":[...],"shop_tiles":[...],"gold_tiles":[...],"debuff_tiles":[...]},  # static; client caches
  "players":[{"id","name","pos","gold","items":[...],"is_bot":bool,"finished":bool},...],
  "current_pid":..., "your_turn":bool, "your_id":..., "you_role":"host|contestant|spectator",
  "deadline":float|None, "last_turn":{...}|None, "winner":{...}|None,
  "shop":{"stock":[{"item","price","affordable"}...]} }   # ONLY for the current player while awaiting=="shop"
```
Only secrecy rule: `shop` present solely for the current player in the shop sub-state.

**Catalogs (data-driven, tunable):** powerups `immunity`/`boost`/`double`/`reroll`;
debuffs (auto) `skip_next`/`slip_back`/`gold_tax`; wheel = weighted outcome table.

### A3. Protocol — `shared/protocol.py`
**Add:** `GAME_SNAKES_AND_LADDERS`; `PHASE_PLAY`, `PHASE_GAMEOVER`; `C_ROLL_DICE`,
`C_USE_POWERUP`, `C_BUY_ITEM`, `C_SKIP_SHOP`; `ERR_NOT_YOUR_TURN`, `ERR_WRONG_SUBSTATE`,
`ERR_BAD_ITEM`. `C_START_GAME` gains optional `{bots:int}` (comment only).
**Keep:** generic `C_ADVANCE_PHASE` (now host force-advance), `C_RETURN_TO_LOBBY`, infra
error codes. **Remove (Chunk 5):** `C_SUBMIT_ANSWER`, `C_SUBMIT_VOTE`,
`GAME_WRONG_ANSWERS`, `PHASE_PROMPT/VOTE/REVEAL/FINAL`, `ERR_BAD_ANSWER`, `ERR_BAD_VOTE`.

### A4. Config — `config.py` (`SAL_*`)
`SAL_MIN_CONTESTANTS=2`, `SAL_BOARD_CELLS=100`, `SAL_SNAKE_COUNT=14`, `SAL_LADDER_COUNT=4`,
`SAL_SHOP_TILES=4`, `SAL_WHEEL_TILES=5`, `SAL_GOLD_TILES=6`, `SAL_DEBUFF_TILES=5`,
`SAL_STARTING_GOLD=100`, `SAL_DICE_SIDES=6`, `SAL_EXACT_FINISH=True`, item prices,
`SAL_ROLL_SECONDS=30`, `SAL_SHOP_SECONDS=20`, `SAL_BOT_DELAY_SECONDS=1.2`. Add
`assert SAL_SNAKE_COUNT > SAL_LADDER_COUNT` + a usable-cells sanity assert. (Remove
`WAO_*` in Chunk 5.)

### A5. Connection driver — `server/connection.py`
Single unified turn-driver replacing `_arm_timer`/`_maybe_autoadvance`/`_advance`/
`_phase_deadline` (keep `_cancel_timer`). New handlers for the four C_* actions; keep
`C_ADVANCE_PHASE`/`C_RETURN_TO_LOBBY`.
```
_drive(room, game)  # call after EVERY mutation; idempotent & self-rescheduling
  cancel prior timer; if game.is_over -> clear deadline, return
  cur = game.current_pid
  if cur is a bot OR an absent human (slot gone/disconnected):
      deadline=None; schedule _auto_turn(SAL_BOT_DELAY) -> act -> broadcast -> _drive   (chains)
  elif host_mode==AUTO:
      deadline = now + (SAL_SHOP_SECONDS if awaiting=="shop" else SAL_ROLL_SECONDS)
      schedule _turn_deadline -> timeout auto-roll/auto-skip -> broadcast -> _drive
  else (HUMAN host): deadline=None  (park; host clicks NEXT = C_ADVANCE_PHASE)
```
- `_auto_turn`: bots use `bot_action()`; absent humans just `roll()`/`skip_shop()`. Bots
  play in **both** host modes.
- Handlers mirror `_on_submit_answer`: call game method; on failure send specific error via
  generalized `_reject_turn(game,pid,want)`; else broadcast + `_drive`. `use_powerup`
  re-drives without passing the turn.
- `_on_roster_change` → `_drive` (not the deleted `_maybe_autoadvance`) so a current actor
  who disconnects can't deadlock the game.
- Every timer callback re-checks `is_over`/`current_pid`/membership; `_drive` always
  cancels prior timer first → **no double-advance races** (`seq`-monotonic test catches it).

**Bots are NOT room `Player`s** — they live only in the game object (`bot_ids`, positions,
gold, items). So `server/rooms.py` needs **no change**, and existing broadcasts
(`room.players … if p.connected`) never touch bots (no `conn is None` guards). Bots are
created in `_on_start_game` from `msg["bots"]`, clamped to
`MAX_PLAYERS_PER_ROOM - human_count`; need ≥2 total (1 human + 1 bot works).

### A6. Client components (self-contained, tunable)
- **`client/board_render.py`** — serpentine cell↔pixel math + draws grid/snakes/ladders/
  glyphs/legend. **Cell→pixel, comment line-by-line:**
  ```python
  # cell n in 1..N, cols×rows, row 0 = BOTTOM. Boustrophedon: even rows L→R, odd R→L.
  # pygame y grows DOWN, so invert from the bottom edge.
  idx=n-1; row=idx//cols; col_in_row=idx%cols
  col = col_in_row if row%2==0 else (cols-1)-col_in_row
  cx = origin_x + col*cell_px + cell_px//2
  cy = board_bottom_y - row*cell_px - cell_px//2
  ```
- **`client/token_anim.py`** — timeline player: consumes `last_turn.steps`, hops token
  along `path`, slides on snake/ladder, hands off to wheel, fires SFX, then snaps tokens to
  authoritative `players[*].pos`. Exposes `is_playing` (scene **locks input** while
  animating). Plays a `last_turn` only when its `seq` exceeds last-seen (animate-once).
- **`client/wheel.py`** — Wheel-of-Names spin decelerating to the server-chosen `index`
  (client never decides the outcome).
- **`client/shop_ui.py`** — panel from `game["shop"].stock`; buttons → `C_BUY_ITEM`/`C_SKIP_SHOP`.
- **`client/cutscene.py`** — animated turn transition ("Alice's turn", "Bob skipped!", win banner).
- **`client/sfx.py`** — synth tones → in-memory **WAV** via `pygame.mixer.Sound(BytesIO(...))`
  (format-robust, no numpy/assets). Lazy `init()` on first click (browser autoplay gesture);
  every mixer call try/except behind `_OK` → **silent no-op** if unavailable.

**Scene (`client/scenes/snakes_and_ladders.py`)** orchestrates (same `Scene` lifecycle as
`WrongAnswersScene`, reads `self.app.gamestate`). Caches static `board` from first state;
animates `last_turn` on `seq` bump; renders board → tokens → HUD (turn, your gold, usable
item buttons, deadline countdown reusing WAO's math) → ROLL (when `your_turn and
awaiting=="roll" and not animating`) → shop overlay → legend → LEAVE/host-NEXT/gameover
BACK. **Portrait 480×800:** header ~120 → 432×432 board → legend → controls ~120 (≈712<800);
shop overlays the board. Reuse `client/ui.py` palette/widgets + `_MARGIN=24`.
**Legend:** "Snake-heavy board · freshly randomized each game · seeded with powerups,
debuffs, shops & wheels."

### A7. Risks (shared) & mitigations
1. **Step ordering in `roll()`** — tile on *post*-transport cell; ≤1 transport, no chaining;
   win after bounce; never snake the start cell. Pinned by board-gen invariants + tests.
2. **Exact-finish bounce** — `target = last-(target-last)`; test both edges.
3. **Auto/bot timer races** — single `_drive` funnel, cancel prior timer first, re-check
   state in callbacks; `seq`-monotonic test catches double-resolves.
4. **Bots in roster / sending to bots** — eliminated by keeping bots out of `room.players`.
5. **Disconnect-during-turn deadlock** — `_on_roster_change`→`_drive` auto-plays absent
   current actor; positions keyed by pid survive slot expiry.
6. **pygbag audio** — lazy init on first click, guarded `_OK` no-op, synth WAV; may be
   silent in-browser (acceptable).
7. **Portrait fit** — fixed budget; shop overlays; `test_canvas_is_portrait` stays green.
8. **Suite green** — removed protocol names referenced only by edited/deleted files (grep
   before deleting).

---

# PART B — Session Chunks (sequenced; each ends green + committed)

Per-chunk loop (project workflow): implement → `python -m pytest tests/` green →
`/code-review high` → fix → commit to `main` → (push when asked) → tick the box.

## Chunk 1 — Game engine (pure logic) + tests  *(additive, can't break anything; biggest)*
**Goal:** the entire authoritative game logic, fully unit-tested headless, before any
network or pixel work. De-risks the hardest parts (step ordering, bounce, transport).
**Files:**
- Edit `shared/protocol.py` — **add** S&L constants (A3); keep WAO ones.
- Edit `config.py` — **add** `SAL_*` (A4) + asserts; keep `WAO_*`.
- Create `server/games/snakes_and_ladders.py` — full class per A2 (board gen, `roll()`
  timeline, catalogs, wheel, `bot_action`, `public`, contract methods).
- Create `tests/test_snakes_and_ladders.py` — pure-logic suite (seeded rng): board
  invariants (snakes>ladders, head>tail, bottom<top, no overlaps, nothing on first/last);
  randomized-per-seed + deterministic-per-seed; dice/wheel determinism; simple move; snake
  slide; ladder climb; **canonical step ordering**; **exact-finish bounce** (98+5→97,
  land-100 wins, bounce-into-snake); gold/wheel/debuff tile effects; shop enter pauses turn
  + `shop` secrecy; buy spends gold & passes turn (reject unaffordable/dup/unknown); skip
  passes turn; powerups (boost/double/immunity) modify roll / block one snake & consumed;
  skip-turn debuff; not-your-turn & wrong-substate rejected; `bot_action`; `public()`
  roles/views + bot rows; `all_submitted` always False; terminal no-ops; `seq` monotonic.
**Verify:** `python -m pytest tests/` green (WAO untouched + new S&L logic). **Done when**
committed.

## Chunk 2 — Server turn-driver + integration tests; retire quiplash server-side
**Goal:** wire the engine into the websocket server (turn-based, bots, timers, disconnect),
verified headless over `FakeConn`.
**Files:**
- Edit `server/connection.py` — rewrite the game-driving section per A5 (`_drive`,
  `_auto_turn`, `_turn_deadline`, four action handlers, `_on_start_game` with bots,
  generalized `_reject_turn`, `_on_roster_change`→`_drive`); swap imports to S&L; delete
  `_PHASE_SECONDS`/`_advance`/`_maybe_autoadvance`/`_arm_timer`/`_phase_deadline`. Add a
  `_make_rng()` seam so tests can force a seed.
- Edit `tests/test_server.py` — **rewrite only the WAO in-game section** (~lines 279–414);
  keep `FakeConn`/`settle`/`open_conn`/lobby/host/disconnect/health tests intact. New:
  start needs ≥2; 1 human+1 bot starts; auto roll flow & turn alternation; not-your-turn
  over wire; bot auto-takes a turn (monkeypatch tiny `SAL_BOT_DELAY_SECONDS`); bot in
  human-host mode; auto deadline auto-rolls (tiny `SAL_ROLL_SECONDS`); human-host
  force-advance + non-host rejected; shop buy over wire; disconnect-during-turn unsticks;
  return-to-lobby ends game; bots never receive sends.
- Delete `server/games/wrong_answers.py`, `server/games/prompts.py`, `tests/test_wrong_answers.py`.
- Verify `server/rooms.py` needs no change (per A5).
- (Keep WAO protocol/config constants — the client wao scene still uses them until Chunk 5.)
**Verify:** `python -m pytest tests/` green (server fully tested headless with bots).
**Done when** committed.

## Chunk 3 — Client foundation: board render + audio + animation timeline
**Goal:** the standalone client modules the scene will compose, with the serpentine math
unit-tested.
**Files:**
- Create `client/board_render.py` (A6 serpentine math + drawing + legend).
- Create `client/sfx.py` (A6 procedural audio, guarded).
- Create `client/token_anim.py` (A6 timeline player).
- (Optional) `client/fx.py` (tiny particle/flash; can fold into token_anim).
- Add to `tests/` (headless, no display): a `test_board_render.py` for `cell_to_xy` math
  (corners, serpentine flips, bottom-origin inversion) and an `sfx` degrades-to-silent test.
**Verify:** `python -m pytest tests/` green (+ new math test). **Done when** committed.

## Chunk 4 — Client components: wheel + shop UI + cutscene
**Goal:** the interactive/overlay components.
**Files:** Create `client/wheel.py`, `client/shop_ui.py`, `client/cutscene.py` (A6). Keep
each self-contained and tunable. Light import/smoke coverage where a pure helper exists.
**Verify:** `python -m pytest tests/` green; modules import cleanly. **Done when** committed.

## Chunk 5 — Scene + go-live: wire, route lobby, retire quiplash client-side, docs
**Goal:** assemble the playable scene and switch the app over to Snakes & Ladders.
**Files:**
- Create `client/scenes/snakes_and_ladders.py` (A6 orchestrator).
- Edit `client/scenes/lobby.py` — route `S_GAME_STARTED` → `SnakesAndLaddersScene`; add a
  show-runner `- bots N +` stepper sending `C_START_GAME, bots=N`.
- Delete `client/scenes/wrong_answers.py`.
- Edit `shared/protocol.py` + `config.py` — **remove** the remaining WAO constants (A3/A4).
- Edit `README.md` (describe the new game); optional: window caption in `client/__main__.py`.
**Verify:** `python -m pytest tests/` fully green **and** manual desktop smoke
(`python -m server` + `python -m client`): host an auto room, add a bot, start, roll →
watch token hop / snake slide / wheel spin / shop / turn-transition cutscene / SFX, reach
the win cutscene; confirm portrait layout + legend. Confirm `test_web_packaging.py` /
`test_async_entry.py` still green (web/pygbag unaffected). **Done when** committed.

---

## Verification summary
- **Primary gate:** `python -m pytest tests/` green at the end of every chunk (CI only
  builds/deploys). Pure-logic + headless server integration cover board gen, the full turn
  machine, bots, timers, disconnect, shop, win.
- **Manual (Chunk 5):** desktop smoke for rendering/audio/cutscene/portrait (tests never
  `pygame.init`).
- **Web/pygbag:** async loop & packaging unchanged; audio degrades to silent if blocked.
