"""`tracker_read` skill package tests (SPEC-013).

Loads the real committed `skills/tracker_read/` package (not a hand-built
fake `SkillSpec`) against a fake tool registry populated with the four
`mcp_tracker__*` names, so these tests catch a real drift between SKILL.md's
front matter and the approved tool contract. No live model, MCP, or network
is involved.
"""

from pathlib import Path

import pytest

from config import SKILLS_ROOT
from conversation import Conversation
from reliability import SkillPolicyViolation, TerminationReason, TurnStatus
from skill_runtime.loader import SkillPackageLoader
from skill_runtime.models import SkillSelection
from skill_runtime.orchestrator import SkillTurnOrchestrator
from skill_runtime.policy import RestrictedToolExecutor
from tests.support import (
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    ScriptedSkillRouter,
    make_tool_call,
    make_tool_registry,
)
from tracing import NullTraceSink

TRACKER_TOOLS = (
    "mcp_tracker__issue_get",
    "mcp_tracker__issues_find",
    "mcp_tracker__queue_get_metadata",
    "mcp_tracker__issue_get_comments",
)


def _load_tracker_read_spec():
    registry = make_tool_registry(
        "sql_query", "python_calculate", "mcp_time__get_current_time", *TRACKER_TOOLS
    )
    skill_registry = SkillPackageLoader().load_all(Path(SKILLS_ROOT), registry)
    return skill_registry.get("tracker_read")


def test_tracker_read_loads_from_the_real_skills_directory():
    spec = _load_tracker_read_spec()
    assert spec.name == "tracker_read"


def test_declares_exactly_the_four_tracker_tools_in_order():
    spec = _load_tracker_read_spec()
    assert spec.allowed_tools == TRACKER_TOOLS


def test_declares_no_local_or_other_mcp_tools():
    spec = _load_tracker_read_spec()
    forbidden = {"sql_query", "python_calculate", "mcp_time__get_current_time"}
    assert forbidden.isdisjoint(spec.allowed_tools)


def test_omitted_when_tracker_tools_are_unavailable():
    # The registry the loader sees must reflect what MCP admission actually
    # produced this run; when Tracker is disabled, none of the four names
    # exist, and app.py passes omit={"tracker_read"} instead of letting this
    # raise SkillPackageError for the whole skill-loading pass.
    registry = make_tool_registry("sql_query", "python_calculate", "mcp_time__get_current_time")
    skill_registry = SkillPackageLoader().load_all(
        Path(SKILLS_ROOT), registry, omit=frozenset({"tracker_read"})
    )
    assert not skill_registry.contains("tracker_read")
    assert skill_registry.contains("sales_analysis")


class TestRestrictedExecution:
    def _restricted(self, handlers=None):
        spec = _load_tracker_read_spec()
        inner = FakeToolExecutor(handlers or {})
        restricted = RestrictedToolExecutor(
            inner, frozenset(spec.allowed_tools), skill="tracker_read"
        )
        return restricted, inner

    def test_allowed_tracker_tool_passes_through(self):
        restricted, inner = self._restricted(
            {"mcp_tracker__issue_get": lambda args: {"ok": True, "data": {}}}
        )
        result = restricted.execute("mcp_tracker__issue_get", {"issue_id": "DATA-142"})
        assert result == {"ok": True, "data": {}}
        assert inner.calls == [("mcp_tracker__issue_get", {"issue_id": "DATA-142"})]

    def test_local_tool_is_rejected(self):
        restricted, inner = self._restricted({"sql_query": lambda args: {"ok": True}})
        with pytest.raises(SkillPolicyViolation) as excinfo:
            restricted.execute("sql_query", {"query": "SELECT 1"})
        assert excinfo.value.requested_tool == "sql_query"
        assert inner.calls == []

    def test_unapproved_tracker_tool_is_rejected_despite_shared_prefix(self):
        # mcp_tracker__issue_add_comment shares the "mcp_tracker__" namespace
        # with every approved tool but is not in allowed_tools -- the policy
        # must reject it by exact name, not by prefix.
        restricted, inner = self._restricted(
            {"mcp_tracker__issue_add_comment": lambda args: {"ok": True}}
        )
        with pytest.raises(SkillPolicyViolation) as excinfo:
            restricted.execute("mcp_tracker__issue_add_comment", {"issue_id": "DATA-142"})
        assert excinfo.value.requested_tool == "mcp_tracker__issue_add_comment"
        assert excinfo.value.skill == "tracker_read"
        assert inner.calls == []


def _build_orchestrator(router, responder, handlers):
    registry = make_tool_registry(
        "sql_query", "python_calculate", "mcp_time__get_current_time", *TRACKER_TOOLS
    )
    skill_registry = SkillPackageLoader().load_all(Path(SKILLS_ROOT), registry)
    executor = FakeToolExecutor(handlers)
    orchestrator = SkillTurnOrchestrator(
        skill_registry=skill_registry,
        router=router,
        tool_registry=registry,
        executor=executor,
        respond=responder,
        renderer_factory=RecordingRenderer,
        default_tools=registry.to_ollama_tools(),
        run_id="run-1",
        max_tool_calls=4,
        max_identical_tool_calls=2,
        model_request_timeout_seconds=5,
        tool_execution_timeout_seconds=5,
        agent_turn_timeout_seconds=30,
        trace_sink=NullTraceSink(),
    )
    return orchestrator, executor


def _explicit_selection():
    return SkillSelection("tracker_read", "explicit request", "explicit", 0, 1)


class TestScriptedTurn:
    def test_explicit_selection_completes_with_an_approved_tool_call(self):
        router = ScriptedSkillRouter(_explicit_selection())
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("mcp_tracker__issue_get", {"issue_id": "DATA-142"})]
                ),
                ScriptedModelResponse(text="DATA-142 is In Progress."),
            ]
        )
        orchestrator, executor = _build_orchestrator(
            router,
            responder,
            {"mcp_tracker__issue_get": lambda args: {"ok": True, "server": "tracker", "tool": "issue_get", "data": {}}},
        )
        conversation = Conversation()
        conversation.add_user_message("Use the tracker_read skill and show me DATA-142.")

        result = orchestrator.run_turn(conversation)

        assert result.selection.skill_name == "tracker_read"
        assert result.outcome.status is TurnStatus.COMPLETED
        assert executor.calls == [("mcp_tracker__issue_get", {"issue_id": "DATA-142"})]

    def test_attempting_a_filtered_tool_stops_the_turn(self):
        router = ScriptedSkillRouter(_explicit_selection())
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[
                        make_tool_call(
                            "mcp_tracker__issue_add_comment",
                            {"issue_id": "DATA-142", "text": "hi"},
                        )
                    ]
                )
            ]
        )
        orchestrator, executor = _build_orchestrator(router, responder, {})
        conversation = Conversation()
        conversation.add_user_message("Use the tracker_read skill and comment on DATA-142.")

        result = orchestrator.run_turn(conversation)

        assert result.outcome.status is TurnStatus.STOPPED
        assert result.outcome.reason is TerminationReason.SKILL_POLICY_VIOLATION
        assert executor.calls == []
