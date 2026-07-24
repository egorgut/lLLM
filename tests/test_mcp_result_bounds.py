"""Generic MCP result-size bound tests (SPEC-013 §14, "Bound external data").

`normalize_result` must never assume an upstream response is small merely
because the request asked for fewer records or fields -- this is the
harness's own backstop, applied identically to every MCP server. No live
session or network is involved; a small fake ``CallToolResult``-like object
is enough to exercise the bounding logic in isolation.
"""

import mcp_integration.adapter as adapter_module
from mcp_integration.adapter import normalize_result


class FakeCallToolResult:
    def __init__(self, structuredContent=None, content=None, isError=False):
        self.structuredContent = structuredContent
        self.content = content
        self.isError = isError


def test_small_result_passes_through_unchanged():
    data = {"key": "DATA-142", "summary": "Add ownership metadata"}
    result = FakeCallToolResult(structuredContent=data)
    envelope = normalize_result("tracker", "issue_get", result)
    assert envelope == {"ok": True, "server": "tracker", "tool": "issue_get", "data": data}


def test_oversized_result_is_truncated_with_a_flag(monkeypatch):
    monkeypatch.setattr(adapter_module, "MCP_RESULT_MAX_CHARS", 50)
    data = {"text": "x" * 1000}
    result = FakeCallToolResult(structuredContent=data)
    envelope = normalize_result("tracker", "issues_find", result)
    assert envelope["ok"] is True
    assert envelope["data"]["truncated"] is True
    assert len(envelope["data"]["preview"]) <= 50


def test_bound_is_generic_across_servers(monkeypatch):
    # Not Tracker-specific: the trusted local `time` server is bounded
    # identically, so the cap can never become Yandex-specific dispatch logic.
    monkeypatch.setattr(adapter_module, "MCP_RESULT_MAX_CHARS", 10)
    data = {"datetime": "2026-07-24T12:00:00+00:00", "timezone": "UTC"}
    result = FakeCallToolResult(structuredContent=data)
    envelope = normalize_result("time", "get_current_time", result)
    assert envelope["data"]["truncated"] is True


def test_error_results_are_not_passed_through_the_size_bound(monkeypatch):
    monkeypatch.setattr(adapter_module, "MCP_RESULT_MAX_CHARS", 5)
    result = FakeCallToolResult(
        structuredContent={"type": "mcp_tool_error", "message": "Authentication failed."},
        isError=True,
    )
    envelope = normalize_result("tracker", "issue_get", result)
    assert envelope["ok"] is False
    assert envelope["error"]["message"] == "Authentication failed."
