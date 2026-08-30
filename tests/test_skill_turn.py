"""Turn-lifecycle integration tests (SPEC-012 §"Integration tests: turn lifecycle").

Drive the real `SkillTurnOrchestrator` with a scripted router, scripted model
responder, and fake executor — no live Ollama, MCP, or real waits. These assert
the end-to-end contract: shared turn_id/deadline, tool restriction, restricted
policy stops, and that routing protocol / selection are never persisted.
"""

from pathlib import Path

from config import BASELINE_TOOL_NAMES
from conversation import Conversation
from reliability import (
    InvalidSkillSelection,
    SkillRoutingTimeout,
    TerminationReason,
    TurnStatus,
)
from skill_runtime.models import SkillSelection, SkillSpec
from skill_runtime.orchestrator import SkillTurnOrchestrator
from skill_runtime.registry import SkillRegistry
from tests.support import (
    FakeClock,
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    ScriptedSkillRouter,
    make_tool_call,
    make_tool_registry,
)
from tracing import MemoryTraceSink

SALES_SPEC = SkillSpec(
    name="sales_analysis",
    description="Analyse sales and revenue data",
    version="1",
    allowed_tools=("sql_query", "python_calculate"),
    instruction="# Sales Analysis\nProcedure body.",
    input_schema={"type": "object", "properties": {}},
    package_path=Path("/skills/sales_analysis"),
    fingerprint="sha256:sales",
)


TRACKER_SPEC = SkillSpec(
    name="tracker_read",
    description="Read and summarise Tracker issues",
    version="1",
    allowed_tools=("mcp_tracker__issue_get",),
    instruction="# Tracker Read\nProcedure body.",
    input_schema={"type": "object", "properties": {}},
    package_path=Path("/skills/tracker_read"),
    fingerprint="sha256:tracker",
)


def skill_registry():
    registry = SkillRegistry()
    registry.register(SALES_SPEC)
    registry.register(TRACKER_SPEC)
    return registry


def build_orchestrator(
    router,
    *,
    responder,
    handlers=None,
    clock=None,
    trace=None,
    on_selection=None,
    max_tool_calls=4,
    registry=None,
    baseline_tools=(),
):
    tool_registry = make_tool_registry(
        "sql_query",
        "python_calculate",
        "mcp_time__get_current_time",
        "mcp_tracker__issue_get",
    )
    executor = FakeToolExecutor(handlers or {})
    return (
        SkillTurnOrchestrator(
            skill_registry=registry or skill_registry(),
            router=router,
            tool_registry=tool_registry,
            executor=executor,
            respond=responder,
            renderer_factory=RecordingRenderer,
            default_tools=tool_registry.to_ollama_tools(),
            run_id="run-1",
            max_tool_calls=max_tool_calls,
            max_identical_tool_calls=2,
            model_request_timeout_seconds=5,
            tool_execution_timeout_seconds=5,
            agent_turn_timeout_seconds=30,
            trace_sink=trace or MemoryTraceSink(),
            clock=clock or __import__("time").monotonic,
            on_selection=on_selection or (lambda _s: None),
            baseline_tools=baseline_tools,
        ),
        executor,
    )


def conversation_with(user_message):
    conversation = Conversation()
    conversation.add_user_message(user_message)
    return conversation


def model_selection(name):
    return SkillSelection(name, "routed", "model", 1, 5)


def explicit_selection(name):
    return SkillSelection(name, "explicit", "explicit", 0, 1)


def none_selection():
    return SkillSelection(None, "no skill", "model", 1, 5)


def test_no_skill_ordinary_answer():
    router = ScriptedSkillRouter(none_selection())
    responder = ScriptedResponder([ScriptedModelResponse(text="An agent loop is ...")])
    orch, _ = build_orchestrator(router, responder=responder)
    result = orch.run_turn(conversation_with("Explain what an agent loop is."))
    assert result.outcome.status is TurnStatus.COMPLETED
    assert result.selection.skill_name is None
    assert result.outcome.final_text == "An agent loop is ..."


def test_explicit_skill_selection_restricts_tools():
    router = ScriptedSkillRouter(explicit_selection("sales_analysis"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})]),
            ScriptedModelResponse(text="Rock earns the most."),
        ]
    )
    trace = MemoryTraceSink()
    orch, executor = build_orchestrator(
        router,
        responder=responder,
        handlers={"sql_query": lambda a: {"ok": True, "rows": [["Rock"]]}},
        trace=trace,
    )
    result = orch.run_turn(conversation_with("use the sales_analysis skill"))
    assert result.outcome.status is TurnStatus.COMPLETED
    assert executor.calls == [("sql_query", {"query": "SELECT 1"})]
    started = next(e for e in trace.events if e["event"] == "turn_started")
    # The skill's own tools, plus the host's reserved activation tool (SPEC-018).
    assert started["available_tools"] == [
        "sql_query",
        "python_calculate",
        "activate_skill",
    ]
    assert started["selected_skill"] == "sales_analysis"


def test_model_selected_skill_counts_all_model_requests():
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"q": "1"})]),
            ScriptedModelResponse(text="done"),
        ]
    )
    orch, _ = build_orchestrator(
        router, responder=responder, handlers={"sql_query": lambda a: {"ok": True}}
    )
    result = orch.run_turn(conversation_with("revenue by genre"))
    # 1 routing request + 2 agent requests.
    assert result.outcome.model_requests == 3


def test_sql_then_python_calculation():
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"q": "1"})]),
            ScriptedModelResponse(tool_calls=[make_tool_call("python_calculate", {"expression": "1+1"})]),
            ScriptedModelResponse(text="35.5%"),
        ]
    )
    orch, executor = build_orchestrator(
        router,
        responder=responder,
        handlers={
            "sql_query": lambda a: {"ok": True, "rows": [[826.65]]},
            "python_calculate": lambda a: {"ok": True, "result": 2},
        },
    )
    result = orch.run_turn(conversation_with("what percentage?"))
    assert result.outcome.status is TurnStatus.COMPLETED
    assert result.outcome.tool_calls_executed == 2
    assert [c[0] for c in executor.calls] == ["sql_query", "python_calculate"]


def test_clarification_with_no_tool_call():
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [ScriptedModelResponse(text="Which sales metric and period should I analyse?")]
    )
    orch, executor = build_orchestrator(router, responder=responder)
    result = orch.run_turn(conversation_with("Analyse sales."))
    assert result.outcome.status is TurnStatus.COMPLETED
    assert result.outcome.tool_calls_executed == 0
    assert executor.calls == []
    assert "metric" in result.outcome.final_text


def test_invalid_routing_produces_failed_outcome():
    router = ScriptedSkillRouter(InvalidSkillSelection("bad selection"))
    responder = ScriptedResponder([])
    orch, _ = build_orchestrator(router, responder=responder)
    conversation = conversation_with("revenue")
    result = orch.run_turn(conversation)
    assert result.outcome.status is TurnStatus.FAILED
    assert result.outcome.reason is TerminationReason.INVALID_SKILL_SELECTION
    assert result.outcome.final_text is None


def test_routing_timeout_produces_timed_out_outcome():
    error = SkillRoutingTimeout("routing timed out")
    error.routing_requests = 1
    router = ScriptedSkillRouter(error)
    orch, _ = build_orchestrator(router, responder=ScriptedResponder([]))
    result = orch.run_turn(conversation_with("revenue"))
    assert result.outcome.status is TurnStatus.TIMED_OUT
    assert result.outcome.reason is TerminationReason.SKILL_ROUTING_TIMEOUT
    assert result.outcome.model_requests == 1


def test_disallowed_tool_stops_turn_with_policy_violation():
    # A foreign *domain* tool, deliberately not a host baseline tool: Time is
    # composed into every active skill's view since PATCH-012-02, so it can no
    # longer stand for "a tool this skill may not call".
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [ScriptedModelResponse(tool_calls=[make_tool_call("mcp_tracker__issue_get", {})])]
    )
    trace = MemoryTraceSink()
    orch, executor = build_orchestrator(
        router,
        responder=responder,
        handlers={"mcp_tracker__issue_get": lambda a: {"ok": True}},
        trace=trace,
        baseline_tools=BASELINE_TOOL_NAMES,
    )
    result = orch.run_turn(conversation_with("look up DATA-142"))
    assert result.outcome.status is TurnStatus.STOPPED
    assert result.outcome.reason is TerminationReason.SKILL_POLICY_VIOLATION
    # The disallowed tool never executed.
    assert executor.calls == []
    violation = next(
        e for e in trace.events
        if e["event"] == "policy_violation" and e.get("policy") == "skill_tool_allowlist"
    )
    assert violation["requested_tool"] == "mcp_tracker__issue_get"
    assert violation["skill"] == "sales_analysis"


def test_selection_and_routing_not_persisted():
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"q": "1"})]),
            ScriptedModelResponse(text="done"),
        ]
    )
    orch, _ = build_orchestrator(
        router, responder=responder, handlers={"sql_query": lambda a: {"ok": True}}
    )
    conversation = conversation_with("revenue by genre")
    orch.run_turn(conversation)
    # The orchestrator never mutates the conversation: only the one tentative
    # user message remains (the app persists the assistant answer on success).
    assert conversation.stored_messages == [
        {"role": "user", "content": "revenue by genre"}
    ]


def test_shared_turn_id_across_routing_and_execution():
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"q": "1"})]),
            ScriptedModelResponse(text="done"),
        ]
    )
    trace = MemoryTraceSink()
    orch, _ = build_orchestrator(
        router, responder=responder, handlers={"sql_query": lambda a: {"ok": True}}, trace=trace
    )
    result = orch.run_turn(conversation_with("revenue"))
    router_turn_id = router.calls[0]["turn_id"]
    assert router_turn_id == result.outcome.turn_id
    turn_ids = {e["turn_id"] for e in trace.events if "turn_id" in e}
    assert turn_ids == {result.outcome.turn_id}


def test_duration_includes_routing():
    clock = FakeClock()

    class AdvancingRouter:
        def select(self, **kwargs):
            clock.advance(5)  # routing consumed 5 seconds
            return model_selection("sales_analysis")

    responder = ScriptedResponder([ScriptedModelResponse(text="done")])
    orch, _ = build_orchestrator(AdvancingRouter(), responder=responder, clock=clock)
    result = orch.run_turn(conversation_with("revenue"))
    assert result.outcome.duration_ms >= 5000


def test_tool_call_budget_still_applies_within_skill():
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"q": "1"})]),
            ScriptedModelResponse(tool_calls=[make_tool_call("python_calculate", {"expression": "2"})]),
            ScriptedModelResponse(text="I could not finish the calculation."),
        ]
    )
    orch, executor = build_orchestrator(
        router,
        responder=responder,
        handlers={
            "sql_query": lambda a: {"ok": True},
            "python_calculate": lambda a: {"ok": True},
        },
        max_tool_calls=1,
    )
    result = orch.run_turn(conversation_with("revenue"))
    # The budget still refuses the second call (SPEC-021 §4.1 leaves the refusal
    # alone); the turn now answers with what it has instead of vanishing.
    assert result.outcome.status is TurnStatus.COMPLETED
    assert result.outcome.reason is TerminationReason.BUDGET_EXHAUSTED
    assert [name for name, _ in executor.calls] == ["sql_query"]


def test_on_selection_called_with_selection():
    seen = []
    router = ScriptedSkillRouter(model_selection("sales_analysis"))
    responder = ScriptedResponder([ScriptedModelResponse(text="done")])
    orch, _ = build_orchestrator(
        router, responder=responder, on_selection=seen.append
    )
    orch.run_turn(conversation_with("revenue"))
    assert seen and seen[0].skill_name == "sales_analysis"


# --- Host baseline tools composed with the active skill (PATCH-012-02) --------
#
# The defect these cover: selecting a domain skill used to *replace* the whole
# tool view, so a Tracker turn silently lost the host's Time tool and could not
# tell whether a deadline had passed.


def test_baseline_tools_are_declared_alongside_the_skills_own():
    router = ScriptedSkillRouter(model_selection("tracker_read"))
    responder = ScriptedResponder([ScriptedModelResponse(text="DATA-142 is open.")])
    orch, _ = build_orchestrator(
        router, responder=responder, baseline_tools=BASELINE_TOOL_NAMES
    )

    orch.run_turn(conversation_with("show me DATA-142"))

    _messages, tools = responder.calls[0]
    assert [t["function"]["name"] for t in tools] == [
        "mcp_tracker__issue_get",
        "mcp_time__get_current_time",
        "activate_skill",
    ]


def test_baseline_tool_executes_under_an_active_skill():
    router = ScriptedSkillRouter(model_selection("tracker_read"))
    responder = ScriptedResponder(
        [
            ScriptedModelResponse(
                tool_calls=[make_tool_call("mcp_tracker__issue_get", {"issue_id": "D-1"})]
            ),
            ScriptedModelResponse(
                tool_calls=[
                    make_tool_call("mcp_time__get_current_time", {"timezone": "UTC"})
                ]
            ),
            ScriptedModelResponse(text="D-1 is overdue."),
        ]
    )
    orch, executor = build_orchestrator(
        router,
        responder=responder,
        handlers={
            "mcp_tracker__issue_get": lambda a: {"ok": True, "data": {"key": "D-1"}},
            "mcp_time__get_current_time": lambda a: {
                "ok": True,
                "datetime": "2026-08-27T10:00:00Z",
            },
        },
        baseline_tools=BASELINE_TOOL_NAMES,
    )

    result = orch.run_turn(conversation_with("is DATA-142 overdue as of now?"))

    # One domain capability and one host baseline capability in the same turn,
    # with no skill change: the restricted executor really dispatched both.
    assert result.outcome.status is TurnStatus.COMPLETED
    assert [name for name, _ in executor.calls] == [
        "mcp_tracker__issue_get",
        "mcp_time__get_current_time",
    ]
    assert result.final_skill == "tracker_read"
    assert result.activations == 0


def test_baseline_does_not_widen_access_to_other_domain_tools():
    router = ScriptedSkillRouter(model_selection("tracker_read"))
    responder = ScriptedResponder(
        [ScriptedModelResponse(tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})])]
    )
    orch, executor = build_orchestrator(
        router,
        responder=responder,
        handlers={"sql_query": lambda a: {"ok": True}},
        baseline_tools=BASELINE_TOOL_NAMES,
    )

    result = orch.run_turn(conversation_with("show me revenue"))

    assert result.outcome.reason is TerminationReason.SKILL_POLICY_VIOLATION
    assert executor.calls == []


def test_toolset_trace_separates_skill_tools_from_baseline():
    router = ScriptedSkillRouter(model_selection("tracker_read"))
    responder = ScriptedResponder([ScriptedModelResponse(text="done")])
    trace = MemoryTraceSink()
    orch, _ = build_orchestrator(
        router, responder=responder, trace=trace, baseline_tools=BASELINE_TOOL_NAMES
    )

    orch.run_turn(conversation_with("show me DATA-142"))

    resolved = next(e for e in trace.events if e["event"] == "skill_toolset_resolved")
    assert resolved["available_tools"] == [
        "mcp_tracker__issue_get",
        "mcp_time__get_current_time",
        "activate_skill",
    ]
    assert resolved["skill_tools"] == ["mcp_tracker__issue_get"]
    assert resolved["baseline_tools"] == ["mcp_time__get_current_time"]


def test_no_skill_path_keeps_the_whole_global_tool_set():
    router = ScriptedSkillRouter(none_selection())
    responder = ScriptedResponder([ScriptedModelResponse(text="It is 10:00.")])
    orch, _ = build_orchestrator(
        router, responder=responder, baseline_tools=BASELINE_TOOL_NAMES
    )

    orch.run_turn(conversation_with("what time is it?"))

    # Baseline composition applies to an *active skill*; without one the model
    # still sees every registered tool, exactly as before.
    _messages, tools = responder.calls[0]
    assert [t["function"]["name"] for t in tools] == [
        "sql_query",
        "python_calculate",
        "mcp_time__get_current_time",
        "mcp_tracker__issue_get",
        "activate_skill",
    ]
