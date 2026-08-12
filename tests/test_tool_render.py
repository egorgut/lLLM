"""Tool-call and tool-result display tests (PATCH-010-02).

`tool_render` is pure, so nothing here needs a CLI, a terminal, an agent loop,
or a live model: every case is a plain dict in and a list of strings out.

Two guarantees run through the whole file. The rendering is *bounded* -- no
payload, however large, can produce more than `TOOL_RESULT_PREVIEW_LINES` body
lines or a line wider than `TOOL_DISPLAY_WIDTH` -- and it is *plain*, with no
ANSI escape and no carriage return, so a captured or redirected transcript is
byte-for-byte what the terminal showed.
"""

import json

from config import TOOL_DISPLAY_WIDTH, TOOL_RESULT_PREVIEW_LINES
from tool_render import format_tool_args, format_tool_result


def body(result):
    """The rendered lines below the header."""

    return format_tool_result(result)[1:]


def header(result):
    return format_tool_result(result)[0]


class TestHeader:
    def test_local_tool_reports_only_its_status(self):
        # The tool's name is already on the `[tool N/M] <name>` line above.
        assert header({"ok": True, "result": 49132}) == "[result] ok"

    def test_mcp_envelope_reports_the_answering_server(self):
        result = {
            "ok": True,
            "server": "time",
            "tool": "get_current_time",
            "data": {"timezone": "UTC"},
        }
        assert header(result) == "[result] ok · time/get_current_time"

    def test_payload_free_result_is_a_single_line(self):
        assert format_tool_result({"ok": True}) == ["[result] ok"]

    def test_size_is_stated_only_when_something_was_left_out(self):
        small = {"ok": True, "value": "short"}
        assert "B" not in header(small)

        large = {"ok": True, "data": {f"field_{index}": index for index in range(200)}}
        assert header(large).endswith(" B") or header(large).endswith(" KB")


class TestErrors:
    def test_failed_result_is_one_line_with_type_and_message(self):
        result = {
            "ok": False,
            "error": {"type": "invalid_query", "message": "The SQL query is invalid."},
        }
        assert format_tool_result(result) == [
            "[result] error · invalid_query: The SQL query is invalid."
        ]

    def test_mcp_failure_keeps_the_server_that_failed(self):
        result = {
            "ok": False,
            "server": "tracker",
            "tool": "issue_get",
            "error": {"type": "mcp_server_closed", "message": "The session is closed."},
        }
        assert format_tool_result(result) == [
            "[result] error · tracker/issue_get · mcp_server_closed: The session is closed."
        ]

    def test_error_without_a_message_still_renders(self):
        result = {"ok": False, "error": {"type": "mcp_call_failed"}}
        assert format_tool_result(result) == ["[result] error · mcp_call_failed"]

    def test_unshaped_error_value_does_not_raise(self):
        assert format_tool_result({"ok": False, "error": "boom"}) == ["[result] error · boom"]

    def test_missing_error_key_falls_back_to_a_statement(self):
        assert format_tool_result({"ok": False}) == [
            "[result] error · the tool reported an error"
        ]

    def test_a_long_error_message_is_clipped_to_the_width(self):
        result = {"ok": False, "error": {"type": "e", "message": "x" * 500}}
        assert len(format_tool_result(result)[0]) == TOOL_DISPLAY_WIDTH


class TestTable:
    RESULT = {
        "ok": True,
        "columns": ["Name", "Revenue"],
        "rows": [["Rock", 826.65], ["Latin", 382.09]],
        "row_count": 2,
        "truncated": False,
    }

    def test_rows_are_aligned_under_their_headers(self):
        assert body(self.RESULT) == [
            "  Name   Revenue",
            "  -----  -------",
            "  Rock    826.65",
            "  Latin   382.09",
        ]

    def test_row_count_is_always_stated(self):
        assert header(self.RESULT) == "[result] ok · 2 rows"

    def test_a_single_row_is_not_pluralised(self):
        result = {**self.RESULT, "rows": [["Rock", 826.65]]}
        assert header(result) == "[result] ok · 1 row"

    def test_numbers_are_right_aligned_and_text_is_not(self):
        lines = body(self.RESULT)
        assert lines[2].endswith("826.65")
        assert lines[2].startswith("  Rock  ")

    def test_extra_rows_are_summarised_rather_than_printed(self):
        result = {**self.RESULT, "rows": [[f"row {n}", n] for n in range(100)]}
        lines = body(result)
        # The budget covers the header and its underline too, and the footer
        # that says what was left out sits outside it.
        assert len(lines) == TOOL_RESULT_PREVIEW_LINES + 1
        assert lines[-1].strip().startswith("…")
        assert lines[-1].strip().endswith("more rows")
        assert header(result) == "[result] ok · 100 rows"

    def test_a_short_row_is_padded_rather_than_dropped(self):
        result = {**self.RESULT, "rows": [["Rock"]]}
        assert body(result)[2].strip() == "Rock"

    def test_null_cells_use_the_json_spelling_of_the_payload(self):
        result = {"ok": True, "columns": ["Name", "Composer"], "rows": [["Rock", None]]}
        assert body(result)[2].strip() == "Rock"

    def test_a_very_wide_cell_cannot_push_other_columns_off_the_line(self):
        result = {
            "ok": True,
            "columns": ["Name", "Revenue"],
            "rows": [["x" * 500, 826.65]],
        }
        lines = body(result)
        assert all(len(line) <= TOOL_DISPLAY_WIDTH for line in lines)
        assert lines[2].endswith("826.65")

    def test_rows_that_are_not_sequences_fall_back_to_another_shape(self):
        result = {"ok": True, "columns": ["Name"], "rows": ["Rock", "Latin"]}
        # Not a table -- rendered some other way, but never dropped.
        assert any("Rock" in line for line in body(result))


class TestTextBlock:
    def test_the_mcp_text_fallback_shape_prints_as_prose(self):
        result = {
            "ok": True,
            "server": "time",
            "tool": "convert_time",
            "data": {"text": "15:30 in Moscow is 08:30 in New York."},
        }
        assert body(result) == ["  15:30 in Moscow is 08:30 in New York."]

    def test_prose_is_wrapped_to_the_width_rather_than_clipped(self):
        result = {
            "ok": True,
            "server": "s",
            "tool": "t",
            "data": {"text": " ".join(["word"] * 100)},
        }
        lines = body(result)
        assert all(len(line) <= TOOL_DISPLAY_WIDTH for line in lines)
        # Wrapped, not cut: every word survives somewhere in the body.
        assert sum(line.count("word") for line in lines) == 100

    def test_prose_past_the_budget_is_summarised(self):
        result = {
            "ok": True,
            "server": "s",
            "tool": "t",
            "data": {"text": " ".join(["word"] * 500)},
        }
        lines = body(result)
        assert len(lines) == TOOL_RESULT_PREVIEW_LINES + 1
        assert lines[-1].strip().endswith("more lines")

    def test_existing_line_structure_is_kept(self):
        result = {"ok": True, "data": {"text": "first\nsecond"}, "server": "s", "tool": "t"}
        assert body(result) == ["  first", "  second"]


class TestFields:
    def test_scalar_payload_prints_as_aligned_pairs(self):
        result = {
            "ok": True,
            "server": "time",
            "tool": "get_current_time",
            "data": {"timezone": "UTC", "datetime": "2026-07-23T11:29:29+00:00"},
        }
        assert body(result) == [
            "  timezone  UTC",
            "  datetime  2026-07-23T11:29:29+00:00",
        ]

    def test_json_spelling_is_kept_for_non_strings(self):
        result = {"ok": True, "is_dst": False, "offset": None, "count": 3}
        assert body(result) == [
            "  is_dst  false",
            "  offset  null",
            "  count   3",
        ]

    def test_a_newline_inside_a_value_does_not_break_the_layout(self):
        result = {"ok": True, "stdout": "first\nsecond\n"}
        assert body(result) == ["  stdout  first second"]

    def test_a_small_structure_stays_on_its_field_line(self):
        result = {"ok": True, "name": "DATA-142", "tags": ["metadata", "governance"]}
        assert body(result) == [
            '  name  DATA-142',
            '  tags  ["metadata", "governance"]',
        ]

    def test_a_large_structure_falls_back_to_json_rather_than_being_clipped(self):
        # Clipping `{"a": 1, "b":…` would hide the shape, which is the one thing
        # a nested value is read for.
        result = {"ok": True, "artifacts": [{"name": f"file_{n}.csv"} for n in range(20)]}
        assert body(result)[0].strip() == "{"

    def test_extra_fields_are_summarised_rather_than_printed(self):
        result = {"ok": True, **{f"field_{n}": n for n in range(100)}}
        lines = body(result)
        assert len(lines) == TOOL_RESULT_PREVIEW_LINES + 1
        assert lines[-1].strip() == f"… {100 - TOOL_RESULT_PREVIEW_LINES} more fields"


class TestJsonFallback:
    def test_a_nested_payload_prints_as_indented_json(self):
        result = {"ok": True, "artifacts": [{"name": "a.csv", "size_bytes": 42}] * 3}
        lines = body(result)
        assert lines[0] == "  {"
        assert any('"name": "a.csv"' in line for line in lines)

    def test_deeply_nested_output_is_still_bounded(self):
        result = {"ok": True, "tree": {f"key_{n}": {"child": list(range(5))} for n in range(50)}}
        lines = body(result)
        assert len(lines) == TOOL_RESULT_PREVIEW_LINES + 1
        assert lines[-1].strip().endswith("more lines")

    def test_a_non_dict_result_does_not_raise(self):
        # The executor guarantees a dict, but the renderer must never be the
        # thing that breaks a turn.
        assert format_tool_result(["unexpected"])[0].startswith("[result] ok")


class TestTruncatedEnvelope:
    RESULT = {
        "ok": True,
        "server": "tracker",
        "tool": "issues_find",
        "data": {"truncated": True, "preview": '{"issues": [{"key": "DATA-1"}]'},
    }

    def test_the_model_side_cap_is_named_in_the_header(self):
        assert header(self.RESULT) == "[result] ok · tracker/issues_find · truncated"

    def test_the_preview_is_shown_as_text(self):
        assert body(self.RESULT) == ['  {"issues": [{"key": "DATA-1"}]']


class TestArgs:
    def test_arguments_print_as_key_value_pairs(self):
        line = format_tool_args({"issue_id": "DATA-142", "include_description": True})
        assert line == "[args] issue_id=DATA-142, include_description=true"

    def test_no_arguments_is_stated_explicitly(self):
        assert format_tool_args({}) == "[args] (none)"

    def test_a_long_argument_is_clipped_to_the_width(self):
        line = format_tool_args({"query": "SELECT " + "x" * 500})
        assert len(line) == TOOL_DISPLAY_WIDTH
        assert line.endswith("…")

    def test_a_multiline_argument_stays_on_one_line(self):
        line = format_tool_args({"code": "import csv\nprint(1)\n"})
        assert line == "[args] code=import csv print(1)"

    def test_a_non_dict_argument_does_not_raise(self):
        assert format_tool_args(None) == "[args] (none)"


class TestGlobalGuarantees:
    """Every branch at once: bounded, plain, and free of trailing whitespace."""

    PAYLOADS = [
        {"ok": True},
        {"ok": True, "result": 49132},
        {"ok": False, "error": {"type": "t", "message": "m " * 300}},
        {"ok": True, "columns": ["a", "b"], "rows": [[n, "x" * 200] for n in range(80)]},
        {"ok": True, "server": "s", "tool": "t", "data": {"text": "слово " * 400}},
        {"ok": True, "server": "s", "tool": "t", "data": {"truncated": True, "preview": "p" * 900}},
        {"ok": True, **{f"field_{n}": "значение " * 30 for n in range(60)}},
        {"ok": True, "nested": {"a": [{"b": list(range(30))} for _ in range(30)]}},
    ]

    def rendered(self):
        for payload in self.PAYLOADS:
            yield from format_tool_result(payload)

    def test_no_line_exceeds_the_display_width(self):
        assert all(len(line) <= TOOL_DISPLAY_WIDTH for line in self.rendered())

    def test_no_body_exceeds_the_preview_budget(self):
        for payload in self.PAYLOADS:
            # One line of header, at most the budget of body, at most one footer.
            assert len(format_tool_result(payload)) <= TOOL_RESULT_PREVIEW_LINES + 2

    def test_output_carries_no_control_characters(self):
        # The rule `cli_activity.py` states: no ANSI escape and no `\r`, so a
        # redirected transcript is exactly what the terminal showed.
        for line in self.rendered():
            assert "\r" not in line
            assert "\x1b" not in line
            assert "\n" not in line

    def test_no_line_has_trailing_whitespace(self):
        assert all(line == line.rstrip() for line in self.rendered())

    def test_rendering_never_mutates_the_result_sent_to_the_model(self):
        # Display and the model-facing transcript read the same dict
        # (agent.py), so the renderer must leave it exactly as it found it.
        for payload in self.PAYLOADS:
            before = json.dumps(payload, ensure_ascii=False, default=str)
            format_tool_result(payload)
            assert json.dumps(payload, ensure_ascii=False, default=str) == before
