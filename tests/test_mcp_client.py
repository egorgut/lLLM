"""`McpClientManager` lifecycle and admission tests (SPEC-013).

Exercises the manager end-to-end -- launch, discovery, admission filtering,
routing, calls, and shutdown -- against a fake stdio transport and session
(`tests/support_mcp.py`), with zero real subprocess or network involvement.
"""

import json

import pytest

from mcp_integration.client import McpClientManager, McpStartupError
from mcp_integration.config import McpServerConfig
from tests.support_mcp import FakeCallToolResult, FakeMcpEnvironment, FakeMcpTool
from tracing import MemoryTraceSink

_APPROVED = frozenset(
    {"issue_get", "issues_find", "queue_get_metadata", "issue_get_comments"}
)
_APPROVED_TOOLS = [
    FakeMcpTool("issue_get"),
    FakeMcpTool("issues_find"),
    FakeMcpTool("queue_get_metadata"),
    FakeMcpTool("issue_get_comments"),
]


def _time_like_config(*, enabled: bool = True) -> McpServerConfig:
    return McpServerConfig(
        server_id="time",
        command="fake-time-cmd",
        args=(),
        enabled=enabled,
        allowed_tools=None,
        required_tools=frozenset(),
    )


def _tracker_like_config(*, enabled: bool = True, env: dict | None = None) -> McpServerConfig:
    return McpServerConfig(
        server_id="tracker",
        command="fake-tracker-cmd",
        args=("--from", "yandex-tracker-mcp==0.7.2", "yandex-tracker-mcp"),
        env=env or {},
        enabled=enabled,
        allowed_tools=_APPROVED,
        required_tools=_APPROVED,
    )


class TestEndToEndAdmission:
    def test_two_servers_register_correctly(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        time_cfg = _time_like_config()
        tracker_cfg = _tracker_like_config()
        env.register(time_cfg.command, time_cfg.args, tools=[FakeMcpTool("get_current_time")])
        env.register(
            tracker_cfg.command,
            tracker_cfg.args,
            tools=[*_APPROVED_TOOLS, FakeMcpTool("issue_add_comment")],
        )

        manager = McpClientManager({"time": time_cfg, "tracker": tracker_cfg}, run_id="test-run")
        try:
            manager.start()
            names = {spec.name for spec in manager.tool_specs()}
            assert names == {
                "mcp_time__get_current_time",
                "mcp_tracker__issue_get",
                "mcp_tracker__issues_find",
                "mcp_tracker__queue_get_metadata",
                "mcp_tracker__issue_get_comments",
            }
        finally:
            manager.close()

    def test_missing_required_tool_fails_start(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(
            tracker_cfg.command,
            tracker_cfg.args,
            tools=_APPROVED_TOOLS[:3],  # issue_get_comments missing
        )
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            with pytest.raises(McpStartupError) as excinfo:
                manager.start()
            assert excinfo.value.error_type == "mcp_required_tool_missing"
            assert "issue_get_comments" in str(excinfo.value)
        finally:
            manager.close()
            manager.close()  # idempotent even after a failed start

    def test_disabled_server_is_never_launched(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config(enabled=False)
        env.register(tracker_cfg.command, tracker_cfg.args, tools=_APPROVED_TOOLS)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            manager.start()
            assert env.stdio_client_calls == []
            assert manager.tool_specs() == []
        finally:
            manager.close()

    def test_call_on_filtered_name_returns_error_without_invoking_session(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        calls: list[tuple[str, dict]] = []

        def handler(name, arguments):
            calls.append((name, arguments))
            return FakeCallToolResult(structuredContent={})

        env.register(
            tracker_cfg.command,
            tracker_cfg.args,
            tools=[*_APPROVED_TOOLS, FakeMcpTool("issue_add_comment")],
            call_handler=handler,
        )
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            manager.start()
            result = manager.call_tool(
                "mcp_tracker__issue_add_comment", {"issue_id": "DATA-142", "text": "hi"}
            )
            assert result["ok"] is False
            assert calls == []
        finally:
            manager.close()

    def test_admitted_call_reaches_the_session(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        calls: list[tuple[str, dict]] = []

        def handler(name, arguments):
            calls.append((name, arguments))
            return FakeCallToolResult(structuredContent={"key": "DATA-142"})

        env.register(tracker_cfg.command, tracker_cfg.args, tools=_APPROVED_TOOLS, call_handler=handler)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            manager.start()
            result = manager.call_tool("mcp_tracker__issue_get", {"issue_id": "DATA-142"})
            assert result == {
                "ok": True,
                "server": "tracker",
                "tool": "issue_get",
                "data": {"key": "DATA-142"},
            }
            assert calls == [("issue_get", {"issue_id": "DATA-142"})]
        finally:
            manager.close()


class TestLifecycle:
    def test_close_is_idempotent(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(tracker_cfg.command, tracker_cfg.args, tools=_APPROVED_TOOLS)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        manager.start()
        manager.close()
        manager.close()
        manager.close()

    def test_close_before_start_is_a_no_op(self):
        manager = McpClientManager({}, run_id="test-run")
        manager.close()


class TestStartupFailureMapping:
    def test_initialize_failure_maps_to_stable_error_type(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(tracker_cfg.command, tracker_cfg.args, tools=[], fail_initialize=True)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            with pytest.raises(McpStartupError) as excinfo:
                manager.start()
            assert excinfo.value.error_type == "mcp_initialize_failed"
        finally:
            manager.close()

    def test_list_tools_failure_maps_to_stable_error_type(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(tracker_cfg.command, tracker_cfg.args, tools=[], fail_list_tools=True)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            with pytest.raises(McpStartupError) as excinfo:
                manager.start()
            assert excinfo.value.error_type == "mcp_tool_discovery_failed"
        finally:
            manager.close()


class TestServerSummaries:
    def test_unfiltered_server_reports_plain_tool_count(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        time_cfg = _time_like_config()
        env.register(time_cfg.command, time_cfg.args, tools=[FakeMcpTool("get_current_time")])
        manager = McpClientManager({"time": time_cfg}, run_id="test-run")
        try:
            manager.start()
            assert manager.server_summaries() == ["connected: time (1 tool)"]
        finally:
            manager.close()

    def test_filtered_server_reports_admitted_and_filtered_counts(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(
            tracker_cfg.command,
            tracker_cfg.args,
            tools=[*_APPROVED_TOOLS, FakeMcpTool("issue_add_comment"), FakeMcpTool("issue_update")],
        )
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            manager.start()
            assert manager.server_summaries() == ["connected: tracker (4 admitted, 2 filtered)"]
        finally:
            manager.close()

    def test_disabled_server_reports_disabled(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config(enabled=False)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run")
        try:
            manager.start()
            assert manager.server_summaries() == ["tracker: disabled"]
        finally:
            manager.close()


class TestTracing:
    def test_events_emitted_in_order(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(
            tracker_cfg.command,
            tracker_cfg.args,
            tools=[*_APPROVED_TOOLS, FakeMcpTool("issue_add_comment")],
        )
        sink = MemoryTraceSink()
        manager = McpClientManager(
            {"tracker": tracker_cfg}, run_id="test-run", trace_sink=sink
        )
        try:
            manager.start()
        finally:
            manager.close()

        events = [event["event"] for event in sink.events]
        assert events == [
            "mcp_server_starting",
            "mcp_tool_admitted",
            "mcp_tool_admitted",
            "mcp_tool_admitted",
            "mcp_tool_admitted",
            "mcp_tool_filtered",
            "mcp_server_ready",
        ]
        ready_event = sink.events[-1]
        assert ready_event["discovered_count"] == 5
        assert ready_event["admitted_count"] == 4
        assert ready_event["filtered_count"] == 1
        assert ready_event["required_count"] == 4

    def test_no_secret_value_appears_in_any_trace_event(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        fake_token = "super-secret-token-value"
        tracker_cfg = _tracker_like_config(
            env={"TRACKER_TOKEN": fake_token, "TRANSPORT": "stdio", "TRACKER_ORG_ID": "org-1"}
        )
        env.register(tracker_cfg.command, tracker_cfg.args, tools=_APPROVED_TOOLS)
        sink = MemoryTraceSink()
        manager = McpClientManager(
            {"tracker": tracker_cfg}, run_id="test-run", trace_sink=sink
        )
        try:
            manager.start()
        finally:
            manager.close()

        dumped = json.dumps(sink.events)
        assert fake_token not in dumped
