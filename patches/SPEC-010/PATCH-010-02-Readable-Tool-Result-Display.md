# PATCH-010-02 — Readable Tool Result Display

## Parent spec

`specs/SPEC-010-Agent-Loop.md`

## Problem

`CliRenderer` prints every tool result as one unbroken `json.dumps` line
(`app.py:204-208`):

```python
print(f"[result] {json.dumps(result, ensure_ascii=False)}")
```

Nothing bounds that line for the screen. The only cap in the path is
`MCP_RESULT_MAX_CHARS = 20_000` (`config.py`), and that is a *model context*
budget applied inside `mcp_integration/adapter.py::_bounded_data` — not a
display budget. A single MCP call can therefore emit 20 000 characters as one
soft-wrapped line into the terminal.

MCP results suffer most, because `normalize_result` wraps the payload in an
envelope (`{"ok", "server", "tool", "data": {...}}`), so the interesting part is
nested one level down inside an already unreadable line. The same problem
applies to `sql_query` and `sandbox_execute`, whose results are the largest the
local tools produce.

That the raw dump is unreadable is already visible in this repository: the CLI
transcripts committed to `README.md` (lines 134, 289, 523, 644) are hand-elided
with `{...}`, `...`, and manual line breaks. The documentation works around the
display defect by editing the transcripts, which makes them no longer faithful
captures of what the CLI prints.

The `[args]` line printed by `tool_call` has the same defect for the same
reason, and is part of the same two-line unit the user reads.

## Expected change

Render a tool result as a status header plus a bounded, indented body chosen by
the recognised shape of the payload (error / table / text / fields / JSON
fallback), and render `[args]` as a bounded `key=value` list.

Display only. The renderer and the model-facing transcript are two independent
calls — `renderer.tool_result(result)` (`agent.py:578`) and
`tool_result_message(call, result)` (`agent.py:94-104`) — so what the model
receives is byte-for-byte unchanged.

Target output:

```text
[tool 3/8] mcp_tracker__issue_get
[args] issue_id=DATA-142, include_description=true
[result] ok · tracker/issue_get · 1.4 KB
  key        DATA-142
  summary    Add ownership metadata to the reporting mart
  status     In Progress
  … 6 more fields

[tool 4/8] sql_query
[args] query=WITH GenreRevenue AS (SELECT g.Name, SUM(...
[result] ok · 5 rows
  Name    Revenue
  ------  -------
  Rock     826.65
  Latin    382.09
  … 3 more rows

[tool 5/8] mcp_time__get_current_time
[result] error · time/get_current_time · invalid_timezone: Unknown timezone
```

## Constraints

- Preserve the parent SPEC's architecture and intent: the `Renderer` Protocol
  (`agent.py:67-74`) is unchanged, and the agent loop keeps knowing nothing
  about presentation.
- **No ANSI escapes and no `\r`**, following the rule stated in
  `cli_activity.py` — output must be identical on a TTY and through a pipe so
  the `README.md` transcripts and test captures stay reproducible. For the same
  reason the display width is a fixed constant, not `shutil.get_terminal_size()`
  and not an `isatty` branch.
- Framework-free: no `rich` or any other new dependency; `requirements.txt` is
  unchanged.
- CLI output stays English, like every other CLI string in the project.
- `tool_result_message`, `normalize_result`, and `_bounded_data` are not
  touched — the model sees exactly what it saw before.
- No new configuration semantics: the display limits are plain constants.

## Acceptance criteria

- A tool result prints a header line and an indented body of at most
  `TOOL_RESULT_PREVIEW_LINES` lines, each at most `TOOL_DISPLAY_WIDTH`
  characters, regardless of payload size.
- When part of the payload is not shown, an explicit footer says how much was
  omitted (`… N more rows` / `… N more fields` / `… N more lines`).
- A failed result prints one `error · …` line carrying the error type and
  message, with no body.
- A tabular payload (`columns` + `rows`) prints as an aligned table; the MCP
  text fallback shape (`{"text": ...}`) prints as a wrapped text block; a flat
  object prints as aligned key/value pairs; anything else falls back to indented
  JSON.
- An MCP envelope shows `server/tool` in the header; a local tool does not
  (its name is already on the `[tool N/M]` line above).
- Rendered output contains no `\r` and no ESC character.
- The message sent to the model is unchanged.
- Existing successful flows continue to work; the project test suite passes.

## Files likely affected

- `tool_render.py` (new)
- `tests/test_tool_render.py` (new)
- `app.py` (`CliRenderer.tool_call`, `CliRenderer.tool_result`)
- `config.py` (two display constants)
- `README.md` (committed CLI transcripts)

This list is advisory, not restrictive.

## Verification

Automated: a new `tests/test_tool_render.py` covering every shape branch
(error, table, text, fields, JSON fallback, the `truncated`/`preview` envelope
from `_bounded_data`) plus the bounds, the omission footers, empty and
non-dict payloads, unicode, and the absence of `\r`/ESC. The existing
`tests/test_cli_activity.py` must keep passing unchanged, since it asserts on
indicator behaviour around these prints. Full `pytest` run.

End-to-end: a live Ollama run of `python app.py` with a scripted dialogue on
stdin exercising `python_calculate`, `sql_query`, the local `time` MCP server,
and a deliberately failing tool call, to capture fresh `README.md` transcripts.
The same run piped to a file must produce the same rendering as the TTY run.

A live model is required only to regenerate honest transcripts. Live-model
*verification* in the sense of `patches/README.md` is **not** required here:
this PATCH does not affect agent decisions, prompts, tool selection, history,
termination, or model-visible tool results.

## Journal strategy

Append a `## Patches` entry to the parent SPEC journal
(`docs/journal/SPEC-010-agent-loop.md`, which already has that section from
PATCH-010-01). This change is deterministic presentation code with no
model-facing or model-decision impact, so no standalone journal is needed.

## Out of scope

- MCP `image` and `resource` content blocks, which `adapter.py::_join_text`
  silently drops today. Supporting them changes the adapter and what the model
  receives — a separate SPEC, not this PATCH.
- `MCP_RESULT_MAX_CHARS` and `_bounded_data`: the model-context bound stays as
  it is.
- A configurable verbosity switch (`summary`/`preview`/`full`). New
  configuration semantics would make this a SPEC per `patches/README.md`.
- Colour and any other ANSI styling.
