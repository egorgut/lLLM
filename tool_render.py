"""Readable terminal rendering of a tool call and its result (PATCH-010-02).

Presentation only, and pure: every function here takes plain data and returns
strings, holds no state, and writes nothing. `app.py`'s `CliRenderer` is the
only caller, so the agent loop and the tools stay unaware that this exists.

Before this module a result was printed as a single `json.dumps` line, unbounded
for the screen -- the only cap in the path, `MCP_RESULT_MAX_CHARS`, is a *model
context* budget applied in `mcp_integration/adapter.py`, so one MCP call could
emit 20 000 characters as one soft-wrapped line. Here the payload's shape is
recognised (error, table, text, fields, anything else) and rendered as a bounded
indented body under a one-line status header.

Nothing but printable characters is emitted: no ANSI escape, no `\\r`, and no
terminal-size lookup -- the width is a fixed constant. The rendering is
therefore identical on a TTY and through a pipe, which is the same rule
`cli_activity.py` follows and what keeps the transcripts committed to
`README.md` reproducible.

What the model receives is untouched: the renderer and `tool_result_message`
(`agent.py`) are two independent calls on the same result dict.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from config import TOOL_DISPLAY_WIDTH, TOOL_RESULT_PREVIEW_LINES

ARGS_PREFIX = "[args] "
RESULT_PREFIX = "[result] "
NO_ARGUMENTS = "(none)"

ELLIPSIS = "…"
SEPARATOR = " · "
INDENT = "  "
# Gap between the key column and the value column of a field list, and between
# two table columns.
COLUMN_GAP = "  "
# A single table column never grows past this, however long one cell is, so one
# wide column cannot push every other column off the line.
MAX_COLUMN_WIDTH = 32

_BODY_WIDTH = TOOL_DISPLAY_WIDTH - len(INDENT)


def format_tool_args(arguments: Any) -> str:
    """Render a tool call's arguments as one bounded ``key=value`` line."""

    if not isinstance(arguments, dict) or not arguments:
        return ARGS_PREFIX + NO_ARGUMENTS
    pairs = ", ".join(f"{key}={_inline(value)}" for key, value in arguments.items())
    return ARGS_PREFIX + _clip(pairs, TOOL_DISPLAY_WIDTH - len(ARGS_PREFIX))


def format_tool_result(result: Any) -> list[str]:
    """Render a tool result as a status header plus a bounded indented body.

    The first line always starts with ``[result] ``; any further line is body,
    indented and clipped. A failed result is a single line -- the error type and
    message are the whole story, and there is no payload worth showing.
    """

    if not isinstance(result, dict):
        # The executor guarantees a dict (tools/executor.py), but a renderer
        # must never be the thing that raises during a turn.
        lines, notes = _render_json(result)
        return [_header("ok", None, notes), *lines]

    origin = _origin(result)
    if result.get("ok") is False:
        return [_header("error", origin, [_error_summary(result)])]

    body, notes = _render_payload(_payload(result, origin))
    return [_header("ok", origin, notes), *body]


def _header(status: str, origin: str | None, notes: list[str]) -> str:
    parts = [status]
    if origin:
        parts.append(origin)
    parts.extend(note for note in notes if note)
    return _clip(RESULT_PREFIX + SEPARATOR.join(parts), TOOL_DISPLAY_WIDTH)


def _origin(result: dict) -> str | None:
    """``server/tool`` for an MCP envelope, nothing for a local tool.

    A local tool's name is already on the ``[tool N/M] <name>`` line above, so
    repeating it would be noise; an MCP result's server is not shown anywhere
    else, and which server answered is worth a glance.
    """

    server = result.get("server")
    tool = result.get("tool")
    if isinstance(server, str) and isinstance(tool, str):
        return f"{server}/{tool}"
    return None


def _payload(result: dict, origin: str | None) -> Any:
    """The part of the envelope worth rendering as a body.

    An MCP result nests everything under ``data`` (mcp_integration/adapter.py);
    a local tool's result *is* the payload, minus the ``ok`` flag the header
    already carries.
    """

    if origin is not None and "data" in result:
        return result["data"]
    return {key: value for key, value in result.items() if key != "ok"}


def _error_summary(result: dict) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        error_type = error.get("type") or "error"
        message = error.get("message") or ""
        return f"{error_type}: {message}".strip().rstrip(":")
    if error:
        return _inline(error)
    return "the tool reported an error"


def _render_payload(payload: Any) -> tuple[list[str], list[str]]:
    """Dispatch on the payload's recognised shape. First match wins."""

    if not isinstance(payload, dict) or not payload:
        if not payload:
            # Nothing to show; the header alone is the whole result.
            return [], []
        return _render_json(payload)

    columns, rows = payload.get("columns"), payload.get("rows")
    if _is_table(columns, rows):
        return _render_table(columns, rows)

    if payload.get("truncated") is True and isinstance(payload.get("preview"), str):
        # The generic MCP size backstop already replaced the payload with a
        # prefix of its own serialization (adapter.py::_bounded_data). Say so in
        # the header: this is a cap the model saw too, not a display cap.
        lines, _ = _render_text(payload["preview"])
        return lines, ["truncated"]

    if set(payload) == {"text"} and isinstance(payload["text"], str):
        return _render_text(payload["text"])

    if _is_field_like(payload):
        return _render_fields(payload)

    return _render_json(payload)


def _is_table(columns: Any, rows: Any) -> bool:
    return (
        isinstance(columns, list)
        and bool(columns)
        and isinstance(rows, list)
        and all(isinstance(row, (list, tuple)) for row in rows)
    )


def _render_table(columns: list, rows: list) -> tuple[list[str], list[str]]:
    """An aligned text table. The row count is always worth stating."""

    # Two of the budget's lines go to the header and its underline.
    limit = max(TOOL_RESULT_PREVIEW_LINES - 2, 1)
    visible = rows[:limit]
    headers = [_inline(column) for column in columns]
    span = range(len(headers))

    # A short row is padded rather than dropped: a malformed row must not cost
    # the reader the rest of the table.
    raw = [[row[index] if index < len(row) else None for index in span] for row in visible]
    cells = [["" if value is None else _inline(value) for value in row] for row in raw]

    widths = [
        min(
            max([len(headers[index])] + [len(row[index]) for row in cells]),
            MAX_COLUMN_WIDTH,
        )
        for index in span
    ]
    # A column whose visible cells are all numbers reads far better right
    # aligned -- that is what makes a revenue or count column scannable.
    numeric = [
        bool(raw) and all(_is_number(row[index]) for row in raw) for index in span
    ]
    plain = [False] * len(headers)

    lines = [
        _table_line(headers, widths, plain),
        _table_line(["-" * width for width in widths], widths, plain),
        *(_table_line(row, widths, numeric) for row in cells),
    ]

    omitted = len(rows) - len(visible)
    if omitted:
        lines.append(f"{INDENT}{ELLIPSIS} {_plural(omitted, 'more row')}")
    return lines, [_plural(len(rows), "row")]


def _table_line(cells: list[str], widths: list[int], numeric: list[bool]) -> str:
    padded = [
        _clip(cell, width).rjust(width) if numeric[index] else _clip(cell, width).ljust(width)
        for index, (cell, width) in enumerate(zip(cells, widths))
    ]
    return _clip(INDENT + COLUMN_GAP.join(padded).rstrip(), TOOL_DISPLAY_WIDTH)


def _render_text(text: str) -> tuple[list[str], list[str]]:
    """A text block, wrapped to the body width with its line structure kept."""

    wrapped: list[str] = []
    for line in text.splitlines() or [""]:
        wrapped.extend(textwrap.wrap(line, width=_BODY_WIDTH) or [""])
    return _bound([INDENT + line for line in wrapped], "more line", text)


def _render_fields(payload: dict) -> tuple[list[str], list[str]]:
    """Aligned ``key   value`` pairs, one per line."""

    keys = [str(key) for key in payload]
    key_width = min(max(len(key) for key in keys), MAX_COLUMN_WIDTH)
    value_width = _BODY_WIDTH - key_width - len(COLUMN_GAP)
    lines = [
        f"{INDENT}{_clip(key, key_width).ljust(key_width)}{COLUMN_GAP}"
        f"{_clip(_inline(value), value_width)}".rstrip()
        for key, value in zip(keys, payload.values())
    ]
    return _bound(lines, "more field", payload)


def _render_json(payload: Any) -> tuple[list[str], list[str]]:
    """The fallback: indented JSON, for a payload no other shape describes."""

    serialized = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    lines = [_clip(INDENT + line, TOOL_DISPLAY_WIDTH) for line in serialized.splitlines()]
    return _bound(lines, "more line", payload)


def _bound(lines: list[str], noun: str, payload: Any) -> tuple[list[str], list[str]]:
    """Cap a body at the preview budget, saying what was left out.

    The size note is added only when something *was* left out: when the whole
    payload is on screen, its byte count tells the reader nothing they cannot
    see.
    """

    if len(lines) <= TOOL_RESULT_PREVIEW_LINES:
        return lines, []
    kept = lines[:TOOL_RESULT_PREVIEW_LINES]
    omitted = len(lines) - TOOL_RESULT_PREVIEW_LINES
    kept.append(f"{INDENT}{ELLIPSIS} {_plural(omitted, noun)}")
    return kept, [_size(payload)]


def _is_field_like(payload: dict) -> bool:
    """Whether ``key   value`` pairs would show this payload honestly.

    A long string is fine -- clipping prose loses little, and the key still
    says what it was. A nested structure is not: clipping ``{"a": 1, "b":…``
    hides the shape, so a payload carrying one falls through to indented JSON
    unless it fits on its line whole.
    """

    key_width = min(max((len(str(key)) for key in payload), default=0), MAX_COLUMN_WIDTH)
    room = _BODY_WIDTH - key_width - len(COLUMN_GAP)
    return all(
        _is_scalar(value) or len(_inline(value)) <= room for value in payload.values()
    )


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _inline(value: Any) -> str:
    """One line of text for a value, whatever it is.

    A string is shown as itself, without JSON quoting and with its whitespace
    collapsed -- quotes and an escaped newline are noise in a cell or a field.
    Everything else keeps its JSON spelling, so `null`, `true`, and numbers
    read the way they do in the payload.
    """

    if isinstance(value, str):
        return " ".join(value.split())
    return " ".join(json.dumps(value, ensure_ascii=False, default=str).split())


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + ELLIPSIS


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _size(payload: Any) -> str:
    size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"
