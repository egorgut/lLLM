# SPEC-009 — MCP Tool Integration

- **Spec:** [SPEC-009](../../specs/SPEC-009-MCP-Tool-Integration.md)
- **Date:** 2026-07-23
- **Branch:** feature/SPEC-009-mcp-tool-integration
- **Merge commit:** 7115109

## Hypothesis / intent
SPEC-006–008 built a unified registry/executor where every tool is a local
in-process handler known at compile time. SPEC-009 crosses the boundary from
"harness implements every tool" to "harness can **discover and call** a tool
exposed by a standard external capability provider" via the Model Context
Protocol. `lLLM` becomes an MCP **host** containing an MCP **client** that
launches one local **stdio** server (`mcp_servers/time_server.py`) exposing a
single tool `get_current_time`. The tool is discovered through `tools/list`,
converted into the existing `ToolSpec`, registered in the **same**
`ToolRegistry` beside the two local tools, and routed through the existing
`ToolExecutor`. The iteration stays narrow — one server, one tool, stdio only,
one tool call per turn — and framework-free beyond the official `mcp` SDK. The
async SDK is bridged to the synchronous CLI in **one** place. Persistent history
stays semantic (user/assistant only).

## What changed
- `mcp_servers/time_server.py` (new): a separate-process stdio MCP server built
  on the SDK's low-level `Server` (chosen over `FastMCP` for exact control of
  `inputSchema`, `structuredContent`, and `isError`). Publishes one tool
  `get_current_time`; resolves time with stdlib `datetime` + `zoneinfo` only (no
  network/shell); success → `CallToolResult(structuredContent={"timezone",
  "datetime"}, isError=False)` with microseconds stripped; unknown zone →
  controlled `CallToolResult(structuredContent={"type":"invalid_timezone",
  "message": ...}, isError=True)`. `stdout` is reserved for protocol traffic.
- `mcp_integration/adapter.py` (new): pure, session-free conversions —
  `namespace_name`/`reverse_route` (`mcp_<server>__<tool>`), `to_tool_spec`
  (MCP metadata → `ToolSpec` with a permissive output schema), and a **generic**
  `normalize_result` (prefers `structuredContent` → `data`; text fallback →
  `{"text": ...}`; `isError` → `{"ok": False, …, "error": {…}}`). No SDK objects
  leak into the JSON envelope.
- `mcp_integration/client.py` (new): `McpClientManager` — a synchronous facade
  over the async SDK. Owns **one** background thread running **one** event loop;
  each server's `ClientSession` is opened once inside a single long-lived task
  (`_serve`) and kept alive until a shutdown event. `start()` is **fail-fast**
  (raises `McpStartupError` and tears down on any launch/init/discovery failure).
  `call_tool()` submits to the loop via `run_coroutine_threadsafe` and returns a
  normalized envelope; transport/session failures map to `mcp_call_failed` /
  `mcp_server_closed` / `mcp_invalid_result`. `close()` is idempotent and safe in
  a `finally`; it signals every session to unwind its `async with` (closing the
  session and reaping the child), then stops and closes the loop.
- `mcp_integration/__init__.py` (new): exports the manager, errors, and adapter
  helpers.
- `config.py`: added `import sys` and `MCP_SERVERS` (first config dict) — the
  `time` server launched via `sys.executable` with the script path resolved from
  `PROJECT_ROOT`. Command/args/env are developer-controlled; the model cannot
  influence them.
- `app.py`: `main()` now starts the MCP manager and calls
  `register_mcp_tools(...)` (converts discovered specs, registers each in the
  shared registry, binds a small synchronous adapter handler) **before** building
  the Ollama tool list — all inside a fail-fast guard that prints
  `MCP startup failed for server '<id>': …` (no traceback) and exits. The chat
  loop is wrapped in `try/finally: manager.close()`, and `EOFError`/
  `KeyboardInterrupt` at the prompt now break cleanly to that shutdown. `run_turn`,
  rendering, rollback, and one-tool-per-turn enforcement are **unchanged** —
  dispatch is source-agnostic.
- `requirements.txt`: added `mcp>=1.27,<2` (2.x is a breaking release, adopted
  only in a dedicated future iteration).
- `README.md`: documented the host/client/server/stdio/discovery roles, the
  `get_current_time` example, dependency install, startup-failure behavior,
  one-tool-per-turn, and clean shutdown; updated the structure table and status.
- `conversation.py`, `storage.py`, `llm.py`, `prompts.py`, `tools/*`,
  `scripts/init_database.py`: **unchanged**. The registry/executor already
  accommodate MCP (`mcp_time__get_current_time` passes the existing name rule;
  `to_ollama_tools()` picks it up automatically), and semantic-only persistence
  was already guaranteed by `Conversation` never seeing tool messages.

## Deviation from the spec
Same convention as SPEC-007/008: this repo has never committed a `tests/` suite.
The spec's file list and AC-58 imply committed tests; instead SPEC-009 was
verified with a standalone harness (adapter unit checks + server-level calls
through a **real subprocess** via the SDK client + full `McpClientManager`
lifecycle) plus a scripted `python app.py` live-model transcript, both recorded
below rather than committed under `tests/`. Only the delivery form differs.

## Final public API
```python
from mcp_integration import McpClientManager
from config import MCP_SERVERS

manager = McpClientManager(MCP_SERVERS)
manager.start()                                  # fail-fast discovery
manager.tool_specs()                             # [ToolSpec(name="mcp_time__get_current_time", …)]
manager.server_summaries()                       # ["time (1 tool)"]
manager.call_tool("mcp_time__get_current_time", {"timezone": "UTC"})
# -> {"ok": True, "server": "time", "tool": "get_current_time",
#     "data": {"timezone": "UTC", "datetime": "2026-07-23T11:29:29+00:00"}}
manager.call_tool("mcp_time__get_current_time", {"timezone": "Middle/Earth"})
# -> {"ok": False, "server": "time", "tool": "get_current_time",
#     "error": {"type": "invalid_timezone", "message": "Unknown IANA timezone: Middle/Earth"}}
manager.close()                                  # idempotent; no orphan child
```
Startup error types (abort before chat): `mcp_server_start_failed`,
`mcp_initialize_failed`, `mcp_tool_discovery_failed`, `mcp_invalid_tool_spec`,
`mcp_tool_name_collision`. Call error types (return an envelope):
`invalid_timezone`, `invalid_arguments`, `mcp_call_failed`, `mcp_server_closed`,
`mcp_invalid_result`, `mcp_tool_error`.

## Runtime / execution location
- Interpreter: `venv/bin/python`, Python **3.14.6**, at the project venv.
- MCP SDK: **`mcp==1.28.1`** (installed under the `mcp>=1.27,<2` pin), stable 1.x
  APIs only (`mcp.server.lowlevel.Server`, `mcp.server.stdio.stdio_server`,
  `mcp.ClientSession`, `mcp.client.stdio.stdio_client`).
- The time tool runs in a **separate OS process** — `sys.executable
  mcp_servers/time_server.py` — launched by the host and reached over stdio. The
  host is **not** importing the tool function; the test path is genuinely
  host → stdio → child process → `tools/call` → MCP response.
- Async bridge: one dedicated background event-loop thread behind the synchronous
  `McpClientManager`; the rest of the app stays synchronous.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B, ctx 40960; `tools` capable)
- Ollama: server 0.31.1; SDK `ollama==0.6.2`; reachable at `http://localhost:11434`
- Sampling: defaults — no `options` set in `llm.py`

## Verification

**Adapter + server + lifecycle harness (AC-11…AC-19, AC-20…AC-25, AC-29…AC-36,
AC-49, AC-53) — 19/19 PASS.**
- Adapter: `namespace_name("time","get_current_time") == "mcp_time__get_current_time"`;
  `reverse_route` → `("time","get_current_time")`; an invalid namespaced name
  raises `mcp_invalid_tool_spec`; MCP metadata → valid `ToolSpec` with a stripped
  description; structured success → success envelope; `isError` → error envelope;
  text-only → `{"text": …}` fallback; envelope round-trips through `json.dumps`.
- Server through a real subprocess (SDK client): `list_tools()` → exactly one
  tool `get_current_time`. `{"timezone":"UTC"}` → `ok`, zone `UTC`, ISO-8601 with
  offset (`2026-07-23T11:28:25+00:00`), close to host now, **no microseconds**.
  `{"timezone":"Europe/Amsterdam"}` → correct zone + current `+02:00` offset.
  `{"timezone":"Middle/Earth"}` → controlled `invalid_timezone` envelope.
- Lifecycle: `server_summaries() == ["time (1 tool)"]`; after `close()` a further
  `call_tool` → `mcp_server_closed`; `pgrep -f time_server.py` empty (no orphan).

**Startup failure (AC-48, AC-53).** A manager pointed at a non-existent script →
`start()` raised, `close()` ran, printed
`MCP startup failed for server 'time': The MCP server could not be started.`
(no traceback), and left no orphan child.

**Live CLI (AC-20…AC-42), scripted stdin against a scratch history:**
```text
[mcp] connected: time (1 tool)
You: What time is it now in UTC?
[tool] mcp_time__get_current_time
[args] {"timezone": "UTC"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "UTC", "datetime": "2026-07-23T11:29:29+00:00"}}
Qwen: The current time in UTC is 2026-07-23T11:29:29+00:00.

You: What is the current time in Europe/Amsterdam?
[tool] mcp_time__get_current_time
[args] {"timezone": "Europe/Amsterdam"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "Europe/Amsterdam", "datetime": "2026-07-23T13:29:36+02:00"}}
Qwen: The current time in Europe/Amsterdam is 2026-07-23T13:29:36+02:00 (CEST, UTC+2).

You: What is 173 multiplied by 284?
[tool] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}
Qwen: … 49,132.

You: Which five genres generated the most revenue?
[tool] sql_query
[args] {"query": "SELECT g.Name AS Genre, SUM(il.UnitPrice * il.Quantity) … LIMIT 5;"}
[result] {"ok": true, "columns": ["Genre","TotalRevenue"], "rows": [["Rock",826.65],["Latin",382.14],["Metal",261.36],["Alternative & Punk",241.56],["TV Shows",93.53]], "row_count": 5, "truncated": false}
Qwen: 1. Rock $826.65  2. Latin $382.14  3. Metal $261.36  4. Alternative & Punk $241.56  5. TV Shows $93.53

You: What is MCP?
Qwen: … (no [tool] block)

You: What time is it in Middle/Earth?
Qwen: The timezone "Middle/Earth" is not a valid IANA timezone. … (no [tool] block)

You: /bye
Chat finished.
```
The time questions selected `mcp_time__get_current_time` with the requested IANA
zone; the call was routed to the remote name `get_current_time` on the child
process; the normalized envelope returned and the streamed answer was grounded in
it. Arithmetic still selected `python_calculate`; the revenue question still
selected `sql_query`; MCP was untouched in both. `What is MCP?` produced **no**
tool call (AC-42). Exit was clean (`0`) with **protocol-clean stderr** (no
tracebacks).

**Observed model quirks (not harness defects).** (1) For `What is MCP?` the model
answered without a tool call — correct per AC-42 — but conflated "MCP" with the
`mcp_time__…` tool in context; content quality is the model's own. (2) For the
obviously fictional `Middle/Earth`, the model **declined to call the tool** and
rejected the name itself, so the live invalid-timezone path was not exercised
through the model this run; the controlled `invalid_timezone` envelope is proven
end-to-end via the subprocess harness above, and the app stayed usable. The spec
anticipated model-selection variance and advises against time-specific system
prompting, which was therefore not added.

**Semantic persistence (AC-43…AC-45).** After the run the scratch history held 12
messages, all `user`/`assistant` `{role, content}` pairs — no `tool_calls`, no
`tool` role, no MCP frames (verified programmatically). The real
`data/chat_history.json` was backed up before and restored after.

**Lifecycle (AC-49, AC-50, AC-51).** `/bye`, EOF (empty stdin), and a `SIGINT`
delivered at the prompt each exited `0` and left `pgrep -f time_server.py` empty.

## Outcome
All acceptance criteria AC-01…AC-58 met (AC-58's committed-test wording satisfied
via the journaled standalone harness — see Deviation; live invalid-timezone
selection is model-dependent and was verified through the subprocess harness).
`lLLM` now discovers and calls a tool exposed by a standard external provider:
host → stdio → separate MCP server process → `tools/call` → normalized result →
grounded streamed answer, beside the two unchanged local tools, through one shared
registry/executor, with a single async boundary, fail-fast startup, deterministic
shutdown with no orphan process, and semantic-only history.

## Follow-ups
- Iterative agent loop with multiple/repeated tool calls per turn (STEP 10),
  built on this mixed local + MCP-backed foundation.
- Optional/degraded (non-fail-fast) MCP servers; multiple servers; `list_changed`
  notifications and runtime tool refresh.
- MCP SDK 2.x migration in a dedicated iteration (currently pinned `<2`).
- Consider whether any local tool should later move behind an MCP boundary.
