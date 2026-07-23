# SPEC-010: Bounded Agent Loop

## Background

SPEC-006 introduced the shared tool contract and registry.

SPEC-007 completed the first executable tool path with the local
`python_calculate` handler.

SPEC-008 connected the same execution path to the local read-only Chinook
SQLite database through `sql_query`.

SPEC-009 added an MCP client boundary and registered the MCP-backed
`mcp_time__get_current_time` tool in the same `ToolRegistry` and
`ToolExecutor` as the local tools.

The current harness can therefore expose tools from different sources through
one model-facing interface:

```text
ToolRegistry
    ├── local: python_calculate
    ├── local: sql_query
    └── mcp:   mcp_time__get_current_time

ToolExecutor
    ├── local Python handler
    ├── local SQLite handler
    └── MCP routing handler
```

However, one user turn is still implemented as a fixed two-stage flow:

```text
model response
    ├── final text → return
    └── one tool call
            │
            ▼
       execute tool
            │
            ▼
       second model response
            ├── final text → return
            └── another tool call → error
```

The current `run_turn(...)` explicitly supports at most one tool execution.
This was an intentional safety boundary while the registry, executor, SQL tool,
and MCP integration were built independently.

That boundary now prevents the harness from behaving as an agent.

A real task may require several reasoning and action steps:

```text
User asks a question
    │
    ▼
Model chooses sql_query
    │
    ▼
SQL tool returns an error
    │
    ▼
Model corrects the query
    │
    ▼
SQL tool returns rows
    │
    ▼
Model uses python_calculate
    │
    ▼
Model returns the final answer
```

The next architectural step is therefore not another tool. It is a reusable,
bounded agent loop that repeatedly gives control back to the model after every
tool result until the model produces a final textual answer.

---

## Goal

Replace the current one-tool-per-turn implementation with a bounded agent loop:

```text
User request
    │
    ▼
LLM decides what to do
    │
    ├── final answer ───────────────────────────────┐
    │                                               │
    └── tool call                                   │
            │                                       │
            ▼                                       │
       ToolExecutor                                 │
            │                                       │
            ▼                                       │
       structured tool result                       │
            │                                       │
            └──────────── back to LLM ──────────────┘
```

The loop must:

1. send the current conversation and available tool declarations to the model;
2. inspect each complete model response;
3. return when the model produces a final text answer without tool calls;
4. execute a requested tool through the existing `ToolExecutor`;
5. append the assistant tool-call message and the tool-result message to the
   temporary turn transcript;
6. call the model again with the updated transcript;
7. allow the model to select the same or a different tool on the next iteration;
8. allow the model to recover from ordinary structured tool errors;
9. enforce a deterministic maximum number of tool executions per user turn;
10. fail clearly when the maximum is reached without a final answer;
11. preserve the current persistent conversation format;
12. preserve streaming for the final user-visible answer;
13. preserve deterministic MCP startup and shutdown;
14. keep tool execution, model transport, conversation storage, and CLI rendering
    as separate responsibilities.

Target interaction:

```text
You: Which genre generated the most revenue, and what percentage of total
revenue did it represent?

[tool 1/4] sql_query
[args] {"query": "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) AS Revenue FROM InvoiceLine il JOIN Track t ON il.TrackId = t.TrackId JOIN Genre g ON t.GenreId = g.GenreId GROUP BY g.Name ORDER BY Revenue DESC LIMIT 1"}
[result] {"ok": true, "columns": ["Name", "Revenue"], "rows": [["Rock", 826.65]], "row_count": 1, "truncated": false}

[tool 2/4] sql_query
[args] {"query": "SELECT SUM(UnitPrice * Quantity) AS TotalRevenue FROM InvoiceLine"}
[result] {"ok": true, "columns": ["TotalRevenue"], "rows": [[2328.6]], "row_count": 1, "truncated": false}

[tool 3/4] python_calculate
[args] {"expression": "826.65 / 2328.6 * 100"}
[result] {"ok": true, "result": 35.49815339689083}

Qwen: Rock generated the most revenue at $826.65, representing approximately
35.5% of total revenue.
```

The exact wording of the final answer is model-dependent. The loop, limits,
message sequence, execution path, and persistence behavior must be deterministic.

---

## User-visible behavior

### 1. Normal answer without tools

```text
You: What is an agent loop?

Qwen: An agent loop is...
```

No tool diagnostics are printed.

### 2. One tool call

Existing single-tool behavior remains valid:

```text
You: What is 173 multiplied by 284?

[tool 1/4] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}

Qwen: The result is 49,132.
```

### 3. Several sequential tool calls

The model may call more than one tool before answering:

```text
You: What time is it in Amsterdam, and how many minutes is that ahead of UTC?

[tool 1/4] mcp_time__get_current_time
[args] {"timezone": "Europe/Amsterdam"}
[result] {...}

[tool 2/4] mcp_time__get_current_time
[args] {"timezone": "UTC"}
[result] {...}

[tool 3/4] python_calculate
[args] {"expression": "2 * 60"}
[result] {"ok": true, "result": 120}

Qwen: Amsterdam is currently 120 minutes ahead of UTC.
```

The example offset is illustrative. The actual result depends on the timestamps
returned at execution time.

### 4. Tool-error recovery

A structured tool failure is returned to the model and does not automatically
abort the turn:

```text
You: Which employee has the largest sales total?

[tool 1/4] sql_query
[args] {"query": "SELECT EmployeeName, SUM(Total) ..."}
[result] {"ok": false, "error": {"type": "sql_execution_error", "message": "..."}}

[tool 2/4] sql_query
[args] {"query": "SELECT e.FirstName || ' ' || e.LastName AS EmployeeName, SUM(i.Total) AS SalesTotal FROM Employee e JOIN Customer c ON c.SupportRepId = e.EmployeeId JOIN Invoice i ON i.CustomerId = c.CustomerId GROUP BY e.EmployeeId ORDER BY SalesTotal DESC LIMIT 1"}
[result] {"ok": true, ...}

Qwen: Jane Peacock has the largest sales total...
```

The model sees the error envelope and may correct its action.

The harness must not attempt to understand or repair the SQL itself.

### 5. Maximum-step termination

If the model keeps requesting tools after the configured limit:

```text
Application error: Agent stopped after 4 tool calls without a final answer.
```

The incomplete user turn is rolled back using the existing application behavior.

No partial assistant answer is persisted.

The application remains usable for the next user request.

### 6. Unknown or invalid tool call

The existing `ToolExecutor` remains authoritative. An unknown tool or malformed
call becomes a controlled turn failure unless the current executor already
returns a safe structured error.

No raw traceback is shown during normal CLI use.

### 7. Multiple tool calls in one model response

This iteration supports sequential tool use, not parallel tool execution.

If one model response contains several tool calls, the harness must reject the
response clearly:

```text
Application error: Parallel tool calls are not supported.
```

The harness must not silently execute only the first call.

Support for parallel calls requires a separate specification because it changes
ordering, failure, cancellation, and transcript semantics.

---

## Core architectural decisions

### 1. Introduce an explicit agent-loop component

The loop must not remain embedded as increasingly complex branching inside the
CLI entry point.

Introduce a dedicated component, for example:

```text
agent.py
```

with a responsibility similar to:

```text
AgentRunner
    ├── build turn transcript
    ├── request model response
    ├── detect final answer or tool call
    ├── execute selected tool
    ├── append tool interaction to transcript
    ├── enforce limits
    └── return final answer
```

Acceptable alternatives include a small `run_agent_turn(...)` function if it is
kept in a dedicated module and remains independently testable.

The agent component must not own:

- persistent chat storage;
- CLI input commands;
- MCP process startup or shutdown;
- tool registration;
- SQL implementation;
- calculator implementation;
- Ollama configuration.

### 2. The loop is bounded

The harness must never allow an unbounded model-controlled loop.

Add a configuration value:

```python
MAX_TOOL_CALLS_PER_TURN = 4
```

The exact default for this spec is `4`.

The counter measures executed tool calls, not raw model requests.

Examples:

```text
final answer without tools       → 0 tool calls
one tool then answer             → 1 tool call
four tools then answer           → 4 tool calls, valid
four tools then fifth request    → stop before executing the fifth
```

The fifth tool request must not be executed.

The limit is host-owned and must never be supplied or changed by the model.

Values below `1` must be rejected at startup or construction time.

### 3. Preserve one model response as one decision point

Each model response represents one agent decision:

```text
decision = final text
or
decision = exactly one tool call
```

This spec does not support:

- parallel tool calls;
- tool batches;
- speculative execution;
- background tool execution;
- nested agents;
- planner/executor sub-agents.

A response containing more than one tool call is invalid for SPEC-010.

### 4. Maintain a temporary per-turn transcript

The persistent `Conversation` currently stores semantic user/assistant messages.

Tool-call protocol messages are implementation details needed by the model during
one active turn. They must remain in a temporary transcript:

```text
persistent context
    + current user message
    + assistant tool call
    + tool result
    + assistant tool call
    + tool result
    + final assistant answer
```

Only the semantic final exchange is persisted:

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."}
]
```

Do not persist:

- `tool_calls`;
- `role: tool` messages;
- raw SQL result rows;
- MCP protocol messages;
- intermediate model text;
- internal step counters.

This preserves the current storage contract and prevents tool output from
permanently expanding `data/chat_history.json`.

### 5. The full active-turn transcript is sent on every iteration

At the start of a turn:

```python
working_messages = list(conversation.messages_for_model)
```

After each tool call:

```python
working_messages.extend(
    [
        assistant_tool_message(call),
        tool_result_message(call, result),
    ]
)
```

The next model request receives the complete working transcript.

Do not rebuild the transcript from persistent conversation after every tool,
because that would discard previous tool observations from the same turn.

Do not mutate `Conversation` with temporary protocol messages.

### 6. Tool results are observations, including errors

Every successfully dispatched tool invocation produces an observation message
for the model.

A result such as:

```json
{
  "ok": false,
  "error": {
    "type": "invalid_timezone",
    "message": "Unknown IANA timezone: Middle/Earth"
  }
}
```

is still a valid tool result and must be appended to the working transcript.

The agent loop continues after such a result, subject to the remaining tool-call
budget.

The distinction is:

```text
tool returned {ok: false, ...}
    → model may reason and recover

harness could not dispatch/parse/execute safely
    → controlled turn failure
```

The agent layer must not introduce tool-specific recovery logic.

### 7. Final answer ends the loop

A model response with no tool calls must contain non-empty text.

That text is the final assistant answer and terminates the loop.

An empty response without tool calls is a controlled error:

```text
Model returned an empty response.
```

A response that mixes non-empty user-facing text with a tool call is ambiguous.
For SPEC-010, intermediate text must not be treated as a final answer or
persisted.

The harness may ignore that intermediate text for user-facing output, but tests
must verify that it does not contaminate the final persisted answer.

The preferred prompt behavior is for the model to emit either a tool call or a
final answer, not both.

### 8. Stream only the final textual answer

Tool-selection responses may be streamed by Ollama internally, but their text
must not appear as a normal `Qwen:` answer before the action completes.

The CLI should render:

```text
[tool N/MAX] ...
[args] ...
[result] ...
```

for each action.

When the model finally responds without a tool call, stream that text using the
existing `stream_response(...)` behavior.

This avoids displaying incomplete statements such as:

```text
Qwen: I will check the database...
```

before the tool diagnostics.

If the current `ModelResponse` API cannot determine whether a response contains
a tool call until its stream has been consumed, the agent layer may collect
intermediate chunks without printing them. Final-answer chunks must still be
printed incrementally once the response is known to be textual.

Any necessary `ModelResponse` refactoring must remain transport-focused and must
not absorb agent-loop policy.

### 9. Keep ToolRegistry and ToolExecutor unchanged in authority

The agent receives the same tool declarations generated by:

```python
registry.to_ollama_tools()
```

The model selects a tool by name.

Execution continues through:

```python
executor.execute(call.name, call.arguments)
```

The agent must not:

- call `python_calculate` directly;
- call SQLite directly;
- call the MCP manager directly;
- inspect a tool's source and branch by local/MCP type;
- duplicate registry lookup logic.

The unified tool boundary created by SPEC-006 through SPEC-009 remains intact.

### 10. Keep MCP lifecycle outside the agent

`McpClientManager` continues to be started before the chat loop and closed in
the existing `finally` path.

The agent uses MCP-backed tools only through the registered executor handler.

The agent does not own or restart MCP sessions.

A mid-turn MCP tool error is handled according to the existing MCP result/error
contract.

Automatic reconnect is out of scope.

### 11. Separate policy errors from tool results

Introduce or retain controlled agent exceptions, for example:

```python
class AgentTurnError(Exception):
    """A controlled failure that aborts the current user turn."""
```

Controlled failures include:

- empty final response;
- more than one tool call in a single response;
- malformed model tool call;
- tool-call limit exceeded;
- executor dispatch failure;
- invalid agent configuration.

Tool domain errors returned as structured JSON are not `AgentTurnError`.

Error messages must be stable enough for tests and understandable in the CLI.

### 12. The CLI owns rollback and persistence

The top-level application continues this transaction-like behavior:

```text
append user message
    │
    ▼
run complete agent turn
    ├── success → append final assistant answer and save
    └── failure → remove user message and do not save partial turn
```

The agent component returns a final string only after the turn has completed.

The agent must not save conversation history itself.

### 13. No hidden chain-of-thought storage or rendering

The loop records only protocol-level actions and observations:

- selected tool;
- structured arguments;
- structured result;
- final answer.

It must not request, persist, or print private chain-of-thought.

Terms such as “reasoning again” in the conceptual diagram mean another model
decision after receiving an observation, not exposure of internal reasoning
tokens.

### 14. Preserve framework-free architecture

Do not introduce LangChain, LangGraph, AutoGen, CrewAI, or another agent
framework in this iteration.

The purpose of SPEC-010 is to understand and own the smallest correct agent
loop on top of the project's existing abstractions.

Framework evaluation may occur later, once the native control flow and contracts
are understood.

---

## Proposed design

### New module

Suggested file:

```text
agent.py
```

Suggested public interface:

```python
class AgentTurnError(Exception):
    """A controlled failure that aborts the current user turn."""


class AgentRunner:
    def __init__(
        self,
        executor: ToolExecutor,
        tools: list[dict],
        max_tool_calls: int,
    ) -> None:
        ...

    def run_turn(self, messages: list[dict]) -> str:
        ...
```

An alternative functional interface is acceptable:

```python
def run_agent_turn(
    messages: list[dict],
    executor: ToolExecutor,
    tools: list[dict],
    max_tool_calls: int,
) -> str:
    ...
```

The caller supplies a snapshot of model-facing messages. The agent must not
receive the mutable `Conversation` object unless there is a compelling reason.

This keeps the dependency direction simple:

```text
app.py
    │
    ├── Conversation
    ├── ToolRegistry
    ├── ToolExecutor
    ├── MCP lifecycle
    └── AgentRunner
            │
            ├── ModelResponse
            └── ToolExecutor
```

### Reference algorithm

```python
def run_turn(messages, executor, tools, max_tool_calls):
    working_messages = list(messages)
    tool_calls_used = 0

    while True:
        response = ModelResponse(working_messages, tools)
        text, tool_calls = consume_model_decision(response)

        if not tool_calls:
            if not text:
                raise AgentTurnError("Model returned an empty response.")
            stream_or_render_final(text)
            return text

        if len(tool_calls) != 1:
            raise AgentTurnError("Parallel tool calls are not supported.")

        if tool_calls_used >= max_tool_calls:
            raise AgentTurnError(
                f"Agent stopped after {max_tool_calls} tool calls "
                "without a final answer."
            )

        call = tool_calls[0]
        tool_calls_used += 1

        render_tool_call(call, tool_calls_used, max_tool_calls)
        result = executor.execute(call.name, call.arguments)
        render_tool_result(result)

        working_messages.extend(
            [
                assistant_tool_message(call),
                tool_result_message(call, result),
            ]
        )
```

This pseudocode is illustrative. The implementation must adapt it to the current
streaming API without weakening final-answer streaming.

### Suggested CLI rendering

Change:

```text
[tool] sql_query
```

to:

```text
[tool 1/4] sql_query
```

This makes the bounded nature of the loop visible and helps debug model
behavior.

The `[args]` and `[result]` lines remain unchanged.

### Suggested configuration

In `config.py`:

```python
MAX_TOOL_CALLS_PER_TURN = 4
```

`app.py` passes the value to the agent component.

The model must never receive this value as a writable parameter.

---

## Prompt changes

Update the system prompt so the model understands the new interaction policy.

The prompt should state, in substance:

```text
You may call one tool at a time.
After each tool result, decide whether to call another tool or answer the user.
Use tools only when needed.
You may retry with corrected arguments after a tool error.
Do not invent tool results.
When enough information is available, return the final answer.
```

The prompt must not claim that tool usage is unlimited.

The exact host-side maximum does not need to be exposed to the model, although a
general instruction to avoid unnecessary calls is recommended.

The prompt should continue to describe `sql_query` constraints and any other
tool-specific guidance already present.

Do not hard-code the list of MCP-discovered tools in the prompt. Tool
declarations remain authoritative.

---

## Model-response handling

### Required decision representation

The existing types may remain:

```python
ModelResponse
ModelToolCall
```

The implementation may add a transport-neutral representation such as:

```python
@dataclass(frozen=True)
class ModelDecision:
    text: str
    tool_calls: tuple[ModelToolCall, ...]
```

This is optional.

The important boundary is:

```text
LLM transport parsing
    → normalized decision
    → agent policy
```

The agent owns loop policy.

`llm.py` owns Ollama request/response parsing.

### Streaming constraint

The current API exposes `text_chunks()` and `tool_calls`.

If a response's tool-call status is available only after consuming the stream,
the implementation may introduce two modes:

```python
response.collect_decision()
response.stream_final_text()
```

or another small equivalent abstraction.

Tests must prove:

- final answer text is still emitted incrementally;
- tool-selection text is not printed as a final answer;
- tool calls remain available after the response is consumed;
- no model request is accidentally executed twice.

Do not buffer all final answers merely to simplify the loop unless the Ollama
client makes incremental final rendering impossible. Any such limitation must be
documented in the journal.

---

## State and persistence

### Persistent state

Unchanged:

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."}
]
```

### Ephemeral state

Exists only during `AgentRunner.run_turn(...)`:

```text
working_messages
tool_calls_used
current model response
current tool call
current tool result
```

### Failure semantics

If the turn fails after one or more tools:

- no final assistant message is returned;
- no tool protocol messages are persisted;
- the caller removes the just-added user message;
- the prior conversation remains intact;
- external side effects already performed by tools cannot be rolled back by the
  conversation layer.

All current tools are read-only or calculation/time tools, so this iteration
does not yet need a side-effect transaction policy.

This limitation must be stated explicitly because future write-capable tools
will require confirmation, idempotency, and recovery design.

---

## Safety and resource controls

### Host-owned hard limit

`MAX_TOOL_CALLS_PER_TURN` is the primary loop safety control.

### Existing tool controls remain active

The agent loop must not weaken:

- the AST allowlist and resource restrictions of `python_calculate`;
- SQLite read-only mode;
- SQLite authorizer restrictions;
- statement and result limits;
- MCP server configuration ownership;
- MCP tool-name namespacing;
- deterministic MCP shutdown.

### No model-controlled executable registration

The model may select only tools already present in `ToolRegistry`.

It cannot:

- register a new tool;
- alter a handler;
- change an MCP launch command;
- change the maximum number of steps;
- invoke arbitrary Python;
- execute arbitrary shell commands.

### Repeated calls are allowed but bounded

The model may call the same tool repeatedly, for example to correct SQL.

Do not add a blanket “same tool cannot be called twice” rule.

A repeated identical call may be useful in rare cases, but endless repetition is
stopped by the global limit.

Loop-cycle detection is out of scope.

### Cancellation

`Ctrl+C` during model generation or tool execution follows the existing
turn-abort path as far as the underlying operation permits.

The application must still reach the outer MCP cleanup `finally` block when the
session exits.

Per-tool timeouts are not introduced by this spec unless already present.

---

## Files expected to change

Suggested changes:

```text
agent.py
    new bounded agent-loop component

app.py
    remove one-tool-specific run_turn logic
    construct/use AgentRunner
    preserve rollback, persistence, CLI loop, and MCP lifecycle

config.py
    add MAX_TOOL_CALLS_PER_TURN = 4

llm.py
    only if required to separate model decision collection from final streaming

prompts.py
    update instructions for sequential tool use and recovery

README.md
    document multi-step agent behavior, limits, and examples

tests/test_agent.py
    deterministic unit tests for loop behavior

tests/test_app.py
    adjust integration tests around rendering/rollback if present

tests/test_llm.py
    adjust only if response-consumption behavior changes

docs/journal/SPEC-010-Agent-Loop.md
    implementation and live-model journal entry
```

The exact test file names may follow the repository's current convention.

Do not move tool implementations into `agent.py`.

---

## Testing strategy

Tests must not require a live Ollama model except for explicit manual validation.

### 1. Final answer without tools

Scripted model decisions:

```text
response 1 → text "Hello", no tool calls
```

Verify:

- one model request;
- zero executor calls;
- final answer returned;
- no tool messages appended.

### 2. One tool then final answer

```text
response 1 → python_calculate
executor   → {"ok": true, "result": 4}
response 2 → text "4"
```

Verify:

- two model requests;
- one executor call;
- correct assistant tool-call message;
- correct tool-result message;
- final answer returned.

### 3. Multiple different tools

```text
response 1 → sql_query
response 2 → python_calculate
response 3 → final text
```

Verify exact order of model requests and executor calls.

### 4. Same-tool retry after structured error

```text
response 1 → sql_query bad SQL
executor   → {"ok": false, ...}
response 2 → sql_query corrected SQL
executor   → {"ok": true, ...}
response 3 → final text
```

Verify the first error result appears in the second request transcript.

### 5. Maximum reached

With limit `4`:

```text
responses 1–4 → tool calls
response 5   → another tool call
```

Verify:

- exactly four executor calls;
- fifth call is not executed;
- controlled error message;
- no final answer returned.

### 6. Final answer on the last allowed step

```text
responses 1–4 → tool calls
response 5   → final text
```

Verify success.

### 7. Parallel tool calls rejected

```text
response 1 → two tool calls
```

Verify:

- zero tools are executed;
- controlled error is raised.

### 8. Empty final response

```text
response 1 → no text, no tool calls
```

Verify controlled error.

### 9. Tool result with `ok: false`

Verify the loop continues and does not convert the result into an exception.

### 10. Executor failure

Mock `executor.execute(...)` to raise.

Verify:

- controlled turn failure or propagated application-safe exception according to
  the selected boundary;
- no subsequent model request;
- no partial persistent conversation.

### 11. Persistent-history isolation

After a successful multi-tool turn, verify that saved messages contain only:

```text
user
assistant final answer
```

After a failed multi-tool turn, verify that the just-added user message is
removed and no temporary tool messages are saved.

### 12. MCP-backed tool through the same loop

Use a fake registered handler or the existing MCP integration test seam.

Verify the agent does not branch on tool source.

### 13. Rendering

Verify diagnostics appear in order:

```text
[tool 1/4]
[args]
[result]
[tool 2/4]
[args]
[result]
Qwen:
```

### 14. Final streaming

Use deterministic chunks such as:

```text
["The ", "answer ", "is 4."]
```

Verify they are rendered incrementally and concatenated correctly.

### 15. Configuration validation

Verify `max_tool_calls <= 0` is rejected.

---

## Manual verification with the live model

Record the exact model name and relevant parameters in the journal.

### Scenario A: no tool

```text
Explain in one sentence what SQLite is.
```

Expected:

- no tool calls;
- normal streamed answer.

### Scenario B: one tool

```text
What is 173 multiplied by 284?
```

Expected:

- one `python_calculate` call;
- correct final answer.

### Scenario C: sequential SQL calls

```text
Which music genre generated the most revenue, and what percentage of all
revenue did it generate?
```

Expected:

- the model uses one or more `sql_query` calls;
- it may use `python_calculate`;
- final answer is grounded in returned values;
- no fabricated rows.

The exact tool sequence is not mandated because it is model-dependent.

### Scenario D: SQL recovery

Prompt the model with a request likely to require joins, then observe whether it
can recover from a tool error.

If the model produces valid SQL on the first attempt, recovery must still be
covered by deterministic tests.

### Scenario E: MCP plus calculation

```text
What time is it now in Europe/Amsterdam and in UTC, and what is the difference
in minutes?
```

Expected:

- one or more MCP time calls;
- optional calculator call;
- final answer based on returned timestamps.

### Scenario F: limit behavior

A deterministic fake model is required for acceptance of the hard limit.
Do not depend on convincing the live model to loop forever.

---

## Acceptance criteria

SPEC-010 is complete when all of the following are true:

1. A dedicated agent-loop component exists outside the CLI command loop.
2. A model can request several sequential tool calls in one user turn.
3. Every tool call is executed through the existing `ToolExecutor`.
4. Local and MCP-backed tools work through the same agent path.
5. The result of every tool call is returned to the model as a tool message.
6. Structured `{ok: false}` tool results remain recoverable observations.
7. The model can call the same tool again with corrected arguments.
8. The model can switch to a different tool on a later iteration.
9. Exactly one tool call per model response is supported.
10. Multiple tool calls in one response are rejected before execution.
11. The default maximum is four executed tool calls per user turn.
12. A fifth requested call is not executed.
13. Reaching the limit produces a controlled, clear turn failure.
14. A final text answer after four calls is accepted.
15. Final user-visible text still streams.
16. Intermediate tool-selection text is not persisted as the assistant answer.
17. Persistent conversation contains only semantic user and final assistant
    messages.
18. A failed turn leaves prior persistent history unchanged.
19. Existing calculator, SQL, MCP discovery, and shutdown protections remain
    intact.
20. Unit tests cover no-tool, one-tool, multi-tool, retry, limit, parallel-call,
    empty-response, failure, persistence, and rendering cases.
21. README documentation describes the bounded agent loop and its limit.
22. A journal entry records implementation decisions and live-model behavior.

---

## Out of scope

SPEC-010 does not include:

- parallel tool execution;
- multiple tool calls from one model response;
- planning graphs or DAGs;
- sub-agents;
- planner/executor separation;
- background tasks;
- autonomous work across user turns;
- scheduled execution;
- human approval workflows;
- write-capable business-system tools;
- transaction rollback for external side effects;
- tool-call deduplication;
- infinite-loop pattern detection beyond the hard call limit;
- token-budget management;
- context summarization;
- persistent storage of tool traces;
- a web UI;
- observability platforms;
- OpenTelemetry;
- remote MCP transport;
- MCP reconnect;
- LangChain, LangGraph, AutoGen, CrewAI, or another agent framework.

---

## Architectural result

Before SPEC-010:

```text
User
  │
  ▼
LLM
  ├── answer
  └── one tool
          │
          ▼
       result
          │
          ▼
       LLM must answer
```

After SPEC-010:

```text
User
  │
  ▼
AgentRunner
  │
  ├───────────────────────────────────────────────┐
  ▼                                               │
LLM decision                                      │
  ├── final text → return                         │
  └── one tool call                               │
          │                                       │
          ▼                                       │
     ToolExecutor                                 │
          │                                       │
          ├── python_calculate                    │
          ├── sql_query                           │
          └── mcp_time__get_current_time          │
          │                                       │
          ▼                                       │
     structured observation ──────────────────────┘
```

The important outcome is not autonomous behavior without limits.

The outcome is a small, inspectable, deterministic harness loop in which:

```text
the model chooses
the host validates and limits
the executor acts
the result becomes the next observation
the model decides again
```

This is the first complete agent runtime of the `lLLM` project.
