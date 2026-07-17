# Wargame: Fuse curve + live word-count display (Word Bomb)

## Mission

1. The auto-host fuse starts at **20s**. Every **accepted** word shortens the
   next fuse by **0.5s**, down to a floor of **7s**. When the bomb explodes,
   the fuse resets to 20s. (Global escalation, not per-player.)
2. Show how many valid answers exist for the current prompt — e.g.
   `20k words` or `804 words` — visible to everyone. The number naturally
   shrinks over a long game because already-used words stop counting.

## Ground truth (verified — do not re-derive)

- Config: `config.py:76-80` (Word Bomb block, `WB_TURN_SECONDS = 25` at :77).
- Driver deadline: `server/connection.py:486` —
  `seconds = WB_TURN_SECONDS if isinstance(game, WordBombGame) else (...)`.
  Import list of config names at `server/connection.py:53-57`.
- Game construction: `server/connection.py:278-281` —
  `words, prompts, index = load_dictionary(WB_MIN_WORDS_PER_PROMPT)` then
  `WordBombGame(..., words=words, prompts=prompts, index=index, lives=WB_LIVES, ...)`.
- Dictionary warm at boot: `server/__main__.py:40` —
  `words, prompts, _ = load_dictionary(WB_MIN_WORDS_PER_PROMPT)`.
- Rules object: `server/games/word_bomb.py`. `derive_prompts` (line ~43)
  already builds a `Counter` `counts[sub]` = number of dictionary words
  containing `sub` — the data for the display exists, it's just discarded.
  `submit_word` (~157): the accept branch bumps `seq` then `_pass_bomb()`.
  `advance()` (~184): the explosion. `_pass_bomb` (~203) picks the new prompt.
  `public()` (~228) is the per-player view dict.
- Client scene: `client/scenes/word_bomb.py` — `draw` section 4 calls
  `_draw_prompt_tiles(...)` with prompt tiles centered on the bomb
  (CENTER=(240,300), BOMB_R=78). The area around y≈196 (above the bomb,
  below the title block) is free during play. `ui.MUTED` labels exist all
  over; `ui.Label(text, (x, y), size, color, center=True)` is the idiom.
  Pure display helpers (`press_of`, `heat_color`, `feed_line`,
  `tail_that_fits`) live at module level and are unit-tested headless in
  `tests/test_word_bomb_scene.py` — add the new formatter beside them.
- Tests: `python -m pytest -q` from repo root. Baseline: 294 passed PLUS
  exactly 2 pre-existing failures in `tests/test_net_url.py` caused by an
  uncommitted TEMP demo edit in `config.py` (DEFAULT_SERVER_URL baked to
  ws://localhost:8765). That edit is INTENTIONAL working-tree state — do NOT
  revert it, do NOT commit it, and do NOT count those 2 failures against
  yourself. Everything else must stay green.
- `tests/test_word_bomb.py` constructs `WordBombGame` directly with toy
  dicts and monkeypatches driver constants (e.g. :285) — read its helpers
  before writing tests; construction sites there may need the new kwargs'
  defaults to keep working (defaults make them no-ops).

## Design decisions (locked)

- Config (replace `WB_TURN_SECONDS` outright — no alias left behind):
  ```python
  WB_FUSE_START = 20.0   # fuse for a fresh bomb (seconds)
  WB_FUSE_STEP = 0.5     # each accepted word shaves this off the next fuse
  WB_FUSE_FLOOR = 7.0    # the fuse never gets shorter than this
  ```
- The GAME owns the curve (pure rules, no config import):
  `WordBombGame.__init__(..., fuse_start=20.0, fuse_step=0.5, fuse_floor=7.0)`
  sets `self.fuse_start/step/floor` and `self.fuse_seconds = fuse_start`.
  - accepted word (in `submit_word`, accept branch, next to the `seq` bump):
    `self.fuse_seconds = max(self.fuse_floor, self.fuse_seconds - self.fuse_step)`
  - explosion (`advance()`, after the life loss, regardless of elimination):
    `self.fuse_seconds = self.fuse_start`
  - The driver reads `game.fuse_seconds` at connection.py:486 in place of
    `WB_TURN_SECONDS`; it passes the three config values into the
    constructor at :278-281.
- Word-count ("options"):
  - `derive_prompts` returns `(prompts, index, counts)` where `counts` is
    `{prompt: total containing words}` filtered to surviving prompts (same
    filtering as `index`). `load_dictionary` returns
    `(words, prompts, index, counts)`. Update BOTH call sites (:278, :40 —
    the boot one becomes `words, prompts, _, _ = ...`).
  - Game takes `counts=None` (default `{}`), stores it, and maintains
    `self.options: int` = `counts.get(prompt, 0)` minus the number of used
    words containing the prompt. Compute via a small method
    `_count_options()` called once in `__init__` and once at the end of
    `_pass_bomb` (the only places the prompt changes):
    ```python
    def _count_options(self) -> None:
        base = self.counts.get(self.prompt, 0)
        self.options = max(0, base - sum(1 for w in self.used if self.prompt in w))
    ```
    (`used` is small — tens of words — so the scan is negligible; the value
    shrinks as the game goes on, which is the requested drift.)
  - `public()` gains `"options": self.options`.
- Client display: module-level pure helper in the scene, next to `feed_line`:
  ```python
  def fmt_options(n: int) -> str:
      """804 -> '804 words', 19983 -> '20k words'."""
      if n >= 10_000:
          return f"{round(n / 1000)}k words"
      return f"{n} words"
  ```
  Drawn in `draw` right after the prompt tiles, only during PHASE_PLAY:
  `ui.Label(fmt_options(gs.get("options", 0)), (240, 196 + oy), 14, ui.MUTED, center=True)`
  — skip drawing when `"options"` is absent from the state (old server), i.e.
  only draw if `gs.get("options") is not None`.

## Battle plan

### Step 1 — config + rules

1. `config.py:77`: delete `WB_TURN_SECONDS`, add the three constants above
   (keep the surrounding comment style).
2. `server/games/word_bomb.py`:
   - `derive_prompts`: also build/return `counts` (`{sub: counts[sub]}` for
     surviving prompts). Docstring: mention the third return.
   - `load_dictionary`: return 4-tuple; docstring updated.
   - `WordBombGame.__init__`: new kwargs `counts=None, fuse_start=20.0,
     fuse_step=0.5, fuse_floor=7.0`; set `self.counts = counts or {}`,
     fuse fields, `self.fuse_seconds = fuse_start`; call
     `self._count_options()` after `self.prompt` is chosen.
   - accept branch of `submit_word` + `advance()` + `_pass_bomb` per the
     locked decisions. `public()` gains `"options"`.
- **Expected observation:** `python -c "from server.games.word_bomb import load_dictionary; w,p,i,c = load_dictionary(); print(len(c), min(c.values()))"`
  prints a prompt count equal to `len(p)` and a min ≥ 500.
- **Counter-move:** unpacking errors → a call site still expects 3 values;
  grep `load_dictionary(` and fix every site.

### Step 2 — driver

`server/connection.py`:
- imports (:53-57): drop `WB_TURN_SECONDS`, add `WB_FUSE_START`,
  `WB_FUSE_STEP`, `WB_FUSE_FLOOR`.
- :278 unpack 4-tuple; pass `counts=counts, fuse_start=WB_FUSE_START,
  fuse_step=WB_FUSE_STEP, fuse_floor=WB_FUSE_FLOOR` to `WordBombGame`.
- :486: `seconds = game.fuse_seconds if isinstance(game, WordBombGame) else (...)`.
- `server/__main__.py:40`: unpack 4-tuple (`_, _`).
- **Expected observation:** `python -c "import server.connection, server.__main__"` → exit 0
  (the `__main__` import runs no loop — confirm; if it does, use compileall).
- **Counter-move:** NameError on removed constant → grep `WB_TURN_SECONDS`
  repo-wide; zero hits must remain outside the wargame docs.

### Step 3 — client display

`client/scenes/word_bomb.py`: add `fmt_options` helper + the one draw line
(see locked decisions). Nothing else changes.
- **Expected observation:** `python -c "from client.scenes.word_bomb import fmt_options; assert fmt_options(804)=='804 words'; assert fmt_options(19983)=='20k words'; assert fmt_options(10000)=='10k words'"` → exit 0.

### Step 4 — tests

4a. `tests/test_word_bomb.py` (game rules; mirror existing toy-dict helpers):
   1. fuse decreases 0.5 per accepted word: fresh game `fuse_seconds == 20.0`;
      after one accept `19.5`.
   2. floor: construct with `fuse_start=7.5, fuse_floor=7.0`; two accepts →
      `7.0` both times (never below).
   3. reset: after `advance()` (explosion) `fuse_seconds == 20.0` (construct
      default, accept a few, then advance).
   4. rejects do NOT change the fuse.
   5. `options`: toy dict where prompt `"ca"` matches 3 words; fresh game
      pinned to that prompt (seed the rng or pass `prompts=["ca"]`) has
      `options == 3`; after an accepted `"ca"`-word and re-pinned prompt
      (single-prompt game re-picks the same one) `options == 2`; `public()`
      carries the same number.
   6. Driver deadline honors the curve: in the existing driver-test style
      (monkeypatched `load_dictionary` + fast bots), assert the broadcast
      `deadline - time.time()` is ≈ `game.fuse_seconds` (tolerance 1s) after
      an accepted word shortens it. If the existing driver tests make this
      awkward, assert instead on connection.py's seconds source by direct
      inspection: `game.fuse_seconds` after accept < start. Prefer the
      deadline assertion; fall back only if flaky.
4b. `tests/test_word_bomb_scene.py`: `fmt_options` cases: `0 -> '0 words'`,
    `804`, `9999 -> '9999 words'`, `10000 -> '10k words'`, `19983 -> '20k words'`.
4c. Fix any test that unpacks `load_dictionary()` 3-ways or references
    `WB_TURN_SECONDS` (grep first).
- **Expected observation:** `python -m pytest -q` → only the 2 pre-existing
  `test_net_url.py` failures (see Ground truth); everything else green.
- **Counter-move:** an untouched test breaks → `git stash && python -m pytest -q`
  to compare against baseline, `git stash pop`, then diagnose your diff.
  Max 2 attempts per failing test, then stop and report.

### Step 5 — docs

`grep -n -i "25s\|25 s\|fuse\|seconds" README.md` — update the Word Bomb
how-to-play line to the new behavior ("the fuse starts at 20s and tightens
by half a second every solved word, floor 7s; an explosion resets it") and
mention the on-screen word count. Skip silently if README says nothing about
timing.

### Step 6 — verification (distinct from the work)

1. `python -m pytest -q` → green except the 2 known net_url failures.
2. `python -m compileall -q client server shared config.py main.py` → exit 0.
3. Behavior probe (no trust in your own tests): a 5-line python REPL script —
   toy game, 3 accepts, print fuse trail `[20.0, 19.5, 19.0, 18.5]`, advance,
   print `20.0`, print `options` before/after an accept.
4. Report the exact pytest tail + the probe output.

## Fork triggers

- **F1:** `derive_prompts` return-shape change breaks a test that calls it
  directly → update that test to unpack 3 values (it's testing prompts/index
  content, not arity).
- **F2:** existing game tests construct `WordBombGame` without `counts` →
  defaults make `options == 0`; if a test then asserts on `public()`
  equality-of-keys, add `"options": 0` to the expectation, not special-casing
  in code.
- **F3:** README has no fuse wording → note "no doc change needed" in the
  report.

## Abort conditions

- Touching `client/ui.py`, seq/timer semantics, or the typing relay → ABORT
  (misread plan).
- More than 2 consecutive failed fixes of the same test → ABORT with output.
- The 2 net_url failures growing to 3+ → your diff touched config defaults;
  ABORT and diagnose before proceeding.

## Red-team notes (folded in)

- *Fuse state on reject:* untouched — rejects must not reward stalling; only
  accepts tighten, only explosions reset.
- *Gameover explosion:* `advance()` resets `fuse_seconds` even when the game
  ends — harmless (no further turns) and keeps the rule uniform.
- *`options` staleness:* recomputed exactly when the prompt changes; a used
  word affects only future prompts' counts, matching what players see.
- *Old-client/new-server & vice versa:* client draws the counter only when
  `options` is present; server ignores unknown fields. Safe both ways.
- *Human-host mode (no deadline):* fuse fields exist but the driver parks
  with no deadline — unchanged behavior; the curve simply never fires.
- *`counts` memory:* one int per surviving prompt (~thousands) — negligible.
- *Bot pacing:* `WB_BOT_DELAY_SECONDS=6` stays under the 7s floor, so bots
  still answer in time even at max heat. No change needed — but state this
  in the final report so a future fuse-floor tweak checks it.

Score: 8/8
Blockers: none
Verdict: SHIP
