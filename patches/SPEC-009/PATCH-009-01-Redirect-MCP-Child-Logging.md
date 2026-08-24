# PATCH-009-01 — Redirect MCP child logging off the terminal

## Parent spec

`specs/SPEC-009-MCP-Tool-Integration.md`

## Problem

Every MCP server this project launches is a child process speaking stdio, and
`McpClientManager._serve` enters the SDK transport as:

```python
async with stdio_client(params) as (read_stream, write_stream):
```

`stdio_client`'s signature is `stdio_client(server, errlog=sys.stderr)`. We never
pass `errlog`, so **the child's stderr is our terminal**. Both the local `time`
reference server and the external Tracker server use the MCP SDK, whose server
side logs one `INFO` line per request through `rich` — with ANSI colour and OSC-8
hyperlinks — so an ordinary turn interleaves this with the CLI's own output:

```text
⠋ Working... 23.5s[08/24/26 21:29:36] INFO  Processing request of type  server.py:733
                            CallToolRequest
```

The line lands mid-frame because a second process owns the same tty. The
activity indicator cannot defend against it: PATCH-010-01's discipline (join the
animation thread before printing anything durable) governs *this* process only.

Two consequences:

- The transcript is corrupted for the user, and the noise scales with tool use —
  one block per MCP call, plus one per server at startup.
- It is indistinguishable, at a glance, from the renderer defect PATCH-010-04
  fixed, which cost real diagnosis time. Two different causes producing similar
  damage is worth removing one of.

The reason it must not simply be discarded is that this stderr is the **only**
place a failing server explains itself. SPEC-009 deliberately keeps startup
errors anonymous — "no tracebacks, paths, environment, or raw stderr reach the
caller" — so the CLI prints `The MCP server could not be started.` and nothing
more. PATCH-013-01 was diagnosed precisely from a child traceback on this stream:
MCP SDK 2.0 removed `mcp.server.fastmcp`, which the pinned Tracker server
imports, and the child died on import.

## Expected change

Give the transport an `errlog` that is a per-run local file instead of the
terminal, following the convention PATCH-011-01 established for traces — one
file per run, generated, git-ignored, never uploaded:

```text
data/mcp/mcp-<run_id>.log
```

- `McpClientManager` takes a host-owned `log_dir`. When it is supplied, `start()`
  opens the run's log file and passes it as `errlog` to every `stdio_client`;
  when it is omitted the historical `sys.stderr` behaviour is kept, so the
  parameter is additive and no existing caller changes meaning.
- `close()` closes the handle, on every path, including a failed startup.
- The startup error message names the log file, so the anonymous
  `McpStartupError` stays anonymous while still telling the operator where the
  real reason is. This is the part that makes the redirect safe rather than a
  loss of diagnosability.

`app.py` and the live eval path pass `MCP_LOG_DIR` from `config.py`. Nothing
about tool results, admission policy, timeouts, or the model-facing surface
changes.

## Constraints

- Preserve SPEC-009's error taxonomy and its rule that raw child output never
  reaches the caller or the model: the log path is a *location*, not content.
- Preserve fail-fast startup and the guarantee that `close()` leaves no child
  process behind.
- Do not silence the child. Discarding stderr would make a PATCH-013-01-class
  failure undiagnosable without editing code first.
- Do not touch the child's own logging configuration or pass server-specific
  environment variables to quiet it: third-party servers are not ours to
  configure, and a host-side redirect works for every server uniformly.
- One correction only: the renderer's own output discipline is PATCH-010-01/-04's
  business and is not revisited here.
- Framework-free, standard library only.

## Acceptance criteria

- With a log directory configured, no MCP child writes to the terminal; a normal
  tool-using turn's terminal output contains no `INFO ... server.py` lines and no
  ANSI escapes originating from a child.
- Every configured server's stderr reaches `data/mcp/mcp-<run_id>.log`.
- Omitting `log_dir` keeps the historical `sys.stderr` behaviour exactly.
- A startup failure still raises `McpStartupError` with its existing
  `error_type`/`server_id`, and the CLI message additionally names the log file.
- The log handle is closed by `close()`, including after a failed `start()`.
- No child output reaches the model, the trace, or a tool result envelope.
- Deterministic suite and scripted eval suite pass; no test spawns a real child.

## Files likely affected

- `config.py`
- `mcp_integration/client.py`
- `app.py`
- `evals/runner.py`
- `tests/support_mcp.py`
- `tests/test_mcp_client.py`
- `.gitignore`
- `README.md`
- `docs/journal/SPEC-009-mcp-tool-integration.md`

This list is advisory, not restrictive.

## Verification

Automated:

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

`FakeMcpEnvironment` records the `errlog` each `stdio_client` call receives, so
tests assert directly that every server is handed the run's log file and not
`sys.stderr`, that the historical default is unchanged when no directory is
configured, and that the handle is closed after both a clean and a failed
startup. No test spawns a real subprocess.

End to end: a live TTY turn that calls an MCP tool. The terminal capture must
contain no `server.py` log line and no child-origin ANSI escape, and
`data/mcp/mcp-<run_id>.log` must hold the lines that used to be on screen.
Compare against the PATCH-010-04 capture, which contains exactly three such
collisions.

## Journal strategy

Append a new `## Patches` section to `docs/journal/SPEC-009-mcp-tool-integration.md`
with a `### PATCH-009-01` entry — deterministic plumbing only, with no
model-facing or model-decision impact (`patches/README.md` → Journal rules).
SPEC-009 has no patches yet, so the section is created here.

## Out of scope

- Changing what the CLI prints for a startup failure beyond adding the log path.
- Surfacing child stderr content in `McpStartupError`, the trace, or tool results.
- Log rotation, retention, size bounds, or cleanup of old run logs.
- Configuring or silencing a server's own logger, or passing it log-level
  environment variables.
- Capturing child stderr for the local `time` server differently from external
  ones: one uniform host-side rule, no per-server special cases.
- Anything about the activity indicator or the renderer.
- MCP SDK 2.x migration, still pinned `<2` by PATCH-013-01.

## Suggested branch and commit conventions

Per the repository PATCH workflow:

```text
branch:
patch/PATCH-009-01-redirect-mcp-child-logging

patch file:
patches/SPEC-009/PATCH-009-01-Redirect-MCP-Child-Logging.md

implementation commit:
Redirect MCP child logging to a per-run file (PATCH-009-01)

merge:
Merge PATCH-009-01: redirect MCP child logging
```
