# SPEC-011 — Agent Reliability & Observability

- **Spec:** [SPEC-011](../../specs/SPEC-011-Agent-Reliability-Observability.md)
- **Date:** 2026-07-24
- **Branch:** feature/SPEC-011-agent-reliability-observability
- **Merge commit:** _pending — recorded in a follow-up commit after merge_

## Hypothesis / intent
SPEC-010 gave the agent a bounded model→tool→model loop, but several failure
modes were still invisible or undifferentiated: a hung model/tool/MCP call
blocked forever, a repeated ineffective tool call went undetected, every
failure collapsed into one `AgentTurnError` string, there was no structured
trace to diagnose a bad run, and — flagged explicitly in every journal entry
since SPEC-007 — the repo had never committed a `tests/` suite. SPEC-011 makes
the existing loop observable, time-bounded, and covered by committed
deterministic tests and a small scripted evaluation suite, without changing
what the agent can do.

## What changed
- `reliability.py` (new): `TurnStatus`, `TerminationReason`, `STATUS_BY_REASON`,
  `USER_MESSAGE_BY_REASON`, the frozen `AgentTurnOutcome`, `AgentRuntimeError`
  and its typed subclasses (`ModelRequestTimeout`, `ToolExecutionTimeout`,
  `TurnTimeoutExceeded`, `RepeatedToolCallError`), `new_id`, `canonical_json`,
  `tool_call_fingerprint`, `sha256_of`, the caller-side `run_with_deadline` /
  `DeadlineExceeded`, and `validate_reliability_config`.
- `tracing.py` (new): `build_event` (schema-versioned, UTC timestamp), the
  `TraceSink` protocol, `JsonlTraceSink` (append-only, lock-guarded),
  `NullTraceSink`, `MemoryTraceSink`, `SafeTraceSink` (warns once, never
  breaks the agent, never recurses), and `preview_and_hash` for size-bounded
  payload previews.
- `agent.py` (rewritten): `AgentTurnError` retired. `AgentRunner.run_turn`
  now returns one `AgentTurnOutcome` per call — never a bare string — and
  emits the full SPEC-011 trace-event contract (`turn_started`,
  `model_request_started`/`model_response_finished`, `tool_call_requested`,
  `tool_execution_started`/`tool_execution_finished`, `policy_violation`,
  `turn_finished`, exactly one terminal event per turn, guaranteed even on an
  unexpected internal exception, which is still re-raised after the terminal
  event so it stays visible to callers/tests). Repeated-call detection uses a
  canonical-JSON fingerprint and is checked **before** the tool-call budget
  (§17): a repeated call can stop the turn before the budget is ever reached.
  Model requests and tool executions are each wrapped in
  `reliability.run_with_deadline` with a deadline that respects the
  whole-turn budget (`min(component_timeout, remaining_turn_time)`); the
  whole-turn deadline is checked before every blocking operation starts, so a
  turn that has run out of time never begins another model request or tool
  call.
- `config.py`: added `MODEL_REQUEST_TIMEOUT_SECONDS = 120`,
  `TOOL_EXECUTION_TIMEOUT_SECONDS = 30`, `AGENT_TURN_TIMEOUT_SECONDS = 180`,
  `MAX_IDENTICAL_TOOL_CALLS = 2`, `TRACE_ENABLED = True`,
  `TRACE_PATH = "data/traces/agent.jsonl"`,
  `TRACE_PAYLOAD_PREVIEW_CHARS = 1000`. `MAX_TOOL_CALLS_PER_TURN` unchanged.
- `llm.py`: `Client(host=OLLAMA_HOST, timeout=MODEL_REQUEST_TIMEOUT_SECONDS)`
  — a component-native defense-in-depth floor. Documented honestly: the
  installed SDK (`ollama==0.6.2`) only exposes this as httpx's
  connect/read/write/pool timeout, which bounds inactivity *between* chunks,
  not the total duration of a long, continuously streaming response. The
  authoritative bound on one full model decision is `agent.py`'s caller-side
  `run_with_deadline` around the whole streamed exchange.
- `mcp_integration/client.py`: `McpClientManager.__init__` gained a
  `call_timeout: float = 30.0` parameter (was a disconnected module constant
  `_CALL_TIMEOUT`); `app.py` now sources it from
  `TOOL_EXECUTION_TIMEOUT_SECONDS`. No other behavior changed — the manager's
  existing catch-all (folding its own internal timeout into an `{"ok":
  false}` envelope) is left as-is; see "MCP timeout precedence" below for why.
- `app.py`: `main()` now owns one `run_id` (generated once) and a fresh
  `turn_id` per turn, wraps the configured `TraceSink` in `SafeTraceSink`,
  emits `run_started` before MCP startup and `run_finished` in the outermost
  `finally` (after `manager.close()`, covering every exit path including a
  fail-fast MCP startup failure). The per-turn block now consumes
  `AgentTurnOutcome`: `COMPLETED` persists and saves; any other status prints
  `Run ID: <turn_id>` alongside the taxonomy's user-facing message and rolls
  back via the existing `conversation.remove_last_message()` — no new
  rollback API was needed. `McpClientManager` is now constructed with
  `call_timeout=TOOL_EXECUTION_TIMEOUT_SECONDS`.
- `tests/` (new, committed): `test_reliability.py`, `test_tracing.py`,
  `test_agent_runner.py`, `test_eval_runner.py`, `support.py` (shared
  scripted fixtures: `ScriptedResponder`, `FakeToolExecutor`,
  `RecordingRenderer`, `FakeClock`). `conftest.py` at the repo root makes the
  root importable from `tests/` regardless of how pytest is invoked.
  **60 tests, no live Ollama, no network, no live MCP server** — this closes
  the deviation carried since SPEC-007.
- `requirements-dev.txt` (new): `-r requirements.txt` + `pytest==9.1.1`.
- `evals/` (new): `cases.json` (9 committed cases across all required
  categories: no-tool, calculator, SQL, SQL recovery, multi-tool, MCP time,
  repetition guard, tool-call budget guard, timeout), `runner.py` (`--suite
  scripted`, the default and CI-safe; `--suite live`, optional, exercises the
  real model/tools/MCP via `app.build_executor`/`McpClientManager`),
  `README.md`. Results are versioned JSON written to `data/evals/`.
- `.gitignore`: added `data/traces/` and `data/evals/` (top-level `evals/` —
  the committed case definitions — stays tracked).
- `README.md`: new "Надёжность и наблюдаемость хода (SPEC-011)" section
  (outcomes, deadlines, repeated-call detection, tracing, tests, evals);
  updated the config block and the file-structure table.

## MCP timeout precedence (deviation resolved, not deferred)
The spec wants a tool timeout to produce `timed_out/tool_timeout` rather than
be silently folded into an `{"ok": false}` observation, but
`mcp_integration/client.py`'s `call_tool` already catches its own internal
`_CALL_TIMEOUT` (now `self._call_timeout`) and does exactly that fold-through
on **any** internal exception. Rather than giving MCP timeouts a third,
special raise-contract (breaking "every tool shares one execution contract"),
`agent.py`'s outer `run_with_deadline` around `executor.execute(...)` starts
its clock strictly before `McpClientManager.call_tool` starts its own, so with
an equal or tighter host-configured timeout the outer `ToolExecutionTimeout`
always wins in the ordinary case — proven by
`TestMcpLikeHangPrecedence` in `test_agent_runner.py`, which simulates exactly
this fold-through with a handler that blocks well past the outer deadline.
The residual (negligible, caller-side-only) race is documented rather than
hidden: Python cannot forcibly cancel the inner coroutine either way.

## Timeout honesty per component (§14)
- **Model requests:** caller-side deadline only (`run_with_deadline` around
  the full streamed decision). The `ollama==0.6.2` SDK's `Client(timeout=...)`
  is a component-native floor but only bounds inter-chunk inactivity via
  httpx, not total stream duration — confirmed by inspecting the installed
  package, not assumed.
- **`python_calculate`/`sql_query`:** already internally bounded (AST
  node/depth/exponent/factorial/sequence limits; SQLite
  `set_progress_handler` cooperative abort). The outer timeout here is a
  safety net that should essentially never fire.
- **MCP:** already a `Future.result(timeout=...)` deadline; does not call
  `task.cancel()` on expiry, so it was already caller-side-deadline-only
  before this spec, unchanged here beyond making the timeout host-configurable.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B, ctx 40960; capabilities:
  completion, tools, thinking)
- Ollama: server 0.31.1; SDK `ollama==0.6.2`; reachable at `http://localhost:11434`
- MCP SDK: `mcp==1.28.1`
- Interpreter: project `venv/bin/python`; `pytest==9.1.1`
- Sampling: defaults — no `options` set in `llm.py`

## Verification

**Committed suite — `pytest` — 60/60 PASS**, no live Ollama, no network, no
live MCP server, runs in well under a second:
- `test_reliability.py` (17): fingerprint key-order independence, different
  tool/arg fingerprints, `run_with_deadline` success/exception/expiry (0.02s
  real wait, no long sleeps), daemon-worker-does-not-block-exit, config
  validation (all invalid-value cases + the `turn_timeout <
  min(model,tool)` rule).
- `test_tracing.py` (13): required fields on every event, UTC/timezone-aware
  timestamps, preview truncation, stable hashing, `NullTraceSink`/
  `MemoryTraceSink`, `JsonlTraceSink` one-JSON-object-per-line under 8
  concurrent threads × 50 events (400 lines, all parse), `SafeTraceSink`
  warns exactly once and never recurses on a permanently broken inner sink.
- `test_agent_runner.py` (18): Scenarios A–I from the spec (direct
  completion; tool-then-completion; structured-error recovery;
  repeated-call stop with `tool_calls_executed=2`; tool-call-budget
  exhaustion; model timeout; tool timeout; whole-turn deadline blocking the
  next operation via a fake clock, no real waiting; trace-sink failure not
  changing the outcome) plus parallel-call rejection, empty response,
  repetition reset (A,A,B,A→complete), canonical-argument-order equivalence,
  the MCP-fold-through precedence case, user interrupt → `cancelled`, input
  snapshot not mutated, and config validation at construction.
- `test_eval_runner.py` (5): expectation-assertion unit checks, committed
  cases load with unique IDs and all 9 required categories present, the
  scripted suite passes end-to-end with zero failures.

**Scripted evaluations — `python -m evals.runner --suite scripted` — 9/9 PASS**
(all in ~0ms except the 55ms deliberately-real timeout case):
```text
[PASS] no-tool-basic-001 (completed/final_answer, 0ms)
[PASS] calc-basic-001 (completed/final_answer, 0ms)
[PASS] sql-basic-001 (completed/final_answer, 0ms)
[PASS] sql-recovery-001 (completed/final_answer, 0ms)
[PASS] multi-tool-001 (completed/final_answer, 0ms)
[PASS] mcp-time-001 (completed/final_answer, 0ms)
[PASS] repetition-guard-001 (stopped/repeated_tool_call, 0ms)
[PASS] budget-guard-001 (stopped/tool_call_limit, 0ms)
[PASS] timeout-scripted-001 (timed_out/model_timeout, 55ms)

9/9 passed (0 failed).
```

**Live evaluations — `python -m evals.runner --suite live` — 5/5 PASS**
(real qwen3:8b, real local tools, real MCP time server):
```text
[PASS] no-tool-basic-001 (completed/final_answer, 5269ms)
[PASS] calc-basic-001 (completed/final_answer, 5793ms)
[PASS] sql-basic-001 (completed/final_answer, 10350ms)
[PASS] multi-tool-001 (completed/final_answer, 16386ms)
[PASS] mcp-time-001 (completed/final_answer, 8651ms)

5/5 passed (0 failed).
```

**Live manual pass (`python app.py`, scripted stdin, scratch history backed up
and restored):**
```text
You: What is an agent loop? Answer in one short sentence.
Qwen: An agent loop is the continuous cycle of observation, action, reward,
and policy update that an AI agent undergoes to learn and adapt...

You: What is 173 multiplied by 284?
[tool 1/4] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}
Qwen: 173 multiplied by 284 is 49,132.

You: Which genre generated the most revenue, and what percentage of total
revenue did it represent?
[tool 1/4] sql_query
[args] {"query": "WITH GenreRevenue AS (...) SELECT ... ORDER BY TotalRevenue DESC LIMIT 1;"}
[result] {"ok": true, "columns": ["Genre","TotalRevenue","Percentage"], "rows": [["Rock",826.65,35.5]], "row_count": 1, "truncated": false}
Qwen: Rock generated the most revenue, accounting for $826.65, 35.5% of total.

You: What time is it now in Europe/Amsterdam?
[tool 1/4] mcp_time__get_current_time
[args] {"timezone": "Europe/Amsterdam"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {...}}
Qwen: Current time in Europe/Amsterdam: 10:35 AM, July 24, 2026 (UTC+2).

You: /bye
```
`tail -n 20 data/traces/agent.jsonl` confirmed: every line is exactly one
valid JSON object (33 lines this run, verified programmatically); one
`turn_id` correlates every event of its turn; exactly one `turn_finished` per
`turn_started`; the SQL text appears (length-limited, per spec §8) but no
result rows; no `chain_of_thought`/`internal_reasoning`/`hidden_thoughts`
fields anywhere.

**Real timeout path.** Temporarily set `TOOL_EXECUTION_TIMEOUT_SECONDS =
0.0001` and asked a Chinook question that forces `sql_query`:
```text
[tool 1/4] sql_query
[args] {"query": "SELECT COUNT(*) FROM Track;"}

Application error: Tool 'sql_query' timed out.
Run ID: 237500c7-0d86-4725-816c-5a36fe70952e
```
`data/chat_history.json` message count was unchanged after this turn (the
user message was rolled back); the trace recorded
`tool_execution_finished{result_ok: null, error_type: "timeout"}` followed by
exactly one `turn_finished{status: "timed_out", reason: "tool_timeout"}`.
Reverted the config change immediately after.

## Outcome
All SPEC-011 acceptance criteria met. Every turn now produces one explicit,
typed `AgentTurnOutcome`; every started turn emits exactly one structured
`turn_finished` trace event; model, tool, and whole-turn deadlines are
enforced and honestly documented as caller-side-only; repeated identical tool
calls are detected and stopped before the global budget; and the project has,
for the first time, a committed `tests/` suite (60 tests) plus a committed,
runnable evaluation suite (9 scripted + 5 live cases) — closing the deviation
every journal entry since SPEC-007 had flagged.

## Follow-ups
- `mcp_integration/client.py`'s internal timeout could `task.cancel()` the
  pending request instead of only abandoning the wait — true cooperative
  cancellation for MCP, not attempted here to keep the diff minimal.
- OpenTelemetry / metrics / trace rotation are explicit non-goals for this
  step; `TraceSink` is the seam left for them.
- `VERBOSE_AGENT_DIAGNOSTICS` (optional per spec) was not implemented.
- Retry policy for timed-out operations, once any tool has side effects
  (SPEC-011 explicitly performs no automatic retries).
