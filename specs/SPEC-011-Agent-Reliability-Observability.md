# SPEC-011: Agent Reliability & Observability

## Background

SPEC-006 introduced the shared tool contract and `ToolRegistry`.

SPEC-007 completed the first executable local tool path with
`python_calculate`.

SPEC-008 connected the same execution path to the local read-only Chinook
SQLite database through `sql_query`.

SPEC-009 added an MCP client boundary and registered the MCP-backed
`mcp_time__get_current_time` tool in the same registry and executor as the local
tools.

SPEC-010 replaced the fixed one-tool-per-turn flow with a bounded agent loop:

```text
User request
    │
    ▼
LLM decision
    │
    ├── final answer ──────────────────────────────┐
    │                                              │
    └── one tool call                              │
            │                                      │
            ▼                                      │
       ToolExecutor                                │
            │                                      │
            ▼                                      │
       structured result                           │
            │                                      │
            └──────────── back to LLM ─────────────┘
```

The current runtime already has one important safety boundary:

```python
MAX_TOOL_CALLS_PER_TURN = 4
```

However, the loop is still reliable only in the narrow sense that it cannot run
forever by executing unlimited tools.

Several failure modes remain weakly defined or invisible:

- a model request may hang;
- a tool handler may hang;
- an MCP call may not return;
- the model may repeat the same ineffective tool call;
- a turn may fail without a machine-readable reason;
- console output is useful to a human but not sufficient for diagnosis;
- there is no committed automated regression suite;
- there is no repeatable evaluation set for agent behavior;
- historical runs cannot be compared;
- a failure cannot be reconstructed from one stable trace.

The current `AgentTurnError` also combines several different conditions behind a
single exception type:

```text
empty final response
parallel tool calls
tool-call budget exhausted
invalid runner configuration
```

That is enough for basic CLI handling, but not enough for systematic reliability
work. The application needs to distinguish:

```text
completed successfully
stopped by a safety policy
timed out
failed because the model transport failed
failed because tool dispatch failed
failed because the runtime contract was violated
interrupted by the user
```

The next architectural step is therefore not a larger agent or another tool.
It is to make the existing agent loop observable, diagnosable, time-bounded, and
protected by committed tests and repeatable evaluations.

---

## Goal

Introduce a small reliability and observability layer around the existing
`AgentRunner` so every user turn:

1. has a stable run identifier;
2. emits a structured trace;
3. has explicit model, tool, and whole-turn time limits;
4. detects repeated identical tool calls;
5. ends with one machine-readable termination state;
6. remains understandable in the CLI;
7. is covered by committed deterministic tests;
8. can be exercised by a small committed evaluation suite;
9. does not require a live Ollama model for unit tests;
10. preserves the existing tool, MCP, conversation, and storage boundaries.

Target lifecycle:

```text
start turn
    │
    ▼
create run context
    │
    ▼
emit turn_started
    │
    ▼
model request ── timeout / transport error ──┐
    │                                        │
    ▼                                        │
decision                                     │
    ├── final text                           │
    │      │                                 │
    │      ▼                                 │
    │   completed                            │
    │                                        │
    └── tool call                            │
            │                                │
            ├── repeated-call policy ────────┤
            ├── tool-call budget ────────────┤
            ├── tool timeout ────────────────┤
            └── execution error ─────────────┤
                                             │
                                             ▼
                                      terminal outcome
                                             │
                                             ▼
                                      emit turn_finished
```

The implementation must produce one authoritative outcome for every started
turn, including failures.

---

## User-visible behavior

### 1. Successful answer without tools

The normal interaction remains concise:

```text
You: What is an agent loop?

Qwen: An agent loop is...
```

No verbose trace is printed by default.

A structured trace is still written in the background:

```json
{"event":"turn_started","run_id":"...","turn_id":"..."}
{"event":"model_request_started","step":1}
{"event":"model_response_finished","step":1,"decision":"final_answer"}
{"event":"turn_finished","status":"completed","reason":"final_answer"}
```

### 2. Successful answer with tools

Existing tool diagnostics remain recognizable:

```text
You: What is 173 multiplied by 284?

[tool 1/4] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}

Qwen: The result is 49,132.
```

The trace adds timestamps, durations, step numbers, and stable identifiers.

### 3. Model timeout

If one model request exceeds the configured timeout:

```text
Application error: Agent turn timed out while waiting for the model.
Run ID: 01J...
```

The current user turn is rolled back.

No partial assistant answer is persisted.

The application remains usable for the next request.

### 4. Tool timeout

If a tool does not finish before its deadline:

```text
Application error: Tool 'sql_query' timed out.
Run ID: 01J...
```

The turn terminates in this iteration.

A timeout is not converted into an ordinary `{"ok": false}` tool observation,
because the harness cannot assume that an interrupted handler reached a safe and
known state.

### 5. Repeated identical tool call

If the model requests the same tool with semantically identical arguments too
many consecutive times:

```text
[tool 1/4] sql_query
[args] {"query": "SELECT ..."}

[tool 2/4] sql_query
[args] {"query": "SELECT ..."}

Application error: Agent stopped after repeating the same tool call 2 times.
Run ID: 01J...
```

The repeated call that crosses the configured threshold must not be executed.

### 6. Tool-call budget exhausted

The existing boundary remains:

```text
Application error: Agent stopped after 4 tool calls without a final answer.
Run ID: 01J...
```

The requested fifth call is not executed.

The trace records the terminal reason as `tool_call_limit`.

### 7. Empty model response

```text
Application error: Model returned an empty response.
Run ID: 01J...
```

The trace records the terminal reason as `empty_model_response`.

### 8. Parallel tool calls

```text
Application error: Parallel tool calls are not supported.
Run ID: 01J...
```

The trace records the terminal reason as `parallel_tool_calls`.

### 9. User interruption

On `Ctrl+C` during an active turn:

```text
Generation interrupted.
Run ID: 01J...
```

The trace records:

```json
{
  "status": "cancelled",
  "reason": "user_interrupt"
}
```

The incomplete exchange is not persisted.

### 10. Trace write failure

Tracing must never silently break the agent.

If the trace sink cannot write:

```text
Warning: trace output is unavailable for this run.
```

The turn may continue if the runtime itself is healthy.

A trace failure must not replace the actual turn outcome.

---

## Scope

This specification includes:

- committed automated tests;
- structured local tracing;
- run and turn identifiers;
- explicit timing measurements;
- model request timeout;
- tool execution timeout;
- whole-turn deadline;
- repeated identical-call detection;
- typed and diagnosable termination states;
- a small committed evaluation dataset;
- a deterministic evaluation runner;
- human-readable evaluation output;
- machine-readable evaluation output;
- README documentation;
- journal documentation.

---

## Non-goals

This specification does not introduce:

- OpenTelemetry SDK;
- Jaeger;
- Tempo;
- Prometheus;
- Grafana;
- a web dashboard;
- distributed tracing across remote services;
- cloud telemetry;
- telemetry upload;
- token-cost accounting;
- prompt/version experiment management;
- LLM-as-a-judge;
- semantic answer grading by another model;
- parallel tool calls;
- retries of model requests;
- retries of timed-out tools;
- cancellation guarantees for arbitrary Python code;
- subprocess isolation for every local tool;
- a generic workflow engine;
- persisted chain-of-thought;
- storage of hidden model reasoning;
- production-grade SLO infrastructure.

The architecture should leave room for OpenTelemetry later, but SPEC-011 uses a
small framework-free local event model.

---

## Core architectural decisions

### 1. A turn returns an explicit outcome

The runtime must no longer represent success as a returned string and every
failure as an unrelated exception message.

Introduce an explicit result model, for example:

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TerminationReason(StrEnum):
    FINAL_ANSWER = "final_answer"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    TOOL_CALL_LIMIT = "tool_call_limit"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    MODEL_TIMEOUT = "model_timeout"
    TOOL_TIMEOUT = "tool_timeout"
    TURN_TIMEOUT = "turn_timeout"
    MODEL_ERROR = "model_error"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    USER_INTERRUPT = "user_interrupt"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class AgentTurnOutcome:
    run_id: str
    turn_id: str
    status: TurnStatus
    reason: TerminationReason
    final_text: str | None
    tool_calls_executed: int
    model_requests: int
    duration_ms: int
    error_message: str | None = None
```

The exact module split may vary, but these semantics are required.

A successful turn is:

```text
status = completed
reason = final_answer
final_text != None
```

Every non-successful outcome has:

```text
final_text = None
```

A partial streamed answer must never become `final_text` after a failed turn.

### 2. Exceptions remain internal control flow

The explicit outcome does not require every low-level component to stop using
exceptions.

The agent layer may use typed internal exceptions:

```python
class AgentRuntimeError(Exception):
    reason: TerminationReason
```

Examples:

```python
class ModelRequestTimeout(AgentRuntimeError): ...
class ToolExecutionTimeout(AgentRuntimeError): ...
class RepeatedToolCallError(AgentRuntimeError): ...
```

However, the boundary called by `app.py` must produce one
`AgentTurnOutcome` for all expected terminal conditions.

Unexpected programming defects may still propagate during tests, but the CLI
boundary must convert them into:

```text
status = failed
reason = internal_error
```

after emitting the terminal trace event.

Raw tracebacks must not be shown during normal CLI use.

### 3. Every started turn receives identifiers

Introduce two identifiers:

```text
run_id  = one application execution
turn_id = one user turn inside that application execution
```

Required properties:

- generated by the host;
- opaque to the model;
- unique enough for local diagnostic use;
- printable in logs;
- included in every trace event;
- not persisted into semantic chat history;
- no external service required.

A UUID4 string is acceptable.

A ULID-like sortable identifier is also acceptable if implemented without a
large dependency.

Do not derive identifiers from user text.

### 4. Structured traces use append-only JSON Lines

Default trace path:

```text
data/traces/agent.jsonl
```

Each line is one complete JSON object.

Example:

```json
{"schema_version":1,"timestamp":"2026-07-24T08:15:22.491Z","event":"turn_started","run_id":"...","turn_id":"..."}
```

JSONL is selected because it is:

- append-only;
- human inspectable;
- streamable;
- easy to parse with Python;
- resilient to one malformed or interrupted final line;
- compatible with future forwarding to an observability system.

The tracer must not rewrite the complete file for each event.

### 5. Trace schema is versioned

Every event must include:

```json
{
  "schema_version": 1
}
```

The schema version changes only when event compatibility changes.

Adding an optional field does not necessarily require a new schema version.

Renaming an event or changing field meaning does require a new schema version.

### 6. Trace events are domain events, not arbitrary log strings

Required event names:

```text
run_started
run_finished
turn_started
model_request_started
model_response_finished
tool_call_requested
tool_execution_started
tool_execution_finished
policy_violation
turn_finished
trace_error
```

Optional additional events are allowed when documented.

Each event must contain:

```text
schema_version
timestamp
event
run_id
```

Turn-scoped events also contain:

```text
turn_id
```

Step-scoped events contain:

```text
step
```

Tool-scoped events contain:

```text
tool_name
tool_call_index
```

### 7. Traces record decisions and outcomes, not hidden reasoning

The trace may record:

- user-text length;
- model name;
- available tool names;
- response type;
- selected tool name;
- normalized arguments or a safe representation;
- tool result status;
- error type;
- duration;
- termination reason;
- counters.

The trace must not claim to record the model's private reasoning.

Do not introduce fields named:

```text
chain_of_thought
internal_reasoning
hidden_thoughts
```

Model text returned before a tool call should not be treated as trustworthy
reasoning telemetry.

### 8. Sensitive and unbounded payloads are not copied blindly

The trace must not dump complete arbitrary payloads without limits.

At minimum:

- user text is omitted by default or truncated;
- final answer text is omitted by default or truncated;
- SQL text may be recorded because it is operationally useful, but must be
  length-limited;
- database result rows are not copied into the trace;
- MCP raw protocol frames are not copied;
- tool arguments are serialized with a size limit;
- tool results record metadata and error envelopes, not entire large results.

Recommended trace representation:

```json
{
  "tool_name": "sql_query",
  "arguments_preview": "{\"query\":\"SELECT ...\"}",
  "arguments_sha256": "...",
  "arguments_truncated": false
}
```

Hashing is for correlation, not security.

Default preview limit:

```python
TRACE_PAYLOAD_PREVIEW_CHARS = 1000
```

### 9. Timing uses both wall-clock timestamps and monotonic durations

Event timestamps use timezone-aware UTC wall time:

```text
2026-07-24T08:15:22.491Z
```

Durations use `time.monotonic()` or `time.perf_counter()`.

Do not calculate elapsed duration by subtracting wall-clock timestamps.

Required duration fields use integer milliseconds:

```text
duration_ms
```

### 10. Time limits are host-owned

Add configuration:

```python
MODEL_REQUEST_TIMEOUT_SECONDS = 120
TOOL_EXECUTION_TIMEOUT_SECONDS = 30
AGENT_TURN_TIMEOUT_SECONDS = 180
MAX_IDENTICAL_TOOL_CALLS = 2
```

Exact defaults for this specification:

```text
model request timeout = 120 seconds
tool execution timeout = 30 seconds
whole turn timeout     = 180 seconds
identical calls        = 2 consecutive executed calls
```

All values are controlled by the host and are never supplied by the model.

Invalid values must be rejected at startup:

```text
timeout <= 0
MAX_IDENTICAL_TOOL_CALLS < 1
AGENT_TURN_TIMEOUT_SECONDS < min(
    MODEL_REQUEST_TIMEOUT_SECONDS,
    TOOL_EXECUTION_TIMEOUT_SECONDS,
)
```

The final validation rule may be slightly less strict if remaining-deadline logic
is implemented correctly, but all defaults must be internally coherent.

### 11. The whole-turn deadline is authoritative

At turn start:

```python
turn_deadline = monotonic_now + AGENT_TURN_TIMEOUT_SECONDS
```

Before every blocking action:

```text
remaining = turn_deadline - monotonic_now
```

The effective timeout is:

```text
min(component_timeout, remaining_turn_time)
```

If no turn time remains, the component must not start.

This prevents several individually valid operations from exceeding the total
turn budget.

### 12. Model timeout wraps one complete model decision

The model timeout covers one call to the model transport, including consumption
of the streaming response required to determine the final tool calls.

The timeout boundary must include:

```text
respond(...)
consume text_chunks()
read authoritative tool_calls
```

A timeout after some chunks were streamed still fails the turn.

The partial text is not persisted.

The trace may record:

```json
{
  "partial_text_chars": 84
}
```

but should not copy the partial content by default.

### 13. Tool timeout wraps one execution

The tool timeout covers:

```python
executor.execute(call.name, call.arguments)
```

A timeout terminates the turn.

This specification does not require feeding a synthetic timeout result back to
the model.

Reason:

- a timed-out operation may still be running;
- future write-capable tools may have unknown side effects;
- retrying or continuing could duplicate work;
- safe cancellation semantics differ by tool type.

### 14. Timeout implementation must be honest about cancellation

Python threads cannot safely terminate arbitrary running code.

Therefore, an implementation that merely returns after a timeout while leaving
an unbounded non-daemon worker alive is not acceptable.

For SPEC-011, use one of these approaches:

#### Acceptable approach A: component-native timeout

Use a timeout supported by the component itself:

- Ollama HTTP client timeout for model calls;
- SQLite progress handler / connection interruption;
- MCP request timeout and process/session cancellation.

#### Acceptable approach B: subprocess isolation

Run the bounded operation in a child process that can be terminated.

#### Acceptable approach C: controlled daemon worker for known read-only demo tools

A daemon worker may be used only if:

- the tool is known to be read-only or pure;
- process exit is not blocked;
- the limitation is documented;
- tests prove the caller returns on deadline;
- no claim of hard cancellation is made.

The selected implementation must document, per component, whether the timeout is:

```text
hard cancellation
cooperative cancellation
caller-side deadline only
```

Do not describe caller-side abandonment as guaranteed cancellation.

### 15. Repeated-call detection uses canonical tool-call fingerprints

A tool call fingerprint is built from:

```text
tool name
+
canonical JSON arguments
```

Canonical JSON requirements:

```python
json.dumps(
    arguments,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
```

Fingerprint example:

```text
sql_query:{"query":"SELECT COUNT(*) FROM Track"}
```

These calls are identical:

```json
{"a": 1, "b": 2}
{"b": 2, "a": 1}
```

These calls are not identical:

```json
{"query": "SELECT 1"}
{"query": "SELECT 2"}
```

The comparison is structural. Do not attempt SQL semantic equivalence.

### 16. Repetition means consecutive identical calls

The default policy observes consecutive calls:

```text
A, A       → threshold reached after the second executed A
A, B, A    → not consecutive; counter resets
A, A, A    → third A is rejected before execution when maximum is 2
```

`MAX_IDENTICAL_TOOL_CALLS = 2` means:

- the first identical call may execute;
- the second consecutive identical call may execute;
- the third consecutive identical request is rejected before execution.

This permits one model retry when a tool result may have changed, while stopping
a clear loop.

The trace records both the fingerprint hash and repetition count.

### 17. Repeated-call detection and tool budget are separate policies

A repeated call may stop the turn before the global tool-call budget.

Example:

```text
max tool calls = 4
max identical calls = 2

A executes
A executes
A requested again → repeated_tool_call
```

Tool calls executed:

```text
2
```

The rejected third call does not increment `tool_calls_executed`.

### 18. Tool failures are still observations

The SPEC-010 rule remains:

```json
{"ok": false, "error": {...}}
```

is an ordinary tool result, not a runtime failure.

The model may recover by selecting another action.

Repeated-call detection still applies to retries after structured errors.

### 19. Trace writing is best-effort but visible

The tracer interface should be injectable:

```python
class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...
```

Required implementations:

```text
JsonlTraceSink
NullTraceSink
```

Tests may use:

```text
MemoryTraceSink
```

If `JsonlTraceSink.emit(...)` fails:

- the agent operation continues when possible;
- a one-time warning is shown;
- repeated warnings are suppressed for that turn;
- trace failure does not overwrite the real terminal reason.

A trace sink must never recursively trace its own trace failure indefinitely.

### 20. Tracing is separated from CLI rendering

`Renderer` remains responsible for user-facing output.

`TraceSink` is responsible for structured diagnostic events.

Do not make the CLI renderer parse trace events to reconstruct normal output.

Do not make the trace sink print normal assistant text.

### 21. Persistence remains semantic

`data/chat_history.json` continues to contain only semantic user and assistant
messages.

Do not persist:

- trace events;
- run IDs;
- turn IDs;
- tool protocol messages;
- tool results;
- timeout metadata;
- evaluation metadata.

Trace data lives under:

```text
data/traces/
```

and is separately git-ignored.

### 22. One final event is mandatory

Every emitted `turn_started` must have exactly one matching `turn_finished`
event unless the complete Python process is terminated externally.

`turn_finished` includes:

```json
{
  "status": "completed|failed|stopped|timed_out|cancelled",
  "reason": "...",
  "tool_calls_executed": 2,
  "model_requests": 3,
  "duration_ms": 1842
}
```

The terminal event is emitted from one `finally`-style boundary.

Do not emit multiple terminal events for one turn.

---

## Proposed module structure

The exact names may vary, but responsibility boundaries must remain clear.

```text
agent.py
    AgentRunner
    loop policy
    repeated-call policy integration
    creation of AgentTurnOutcome

reliability.py
    TurnStatus
    TerminationReason
    AgentTurnOutcome
    timeout/deadline helpers
    tool-call fingerprint helper

tracing.py
    TraceEvent
    TraceSink protocol
    JsonlTraceSink
    NullTraceSink
    payload preview helpers

evals/
    cases.json
    runner.py
    scripted model fixtures or adapters
    README.md

tests/
    test_agent_runner.py
    test_reliability.py
    test_tracing.py
    test_eval_runner.py
```

It is acceptable to place the enums and outcome dataclass in `agent.py` if the
module remains readable.

Do not move tool implementations into the reliability layer.

---

## Detailed agent-turn contract

### Input

The turn runner receives:

```python
messages: list[dict[str, Any]]
```

plus injected dependencies:

```text
model responder
tool executor
tool declarations
renderer
trace sink
clock
configuration
ID factory
```

Clock and ID factory injection are recommended for deterministic tests.

### Output

The runner returns:

```python
AgentTurnOutcome
```

It must not return a bare string.

For compatibility, a temporary adapter may expose:

```python
def run_turn_text(...) -> str:
    outcome = run_turn(...)
    ...
```

but `app.py` should consume the explicit outcome directly by the end of this
step.

### Successful completion

A final response without tool calls and with non-empty text produces:

```python
AgentTurnOutcome(
    status=TurnStatus.COMPLETED,
    reason=TerminationReason.FINAL_ANSWER,
    final_text=text,
    ...
)
```

### Controlled stop

Policy boundaries produce:

```text
status = stopped
```

Examples:

```text
tool_call_limit
repeated_tool_call
parallel_tool_calls
empty_model_response
```

`empty_model_response` may alternatively use `failed`, but one choice must be
documented and tested. For this specification, use:

```text
empty_model_response → failed
parallel_tool_calls  → stopped
tool_call_limit      → stopped
repeated_tool_call   → stopped
```

### Timeout

Timeouts produce:

```text
status = timed_out
```

Reasons:

```text
model_timeout
tool_timeout
turn_timeout
```

### Cancellation

`KeyboardInterrupt` produces:

```text
status = cancelled
reason = user_interrupt
```

The interrupt may be re-raised after the terminal event if the CLI needs its
existing control flow, but the trace must already be complete.

### Infrastructure failure

Unexpected model transport failure:

```text
status = failed
reason = model_error
```

Tool dispatch failure outside an ordinary structured tool result:

```text
status = failed
reason = tool_execution_error
```

Unexpected runtime defect converted at CLI boundary:

```text
status = failed
reason = internal_error
```

---

## Trace event contract

### `run_started`

Emitted once when the application initializes tracing.

Required fields:

```json
{
  "event": "run_started",
  "run_id": "...",
  "model_name": "qwen3:8b",
  "app_version": null
}
```

Git commit SHA may be included when easily available, but this spec does not
require executing Git on every startup.

### `turn_started`

Required fields:

```json
{
  "event": "turn_started",
  "run_id": "...",
  "turn_id": "...",
  "message_count": 8,
  "available_tools": [
    "python_calculate",
    "sql_query",
    "mcp_time__get_current_time"
  ],
  "limits": {
    "max_tool_calls": 4,
    "max_identical_tool_calls": 2,
    "model_timeout_seconds": 120,
    "tool_timeout_seconds": 30,
    "turn_timeout_seconds": 180
  }
}
```

Do not record the full persistent conversation.

### `model_request_started`

Required fields:

```json
{
  "event": "model_request_started",
  "turn_id": "...",
  "step": 1,
  "model_request_index": 1,
  "working_message_count": 8,
  "remaining_turn_ms": 179992
}
```

### `model_response_finished`

Required fields:

```json
{
  "event": "model_response_finished",
  "turn_id": "...",
  "step": 1,
  "model_request_index": 1,
  "decision": "final_answer|tool_call|invalid",
  "tool_call_count": 0,
  "text_chars": 148,
  "duration_ms": 921
}
```

Optional fields may include token counts if Ollama provides them reliably.

Token counts are not required for SPEC-011.

### `tool_call_requested`

Required fields:

```json
{
  "event": "tool_call_requested",
  "turn_id": "...",
  "step": 1,
  "tool_call_index": 1,
  "tool_name": "sql_query",
  "arguments_preview": "{\"query\":\"SELECT ...\"}",
  "arguments_sha256": "...",
  "arguments_truncated": false,
  "consecutive_identical_count": 1
}
```

This event is emitted before policy checks that may reject execution.

### `tool_execution_started`

Required fields:

```json
{
  "event": "tool_execution_started",
  "turn_id": "...",
  "tool_call_index": 1,
  "tool_name": "sql_query",
  "effective_timeout_ms": 30000
}
```

### `tool_execution_finished`

Required fields:

```json
{
  "event": "tool_execution_finished",
  "turn_id": "...",
  "tool_call_index": 1,
  "tool_name": "sql_query",
  "result_ok": true,
  "error_type": null,
  "duration_ms": 7
}
```

For a row-returning SQL result, allowed metadata includes:

```text
row_count
column_count
truncated
```

Do not include rows.

### `policy_violation`

Required fields:

```json
{
  "event": "policy_violation",
  "turn_id": "...",
  "policy": "tool_call_limit|repeated_tool_call|parallel_tool_calls",
  "message": "..."
}
```

### `turn_finished`

Required fields:

```json
{
  "event": "turn_finished",
  "turn_id": "...",
  "status": "completed",
  "reason": "final_answer",
  "tool_calls_executed": 2,
  "model_requests": 3,
  "final_text_chars": 184,
  "duration_ms": 1432
}
```

### `run_finished`

Emitted during deterministic application shutdown.

Required fields:

```json
{
  "event": "run_finished",
  "run_id": "...",
  "duration_ms": 381229
}
```

Absence of `run_finished` after a hard process kill is acceptable.

---

## Timeout design

### Model transport

The model layer should expose a timeout-aware boundary.

Preferred direction:

```python
respond(
    messages,
    tools,
    timeout_seconds=effective_timeout,
)
```

If the Ollama SDK supports client/request timeout configuration, use that
official boundary.

Do not scatter timeout constants through `agent.py` and `llm.py`.

### Streaming

The complete streaming decision must remain atomic from the turn perspective:

```text
start response
stream chunks
finish response
inspect tool_calls
```

If the stream fails or times out:

- renderer may already have printed partial text;
- outcome is not completed;
- partial text is not saved;
- trace identifies the failure;
- CLI prints a newline before the error message when necessary.

### Local tools

`python_calculate` is pure and already resource-bounded internally.

`sql_query` already has SQLite work limits.

These internal safeguards remain and complement the outer wall-clock timeout.

The timeout layer must not remove existing AST limits, SQL authorizer rules,
row limits, or progress-handler limits.

### MCP

The MCP manager should accept or enforce a per-call timeout.

On timeout:

- pending request is cancelled when supported;
- unhealthy session state is not silently reused;
- if the MCP SDK/session becomes unusable, the manager may mark that server
  unavailable for subsequent turns;
- child process cleanup remains deterministic at application shutdown.

A complete MCP reconnect policy is outside this spec.

### Whole-turn timeout precedence

When a component timeout and whole-turn deadline expire at effectively the same
time, the terminal reason should be deterministic.

Use:

```text
turn_timeout
```

when no positive turn time remains before the operation starts.

Use:

```text
model_timeout
tool_timeout
```

when the component-specific effective timeout expires while the turn deadline
still had time at operation start.

Tests must freeze/control time to verify this behavior.

---

## Repeated-call detection design

State held per active turn:

```python
last_fingerprint: str | None
consecutive_identical_count: int
```

Algorithm:

```python
fingerprint = tool_call_fingerprint(call)

if fingerprint == last_fingerprint:
    next_count = consecutive_identical_count + 1
else:
    next_count = 1

emit tool_call_requested(..., consecutive_identical_count=next_count)

if next_count > MAX_IDENTICAL_TOOL_CALLS:
    stop before executor.execute(...)

last_fingerprint = fingerprint
consecutive_identical_count = next_count
```

The state resets at the start of every user turn.

A structured tool failure does not reset repetition if the model requests the
same call again.

A different tool name always produces a different fingerprint.

Malformed non-JSON arguments must be handled deterministically:

- if `ModelToolCall.arguments` is already a dictionary, canonicalize it;
- if transport can supply a raw string, parse it at the transport boundary;
- malformed arguments must not reach the fingerprint helper as ambiguous data.

---

## Committed tests

### 1. Test framework

Add:

```text
pytest
```

as a development/test dependency.

Acceptable dependency approaches:

```text
requirements-dev.txt
```

or a documented optional test section in `pyproject.toml`.

Do not require pytest for ordinary runtime execution if avoidable.

### 2. Test directory

Create and commit:

```text
tests/
```

The committed suite is a required deliverable of this specification.

Journal-only or manual verification is no longer sufficient.

### 3. No live model in unit tests

Unit tests must use a scripted model responder.

Example shape:

```python
class ScriptedResponder:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, messages, tools):
        self.calls.append(...)
        return next(self.responses)
```

Tests must not require:

- Ollama running;
- a downloaded model;
- internet;
- MCP package network access;
- a real wall-clock delay;
- the generated Chinook database unless a test is explicitly marked integration.

### 4. No real sleeping in timeout tests

Inject a fake clock, fake timeout runner, or deterministic blocking fixture.

A unit test must not wait 30, 120, or 180 seconds.

The complete committed test suite should normally finish in a few seconds.

### 5. Required agent tests

At minimum:

#### Final answer without tools

Assert:

- completed outcome;
- `final_answer` reason;
- zero tool calls;
- one model request;
- terminal trace emitted once.

#### One successful tool call

Assert:

- tool executes once;
- result is appended to next model transcript;
- final outcome completes;
- counters are correct.

#### Several different tool calls

Assert:

- sequential order;
- each observation appears in subsequent transcript;
- final completion after multiple steps.

#### Structured tool error recovery

Assert:

- `{"ok": false}` is appended as observation;
- loop continues;
- later correction may complete.

#### Tool-call limit

Assert:

- call over limit is not executed;
- outcome is stopped;
- reason is `tool_call_limit`.

#### Parallel tool calls

Assert:

- none execute;
- reason is `parallel_tool_calls`.

#### Empty response

Assert:

- failed outcome;
- reason is `empty_model_response`.

#### Repeated identical call

Assert:

- first two calls execute with default threshold;
- third identical request does not execute;
- reason is `repeated_tool_call`.

#### Repetition reset

Sequence:

```text
A, A, B, A, final
```

Assert no repeated-call stop.

#### Canonical argument order

Assert:

```json
{"a":1,"b":2}
{"b":2,"a":1}
```

produce the same fingerprint.

#### Different argument values

Assert they do not produce the same fingerprint.

#### Model timeout

Assert:

- timed-out outcome;
- reason `model_timeout`;
- no final text;
- terminal event exactly once.

#### Tool timeout

Assert:

- timed-out outcome;
- reason `tool_timeout`;
- no subsequent model request.

#### Whole-turn timeout before next step

Assert operation does not start after deadline.

#### User interrupt

Assert cancelled outcome or documented re-raise behavior after trace finalization.

### 6. Required tracing tests

At minimum:

- JSONL writes one valid JSON object per line;
- every event contains schema version and run ID;
- turn events contain turn ID;
- UTC timestamps are timezone-aware;
- payload preview truncates deterministically;
- hashes are stable;
- SQL rows are not copied into trace;
- exactly one `turn_finished` is emitted;
- trace sink failure does not change successful agent outcome;
- trace sink failure warning is emitted at most once per turn;
- concurrent or rapid writes do not merge two JSON objects onto one line.

The application is currently single-user and single-turn, so process-wide
high-concurrency guarantees are not required.

### 7. Required configuration tests

Assert invalid values fail clearly:

```text
timeout <= 0
max tool calls < 1
max identical calls < 1
```

### 8. Integration tests

Optional committed integration tests may verify:

- real `python_calculate`;
- real generated Chinook SQLite database;
- local MCP time server;
- live Ollama.

They must be marked and skipped by default when prerequisites are absent.

Suggested markers:

```text
integration
live_model
mcp
```

The default `pytest` command must not require Ollama.

---

## Evaluation suite

### 1. Purpose

Unit tests verify deterministic runtime contracts.

Evaluations verify that the assembled agent can solve representative tasks and
follow intended behavior when driven by a real model.

These are different:

```text
unit test:
    did the harness enforce the policy?

evaluation:
    did the model + harness complete the task acceptably?
```

### 2. Committed cases

Create:

```text
evals/cases.json
```

or:

```text
evals/cases.jsonl
```

Each case has a stable ID.

Example:

```json
{
  "id": "calc-basic-001",
  "prompt": "What is 173 multiplied by 284?",
  "expectation": {
    "allowed_tools": ["python_calculate"],
    "required_tools": ["python_calculate"],
    "max_tool_calls": 1,
    "status": "completed",
    "answer_contains": ["49132"]
  }
}
```

### 3. Required initial cases

Commit at least these categories:

#### No-tool answer

A conceptual question that should complete without a tool.

#### Calculator

A deterministic arithmetic task.

#### SQL single query

A deterministic Chinook question with a known answer.

#### SQL recovery

A scripted evaluation or deterministic fixture that forces one structured SQL
error and then allows correction.

For a live-model eval, do not require the model to make an error on purpose.

#### Multi-tool task

A task requiring at least two tool calls, preferably SQL plus calculator.

#### MCP time

A task requiring `mcp_time__get_current_time`, with assertions focused on tool
use and successful completion rather than a hard-coded timestamp.

#### Repetition guard

A scripted-model case that requests the same call beyond the threshold.

#### Tool-call budget guard

A scripted-model case that exceeds the global budget.

#### Timeout

A deterministic scripted case.

### 4. Deterministic and live suites are separate

Support two modes:

```text
scripted
live
```

Scripted evaluations:

- run without Ollama;
- use predetermined model decisions;
- validate trace and outcome contracts;
- are suitable for CI.

Live evaluations:

- use the configured Ollama model;
- may vary;
- are run manually in this iteration;
- write results for comparison;
- must never be part of the default unit test gate.

### 5. Evaluation result format

Write machine-readable results under:

```text
data/evals/
```

Example:

```json
{
  "schema_version": 1,
  "suite": "scripted",
  "started_at": "...",
  "model": "scripted",
  "summary": {
    "total": 8,
    "passed": 8,
    "failed": 0
  },
  "cases": [
    {
      "id": "calc-basic-001",
      "passed": true,
      "status": "completed",
      "reason": "final_answer",
      "tool_calls": ["python_calculate"],
      "duration_ms": 3,
      "failures": []
    }
  ]
}
```

### 6. Evaluation assertions

Initial evaluator supports objective assertions only:

```text
expected status
expected termination reason
required tool names
allowed tool names
minimum tool-call count
maximum tool-call count
answer contains substring
answer matches regular expression
tool result success/failure pattern
maximum duration
```

No LLM judge is introduced.

Case-insensitive substring matching is acceptable when documented.

### 7. Evaluation command

Provide a documented command, for example:

```bash
python -m evals.runner --suite scripted
```

and optionally:

```bash
python -m evals.runner --suite live
```

The exact CLI may vary.

The command must exit non-zero when any required case fails.

### 8. Evaluation history

Result filenames should include timestamp and suite:

```text
data/evals/20260724T081522Z-scripted.json
```

Generated results are git-ignored.

The committed case definitions are version-controlled.

---

## CLI integration

`app.py` remains responsible for:

- reading user input;
- handling `/reset` and `/bye`;
- adding the user message;
- calling the turn runner;
- persisting only successful semantic exchanges;
- rolling back unsuccessful turns;
- printing concise diagnostics;
- owning MCP startup/shutdown.

Recommended flow:

```python
conversation.add_user(user_text)

outcome = runner.run_turn(conversation.messages_for_model)

if outcome.status is COMPLETED:
    conversation.add_assistant(outcome.final_text)
    store.save(conversation)
else:
    conversation.rollback_last_user_message()
    renderer.turn_error(outcome)
```

The exact existing rollback API should be reused.

### Diagnostic output

Every unsuccessful turn shown to the user includes:

```text
Run ID: <run_id or turn_id>
```

Prefer the turn identifier if it is the direct correlation key in the trace.

Do not print the full trace path on every successful turn.

A startup message may state:

```text
Tracing: data/traces/agent.jsonl
```

but this is optional.

### Verbose diagnostics

An optional configuration flag may enable additional CLI timing output:

```python
VERBOSE_AGENT_DIAGNOSTICS = False
```

This flag is not required.

The structured trace is always authoritative.

---

## Configuration

Add one authoritative configuration section:

```python
# Agent reliability (SPEC-011)
MODEL_REQUEST_TIMEOUT_SECONDS = 120
TOOL_EXECUTION_TIMEOUT_SECONDS = 30
AGENT_TURN_TIMEOUT_SECONDS = 180
MAX_IDENTICAL_TOOL_CALLS = 2

# Local structured tracing
TRACE_ENABLED = True
TRACE_PATH = "data/traces/agent.jsonl"
TRACE_PAYLOAD_PREVIEW_CHARS = 1000
```

Existing:

```python
MAX_TOOL_CALLS_PER_TURN = 4
```

remains unchanged.

Configuration values should be passed into components explicitly rather than
imported from `config.py` throughout the codebase.

---

## Error taxonomy

The user-facing message and machine-readable reason are separate.

Example mapping:

| Reason | Status | User-facing message |
|---|---|---|
| `final_answer` | `completed` | none |
| `empty_model_response` | `failed` | Model returned an empty response. |
| `parallel_tool_calls` | `stopped` | Parallel tool calls are not supported. |
| `tool_call_limit` | `stopped` | Agent stopped after 4 tool calls without a final answer. |
| `repeated_tool_call` | `stopped` | Agent stopped after repeating the same tool call 2 times. |
| `model_timeout` | `timed_out` | Agent turn timed out while waiting for the model. |
| `tool_timeout` | `timed_out` | Tool '<name>' timed out. |
| `turn_timeout` | `timed_out` | Agent turn exceeded its total time limit. |
| `model_error` | `failed` | Model request failed. |
| `tool_execution_error` | `failed` | Tool execution failed. |
| `user_interrupt` | `cancelled` | Generation interrupted. |
| `internal_error` | `failed` | Unexpected application error. |

Internal exception details belong in the trace.

The CLI message should not expose:

- credentials;
- filesystem secrets;
- raw stack traces;
- full MCP protocol frames;
- complete database rows.

---

## Security and privacy

### Local-only default

Trace and evaluation outputs remain local.

No telemetry is sent over the network by the observability layer.

The existing Ollama and MCP behavior is unchanged.

### File permissions

Use ordinary user-owned files.

Creating OS-specific secure permissions is optional for this local laboratory,
but directories must be created safely with:

```python
mkdir(parents=True, exist_ok=True)
```

### Trace injection

All trace content is JSON-encoded.

Never build JSONL lines with string concatenation around unescaped model or user
content.

### Log growth

This iteration does not require rotation.

Document that `data/traces/agent.jsonl` grows append-only.

A future specification may add:

- size rotation;
- retention;
- compression;
- per-run files.

### Git hygiene

Add generated outputs to `.gitignore`:

```text
data/traces/
data/evals/
```

Do not ignore committed evaluation case definitions under `evals/`.

---

## Implementation plan

### Phase 1: Reliability types

1. Add `TurnStatus`.
2. Add `TerminationReason`.
3. Add `AgentTurnOutcome`.
4. Add configuration validation.
5. Add canonical tool-call fingerprint helper.

### Phase 2: Trace infrastructure

1. Add versioned trace event builder.
2. Add `TraceSink` protocol.
3. Add `JsonlTraceSink`.
4. Add `NullTraceSink`.
5. Add payload preview and hashing.
6. Add run and turn IDs.
7. Add terminal-event guarantee.

### Phase 3: Agent integration

1. Change `AgentRunner.run_turn` to return `AgentTurnOutcome`.
2. Emit model and tool lifecycle events.
3. Add counters and durations.
4. Add repeated-call detection.
5. Preserve structured tool-error recovery.
6. Preserve temporary per-turn transcript.
7. Preserve final text streaming.

### Phase 4: Timeouts

1. Add whole-turn deadline.
2. Add model request timeout boundary.
3. Add tool execution timeout boundary.
4. Document cancellation semantics per component.
5. Add deterministic timeout tests.

### Phase 5: Application integration

1. Update `app.py` to consume outcomes.
2. Roll back every unsuccessful turn.
3. Persist only completed final answers.
4. Print correlation ID for failures.
5. Emit run lifecycle events.
6. Preserve MCP shutdown.

### Phase 6: Committed tests

1. Add pytest setup.
2. Add scripted responder.
3. Add fake executor and renderer.
4. Add memory trace sink.
5. Cover every terminal state.
6. Keep default suite independent of Ollama.

### Phase 7: Evaluations

1. Add committed cases.
2. Add scripted evaluator.
3. Add optional live evaluator.
4. Add JSON result writer.
5. Add non-zero failure exit.
6. Document commands.

### Phase 8: Documentation

1. Update README.
2. Update `.gitignore`.
3. Add SPEC journal entry.
4. Record implementation decisions and deviations.
5. Record exact test and eval commands.
6. Record live verification separately from deterministic tests.

---

## Required file changes

Expected additions:

```text
reliability.py
tracing.py
tests/
evals/
requirements-dev.txt
specs/SPEC-011-Agent-Reliability-Observability.md
docs/journal/SPEC-011-agent-reliability-observability.md
```

Expected modifications:

```text
agent.py
app.py
llm.py
config.py
tools/executor.py            # only if timeout seam belongs here
mcp_client.py or equivalent  # timeout propagation if needed
README.md
.gitignore
```

The exact MCP filename must follow the existing repository structure.

Avoid unrelated refactoring.

---

## Acceptance criteria

### Runtime outcomes

- [ ] `AgentRunner` returns an explicit `AgentTurnOutcome`.
- [ ] Every outcome has stable `status` and `reason` values.
- [ ] Successful completion uses `completed/final_answer`.
- [ ] Unsuccessful turns have no persisted final assistant message.
- [ ] Partial streamed text is never persisted after failure.
- [ ] CLI failures show a correlation identifier.

### Tracing

- [ ] Tracing uses append-only JSONL.
- [ ] Every event includes `schema_version`, UTC timestamp, event name, and run ID.
- [ ] Turn events include turn ID.
- [ ] Model request start and finish are traced.
- [ ] Tool request, execution start, and execution finish are traced.
- [ ] Every started turn emits exactly one terminal event.
- [ ] Terminal event includes status, reason, counters, and duration.
- [ ] Large payloads are truncated.
- [ ] Database rows are not copied into traces.
- [ ] Trace writing failure does not replace the real agent outcome.
- [ ] Generated traces are git-ignored.

### Timeouts

- [ ] Model requests have a host-owned timeout.
- [ ] Tool executions have a host-owned timeout.
- [ ] Whole turns have a host-owned deadline.
- [ ] Effective component timeout respects remaining turn time.
- [ ] Timeout values are validated.
- [ ] Timeout tests do not use long real sleeps.
- [ ] Cancellation limitations are documented honestly.
- [ ] A timed-out turn is rolled back.
- [ ] No automatic retry follows a timeout.

### Repeated-call detection

- [ ] Tool calls are fingerprinted using canonical JSON.
- [ ] Argument key order does not change the fingerprint.
- [ ] Tool name is part of the fingerprint.
- [ ] Consecutive identical calls are counted.
- [ ] A different call resets the counter.
- [ ] The call that exceeds the threshold is not executed.
- [ ] Repeated-call stop has reason `repeated_tool_call`.
- [ ] Repetition state resets for every new user turn.

### Tests

- [ ] A committed `tests/` directory exists.
- [ ] Default tests do not require Ollama.
- [ ] Default tests do not require internet.
- [ ] Default tests do not require a live MCP server.
- [ ] Success without tools is covered.
- [ ] Single-tool success is covered.
- [ ] Multi-tool success is covered.
- [ ] Structured tool-error recovery is covered.
- [ ] Tool-call limit is covered.
- [ ] Parallel call rejection is covered.
- [ ] Empty response is covered.
- [ ] Repeated-call stop and reset are covered.
- [ ] Model timeout is covered.
- [ ] Tool timeout is covered.
- [ ] Whole-turn timeout is covered.
- [ ] Trace schema and terminal-event guarantees are covered.
- [ ] Trace sink failure behavior is covered.
- [ ] `pytest` exits successfully in a clean local environment after installing
      documented test dependencies.

### Evaluations

- [ ] Committed eval case definitions exist.
- [ ] Each case has a stable ID.
- [ ] Scripted evaluations run without Ollama.
- [ ] Scripted evaluations validate objective assertions.
- [ ] Evaluation runner exits non-zero on failure.
- [ ] Evaluation results are written as versioned JSON.
- [ ] Generated evaluation results are git-ignored.
- [ ] Optional live suite is clearly separated and documented.
- [ ] At least one no-tool, calculator, SQL, multi-tool, MCP, repetition, budget,
      and timeout case exists across scripted/live modes.

### Compatibility

- [ ] Existing tools still use the shared `ToolRegistry` and `ToolExecutor`.
- [ ] Structured `{"ok": false}` tool results remain recoverable observations.
- [ ] Persistent chat format remains unchanged.
- [ ] Temporary tool protocol messages remain unpersisted.
- [ ] Final text still streams.
- [ ] MCP startup and shutdown remain deterministic.
- [ ] `/reset`, `/bye`, EOF, and `Ctrl+C` behavior remain usable.
- [ ] Existing `MAX_TOOL_CALLS_PER_TURN = 4` behavior remains enforced.

### Documentation

- [ ] README explains traces, limits, tests, and eval commands.
- [ ] README distinguishes tests from evaluations.
- [ ] README documents trace location and local-only behavior.
- [ ] Journal records implementation decisions, tests, live checks, and
      deviations.
- [ ] Journal records the final merge commit after merge.

---

## Required deterministic scenarios

### Scenario A: direct completion

Script:

```text
model → final text
```

Expected:

```text
status = completed
reason = final_answer
model_requests = 1
tool_calls_executed = 0
```

### Scenario B: tool then completion

Script:

```text
model → python_calculate
tool  → ok
model → final text
```

Expected:

```text
completed/final_answer
model_requests = 2
tool_calls_executed = 1
```

### Scenario C: structured failure then recovery

Script:

```text
model → sql_query A
tool  → {ok:false}
model → sql_query B
tool  → {ok:true}
model → final text
```

Expected:

```text
completed/final_answer
tool_calls_executed = 2
```

### Scenario D: repeated call

Script:

```text
model → A
tool  → result
model → A
tool  → result
model → A
```

Expected with default threshold:

```text
stopped/repeated_tool_call
tool_calls_executed = 2
third A not executed
```

### Scenario E: global budget

Script:

```text
A → B → C → D → E
```

Expected:

```text
A through D execute
E does not execute
stopped/tool_call_limit
```

### Scenario F: model timeout

Script:

```text
model request exceeds effective timeout
```

Expected:

```text
timed_out/model_timeout
tool_calls_executed = 0
turn_finished exactly once
```

### Scenario G: tool timeout

Script:

```text
model → tool A
tool A exceeds effective timeout
```

Expected:

```text
timed_out/tool_timeout
no next model request
```

### Scenario H: turn deadline

Script:

```text
first operation consumes remaining turn budget
next operation requested
```

Expected:

```text
next operation not started
timed_out/turn_timeout
```

### Scenario I: trace failure

Script:

```text
trace sink raises on one event
agent completes normally
```

Expected:

```text
completed/final_answer
warning at most once
no recursive trace failure
```

---

## Manual verification

After deterministic tests pass:

### 1. Normal answer

```text
What is an agent loop?
```

Confirm:

- no tool call;
- final answer streams;
- completed trace exists.

### 2. Calculator

```text
What is 173 multiplied by 284?
```

Confirm:

- one calculator call;
- trace shows model/tool durations;
- final answer is persisted.

### 3. SQL multi-step

```text
Which genre generated the most revenue, and what percentage of total revenue
did it represent?
```

Confirm:

- one or more SQL/calculator calls;
- final answer completes within limits;
- SQL rows are absent from trace.

### 4. MCP

```text
What time is it now in Europe/Amsterdam?
```

Confirm:

- MCP tool call;
- timeout configuration reaches MCP boundary;
- shutdown remains clean.

### 5. Inspect trace

```bash
tail -n 20 data/traces/agent.jsonl
```

Confirm:

- every line is valid JSON;
- one turn ID correlates all events;
- one terminal event exists;
- no hidden reasoning or large result rows appear.

### 6. Run tests

```bash
pytest
```

Confirm no live model is required.

### 7. Run scripted evaluations

```bash
python -m evals.runner --suite scripted
```

Confirm:

- summary is printed;
- JSON result is written;
- exit code is zero.

### 8. Optional live evaluations

```bash
python -m evals.runner --suite live
```

Record the model name and results in the journal.

Live evaluation variability is not a reason to weaken deterministic runtime
tests.

---

## Design notes for future steps

SPEC-011 intentionally creates stable seams for later evolution.

### OpenTelemetry

`TraceSink` may later gain:

```text
OpenTelemetryTraceSink
```

without changing agent-loop domain events.

### Metrics

Terminal outcomes can later produce counters:

```text
agent_turns_total
agent_turn_duration_seconds
agent_tool_calls_total
agent_timeouts_total
```

Metrics are not implemented here.

### Retries

Future retry policy must distinguish:

- safe idempotent reads;
- pure calculations;
- MCP calls with unknown semantics;
- write-capable tools.

SPEC-011 performs no automatic retries.

### Side effects

When write tools are introduced, timeout and cancellation semantics require:

- idempotency keys;
- transaction identifiers;
- reconciliation;
- explicit unknown-outcome states.

A future status may need:

```text
indeterminate
```

Current tools are read-only or pure, so that state is not introduced yet.

### Trace rotation

A later spec may add:

- per-run trace files;
- retention;
- rotation by size;
- compression;
- indexing.

### Evaluation quality

A later eval layer may add:

- semantic grading;
- answer datasets;
- model comparisons;
- prompt versioning;
- regression thresholds;
- CI trend reports.

SPEC-011 begins with objective, deterministic assertions.

---

## Completion definition

SPEC-011 is complete when the project can answer all of these questions from
committed code and one local trace:

```text
Did the turn complete?
Why did it stop?
How many model requests happened?
Which tools were requested?
Which tools actually executed?
How long did each blocking operation take?
Did a timeout occur?
Did the model repeat the same action?
Was the final answer persisted?
Can the behavior be reproduced by a deterministic test or eval?
```

At that point, `lLLM` is no longer only an agent that can act.

It is an agent runtime whose behavior can be tested, bounded, inspected, and
diagnosed.
