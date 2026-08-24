# PATCH-010-03 — Turn Elapsed Time Display

## Parent spec

`specs/SPEC-010-Agent-Loop.md`

Refines the CLI presentation introduced by
`patches/SPEC-010/PATCH-010-01-CLI-Turn-Activity-Indicator.md`.

## Problem

PATCH-010-01 solved "is it hung?" — during every silent stretch of a turn the CLI
shows a `⠋ Working...` spinner instead of nothing. It did not solve "how long has
this been going?", and on this project that question has a wide answer.

The spinner looks identical at second 3 and at second 300. Measured on this host:

| stretch | `fast` / qwen3:8b | `next` / qwen3.8:27b |
| --- | ---: | ---: |
| skill routing | ~0.8 s | ~2.7–4.9 s |
| one tool-emitting agent decision | ~11 s | ~26–33 s |
| whole tool-assisted turn | ~15 s | ~59 s |

`deep` / qwen3:32b is slower again, and the whole-turn deadlines the profiles
carry go up to 600 s. So a user watching the spinner cannot distinguish "the 27B
model is thinking, as it always does" from "this has been running four times
longer than usual and something is wrong". They also get no answer at all to the
plainest question about a local model — *how fast was that?* — even though the
harness already measures it: `AgentTurnOutcome.duration_ms` is computed for every
turn and then discarded by the chat loop.

Nothing is displayed anywhere in `app.py` today. The only place the project
prints a duration is the eval harness (`evals/runner.py`).

## Expected change

Two additions, both presentation-only.

1. The indicator's line carries a live elapsed counter: `⠋ Working... 4.2s`.
   It measures the **turn**, not the current silent stretch — the indicator is
   stopped and restarted once per durable line (`[skill]`, `[tool N/4]`,
   `[result]`), and a counter that reset on each of those would be useless.
2. When the turn ends, one footer line reports the total: `[time] 52.6s`.

### This is elapsed time, not progress

PATCH-010-01 §"Expected change" requires that the indicator show **activity, not
measurable progress**: "the patch must not display invented percentages, ETAs,
token counts, or fake progress bars". That constraint is preserved, not bent.
Elapsed time is none of the banned things: it is a measured fact about what has
already happened, not a prediction about what remains. The harness still does not
claim to know how far a model decision has come.

PATCH-010-01's `## Out of scope` bans token-per-second metrics, GPU/RAM
telemetry, and ETA estimation; it does not list elapsed time, and it closes by
deferring "richer runtime telemetry" to separate work. This patch is that work,
kept to the single cheapest and least speculative measurement available.

### TTY only

Both the counter and the `[time]` line appear **only** when stdout is a TTY, on
the indicator's existing detection (`ActivityIndicator.enabled`).

This is the load-bearing decision of the patch. PATCH-010-01 promised that
non-TTY stdout receives "not a single byte ... so redirected output, test
captures, and the transcripts already committed to `README.md` and the journals
stay byte-for-byte unchanged", and PATCH-010-02 chose a fixed `TOOL_DISPLAY_WIDTH`
over the real terminal width for the same reason. A wall-clock duration is
irreproducible by nature — it differs on every run — so printing it into piped
output would break exactly the property those two patches went out of their way
to protect. Gating on TTY keeps every committed transcript correct as written,
and leaves the eval harness and every test capture untouched.

## Constraints

- Preserve the parent SPEC's architecture and intent: presentation only. No
  change to agent policy, prompts, tool contracts, the model transport, tracing,
  persistence, deadlines, or anything the model can observe.
- Preserve every PATCH-010-01 guarantee: `\r` and spaces only and never an ANSI
  escape; the animation thread joined before `stop()` returns so no frame can
  follow it; `start()`/`stop()` idempotent and callable from any thread; complete
  silence when stdout is not a TTY.
- No new configuration. No CLI flag, no `config.py` constant to switch this off —
  the TTY test the indicator already performs is the only condition.
- The `agent.Renderer` protocol stays unchanged, so `RecordingRenderer` and
  `evals/runner.py` need no edits.
- Framework-free, standard library only.

## Acceptance criteria

- On a TTY, the indicator line shows the elapsed time and it advances.
- The counter is continuous across a whole turn: it does not restart when the
  indicator stops for `[skill]`, `[tool N/4]`, or `[result]` and starts again.
- On a TTY, a finished turn prints exactly one `[time] …` line, on a clean line,
  after the answer or error text.
- The `[time]` line appears for every outcome: completed, cancelled, failed, and
  an unexpected application error.
- Erasing the indicator leaves the line visually empty however wide the counter
  grew — no residue from a value that outgrew the label.
- With stdout redirected, output is byte-for-byte what it was before this patch:
  no `\r`, no frame glyph, no `[time]` line.
- The existing 19 `tests/test_cli_activity.py` checks still describe correct
  behavior (one exact-bytes assertion is updated to include the counter), and the
  full deterministic suite and the scripted eval suite are unaffected.
- No live-model verification is required: the model sees exactly the same
  prompts, tools, and results as before.

## Files likely affected

- `cli_activity.py`
- `app.py`
- `tests/test_cli_activity.py`
- `README.md`
- `docs/journal/SPEC-010-agent-loop.md`

This list is advisory, not restrictive.

## Verification

Automated:

```bash
python -m pytest tests/test_cli_activity.py -q
python -m pytest -q
python -m evals.runner --suite scripted
```

New deterministic coverage drives an injected fake clock rather than sleeping,
matching the existing file's rule that only one test may depend on real timing:
the counter is drawn and advances; it is cumulative across a stop/start cycle
within one turn; erasure clears a grown line completely; the formatting helper
handles sub-minute, exactly-a-minute, and over-a-minute values; a redirected
stream still receives zero bytes with the clock advanced far.

Non-TTY regression, end to end:

```bash
printf '...\n/bye\n' | python app.py > piped.txt
grep -c $'\r' piped.txt        # 0
grep -c '\[time\]' piped.txt   # 0
```

TTY behavior, end to end: `app.py` driven inside a real pty, as PATCH-010-01
verified itself. Replaying the carriage returns the way a terminal does must
leave no line showing a spinner frame or a stale counter, and the reconstructed
transcript must end with a single `[time] …` line. A live Ollama model is used to
produce a realistic turn, but no model-behavior claim is made or required.

## Journal strategy

Append a `### PATCH-010-03` subsection to the parent journal
`docs/journal/SPEC-010-agent-loop.md`, under its existing `## Patches` heading —
the change is deterministic presentation only, with no model-facing or
model-decision impact (`patches/README.md` → Journal rules). This is the same
call PATCH-010-01 and PATCH-010-02 made. No standalone patch journal.

## Out of scope

- Per-phase timing (routing / model / tools) in the footer or anywhere else.
- Tokens per second, tokens generated, model load, RAM, or GPU telemetry.
- Percentage progress, ETA, or any prediction about remaining time.
- Any output at all when stdout is not a TTY, including the `[time]` line.
- A CLI flag or `config.py` constant to enable, disable, or format the timer.
- Changing `AgentTurnOutcome.duration_ms`, the trace format, or any trace field.
- Changing timeout values, deadlines, or model profiles.
- Retro-editing the transcripts committed to `README.md` and the journals; the
  TTY-only decision is what keeps them accurate, so they must not be touched.
- Colour, ANSI escapes, cursor movement, or any richer terminal UI.

## Suggested branch and commit conventions

Per the repository PATCH workflow:

```text
branch:
patch/PATCH-010-03-turn-elapsed-time-display

patch file:
patches/SPEC-010/PATCH-010-03-Turn-Elapsed-Time-Display.md

implementation commit:
Show elapsed time during and after a turn (PATCH-010-03)

merge:
Merge PATCH-010-03: turn elapsed time display
```
