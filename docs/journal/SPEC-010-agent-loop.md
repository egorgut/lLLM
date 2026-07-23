# SPEC-010 — Bounded Agent Loop

- **Spec:** [SPEC-010](../../specs/SPEC-010-Agent-Loop.md)
- **Date:** 2026-07-23
- **Branch:** feature/SPEC-010-agent-loop
- **Merge commit:** <pending>

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
