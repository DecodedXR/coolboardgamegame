# Wargame: Type-on-the-Bomb (no click, no prompt popup, live typing for everyone)

## Mission

Word Bomb input today: on your turn you must click/tap the text box at the
bottom; in the browser that tap opens a `window.prompt` dialog (an extra modal
step). Change it so:

1. On your turn you just type — no clicking any box first, no prompt dialog on
   desktop/desktop-browser.
2. The typed text renders **on the bomb itself** (not a bottom input row).
3. Every other player (and spectators/human host) sees the current player's
   in-progress typing in real time.

Mobile browsers still need `window.prompt` (a canvas cannot summon a phone's
soft keyboard) — that fallback moves to **tapping the bomb**, and it
auto-submits on OK.

## Ground truth (verified during recon — do not re-derive)

- Scene: `client/scenes/word_bomb.py`. It builds `ui.TextInput` + SUBMIT button
  at `on_enter` (lines ~131–132), routes events in `handle_event` (~354), draws
  the input row in `_draw_input` (~578), and already auto-focuses on turn
  transition in `_ingest_state` (~236–239).
- `ui.TextInput.handle` (client/ui.py:101) is where the browser tap→prompt
  lives. **Do not edit `client/ui.py`** — TextInput is used by connect/menu/
  lobby scenes and its tests (`tests/test_browser_input.py`) must keep passing.
- `client/browser_io.py` has `is_browser()` and `prompt(label, current)`.
- Wire protocol: `shared/protocol.py`; client→server constants `C_*`,
  server→client `S_*`. Server dispatch table: `server/connection.py:90–106`;
  word-submit handler `_on_submit_word` at :360; broadcast pattern
  `_broadcast_game` at :428 (`asyncio.gather` of `_safe_send` to every
  connected room player).
- Inbound server messages go straight to the active scene:
  `client/__main__.py:77` calls `self.scene.on_message(msg)`. Scenes ignore
  unknown types (if/elif chains) — a new S_ message needs no central dispatch
  change, and a stray one arriving in the lobby is silently ignored.
- Game rules object: `server/games/word_bomb.py` (`WordBombGame`);
  `is_current(pid)`, `is_over` exist. Typing must NOT touch `seq`/state (the
  anti-fuse-reset rule) — relay only, never `_drive`.
- Bomb geometry: `CENTER = (240, 300)`, `BOMB_R = 78`, canvas 480x800. Prompt
  tiles sit centered ON the bomb (tile bottom ≈ y=326). Countdown label at
  y≈412. So the typed word goes at `(240, CENTER[1]+48)` — inside the bomb,
  below the tiles, above the countdown.
- Tests that will break: `tests/test_word_bomb_scene.py`
  `test_submit_sends_the_word_and_clears_but_ignores_empty` (uses
  `scene.input.text`). The `_input_flash` tests (:228, :262–269) must keep
  passing — keep the `_input_flash` timer, it now rattles the on-bomb text.
- Run tests with `python -m pytest` from the repo root (pytest.ini present).

## Design decisions (locked — do not re-open)

- Scene owns a plain string `self.typed` (max 32 chars). The word-bomb scene's
  `ui.TextInput` and SUBMIT button are **deleted** (Enter submits; mobile
  prompt auto-submits).
- New wire messages in `shared/protocol.py`:
  - `C_TYPING = "typing"` — `{text}` (current player, word bomb only)
  - `S_TYPING = "typing_update"` — `{pid, text}`
- Server relays typing fire-and-forget: no game mutation, no seq, no timers,
  silent drop when illegal (never an S_ERROR per keystroke).
- Receivers store `self.live_typing` and draw it only while its sender is
  still `current_pid`; cleared on every `current_pid` change.
- Sender clears everyone by sending `C_TYPING text=""` right after submitting.

## Battle plan

### Step 1 — protocol constants

Edit `shared/protocol.py`. Under the Word Bomb client→server section (after
`C_SUBMIT_WORD`, line ~54) add:

```python
C_TYPING = "typing"                  # {text}            (current player, word bomb; relayed, not stored)
```

Under the Server→Client section (after `S_ERROR`/`S_PONG`) add:

```python
S_TYPING = "typing_update"           # {pid, text}  (word bomb: current player's in-progress text)
```

- **Expected observation:** `python -m pytest tests/test_word_bomb.py -q`
  still passes (constants are additive).
- **Counter-move:** import error / typo → re-read the diff; nothing else in
  this step can fail.

### Step 2 — server relay handler

Edit `server/connection.py`:

1. In `self._dispatch` (line ~103, next to `C_SUBMIT_WORD`) add:
   `protocol.C_TYPING: GameServer._on_typing,`
2. Add next to `_on_submit_word` (~line 376):

```python
async def _on_typing(self, ctx: ConnCtx, msg: dict[str, Any]) -> None:
    """Relay the current player's in-progress text to the room. Fire-and-forget:
    never mutates game state / seq / timers (the anti-fuse-reset rule), and
    silently drops illegal senders — erroring per keystroke would spam a
    client whose turn just ended mid-word."""
    room, player, game = self._require_game(ctx)
    if game is None or not isinstance(game, WordBombGame):
        return
    if game.is_over or not game.is_current(player.id):
        return
    text = "".join(ch for ch in str(msg.get("text") or "") if ch.isprintable())[:32]
    frame = protocol.encode(protocol.S_TYPING, pid=player.id, text=text)
    await asyncio.gather(
        *(self._safe_send(p.conn, frame) for p in room.players.values() if p.connected),
        return_exceptions=True,
    )
```

(`WordBombGame` is already imported in connection.py — `_on_buy_item` line 344
uses it.)

- **Expected observation:** server module imports:
  `python -c "import server.connection"` exits 0.
- **Counter-move:** NameError on `WordBombGame`/`asyncio` → check the existing
  imports at the top of connection.py and match them.

### Step 3 — client scene: own the text, type with no click

Edit `client/scenes/word_bomb.py`.

**3a. `on_enter`:** replace

```python
self.input = ui.TextInput((24, 640, 300, 50), placeholder="type a word...", max_len=32)
self.submit_btn = ui.Button("SUBMIT", (336, 640, 120, 50), self._submit)
```

with

```python
self.typed = ""          # my in-progress word (your-turn only)
self.live_typing = ""    # the current player's in-progress word (theirs)
```

Keep `self._input_flash` and both `detonate_btn`/`lobby_btn` exactly as they
are.

**3b. `_submit`:**

```python
def _submit(self) -> None:
    if not self.typed:
        return
    self.app.net.send(protocol.C_SUBMIT_WORD, word=self.typed)
    self.typed = ""
    self.app.net.send(protocol.C_TYPING, text="")   # clears everyone's view
```

**3c. `_ingest_state`:** in the `cur != self._prev_current` block, replace the
two `self.input...` lines (`self.input.text = ""` / `self.input.focused =
True`) with `self.typed = ""`, and add `self.live_typing = ""` unconditionally
inside the same `cur != self._prev_current` block (any bomb pass invalidates
the old typer's text).

**3d. `on_message`:** add a branch:

```python
elif t == protocol.S_TYPING:
    if msg.get("pid") == self.gs.get("current_pid") and msg.get("pid") != self.my_id:
        self.live_typing = str(msg.get("text") or "")[:32]
```

**3e. `handle_event`:** replace the whole "Input row" block (everything after
the show-runner controls) with:

```python
# Typing, live only on our own play-turn. No focus, no box: keys go
# straight onto the bomb, and every edit is relayed to the room.
if gs.get("your_turn") and gs.get("phase") == protocol.PHASE_PLAY:
    if event.type == pygame.KEYDOWN:
        before = self.typed
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._submit()
            return
        if event.key == pygame.K_BACKSPACE:
            self.typed = self.typed[:-1]
        elif event.unicode and event.unicode.isprintable() and len(self.typed) < 32:
            self.typed += event.unicode
        if self.typed != before:
            self.app.net.send(protocol.C_TYPING, text=self.typed)
    # Browser touch fallback: a phone can't type into the canvas, so
    # tapping the bomb opens the native prompt, then submits at once.
    elif (browser_io.is_browser() and event.type == pygame.MOUSEBUTTONDOWN
            and event.button == 1
            and math.hypot(event.pos[0] - CENTER[0], event.pos[1] - CENTER[1]) <= BOMB_R + 16):
        self.typed = browser_io.prompt("type a word...", self.typed)[:32]
        self._submit()
```

Note `_submit` no-ops on empty text, so a cancelled prompt is harmless.

**3f. drawing:** delete `_draw_input` and the section-11 call to it, plus the
`self.submit_btn.draw(surf)` reference. Add a module-level pure helper next to
`feed_line`:

```python
def tail_that_fits(text: str, max_w: int, measure) -> str:
    """Longest tail of ``text`` whose ``measure(tail)`` fits ``max_w`` — long
    words keep the end (where the typing happens) on screen."""
    while text and measure(text) > max_w:
        text = text[1:]
    return text
```

and a scene method, called from `draw` right after `_draw_prompt_tiles`
(so it inherits the same shake `ox, oy`), only when
`gs.get("phase") == protocol.PHASE_PLAY`:

```python
def _draw_typed(self, surf, gs, now, ox, oy) -> None:
    mine = bool(gs.get("your_turn"))
    text = self.typed if mine else self.live_typing
    if not mine and not text:
        return
    shown = tail_that_fits(text, 400, lambda s: ui.get_font(20).size(s)[0])
    if mine and (now % 1.0) < 0.6:
        shown += "_"                       # blinking caret: type right here
    if self._input_flash > 0:              # the reject rattle, now on the bomb
        ox += int(4 * math.sin(now * 60) * self._input_flash / 0.3)
        color = HOT
    else:
        color = ui.GOOD if (gs.get("prompt") or "").lower() in text.lower() else ui.TEXT
    ui.Label(shown, (CENTER[0] + ox, CENTER[1] + 48 + oy), 20, color, center=True).draw(surf)
```

- **Expected observation:** `grep -n "self.input" client/scenes/word_bomb.py`
  returns nothing; `python -m pytest tests/test_word_bomb_scene.py -q` fails
  ONLY on `test_submit_sends_the_word_and_clears_but_ignores_empty` (fixed in
  Step 4).
- **Counter-move:** other scene tests fail → read the failure; the likely
  cause is a leftover reference to `input`/`submit_btn` in `draw` or
  `handle_event` — remove it. Do not reintroduce TextInput.

### Step 4 — tests

**4a. Fix:** in `tests/test_word_bomb_scene.py`, update
`test_submit_sends_the_word_and_clears_but_ignores_empty` to use
`scene.typed` instead of `scene.input.text`, and assert the trailing
`C_TYPING text=""` send (read the file's fake-net helper first; assert with
whatever pattern its other tests use for `net.send` recording).

**4b. Add scene tests** (same fakes/style as the existing ones in that file):

1. Your-turn KEYDOWN `"a"` appends to `typed` AND sends `C_TYPING` with
   `text="a"`; a non-text key (e.g. `K_LSHIFT`, `unicode=""`) sends nothing.
2. Enter with text calls submit: sends `C_SUBMIT_WORD` then `C_TYPING ""`,
   `typed == ""`.
3. `on_message` with `S_TYPING` for the current (other) player sets
   `live_typing`; the same message with `pid == my id` does not.
4. A state ingest that changes `current_pid` clears both `typed` and
   `live_typing`.
5. Browser bomb-tap: monkeypatch `browser_io.is_browser` → True and
   `browser_io.prompt` → `"hello"`; a MOUSEBUTTONDOWN at `(240, 300)` on your
   turn sends `C_SUBMIT_WORD word="hello"`; a click at `(24, 706)` (DETONATE's
   corner, far off-bomb) opens no prompt.
6. `tail_that_fits("abcdef", 3, len)` → `"def"`; `tail_that_fits("ab", 3,
   len)` → `"ab"` (pure, no pygame).

**4c. Add a server test** in `tests/test_server.py` (mirror its existing
word-bomb setup helpers): current player sends `C_TYPING text="hel"` → every
connected player (including the sender) receives `S_TYPING` with the sender's
pid and `"hel"`; a NON-current player sending `C_TYPING` produces no outbound
frames and no error.

- **Expected observation:** `python -m pytest -q` → all green, zero failures.
- **Counter-move:** a pre-existing test fails that you did not touch → STOP,
  re-run `git stash && python -m pytest -q` to confirm it was already broken;
  if it passes on a clean tree, your diff caused it — diagnose before
  continuing.

### Step 5 — docs sweep

`grep -rn -i "click\|tap\|type" README.md docs/ 2>/dev/null` (skip if no docs
dir). Update any "click the box / tap the field to type" instruction for Word
Bomb to: "your turn: just type — the word appears on the bomb (on phones, tap
the bomb)".

- **Expected observation:** grep after editing shows no stale instruction.
- **Counter-move:** no docs mention input → skip, note it in the commit body.

### Step 6 — verification runs (distinct from the work)

1. `python -m pytest -q` → 0 failures.
2. `python -c "import server.connection, client.scenes.word_bomb, shared.protocol"` → exit 0.
3. Contract check, no trust in your own edit: `python - <<EOF`-style one-liner
   asserting `protocol.C_TYPING == "typing"` and
   `protocol.S_TYPING == "typing_update"` and that
   `"typing" in __import__("server.connection", fromlist=["GameServer"]).GameServer()._dispatch`.
4. OPTIONAL (only if a browser is available; otherwise mark "not run" in the
   report): pygbag build + two tabs; on your turn press keys with the canvas
   focused and confirm the word appears on the bomb in BOTH tabs. Known
   automation traps are recorded in the project memory
   (`word-bomb-browser-playtest`): double-click to focus the canvas first;
   stub `window.prompt` from JS for the tap path.

## Fork triggers

- **F1:** `tests/test_word_bomb_scene.py` fakes don't record `net.send` calls
  → extend the fake net in that file the same way its `_submit` test already
  observes sends (read it; it must exist because the current submit test
  asserts a send).
- **F2:** `event.pos` missing on synthetic MOUSEBUTTONDOWN events in existing
  tests (pygame lets you build events without `pos`) → guard the bomb-tap
  branch with `getattr(event, "pos", None)`.
- **F3:** Step 5 finds a "tap anywhere to submit" doc line (there was such a
  mechanic) → delete it; that mechanic is removed by this change.

## Abort conditions

- Any edit would touch `client/ui.py`, `server/games/word_bomb.py` game rules,
  or `seq`/`_drive` handling → ABORT, the plan is being misread; typing is
  relay-only.
- `python -m pytest -q` on a CLEAN tree (before your edits) is not green →
  ABORT and report; this plan assumes a green baseline.
- More than 2 consecutive failed attempts to fix the same test → ABORT with
  the failure output rather than thrash.

## Red-team notes (already folded in)

- *Keystroke flood?* Human typing is ≤ ~15 msg/s of ~60-byte frames; the
  relay has no state, no timers — no amplification. Rejected keystrokes after
  a turn ends are dropped silently, so no error spam.
- *Race: S_TYPING for a player whose turn just ended* → receiver checks the
  sender is still `current_pid` at receive time, and every `current_pid`
  change wipes `live_typing`.
- *Missed final `text=""`* (dropped frame) → the next turn's `current_pid`
  change wipes it anyway; staleness is bounded by one turn.
- *Desktop browser user clicks the bomb* → they get the prompt, but it is
  opt-in and prefilled with their current text; typing never requires it.
- *32-char word wider than the bomb* → `tail_that_fits` keeps the caret end
  visible; 400px cap stays inside the 480px canvas.
- *Enter with empty text* → `_submit` no-ops (unchanged guard).
- *Spectators/human host* → they receive S_TYPING like anyone else and their
  `your_turn` is always false, so they render `live_typing`. No special case.

Score: 8/8
Blockers: none
Verdict: SHIP
