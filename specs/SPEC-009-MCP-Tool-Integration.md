# SPEC-009: MCP Tool Integration

## Background

SPEC-006 introduced the shared tool contract and registry:

```text
ToolSpec
    │
    ▼
ToolRegistry
```

SPEC-007 completed the first executable tool path with the local
`python_calculate` handler.

SPEC-008 connected the same path to a real relational database through the local
read-only `sql_query` handler.

The current application therefore knows every tool in advance and binds every
tool name directly to an in-process Python handler:

```text
ToolRegistry
    ├── python_calculate
    └── sql_query

ToolExecutor
    ├── python_calculate → local Python handler
    └── sql_query       → local SQLite handler
```

This works for a small number of internal tools, but it does not scale cleanly to
external systems. A new GitHub, calendar, tracker, filesystem, or corporate
database integration would currently require custom connection, discovery,
schema-conversion, execution, lifecycle, and error-handling code inside the
`lLLM` harness.

Model Context Protocol (MCP) introduces a standard boundary between an AI host
application and external capability providers.

For this iteration:

- `lLLM` is the MCP **host**;
- `lLLM` contains an MCP **client**;
- a separate local process is the MCP **server**;
- the server exposes one tool named `get_current_time`;
- the client discovers the tool through MCP;
- the existing registry exposes it to Ollama;
- the existing executor routes the model-selected call to the MCP server.

The target path is:

```text
User request
    │
    ▼
Ollama model selects get_current_time
    │
    ▼
ToolRegistry resolves an MCP-backed tool
    │
    ▼
ToolExecutor routes the call to MCP
    │
    ▼
MCP client sends tools/call over stdio
    │
    ▼
MCP server executes get_current_time
    │
    ▼
Structured MCP result returns to the model
    │
    ▼
Model produces the final answer
```

This step is about protocol integration, not agent autonomy. The application
continues to support at most one tool execution per user turn.

---

## Goal

Add the first MCP-backed tool to `lLLM`.

The harness must:

1. launch one local MCP server as a child process over `stdio`;
2. initialize one MCP client session;
3. request the server's tool list;
4. convert the discovered MCP tool into the existing internal `ToolSpec`;
5. register the converted tool in the shared `ToolRegistry`;
6. route execution through the existing `ToolExecutor`;
7. call the tool through MCP rather than through a direct Python handler;
8. normalize the MCP response into the existing JSON-compatible tool-result path;
9. return the result to the model;
10. stream the model's final answer;
11. close the MCP session and child process cleanly;
12. preserve the current one-tool-per-turn and conversation-persistence behavior.

Target interaction:

```text
You: What time is it now in UTC?

[tool] mcp_time__get_current_time
[args] {"timezone": "UTC"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "UTC", "datetime": "2026-07-23T09:15:30+00:00"}}

Qwen: The current time in UTC is 09:15 on July 23, 2026.
```

The exact timestamp must be produced at execution time by the MCP server and must
not be hard-coded.

---

## User-visible behavior

### Successful MCP tool call

```text
You: What is the current time in Europe/Amsterdam?

[tool] mcp_time__get_current_time
[args] {"timezone": "Europe/Amsterdam"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "Europe/Amsterdam", "datetime": "2026-07-23T11:15:30+02:00"}}

Qwen: The current time in Amsterdam is 11:15 on July 23, 2026.
```

The CLI keeps the current tool-call rendering convention:

```text
[tool] ...
[args] ...
[result] ...
```

The model-facing tool name is namespaced to avoid future collisions.

### Normal local-tool response

Existing local tools continue to work unchanged:

```text
You: What is 173 multiplied by 284?

[tool] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}

Qwen: The result is 49,132.
```

### Normal non-tool response

```text
You: What is Model Context Protocol?

Qwen: Model Context Protocol is a standard interface...
```

No MCP status is printed when the model does not request an MCP tool.

### Invalid timezone

```text
You: What time is it in Middle/Earth?

[tool] mcp_time__get_current_time
[args] {"timezone": "Middle/Earth"}
[result] {"ok": false, "server": "time", "tool": "get_current_time", "error": {"type": "invalid_timezone", "message": "Unknown IANA timezone: Middle/Earth"}}

Qwen: I could not determine the time because that timezone is not recognized.
```

The application remains usable after the failed tool execution.

### MCP server startup failure

If the configured MCP server cannot be started or initialized, application
startup must fail clearly before entering the chat loop:

```text
MCP startup failed for server 'time': ...
```

The error must not be presented as an ordinary model tool result because tool
discovery never completed.

No Python traceback should be shown during normal CLI use unless an explicit
debug mode already exists.

---

## Core architectural decisions

### 1. Use the official MCP Python SDK

Use the official `mcp` Python package.

Pin the dependency to the current stable major version:

```text
mcp>=1.27,<2
```

The upper bound is intentional. MCP Python SDK 2.x is a breaking release and
must be adopted in a separate explicit iteration after this spec has been
implemented and verified.

Do not implement MCP framing, JSON-RPC messages, initialization, or stdio
transport manually.

Do not add an MCP framework abstraction beyond the official SDK and the small
adapter required by this project.

### 2. Support only local stdio transport

SPEC-009 supports one server launched locally by the harness:

```text
lLLM process
    │
    ├── Ollama HTTP client
    │
    └── MCP stdio client
            │
            ▼
      child Python process
            │
            ▼
      local time MCP server
```

The server communicates through standard input and standard output.

Out of scope:

- Streamable HTTP;
- SSE;
- remote MCP servers;
- OAuth;
- API keys;
- network authentication;
- server registries;
- reconnect loops;
- multiple MCP transports.

The server must not write logs or diagnostics to `stdout`, because `stdout` is
reserved for the MCP protocol. Diagnostics may go to `stderr`.

### 3. The server is a separate process

The time MCP server must run as a real child process.

Do not bypass MCP by importing and calling the tool function directly from the
host.

The test must prove that:

```text
host process
→ stdio transport
→ MCP server process
→ tools/call
→ MCP response
```

The server may live inside the same repository, but it remains a separate
runtime process.

Suggested location:

```text
mcp_servers/time_server.py
```

Suggested launch command:

```text
<current-venv-python> mcp_servers/time_server.py
```

Use `sys.executable` rather than a hard-coded `python` command so that the child
server runs in the same virtual environment as `app.py`.

### 4. MCP tools are discovered, not duplicated manually

The MCP server is the authoritative source for:

- tool name;
- description;
- input schema.

At startup, the host must call the equivalent of:

```text
initialize
tools/list
```

The host must convert the returned MCP tool metadata into the existing internal
`ToolSpec`.

Do not manually create a second independent copy of the
`get_current_time` description and input schema inside the host.

The adapter may add host-owned metadata such as source and server identity, but
the functional contract comes from MCP discovery.

### 5. Keep one shared ToolRegistry

Do not create a separate registry that is visible only to MCP tools.

The existing `ToolRegistry` remains the unified model-facing catalog:

```text
ToolRegistry
    ├── local: python_calculate
    ├── local: sql_query
    └── mcp:   mcp_time__get_current_time
```

The registry must remain the single source used to generate Ollama
function-tool declarations.

The model must not need to understand whether a tool is local or MCP-backed.

### 6. Namespace MCP tool names

An MCP server may expose a tool name that conflicts with a local tool or with a
tool from another MCP server.

The model-facing name must therefore be deterministic and namespaced:

```text
mcp_<server_id>__<tool_name>
```

For this iteration:

```text
server_id: time
remote tool name: get_current_time
model-facing name: mcp_time__get_current_time
```

Allowed characters must remain compatible with the existing Ollama
function-tool interface and the current `ToolSpec` validation rules.

The adapter must preserve a reverse mapping:

```text
mcp_time__get_current_time
    → server_id: time
    → remote tool name: get_current_time
```

The original MCP tool name is used for `session.call_tool(...)`.

### 7. Extend ToolSpec with source metadata only when necessary

The existing public behavior of local tools must not change.

The implementation may extend the internal tool contract with explicit source
metadata, for example:

```python
source = "local"
```

or:

```python
source = "mcp"
server_id = "time"
remote_name = "get_current_time"
```

However, avoid embedding live SDK objects, open sessions, subprocess handles, or
callbacks inside `ToolSpec`.

`ToolSpec` is declarative metadata.

Runtime connections and routing state belong in a dedicated MCP client/manager
component.

If the current executor can route MCP tools through ordinary registered
handlers without complicating the design, a generated adapter handler is also
acceptable. The implementation must still keep MCP lifecycle state outside
`ToolSpec`.

### 8. Add a small MCP client manager

Introduce one host-side component responsible for the MCP lifecycle.

Suggested responsibility:

```text
McpClientManager
    ├── start configured server
    ├── initialize ClientSession
    ├── list tools
    ├── map discovered tools to ToolSpec
    ├── call remote tool
    └── close session and process
```

The exact class and file names may differ, but responsibilities must remain
separated from:

- `Conversation`;
- JSON persistence;
- Ollama response parsing;
- SQL execution;
- Python calculation;
- CLI command parsing.

A suggested location is:

```text
mcp/client.py
```

or:

```text
mcp_client.py
```

Avoid naming a project package `mcp` if that would shadow the installed
third-party `mcp` package.

A safe project package name is:

```text
mcp_integration/
```

### 9. Manage the asynchronous SDK explicitly

The MCP Python SDK client is asynchronous.

The current CLI and tool execution path are synchronous.

SPEC-009 must bridge this boundary in one clear place rather than scattering
`asyncio.run(...)` calls across the application.

Preferred lifecycle:

```text
application startup
    → create one event loop / async runtime boundary
    → open MCP stdio session
    → discover and register tools
    → run chat loop while session stays alive
application shutdown
    → close MCP session
    → terminate child process
    → close async runtime
```

An implementation that converts the whole top-level application entry point to
`async` is acceptable if it keeps the public CLI behavior unchanged.

An implementation that hosts a dedicated event loop behind a synchronous
manager is also acceptable if lifecycle and failure behavior are deterministic.

Not acceptable:

- a new `asyncio.run(...)` for every MCP call;
- opening a new MCP process for every user request;
- keeping an orphan child process after `/bye`, EOF, or `KeyboardInterrupt`;
- mixing several undocumented event loops.

### 10. Define one time tool

The MCP server exposes exactly one tool:

```text
get_current_time
```

Input:

```json
{
  "timezone": "Europe/Amsterdam"
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "timezone": {
      "type": "string",
      "description": "IANA timezone name such as UTC or Europe/Amsterdam"
    }
  },
  "required": ["timezone"],
  "additionalProperties": false
}
```

The server must use the Python standard library:

```python
datetime
zoneinfo
```

It must not call a web API, operating-system shell command, or external time
service.

The result must contain an ISO 8601 timestamp with timezone offset:

```json
{
  "timezone": "Europe/Amsterdam",
  "datetime": "2026-07-23T11:15:30+02:00"
}
```

The timestamp should include seconds. Microseconds should be omitted for stable,
readable output.

An unknown timezone must produce a controlled tool error.

### 11. Normalize MCP results before returning them to the model

MCP tool results may contain one or more content blocks and may optionally
contain structured content.

The host must convert the SDK-specific response into a JSON-compatible envelope
used by the existing application.

Successful result:

```json
{
  "ok": true,
  "server": "time",
  "tool": "get_current_time",
  "data": {
    "timezone": "Europe/Amsterdam",
    "datetime": "2026-07-23T11:15:30+02:00"
  }
}
```

Controlled tool failure:

```json
{
  "ok": false,
  "server": "time",
  "tool": "get_current_time",
  "error": {
    "type": "invalid_timezone",
    "message": "Unknown IANA timezone: Middle/Earth"
  }
}
```

Transport/session failure during a call:

```json
{
  "ok": false,
  "server": "time",
  "tool": "get_current_time",
  "error": {
    "type": "mcp_call_failed",
    "message": "The MCP tool call failed."
  }
}
```

Do not expose:

- Python tracebacks;
- subprocess internals;
- raw SDK object representations;
- environment variables;
- absolute project paths;
- the complete server launch command;
- arbitrary server `stderr` output.

Prefer MCP `structuredContent` when present.

If only text content is present, preserve it in a stable field such as:

```json
{
  "ok": true,
  "server": "time",
  "tool": "get_current_time",
  "data": {
    "text": "..."
  }
}
```

Do not make the result normalizer specific only to the time tool.

### 12. Startup discovery is fail-fast

Tool discovery occurs before the interactive chat loop starts.

If the server cannot be launched, initialized, or queried with `tools/list`, the
application must fail clearly.

Do not silently continue without the configured MCP server in SPEC-009.

This fail-fast rule keeps the first implementation deterministic and makes
configuration errors visible.

Optional/degraded MCP servers may be introduced in a later iteration.

### 13. Tool-list changes during runtime are out of scope

Discover tools once during application startup.

Do not implement:

- dynamic tool-list refresh;
- `notifications/tools/list_changed`;
- hot reload;
- server reconnection;
- server replacement while the chat is running.

These capabilities belong to later MCP hardening.

### 14. Keep the bounded one-tool turn

SPEC-009 does not introduce an agent loop.

The existing turn remains:

```text
first model response
    │
    ├── no tool call → final answer
    │
    └── one tool call
            │
            ▼
        one execution
            │
            ▼
        second model response
            │
            └── final answer only
```

Reject:

- multiple tool calls in the first model response;
- any additional tool call after the MCP result;
- chained local and MCP calls in the same user turn;
- automatic retries chosen by the model.

A full iterative agent loop remains STEP 10.

### 15. Preserve semantic conversation history

MCP protocol messages and tool results are temporary execution context.

Persistent history remains limited to semantic conversation messages:

```text
user
assistant
```

Do not persist:

- MCP initialization messages;
- discovered tool declarations;
- raw MCP requests or responses;
- child-process metadata;
- temporary tool-result messages.

The final assistant answer may be persisted under the existing policy.

### 16. Local tools remain first-class

`python_calculate` and `sql_query` must remain direct local tools.

Do not convert them to MCP in SPEC-009.

This iteration intentionally proves that the unified registry and executor can
support both execution sources:

```text
local handler
MCP server
```

The project can evaluate later whether any local tool should be moved behind an
MCP boundary.

### 17. Configuration is explicit and minimal

Add one configuration entry for the time server.

A simple Python configuration is sufficient:

```python
MCP_SERVERS = {
    "time": {
        "command": sys.executable,
        "args": ["mcp_servers/time_server.py"],
    }
}
```

The exact shape may differ.

Requirements:

- server ID is explicit;
- command and arguments are controlled by the developer;
- the model cannot modify them;
- the model cannot supply environment variables;
- paths are resolved independently of the current working directory;
- no credentials are required;
- no config file format migration is required.

Do not allow arbitrary user chat input to launch commands.

### 18. Shutdown is deterministic

The MCP connection and child process must be closed on:

- `/bye`;
- EOF;
- `KeyboardInterrupt`;
- normal application completion;
- startup failure after the child process has started;
- an exception escaping the chat loop.

Use context managers and `finally` blocks as appropriate.

After application exit, no `time_server.py` child process should remain.

### 19. Logging stays concise

The normal CLI must not print every MCP protocol frame.

User-visible output remains at the current abstraction level:

```text
[tool]
[args]
[result]
```

Startup may print one concise line such as:

```text
[mcp] connected: time (1 tool)
```

This line is optional.

Detailed SDK/protocol debugging is out of scope unless it is behind an explicit
debug setting that defaults to off.

---

## Proposed internal flow

### Startup

```text
main
    │
    ▼
build local ToolRegistry and ToolExecutor
    │
    ▼
start McpClientManager
    │
    ▼
launch time server through stdio
    │
    ▼
initialize ClientSession
    │
    ▼
session.list_tools()
    │
    ▼
MCP tool adapter
    │
    ├── convert tool metadata to ToolSpec
    └── create reverse routing metadata
    │
    ▼
register mcp_time__get_current_time
    │
    ▼
enter CLI chat loop
```

### Tool execution

```text
model tool call:
mcp_time__get_current_time
    │
    ▼
ToolExecutor
    │
    ▼
MCP route lookup
    │
    ▼
session.call_tool(
    "get_current_time",
    {"timezone": "Europe/Amsterdam"}
)
    │
    ▼
normalize CallToolResult
    │
    ▼
return JSON-compatible result
```

### Shutdown

```text
leave CLI loop
    │
    ▼
close ClientSession
    │
    ▼
close stdio transport
    │
    ▼
terminate/reap child process
```

---

## Suggested file changes

The exact decomposition may vary, but the resulting responsibilities should be
easy to identify.

### New files

```text
mcp_servers/time_server.py
```

Responsibilities:

- define the MCP server;
- publish `get_current_time`;
- validate timezone;
- return structured data;
- run over stdio;
- keep stdout protocol-clean.

```text
mcp_integration/client.py
```

Responsibilities:

- launch configured stdio server;
- initialize client session;
- discover tools;
- execute remote calls;
- close resources.

```text
mcp_integration/adapter.py
```

Responsibilities:

- namespace MCP names;
- convert MCP tool metadata to `ToolSpec`;
- keep reverse routing metadata;
- normalize `CallToolResult`.

The client and adapter may be combined if the resulting file remains small and
the responsibilities stay clear.

### Modified files

```text
requirements.txt
```

Add the stable dependency constraint:

```text
mcp>=1.27,<2
```

```text
config.py
```

Add the controlled local MCP server configuration.

```text
app.py
```

Integrate MCP startup, discovered-tool registration, call routing, and shutdown
without changing CLI semantics.

```text
tools/registry.py
```

Change only if required to represent tool source metadata or register converted
MCP tool specs.

```text
tools/executor.py
```

Change only if required for source-aware routing. Existing local-handler
behavior must remain compatible.

```text
tools/__init__.py
```

Export any new public project abstractions only when needed.

```text
README.md
```

Document:

- what MCP adds;
- that `lLLM` is the host;
- the local stdio time server;
- the discovered `get_current_time` tool;
- dependency installation;
- sample CLI interaction;
- startup failure behavior;
- one-tool-per-turn limitation;
- clean shutdown.

### Files that should remain semantically unchanged

```text
conversation.py
storage.py
tools/python_calculate.py
tools/sql_query.py
scripts/init_database.py
```

`llm.py` and `prompts.py` should change only if the current generic tool path
requires a small compatibility adjustment. Do not add time-tool-specific
instructions to the system prompt unless live-model testing proves the generic
tool description is insufficient.

---

## Tool schema conversion

For every discovered MCP tool, convert:

```text
MCP Tool.name
MCP Tool.description
MCP Tool.inputSchema
```

into the existing internal contract:

```text
ToolSpec.name
ToolSpec.description
ToolSpec.input_schema
```

For the time tool:

```text
MCP name:
get_current_time

Model-facing name:
mcp_time__get_current_time
```

The output schema may be preserved if the current `ToolSpec` supports it, but
adding full MCP output-schema support is not required for this iteration.

Reject discovery before entering the chat loop if:

- tool name is empty;
- description violates an existing required invariant;
- input schema is not an object schema compatible with the existing tool
  declaration path;
- namespacing produces an invalid model-facing name;
- two converted tools produce the same model-facing name;
- the converted name collides with an already registered local tool.

Do not silently rename collisions beyond the deterministic namespace rule.

---

## Error taxonomy

### Startup errors

These abort application startup:

```text
mcp_server_start_failed
mcp_initialize_failed
mcp_tool_discovery_failed
mcp_invalid_tool_spec
mcp_tool_name_collision
```

The CLI message should identify the configured server ID but should not expose
sensitive internals.

### Call errors

These return a structured tool result and allow the model to answer:

```text
invalid_arguments
invalid_timezone
mcp_call_failed
mcp_invalid_result
mcp_server_closed
```

### Existing local-tool errors

Existing local tool error behavior remains unchanged.

---

## Testing and verification

This repository's established process may use committed tests or a standalone
verification script recorded in the iteration journal. The delivery form may
follow the existing project convention, but all behaviors below must be
verified.

### Unit-level verification

Verify the name adapter:

```text
("time", "get_current_time")
→ "mcp_time__get_current_time"
```

Verify invalid or colliding names fail deterministically.

Verify MCP tool metadata converts to a valid `ToolSpec`.

Verify reverse routing returns:

```text
server_id = "time"
remote_name = "get_current_time"
```

Verify structured MCP success normalizes to the standard success envelope.

Verify MCP `isError` or controlled error content normalizes to the standard
error envelope.

Verify SDK objects do not leak into JSON serialization.

### Server verification

Launch the server as a child process through the official SDK client.

Call `list_tools` and verify exactly one tool is returned:

```text
get_current_time
```

Call with:

```json
{"timezone": "UTC"}
```

Verify:

- `ok` path succeeds;
- returned timezone is `UTC`;
- datetime parses as ISO 8601;
- UTC offset is present;
- timestamp is close to the host's current time;
- microseconds are omitted.

Call with:

```json
{"timezone": "Europe/Amsterdam"}
```

Verify the returned zone and current offset are correct for the execution date.

Call with:

```json
{"timezone": "Middle/Earth"}
```

Verify a controlled `invalid_timezone` failure.

### Host integration verification

Start the host and verify:

1. the MCP server process starts;
2. initialization succeeds;
3. `tools/list` is called;
4. the converted tool appears in the shared registry;
5. local tools remain present;
6. Ollama receives all three declarations;
7. a model-selected MCP call is routed to the remote name;
8. the normalized result returns to the model;
9. the final answer streams;
10. only user and final assistant messages are persisted;
11. `/bye` leaves no child process.

### Live-model scenarios

Record the exact model name and runtime parameters in the journal.

Run at least these prompts:

```text
What time is it now in UTC?
```

Expected:

- model selects `mcp_time__get_current_time`;
- argument contains `UTC`;
- result comes from MCP;
- final answer is grounded in the returned timestamp.

```text
What time is it now in Europe/Amsterdam?
```

Expected:

- model selects the MCP tool;
- argument contains the requested IANA timezone;
- final answer uses the tool result.

```text
What is 173 multiplied by 284?
```

Expected:

- model still selects `python_calculate`;
- MCP is not used.

```text
Which five genres generated the most revenue?
```

Expected:

- model still selects `sql_query`;
- MCP is not used.

```text
What is MCP?
```

Expected:

- model answers without a tool call.

```text
What time is it in Middle/Earth?
```

Expected:

- the MCP tool returns a controlled failure;
- the model explains the limitation;
- the application remains usable.

### Lifecycle verification

Verify shutdown after:

```text
/bye
EOF
Ctrl+C
```

Verify no MCP server child remains in each case.

Verify an invalid configured server path fails before the chat prompt and does
not leave a child process.

---

## Acceptance criteria

### Architecture

- **AC-01:** `lLLM` acts as an MCP host and contains an MCP client.
- **AC-02:** The MCP server runs as a separate child process.
- **AC-03:** Host-server communication uses the official SDK's stdio transport.
- **AC-04:** MCP protocol framing and JSON-RPC are not implemented manually.
- **AC-05:** The MCP session stays open for the lifetime of the chat application.
- **AC-06:** The MCP process is not restarted for every tool call.
- **AC-07:** MCP lifecycle logic is separated from conversation persistence and
  local tool implementations.

### Dependency and versioning

- **AC-08:** `requirements.txt` includes `mcp>=1.27,<2`.
- **AC-09:** The implementation uses stable MCP Python SDK 1.x APIs.
- **AC-10:** SDK 2.x migration is explicitly out of scope.

### MCP server

- **AC-11:** The repository contains one local stdio MCP server.
- **AC-12:** The server exposes exactly one tool named `get_current_time`.
- **AC-13:** The tool requires one `timezone` string argument.
- **AC-14:** The tool accepts IANA timezone names.
- **AC-15:** The tool uses `datetime` and `zoneinfo`, with no network request.
- **AC-16:** Successful output contains timezone and ISO 8601 datetime with
  offset.
- **AC-17:** Microseconds are omitted.
- **AC-18:** Unknown timezones produce a controlled error.
- **AC-19:** The server keeps stdout reserved for MCP protocol traffic.

### Discovery and registry

- **AC-20:** Tool discovery occurs through MCP `tools/list` during startup.
- **AC-21:** The host does not duplicate the remote tool's functional schema
  manually.
- **AC-22:** The discovered tool is converted to the existing `ToolSpec`.
- **AC-23:** The MCP tool is registered in the same `ToolRegistry` as local
  tools.
- **AC-24:** The model-facing name is
  `mcp_time__get_current_time`.
- **AC-25:** Reverse routing preserves server ID and original MCP tool name.
- **AC-26:** Local tools remain registered and operational.
- **AC-27:** Name collisions fail deterministically before entering the chat
  loop.
- **AC-28:** Invalid discovered schemas fail clearly during startup.

### Execution and normalization

- **AC-29:** A model call to `mcp_time__get_current_time` is routed through MCP.
- **AC-30:** The host calls the remote tool name `get_current_time`.
- **AC-31:** The server result is returned through the existing tool-result turn.
- **AC-32:** MCP SDK result objects are normalized to JSON-compatible data.
- **AC-33:** Structured content is preferred when available.
- **AC-34:** A text-only MCP result can still be normalized generically.
- **AC-35:** Controlled server failures use a stable error envelope.
- **AC-36:** Transport failures do not expose tracebacks, paths, environment
  variables, or raw stderr to the model.
- **AC-37:** The CLI prints `[tool]`, `[args]`, and `[result]` using the existing
  convention.

### Turn semantics

- **AC-38:** At most one tool call is executed per user turn.
- **AC-39:** Additional tool calls after the result remain unsupported.
- **AC-40:** SPEC-009 does not implement retries, planning, or a multi-step agent
  loop.
- **AC-41:** Final answers continue to stream.
- **AC-42:** Non-tool responses remain unchanged.

### Persistence

- **AC-43:** Persistent history contains only semantic user and assistant
  messages.
- **AC-44:** MCP protocol messages are not persisted.
- **AC-45:** Raw tool calls and results are not persisted.
- **AC-46:** Existing `/reset` behavior remains unchanged.

### Startup and shutdown

- **AC-47:** MCP server launch and discovery complete before the chat loop starts.
- **AC-48:** Startup fails clearly if launch, initialization, or discovery fails.
- **AC-49:** `/bye` closes the MCP session and child process.
- **AC-50:** EOF closes the MCP session and child process.
- **AC-51:** `KeyboardInterrupt` closes the MCP session and child process.
- **AC-52:** Exceptions escaping the chat loop still trigger MCP cleanup.
- **AC-53:** Failed startup does not leave an orphan child process.

### Documentation and verification

- **AC-54:** README explains the host, client, server, stdio, and discovery roles.
- **AC-55:** README includes a successful `get_current_time` example.
- **AC-56:** README states that only one tool call per turn is supported.
- **AC-57:** The iteration journal records the MCP SDK version, model version,
  verification commands, live prompts, observed calls, and shutdown checks.
- **AC-58:** Verification covers MCP success, invalid timezone, local Python tool,
  local SQL tool, non-tool response, startup failure, and clean shutdown.

---

## Non-goals

SPEC-009 does not implement:

- a multi-step agent loop;
- more than one tool call per user turn;
- automatic model retries;
- SQL self-correction;
- multiple MCP servers;
- more than one MCP tool;
- MCP resources;
- MCP prompts;
- MCP sampling;
- MCP elicitation;
- MCP roots;
- MCP tasks;
- tool-list change notifications;
- runtime tool refresh;
- Streamable HTTP;
- SSE;
- remote MCP servers;
- OAuth;
- authentication;
- secrets management;
- user approval flows;
- write-capable external actions;
- GitHub integration;
- Yandex Tracker integration;
- calendar integration;
- filesystem access;
- migration of existing local tools to MCP;
- MCP SDK 2.x migration;
- a general plugin marketplace;
- arbitrary command execution from chat input;
- production-grade process supervision;
- background reconnect or health-check loops.

---

## Risks and mitigations

### SDK 2.x is imminent and breaking

Mitigation:

```text
mcp>=1.27,<2
```

Record the exact installed version in the journal. Upgrade only in a dedicated
future iteration.

### Async lifecycle complicates the synchronous CLI

Mitigation:

- define one explicit async boundary;
- keep one session alive;
- use context managers;
- verify `/bye`, EOF, and `KeyboardInterrupt`;
- do not open a new event loop per call.

### Child-process stdout corrupts MCP transport

Mitigation:

- reserve stdout for the protocol;
- send diagnostics to stderr;
- keep the server implementation minimal.

### Remote and local tool names may collide

Mitigation:

```text
mcp_<server_id>__<tool_name>
```

Validate collisions before chat startup.

### MCP result shapes vary

Mitigation:

- normalize in one generic adapter;
- prefer structured content;
- support text fallback;
- never pass raw SDK objects to JSON serialization.

### The model may not select the new tool reliably

Mitigation:

- use a precise discovered description;
- use clear live prompts;
- record actual model behavior;
- do not add time-specific system prompting unless testing proves it necessary.

### A server crash can invalidate the session

Mitigation for this iteration:

- return a controlled `mcp_server_closed` or `mcp_call_failed` result;
- keep the application from crashing where practical;
- do not implement reconnection in SPEC-009.

---

## Definition of done

SPEC-009 is complete when a fresh project environment can:

1. install the pinned MCP SDK dependency;
2. start `python app.py`;
3. launch the local time MCP server over stdio;
4. initialize an MCP client session;
5. discover `get_current_time`;
6. register it as `mcp_time__get_current_time` beside the two existing local
   tools;
7. let Qwen select it for a current-time question;
8. execute the call through the separate MCP process;
9. return a normalized structured result;
10. stream a grounded final answer;
11. continue to execute `python_calculate` and `sql_query`;
12. persist only semantic conversation messages;
13. reject unsupported extra tool calls;
14. handle invalid timezone input cleanly;
15. close the MCP session and child process on every normal exit path;
16. record the implementation and live-model evidence in the SPEC-009 journal.

At that point, `lLLM` has crossed the intended architectural boundary:

```text
before SPEC-009:
harness knows and directly implements every tool

after SPEC-009:
harness can discover and call a tool exposed by a standard external capability
provider
```

The next step may build the iterative agent loop on top of this mixed local and
MCP-backed tool foundation.
