"""`McpClientManager` lifecycle and admission tests (SPEC-013, PATCH-009-01).

Exercises the manager end-to-end -- launch, discovery, admission filtering,
routing, calls, and shutdown -- against a fake stdio transport and session
(`tests/support_mcp.py`), with zero real subprocess or network involvement.
"""

import json
import sys

import pytest

from mcp_integration.client import McpClientManager, McpStartupError, mcp_log_path
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


class TestChildLogging:
    """Where a child server's own stderr goes (PATCH-009-01).

    The SDK's `stdio_client(server, errlog=sys.stderr)` puts it on this
    process's terminal by default, where the MCP server's per-request `INFO`
    logging collides with the CLI's own output and its activity indicator.
    """

    def _env_with_two_servers(self, monkeypatch):
        env = FakeMcpEnvironment(monkeypatch)
        time_cfg, tracker_cfg = _time_like_config(), _tracker_like_config()
        env.register(time_cfg.command, time_cfg.args, tools=[FakeMcpTool("get_current_time")])
        env.register(tracker_cfg.command, tracker_cfg.args, tools=_APPROVED_TOOLS)
        return env, {"time": time_cfg, "tracker": tracker_cfg}

    def test_every_child_is_pointed_at_the_runs_log_file(self, monkeypatch, tmp_path):
        env, servers = self._env_with_two_servers(monkeypatch)
        manager = McpClientManager(servers, run_id="test-run", log_dir=tmp_path)

        try:
            manager.start()
            expected = mcp_log_path(tmp_path, "test-run")
            assert len(env.errlogs) == 2
            for errlog in env.errlogs:
                assert errlog is not sys.stderr
                assert errlog.name == str(expected)
            # Both children share one handle: one file per run, not per server.
            assert env.errlogs[0] is env.errlogs[1]
        finally:
            manager.close()

    def test_the_log_file_is_created_under_the_configured_directory(
        self, monkeypatch, tmp_path
    ):
        env, servers = self._env_with_two_servers(monkeypatch)
        manager = McpClientManager(servers, run_id="test-run", log_dir=tmp_path / "mcp")

        try:
            manager.start()
            assert manager.log_path == tmp_path / "mcp" / "mcp-test-run.log"
            assert manager.log_path.is_file()
        finally:
            manager.close()

    def test_without_a_log_directory_children_still_inherit_stderr(
        self, monkeypatch
    ):
        # The parameter is additive: an existing caller that does not pass it
        # keeps the SDK's historical behaviour exactly.
        env, servers = self._env_with_two_servers(monkeypatch)
        manager = McpClientManager(servers, run_id="test-run")

        try:
            manager.start()
            assert env.errlogs == [sys.stderr, sys.stderr]
            assert manager.log_path is None
        finally:
            manager.close()

    def test_close_releases_the_log_handle(self, monkeypatch, tmp_path):
        env, servers = self._env_with_two_servers(monkeypatch)
        manager = McpClientManager(servers, run_id="test-run", log_dir=tmp_path)
        manager.start()
        handle = env.errlogs[0]

        manager.close()

        assert handle.closed is True

    def test_a_failed_startup_still_releases_the_log_handle(self, monkeypatch, tmp_path):
        # start() tears down what it managed to build; the log handle is part of
        # that, including on the path where the loop never came up.
        env = FakeMcpEnvironment(monkeypatch)
        tracker_cfg = _tracker_like_config()
        env.register(tracker_cfg.command, tracker_cfg.args, tools=_APPROVED_TOOLS,
                     fail_initialize=True)
        manager = McpClientManager({"tracker": tracker_cfg}, run_id="test-run",
                                   log_dir=tmp_path)

        with pytest.raises(McpStartupError):
            manager.start()
        handle = env.errlogs[0]
        manager.close()

        assert handle.closed is True

    def test_an_unwritable_log_directory_falls_back_to_stderr(
        self, monkeypatch, tmp_path
    ):
        # Logging is not worth refusing to start over: noisy but working beats
        # not working. The fallback is exactly the historical behaviour.
        env, servers = self._env_with_two_servers(monkeypatch)
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        manager = McpClientManager(servers, run_id="test-run", log_dir=blocked)

        try:
            manager.start()
            assert env.errlogs == [sys.stderr, sys.stderr]
        finally:
            manager.close()
