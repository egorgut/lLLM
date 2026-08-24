# SPEC-010 — Bounded Agent Loop

- **Spec:** [SPEC-010](../../specs/SPEC-010-Agent-Loop.md)
- **Date:** 2026-07-23
- **Branch:** feature/SPEC-010-agent-loop
- **Merge commit:** cf0904b

## Hypothesis / intent
SPEC-006–009 built a unified registry/executor serving local + MCP tools, but a
user turn was still a fixed two-stage flow: `run_turn()` allowed **at most one**
tool execution and then forced a final answer (erroring on any further tool
call). That boundary was an intentional safety rail while the pieces were built
independently; it now prevents the harness from behaving as an agent. SPEC-010
replaces it with a **reusable, bounded agent loop**: after every tool result the
model gets control again until it emits a final textual answer, capped by a
host-owned `MAX_TOOL_CALLS_PER_TURN = 4`. This lets a single turn chain several
sequential actions and, crucially, recover from an ordinary structured tool
error (`{ok: false}`) by retrying with corrected arguments. Framework-free
(no LangChain/LangGraph/etc.), one decision = one model response, one tool per
response, semantic-only persistence.

## What changed
- `agent.py` (new): `AgentRunner` — the loop policy component, plus
  `AgentTurnError` and the ephemeral-transcript builders `assistant_tool_message`
  / `tool_result_message` (moved out of `app.py`). `run_turn(messages)` takes a
  **snapshot** of model-facing messages (never the mutable `Conversation`),
  maintains a per-turn `working_messages` transcript + `tool_calls_used` counter,
  and loops: request a response → stream its text → if no tool calls, return the
  final text (empty ⇒ `AgentTurnError`); if >1 tool call, raise
  `Parallel tool calls are not supported.` **before** executing; if the budget is
  spent, raise `Agent stopped after 4 tool calls without a final answer.`;
  otherwise render, `executor.execute(...)`, append the call + result observation,
  and continue. Model transport (`respond`) and rendering (`renderer`) are
  **injected** — this is the seam that makes the loop deterministically testable
  without a live model. Config validation: `max_tool_calls < 1` rejected at
  construction.
- `config.py`: added `MAX_TOOL_CALLS_PER_TURN = 4` (host-owned; never
  model-writable).
- `app.py`: deleted `run_turn()` and `TurnError` and the one-tool-specific logic.
  Added `CliRenderer` (a small stateful sink carrying the lazy-`Qwen:`-prefix
  behavior from the old `stream_response`, plus the new `[tool N/MAX]` header).
  `main()` builds an `AgentRunner` **per turn** (a fresh `CliRenderer` resets the
  per-turn prefix state) with `respond=lambda m, t: ModelResponse(m, t)` and
  `renderer=CliRenderer()`, and calls `runner.run_turn(conversation.messages_for_model)`.
  Rollback/persistence (`add_user_message` → run → success saves final answer;
  `KeyboardInterrupt`/any `Exception` prints `Application error: …` and
  `remove_last_message()`), the MCP start/`register_mcp_tools`/`finally:
  manager.close()` lifecycle, and command handling are **unchanged in shape**.
- `prompts.py`: rewrote the tool-policy paragraph — work in steps, one tool at a
  time, decide after each result, retry with corrected args after an error, don't
  invent results, answer when enough info; replaced the "only one SQL execution
  per turn" wording ("each call runs exactly one SELECT … you may run another
  query on a later step"). Cap value is **not** exposed to the model. Updated the
  stale schema comment.
- `README.md`: new "Агентный цикл" section (bounded loop, `MAX_TOOL_CALLS_PER_TURN`,
  `[tool N/4]` rendering, `{ok:false}` recovery, ephemeral-transcript /
  semantic-persistence, parallel-call rejection); updated the three tool examples
  to `[tool 1/4]`, softened the "one tool per turn" phrasings, added the config
  value, added `agent.py` to the structure table, and updated the status.
- `llm.py`, `conversation.py`, `storage.py`, `tools/*`, `mcp_integration/*`,
  `mcp_servers/*`: **unchanged**. `ModelResponse` already separates streamed text
  from `tool_calls`, so no transport refactor was needed (see Streaming note).

## Deviation from the spec
Same convention as SPEC-007/008/009: this repo has never committed a `tests/`
suite. The spec's file list implies `tests/test_agent.py`; instead the 15
Testing scenarios were run through a **standalone deterministic harness** (fake
scripted `respond` + recording renderer/executor) recorded below, not committed
under `tests/`. Only the delivery form differs — every scenario is covered.

## Streaming note (spec §8)
The spec allows the agent layer to buffer intermediate chunks if a response's
tool-call status is only knowable after consuming the stream. In practice
**qwen3:8b emits empty `message.content` on a tool-selection response** (tool
calls only), so the existing lazy-`Qwen:`-prefix mechanism already prints nothing
for intermediate steps and streams **only** the final textual answer,
incrementally — no buffering was introduced (spec §8 prefers this). The one
theoretical mixed text+tool response is handled by policy: `tool_calls` are
authoritative, so any intermediate text is discarded and never persisted (proven
by the harness "Extra" check). No live mixed response was observed.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B, ctx 40960; capabilities:
  completion, tools, thinking)
- Ollama: server 0.31.1; SDK `ollama==0.6.2`; reachable at `http://localhost:11434`
- Interpreter: project `venv/bin/python`
- Sampling: defaults — no `options` set in `llm.py`

## Verification

**Deterministic harness — 38/38 PASS** (fake model + recording executor/renderer),
covering spec Testing 1–15 plus an intermediate-text-discard check:
- no-tool (1 request, 0 executor calls); one-tool-then-answer (2 requests, 1
  call, correct assistant-tool + tool-result messages appended to the 2nd
  request); multi-different-tool order (sql→python→text); same-tool retry after
  `{ok:false}` (first error envelope present in the 2nd request transcript);
  **limit** (5 tool requests, exactly **4** executor calls, 5th not executed,
  stable message `Agent stopped after 4 tool calls without a final answer.`);
  final answer on the 4th allowed step accepted; **parallel** calls → 0
  executions + `Parallel tool calls are not supported.`; empty response →
  `Model returned an empty response.`; `{ok:false}` continues (no exception);
  executor raises `ToolExecutionError` → propagates, **no** further model request;
  input snapshot unmutated (no protocol messages leak to the caller);
  **MCP-backed** tool via a fake registered handler runs through the same path
  (no source branching); rendering order
  `tool_call,tool_result,tool_call,tool_result,text,text,text` with numbering
  `1/4` then `2/4`; incremental streaming of `["The ","answer ","is 4."]`
  concatenated to `The answer is 4.`; `max_tool_calls` of `0` and `-1` rejected.

**Live model (`python app.py`, scripted stdin, scratch history) — Scenarios A–E:**
```text
[mcp] connected: time (1 tool)

You: What is 173 multiplied by 284?
[tool 1/4] python_calculate
[args] {"expression": "(173 * 284)"}
[result] {"ok": true, "result": 49132}
Qwen: The product of 173 and 284 is 49,132.

You: Which music genre generated the most revenue, and what percentage of all revenue did it generate?
[tool 1/4] sql_query
[args] {"query": "... (SELECT SUM(UnitPrice * Quantity) FROM InvoiceLine JOIN Track ON ...) ... LIMIT 1;"}
[result] {"ok": false, "error": {"type": "invalid_query", "message": "The SQL query is invalid."}}
[tool 2/4] sql_query
[args] {"query": "... (SELECT SUM(UnitPrice * Quantity) FROM InvoiceLine) * 100 ... GROUP BY g.GenreId ORDER BY TotalRevenue DESC LIMIT 1;"}
[result] {"ok": true, "columns": ["GenreName","TotalRevenue","Percentage"], "rows": [["Rock",826.65,35.499871167224946]], "row_count": 1, "truncated": false}
Qwen: The music genre that generated the most revenue is Rock, contributing $826.65, which accounts for 35.5% of all revenue.

You: What time is it now in Europe/Amsterdam and in UTC, and what is the difference in minutes?
[tool 1/4] mcp_time__get_current_time
[args] {"timezone": "Europe/Amsterdam"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "Europe/Amsterdam", "datetime": "2026-07-23T14:10:22+02:00"}}
Qwen: The current time in Europe/Amsterdam is 14:10 (UTC+2), and in UTC it is 12:10. The time difference is 120 minutes (2 hours).

You: Explain in one sentence what SQLite is.
Qwen: SQLite is a self-contained, serverless relational database management system that stores the entire database in a single disk file and uses SQL for querying and managing data.

You: /bye
Chat finished.
```
Exit `0`, **clean stderr** (no tracebacks).

Highlights: Scenario C exercised **live multi-step tool-error recovery** — the
model's first `sql_query` (a division subquery joined to `Track`) returned a
controlled `{ok:false, invalid_query}`, the loop fed it back, and the model
corrected the query on `[tool 2/4]` and grounded its 35.5% answer in the returned
row (spec §4, live). The `[tool N/4]` counter is visible; the final answer
streamed after the diagnostics. Scenario E used **one** MCP time call and
computed the UTC offset itself — fewer calls than the illustrative example, but a
correct grounded answer (tool sequence is model-dependent, as the spec allows).
Scenario A produced no tool block.

**Semantic persistence.** After the run the scratch history held **8** messages,
all `user`/`assistant` with only `{role, content}` keys — **no** `tool_calls`,
**no** `role:tool` (verified programmatically). The real `data/chat_history.json`
was backed up before and restored after.

## Outcome
All 22 acceptance criteria met. `lLLM` now has its first complete agent runtime:
the model chooses, the host validates and bounds (≤4 executed tools/turn,
parallel rejected, empty rejected), the executor acts through the unchanged
unified boundary, each structured result becomes the next observation, and the
model decides again — until a streamed final answer. Local and MCP-backed tools
run through the same path with no source branching; failed turns roll back with
no partial persistence; existing calculator/SQL/MCP protections are untouched.

## Follow-ups
- Parallel/batch tool calls (needs new ordering/failure/cancellation semantics) —
  its own spec, as SPEC-010 §7 notes.
- Loop-cycle / repeated-call detection beyond the hard limit; per-tool timeouts.
- Side-effect transaction policy once write-capable tools arrive (current tools
  are read-only/compute, so turn rollback can't undo external effects).
- Optional committed `tests/` suite to end the recurring journal-only deviation.

## Patches

### PATCH-010-01 — CLI Turn Activity Indicator

- **Patch:** [PATCH-010-01](../../patches/SPEC-010/PATCH-010-01-CLI-Turn-Activity-Indicator.md)
- **Date:** 2026-08-11
- **Branch:** patch/PATCH-010-01-cli-turn-activity-indicator
- **Implementation commit:** `3f07b17`
- **Merge commit:** `4ab44fc`

#### Reason

This step's presentation prints only durable output — `[skill]`, `[tool N/4]`,
`[args]`, `[result]`, streamed `Qwen: …`, errors. Everything between those is
silent, and the silence is not short. Measured on the live smoke run below:
skill routing, then the first model decision, then tool execution, then the next
model decision — ten distinct silent stretches across three turns, the longest
well over ten seconds on qwen3:8b, and worse on a larger local model. As the
"Streaming note" above records, a tool-selecting response streams **no** text at
all, so the terminal shows literally nothing between the user's Enter and the
first `[tool …]` line. A user cannot tell that from a hung process.

#### Change

Presentation only; no agent policy, prompt, tool contract, transport, tracing,
persistence, or model change.

- `cli_activity.py` (new): `ActivityIndicator` — a one-line `⠋ Working...`
  spinner. `start()`/`stop()` are idempotent and callable from any thread, which
  the loop requires: `tool_call`/`tool_result` arrive on the main thread while
  `text` arrives on the deadline worker thread (`agent.py`). `start()` draws the
  first frame **synchronously on the calling thread**, so activity is visible the
  moment a wait begins rather than one tick later, then hands off to a daemon
  thread animating on `Event.wait`. `stop()` sets the event and **joins before
  erasing**, so no frame can reach the stream after it returns — that is what
  makes it safe for the caller to print immediately afterwards. Only `\r` and
  spaces, never an ANSI escape. It is also a context manager.
- `app.py`: `CliRenderer` takes the indicator and drives the lifecycle around
  each durable line — stop before `[tool …]`/`[args]`, restart for the tool's own
  (silent) execution, stop before `[result]`, restart for the next model
  decision, stop once and for all on the first final-answer chunk. `announce_skill`
  does the same around `[skill]`. The chat loop owns one indicator for the session
  and wraps the turn in `with indicator:` — routing's silence starts before any
  renderer exists, and `__exit__` covers every exit path, including a mid-turn
  `KeyboardInterrupt`, a timeout outcome, and an unexpected exception.
- The `agent.Renderer` protocol is unchanged, so `RecordingRenderer` and
  `evals/runner.py` needed no edits.
- Non-TTY stdout writes **nothing at all** — not even a static line — so piped
  output, test captures, evals, and every transcript already committed to
  `README.md` and these journals stay byte-for-byte what they were.
- Housekeeping in the same patch: the patch note was moved from `specs/` to its
  conventional home `patches/SPEC-010/`.

#### Verification

- `python -m pytest tests/test_cli_activity.py -q` — 19 passed. New file covers
  non-TTY silence (including "no `\r` and no frame character in captured turn
  output"), synchronous first frame, erasure leaving an empty line, idempotent
  start/stop, daemon thread with no survivor after `stop()`, a cross-thread stop
  writing nothing after it returns, `with` clearing on a controlled error / an
  unexpected exception / a `KeyboardInterrupt`, renderer ordering against every
  durable line, unchanged default rendering, a real two-tool `AgentRunner` turn
  asserting the full start/stop interleaving, and a timed-out turn leaving no
  indicator or thread behind. Only one check depends on timing (that the
  animation advances at all) and it is bounded, not a fixed sleep.
- `python -m pytest -q` — 574 passed, 29 skipped. No regression in the agent,
  skills, tool, MCP, sandbox, persistence, or reliability suites.
- Non-TTY, live: `printf '…\n/bye\n' | python app.py` exited `0` with **zero**
  `\r` bytes in stdout and output identical in shape to pre-patch.
- Live TTY smoke test: `app.py` driven inside a real pty against qwen3:8b —
  (1) a plain question, (2) a `python_calculate` question, (3) a two-tool
  `sales_analysis` question. The raw byte capture contains **764** spinner
  frames, **10** erase sequences, and **0** ANSI escapes. Ten erasures is exactly
  the number of silent stretches the three turns contain (1 + 3 + 6), i.e. every
  restart was matched by a stop before durable output. Replaying the carriage
  returns the way a terminal does leaves **0** lines showing a spinner frame, and
  the reconstructed transcript is indistinguishable from a pre-patch one:

```text
You: Сколько будет 1234 * 5678? Посчитай.

[tool 1/4] python_calculate
[args] {"expression": "1234 * 5678"}
[result] {"ok": true, "result": 7006652}

Qwen: 1234 умножить на 5678 равно **7 006 652**.

You: Какой жанр принёс больше всего выручки? А сколько треков в этом жанре?
[skill] sales_analysis

[tool 1/4] sql_query
...
[tool 2/4] sql_query
[args] {"query": "SELECT COUNT(*) AS TrackCount FROM Track WHERE GenreId = (...)"}
[result] {"ok": true, "columns": ["TrackCount"], "rows": [[1297]], ...}

Qwen: Жанр **Rock** принёс больше всего выручки — **826.65 долларов**. В этом
жанре **1297 треков**.
```

  The raw bytes around each hand-off show the intended shape, e.g.
  `…⠧ Working...\r` + `␣×12` + `\r` + `\r\n[tool 1/4] python_calculate`.
- No live *model-behavior* journal is required: the patch changes deterministic
  presentation only, and the model saw exactly the same prompts, tools, and
  results as before.

#### Outcome

All acceptance criteria met. One deliberate refinement over the patch note's
literal lifecycle, recorded in the note itself: the indicator also runs during
tool *execution* (restarted after `[args]`), because that stretch is silent too
and a sandbox call can hold it for a while. Scope stayed inside the patch — no
new dependency, no configuration, no percentages/ETA/telemetry, no colors or
terminal UI, and no unrelated CLI cleanup.

### PATCH-010-02 — Readable Tool Result Display

- **Patch:** [PATCH-010-02](../../patches/SPEC-010/PATCH-010-02-Readable-Tool-Result-Display.md)
- **Date:** 2026-08-12
- **Branch:** patch/PATCH-010-02-readable-tool-result-display
- **Implementation commit:** `0a6de1d`
- **Merge commit:** `cc65a0e`

#### Reason

PATCH-010-01 above made the *silence* between the durable lines legible. This
patch makes one of those durable lines legible: `[result]`.

`CliRenderer.tool_result` printed the whole result dict as a single
`json.dumps` line, with nothing bounding it for the screen. The only cap in the
path, `MCP_RESULT_MAX_CHARS = 20_000`, is a *model context* budget applied in
`mcp_integration/adapter.py::_bounded_data`, so one MCP call could emit 20 000
characters as one soft-wrapped line. MCP suffered most, because
`normalize_result` nests the interesting part one level down under `data` inside
an envelope that is itself on that line.

The clearest evidence was in this repository: every large transcript committed
to `README.md` had been hand-elided with `{...}`, `...`, or manual line breaks.
The documentation was editing around a display defect, which also meant those
transcripts were no longer faithful captures.

#### Change

Presentation only. No agent policy, prompt, tool contract, transport, tracing,
persistence, or model change — and specifically no change to what the model
receives: the renderer and `tool_result_message` are two independent calls on
the same dict (`agent.py`), and only the first one was touched.

- `tool_render.py` (new): pure functions, no state and no I/O.
  `format_tool_result` recognises the payload's shape and renders a status
  header plus a bounded indented body — an error envelope becomes one
  `error · <type>: <message>` line; `columns`+`rows` becomes an aligned table
  with numeric columns right-aligned; the MCP text fallback (`{"text": …}`)
  becomes a wrapped text block; a dict of scalars becomes aligned key/value
  pairs; anything else falls back to indented JSON. A nested value is kept on a
  field line only if it fits whole — clipping `{"a": 1, "b":…` would hide the
  one thing a structure is read for, so such a payload goes to the JSON branch
  instead. `format_tool_args` renders `[args]` as a bounded `key=value` list.
- The MCP envelope's `server/tool` appears in the header
  (`ok · tracker/issue_get`); a local tool's does not, because
  `[tool N/M] <name>` is already the line above.
- The size note (`1.4 KB`) is printed only when something was actually left
  out. When the whole payload is on screen its byte count tells the reader
  nothing they cannot see.
- `config.py`: `TOOL_DISPLAY_WIDTH = 100` and `TOOL_RESULT_PREVIEW_LINES = 16`,
  documented next to `MCP_RESULT_MAX_CHARS` as the different concern they are —
  that one bounds what the *model* receives, these bound only the screen. No
  new configuration semantics, which is what kept this a PATCH rather than a
  SPEC.
- The width is a constant rather than `shutil.get_terminal_size()`, and there is
  no `isatty` branch: the rendering is identical on a TTY and through a pipe,
  the same rule `cli_activity.py` follows, so committed transcripts stay
  reproducible.
- `app.py`: `CliRenderer` calls the two formatters; the indicator lifecycle from
  PATCH-010-01 is untouched. `import json` became unused and was removed.
- The `agent.Renderer` protocol is unchanged, so `RecordingRenderer` and
  `evals/runner.py` needed no edits.

#### Verification

- `python -m pytest tests/test_tool_render.py -q` — 44 passed. New file covers
  every shape branch, both header forms, the omission footers and their
  singular/plural, the `truncated`/`preview` envelope, `[args]` rendering, and
  the edge cases a renderer must survive rather than raise on: an empty payload,
  a non-dict result, an error value that is not a dict, a row shorter than its
  header, rows that are not sequences, a 500-character cell, and a newline
  inside a value. A final group asserts the invariants across all branches at
  once — no line wider than `TOOL_DISPLAY_WIDTH`, no body past the budget, no
  `\r`/ESC/`\n` inside a line, no trailing whitespace, and no mutation of the
  dict that also goes to the model.
- `python -m pytest -q` — 636 passed, 29 skipped.
- Three assertions in `tests/test_cli_activity.py` pinned the old raw-JSON lines
  and were updated to the new rendering. They test the indicator lifecycle, not
  the format; the interleaving they assert is unchanged. One test name was
  corrected with them: `test_default_renderer_output_is_unchanged` had meant
  "no indicator markers when no indicator is injected", which its new name now
  says.
- Live, qwen3:8b (`fast` profile, digest `500a1f067a9f`, Q4_K_M, 8.2B, ctx
  40960, Ollama defaults for sampling — the project sets no options), scripted
  on stdin against `python app.py`. Exercised live: `python_calculate` (field
  pair), `sql_query` at 1, 5, and 30 rows, and `mcp_time__get_current_time`
  (MCP envelope with the server in the header). The 30-row case is the one
  worth recording, since it shows every bound at once — 14 rows shown of 30, the
  `Name` column capped at 32 characters, `Milliseconds` right-aligned, and NULL
  composers rendered blank:

```text
[tool 1/4] sql_query
[args] query=SELECT Name, Composer, Milliseconds FROM Track ORDER BY Milliseconds DESC LIMIT 30;
[result] ok · 30 rows
  Name                              Composer  Milliseconds
  --------------------------------  --------  ------------
  Occupation / Precipice                           5286953
  Through a Looking Glass                          5088838
  Greetings from Earth, Pt. 1                      2960293
  …
  The Gun On Ice Planet Zero, Pt.…                 2924341
  … 16 more rows
```

  Incidentally confirming the display/model split: the model's answer described
  the missing composers as "marked as `NULL`", which is the raw JSON it received
  — not the blank cells the screen showed.
- The error branch could not be captured live. Three attempts to provoke a real
  tool error (an invented IANA timezone twice, a query against a non-existent
  column) were all refused by the model *before* it called the tool, which is
  correct behaviour on its part. The branch is covered by unit tests instead,
  and the `invalid_query` transcript in `README.md` was reformatted by hand
  rather than re-captured — as was the Tracker `issue_get` transcript, whose
  payload was already illustrative (`{...}`) in the original.
- No live *model-behavior* verification is required by `patches/README.md`: the
  patch does not touch agent decisions, prompts, tool selection, history,
  termination, or model-visible tool results. The live runs above exist to
  produce honest transcripts, not to judge model behaviour.

#### Outcome

All acceptance criteria met. `README.md` was updated: every committed transcript
now shows real rendered output instead of hand-elided JSON, a new subsection
documents the shapes and the two constants, and `tool_render.py` was added to
the structure table. The historical transcripts inside PATCH-010-01's entry
above were deliberately left in the old format — they record what that run
actually printed.

Scope stayed inside the patch: no new dependency, no configuration switch, no
colour or ANSI, and MCP `image`/`resource` content blocks — still dropped in
`adapter.py::_join_text` — were left alone, since supporting them would change
what the model receives and belongs in its own SPEC.

### PATCH-010-03 — Turn Elapsed Time Display

- **Patch:** [PATCH-010-03](../../patches/SPEC-010/PATCH-010-03-Turn-Elapsed-Time-Display.md)
- **Date:** 2026-08-24
- **Branch:** patch/PATCH-010-03-turn-elapsed-time-display
- **Implementation commit:** `<short-sha>`
- **Merge commit:** `<short-sha>`

#### Reason

PATCH-010-01 answered "is it hung?". It did not answer "how long has this been
going?", and on this project the honest answer ranges over two orders of
magnitude: routing takes ~0.8 s on `fast` and ~2.7–4.9 s on `next`; a single
tool-emitting decision ~11 s against ~26–33 s; the SPEC-019 split run measured a
58.7 s turn, and profile turn deadlines go up to 600 s. A spinner at second 3 and
a spinner at second 300 are the same picture, so the user cannot tell an ordinary
`deep` turn from one that is going wrong. Meanwhile the harness already measures
the duration — `AgentTurnOutcome.duration_ms` is computed for every turn and then
discarded by the chat loop — and nothing in `app.py` has ever printed it.

#### Change

Presentation only; no agent policy, prompt, tool contract, transport, tracing,
persistence, deadline, or model change. The `agent.Renderer` protocol is
untouched, so `RecordingRenderer` and `evals/runner.py` needed no edits.

- `cli_activity.py`: `format_elapsed()` (`4.2s` / `52.6s` / `3m 21.4s`, rounded
  before the minute split so the line never reads `60.0s` for one frame) and
  `format_turn_time()` (`[time] 52.6s`), shared so the ticking counter and the
  footer cannot drift apart. `ActivityIndicator` gains an injected `clock`
  (the project's established test seam), a turn-scoped origin, and the counter in
  its frame: `⠋ Working... 4.2s`.
- The origin is stamped by `__enter__`, not by `start()`. The indicator stops and
  restarts once per durable line, so a per-`start()` origin would reset the
  counter at every `[skill]`, `[tool N/4]`, and `[result]` — the number would
  measure the current silence rather than the turn. A bare `start()` outside a
  `with` block still stamps its own.
- Erase width became dynamic. It used to be fixed at construction
  (`max(frame) + 1 + len(label)`); a counter growing to `1m 41.0s` draws a wider
  line than that, and a fixed erase would have left its tail on screen. The
  widest line drawn since the last stop is tracked and erased instead — read
  without a lock, safely, because `stop()` only reads it after `thread.join()`.
- `app.py`: one wall clock stamped before `with indicator:`, and
  `print_turn_time(indicator, seconds)` in all four outcome branches — completed,
  cancelled, failed, and the unexpected-exception path that has no `outcome` at
  all. `outcome.duration_ms` was deliberately *not* reused despite already
  covering routing plus the agent loop: it is absent on that exception path, and
  a second origin could disagree with the counter the user just watched.
- The completed branch now terminates the streamed answer with `print()` before
  the footer. The first pty run caught this: the final answer is streamed without
  a trailing newline, so the footer landed glued to the end of the last sentence:

```text
Расчёт основан на суммировании (UnitPrice × Quantity) ... из таблицы InvoiceLine.[time] 24.5s
```

#### The TTY-only decision

Both the counter and the footer appear only when stdout is a TTY, gated on the
indicator's existing `enabled` property rather than a second `isatty` check.

This is the load-bearing choice. PATCH-010-01 promised non-TTY stdout "not a
single byte ... so redirected output, test captures, and the transcripts already
committed to `README.md` and the journals stay byte-for-byte unchanged", and
PATCH-010-02 chose a fixed `TOOL_DISPLAY_WIDTH` over the real terminal width for
the same reason. A wall-clock duration is irreproducible by construction, so
printing it into piped output would have broken precisely what those two patches
were built to protect. Every committed transcript therefore stays correct as
written and none were touched.

An "always print, behind a `--show-time` flag" variant was considered and
rejected: it would add configuration and lifecycle semantics, which is SPEC
territory, not a patch.

#### Verification

- `python -m pytest tests/test_cli_activity.py -q` — 36 passed, from 19. New
  groups cover the formatter at its boundaries (`59.94` → `59.9s`, `59.96` →
  `1m 00.0s`, negative clock readings), that the counter is cumulative across a
  stop/start cycle within one turn, that a new turn restarts from zero, that
  erasure clears a line that grew past the label and does not carry that width
  into the next turn, that a redirected stream still receives zero bytes with the
  clock advanced an hour, and that the footer prints on a TTY and not otherwise.
  One existing assertion changed: `test_start_draws_the_first_frame_immediately`
  now expects `\r⠋ Working... 0.0s`. Nothing sleeps — the tests drive a
  `FakeClock` and redraw through the public `stop()`/`start()` path, which is
  what a real turn does around every durable line.
- `python -m pytest -q` — 754 passed, 29 skipped (from 737/29).
- `python -m evals.runner --suite scripted` — 40/40, unchanged.
- Non-TTY, live: `printf '…\n/bye\n' | python app.py > piped.txt` exited `0` with
  **0** carriage returns, **0** frame glyphs, and **0** `[time]` lines. The
  transcript is identical in shape to pre-patch.
- TTY, live: `app.py` driven inside a real pty against `fast`/qwen3:8b. Within
  the chat region the capture holds **77** spinner frames with **77 distinct**
  counter readings advancing `0.0s → 9.4s`, **2** erase sequences (one per silent
  stretch), **0** ANSI escapes, and exactly **1** `[time]` line. Replaying the
  carriage returns the way a terminal does leaves **0** lines showing a spinner
  frame or a stale counter:

```text
You: Какой жанр принёс больше всего выручки?
[skill] sales_analysis

Qwen: Жанр, который принёс больше всего выручки, — **Rock**.
**Общая выручка:** $826.65.

Расчёт основан на суммировании `(UnitPrice × Quantity)` для всех треков в жанре "Rock".
[time] 10.9s
```

- **A discrepancy worth recording rather than hiding:** the footer read `10.9s`
  while the last counter frame read `9.4s`. That is correct and not a rounding
  artefact — the indicator stops for good on the first final-answer chunk
  (PATCH-010-01's rule), and the turn kept running for another ~1.5 s while the
  rest of the answer streamed. The counter measures up to the last moment the CLI
  was silent; the footer measures the whole turn. `README.md` now says so.
- No live *model-behavior* verification is required (`patches/README.md` →
  Verification): the model saw exactly the same prompts, tools, and results. The
  live runs exist to prove terminal behavior, not to judge the model.

#### Outcome

All acceptance criteria met. The counter measures the turn rather than the
current silence, the footer appears for every outcome, erasure follows the grown
line, and the non-TTY byte-for-byte guarantee is intact — which is why none of
the eight committed `README.md` transcripts needed editing.

Scope stayed inside the patch: no per-phase breakdown, no tokens/s or GPU
telemetry, no ETA, no configuration flag, no trace or `duration_ms` change, and
no colour or ANSI. PATCH-010-01's `## Out of scope` deferred "richer runtime
telemetry" to separate work; this took only the cheapest, least speculative
measurement available and left the rest deferred.
