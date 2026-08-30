"""Mid-turn skill activation tests (SPEC-018 §7.1; PATCH-018-01).

SPEC-018 was merged with reduced verification: the mechanism was checked by hand
and then left protected by nothing but review. These are the committed tests
§7.1 asked for — the real `SkillTurnOrchestrator`, the real
`SkillActivationHandler`, the real `RestrictedToolExecutor` and prompt
composition, driven by a scripted router, scripted model, and fake executor. No
live Ollama, MCP, or database is involved.

Numbered references below are SPEC-018 §7.1's list.
"""

from pathlib import Path

import pytest

from config import BASELINE_TOOL_NAMES
from conversation import Conversation
from reliability import TerminationReason, TurnStatus
from skill_runtime.activation import (
    ACTIVATE_SKILL_TOOL_NAME,
    SkillActivationHandler,
    build_activate_skill_declaration,
)
from skill_runtime.models import SkillSelection, SkillSpec
from skill_runtime.orchestrator import SkillTurnOrchestrator
from skill_runtime.prompting import compose_active_skill
from skill_runtime.registry import SkillRegistry
from tests.support import (
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
    description="Read and summarise tracker issues",
    version="2",
    allowed_tools=("mcp_tracker__issue_get",),
    instruction="# Tracker Read\nProcedure body.",
    input_schema={"type": "object", "properties": {}},
    package_path=Path("/skills/tracker_read"),
    fingerprint="sha256:tracker",
)

TOOL_NAMES = (
    "sql_query",
    "python_calculate",
    "mcp_tracker__issue_get",
    "mcp_time__get_current_time",
)


def skill_registry(*specs: SkillSpec) -> SkillRegistry:
    registry = SkillRegistry()
    for spec in specs or (TRACKER_SPEC, SALES_SPEC):
        registry.register(spec)
    return registry


def build_orchestrator(
    router,
    *,
    responder,
    handlers=None,
    trace=None,
    registry=None,
    max_tool_calls=4,
    max_skill_activations=2,
    on_activation=None,
    baseline_tools=(),
):
    tool_registry = make_tool_registry(*TOOL_NAMES)
    executor = FakeToolExecutor(handlers or {})
    orchestrator = SkillTurnOrchestrator(
        skill_registry=skill_registry() if registry is None else registry,
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
        max_skill_activations=max_skill_activations,
        on_activation=on_activation or (lambda _spec, _replaced: None),
        baseline_tools=baseline_tools,
    )
    return orchestrator, executor


def conversation_with(user_message: str) -> Conversation:
    conversation = Conversation()
    conversation.add_user_message(user_message)
    return conversation


def routed_to(name: str | None) -> ScriptedSkillRouter:
    return ScriptedSkillRouter(SkillSelection(name, "routed", "model", 1, 5))


def activate(name) -> object:
    return make_tool_call(ACTIVATE_SKILL_TOOL_NAME, {"name": name})


def declared_names(responder: ScriptedResponder, request_index: int) -> list[str]:
    _messages, tools = responder.calls[request_index]
    return [tool["function"]["name"] for tool in tools]


def system_content(responder: ScriptedResponder, request_index: int) -> str:
    messages, _tools = responder.calls[request_index]
    return messages[0]["content"]


def tool_result_payload(responder: ScriptedResponder, request_index: int) -> str:
    """The last tool-result message the model saw on request ``request_index``."""

    messages, _tools = responder.calls[request_index]
    return messages[-1]["content"]


class TestDeclaration:
    """§4.2 — host-generated from the catalog, absent when there is nothing to
    activate."""

    def test_declaration_is_rendered_from_the_catalog(self):
        declaration = build_activate_skill_declaration(skill_registry().catalog())
        function = declaration["function"]
        assert function["name"] == ACTIVATE_SKILL_TOOL_NAME
        parameters = function["parameters"]
        # Catalog order, i.e. registration order — not re-sorted for the model.
        assert parameters["properties"]["name"]["enum"] == [
            "tracker_read",
            "sales_analysis",
        ]
        assert parameters["required"] == ["name"]
        assert parameters["additionalProperties"] is False
        # Only the compact catalog descriptions, never a full instruction.
        description = parameters["properties"]["name"]["description"]
        assert "Analyse sales and revenue data" in description
        assert "Procedure body" not in description

    def test_description_triggers_on_a_capability_gap(self):
        """PATCH-018-02.

        The original wording triggered on reclassification — "when the work
        turns out to belong to a different class". Live, that read as false in
        the case the tool exists for: a correctly routed skill, followed by a
        step of a different kind. The trigger is now the capability gap, and it
        says out loud that a correct prior selection is no objection.
        """

        declaration = build_activate_skill_declaration(skill_registry().catalog())
        description = declaration["function"]["description"]

        assert "cannot do" in description
        assert "right one for the work already finished" in description
        assert "step of a different kind" in description
        # Replacement semantics stay part of what the model is told.
        assert "replacing any procedure" in description

    def test_empty_registry_produces_no_declaration(self):
        """§7.1/11."""

        assert build_activate_skill_declaration(SkillRegistry().catalog()) is None

    def test_empty_registry_never_declares_activate_skill_to_the_model(self):
        """§7.1/11, end to end."""

        responder = ScriptedResponder([ScriptedModelResponse(text="done")])
        trace = MemoryTraceSink()
        orch, _ = build_orchestrator(
            routed_to(None),
            responder=responder,
            registry=SkillRegistry(),
            trace=trace,
        )

        result = orch.run_turn(conversation_with("hello"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert ACTIVATE_SKILL_TOOL_NAME not in declared_names(responder, 0)
        started = next(e for e in trace.events if e["event"] == "turn_started")
        assert ACTIVATE_SKILL_TOOL_NAME not in started["available_tools"]
        assert result.final_skill is None
        assert result.activations == 0

    def test_activate_skill_is_declared_with_and_without_a_router_skill(self):
        """§4.2 — the declaration is appended in both cases."""

        for selected in (None, "tracker_read"):
            responder = ScriptedResponder([ScriptedModelResponse(text="done")])
            orch, _ = build_orchestrator(routed_to(selected), responder=responder)
            orch.run_turn(conversation_with("hello"))
            assert declared_names(responder, 0)[-1] == ACTIVATE_SKILL_TOOL_NAME

    def test_the_policy_never_promises_a_tool_the_turn_lacks(self):
        """PATCH-018-02 — the invariant behind the new policy line.

        The policy tells an active skill it can call `activate_skill`. That is
        only honest because the block is composed for a *selected* skill, which
        requires a registry entry, which means a declaration exists. Pinned here
        rather than left to inspection: a future change that composes the block
        without the declaration must fail this, not reach a live model.
        """

        for skill in ("tracker_read", "sales_analysis"):
            responder = ScriptedResponder([ScriptedModelResponse(text="done")])
            orch, _ = build_orchestrator(routed_to(skill), responder=responder)
            orch.run_turn(conversation_with("hello"))

            system = system_content(responder, 0)
            assert "<active_skill_policy>" in system
            assert ACTIVATE_SKILL_TOOL_NAME in system
            assert ACTIVATE_SKILL_TOOL_NAME in declared_names(responder, 0)


class TestActivationFromNoSkill:
    """§7.1/3 — declarations, executor, and system block all change."""

    def test_activation_replaces_the_whole_view(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "SELECT 1"})]
                ),
                ScriptedModelResponse(text="Rock."),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to(None),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True, "rows": [["Rock"]]}},
        )

        result = orch.run_turn(conversation_with("which genre earns the most?"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.selection.skill_name is None
        assert result.final_skill == "sales_analysis"
        assert result.activations == 1
        # Declarations: the unrestricted global set, then the skill's own.
        assert declared_names(responder, 0) == [*TOOL_NAMES, ACTIVATE_SKILL_TOOL_NAME]
        assert declared_names(responder, 1) == [
            "sql_query",
            "python_calculate",
            ACTIVATE_SKILL_TOOL_NAME,
        ]
        # System block: none before, exactly one wrapper after.
        assert "<active_skill" not in system_content(responder, 0)
        assert system_content(responder, 1).count("<active_skill ") == 1
        assert compose_active_skill(SALES_SPEC) in system_content(responder, 1)
        # Executor: the newly allowed tool ran.
        assert [name for name, _ in executor.calls] == ["sql_query"]

    def test_the_model_receives_a_receipt_not_the_instruction(self):
        """§4.3 — the instruction went to the system layer."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, _ = build_orchestrator(routed_to(None), responder=responder)

        orch.run_turn(conversation_with("hello"))

        payload = tool_result_payload(responder, 1)
        assert '"ok": true' in payload
        assert '"skill": "sales_analysis"' in payload
        assert '"version": "1"' in payload
        assert '"replaced": null' in payload
        assert "Procedure body" not in payload


class TestReplacement:
    """§7.1/4, §7.1/5 — replacement, never composition."""

    def test_exactly_one_wrapper_survives_a_replacement(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("mcp_tracker__issue_get", {"id": "A-1"})]
                ),
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, _ = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            handlers={"mcp_tracker__issue_get": lambda a: {"ok": True}},
        )

        result = orch.run_turn(conversation_with("read A-1 then check revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.selection.skill_name == "tracker_read"
        assert result.final_skill == "sales_analysis"
        assert result.activations == 1
        before, after = system_content(responder, 0), system_content(responder, 2)
        assert before.count("<active_skill ") == 1
        assert after.count("<active_skill ") == 1
        assert 'name="tracker_read"' in before
        assert 'name="sales_analysis"' in after
        assert "Tracker Read" not in after
        # The base system prompt is intact on both sides of the switch.
        base = Conversation().messages_for_model()[0]["content"]
        assert after.startswith(base)

    def test_replacement_wraps_the_original_executor_not_the_previous_wrapper(self):
        """§7.1/5 — restrictions must not accumulate.

        `sql_query` is forbidden by `tracker_read` and permitted by
        `sales_analysis`; it runs only if the new restricted executor was built
        over the original global executor.
        """

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "SELECT 1"})]
                ),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True}},
        )

        result = orch.run_turn(conversation_with("read A-1 then check revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert [name for name, _ in executor.calls] == ["sql_query"]

    def test_activating_the_already_active_skill_changes_nothing(self):
        """§4.4 — acknowledged, counted, but nothing is recomposed."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        trace = MemoryTraceSink()
        orch, _ = build_orchestrator(
            routed_to("tracker_read"), responder=responder, trace=trace
        )

        result = orch.run_turn(conversation_with("read A-1"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.final_skill == "tracker_read"
        assert result.activations == 1
        assert system_content(responder, 0) == system_content(responder, 1)
        assert declared_names(responder, 0) == declared_names(responder, 1)
        activated = next(e for e in trace.events if e["event"] == "skill_activated")
        assert activated["replaced_skill"] == "tracker_read"
        assert activated["recomposed"] is False

    def test_on_activation_callback_reports_the_switch(self):
        """§4.11 — the CLI's switch line is driven by this callback."""

        seen: list[tuple[str, str | None]] = []
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, _ = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            on_activation=lambda spec, replaced: seen.append((spec.name, replaced)),
        )

        orch.run_turn(conversation_with("read A-1 then check revenue"))

        assert seen == [("sales_analysis", "tracker_read")]


class TestAllowlistAfterActivation:
    """§7.1/6 — the new allowlist governs from the activation point forward."""

    def test_tool_forbidden_by_the_new_skill_is_rejected_before_its_handler(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "SELECT 1"})]
                ),
            ]
        )
        trace = MemoryTraceSink()
        orch, executor = build_orchestrator(
            routed_to("sales_analysis"),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True}},
            trace=trace,
        )

        result = orch.run_turn(conversation_with("read A-1"))

        assert result.outcome.status is TurnStatus.STOPPED
        assert result.outcome.reason is TerminationReason.SKILL_POLICY_VIOLATION
        # It was legal a moment ago, and still never reached its handler.
        assert executor.calls == []
        violation = next(
            e
            for e in trace.events
            if e["event"] == "policy_violation"
            and e.get("policy") == "skill_tool_allowlist"
        )
        assert violation["skill"] == "tracker_read"
        assert violation["requested_tool"] == "sql_query"

    def test_tools_already_executed_under_the_previous_view_are_unaffected(self):
        """§4.5 — they were legal when they ran."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "SELECT 1"})]
                ),
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to("sales_analysis"),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True}},
        )

        result = orch.run_turn(conversation_with("revenue, then read A-1"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert [name for name, _ in executor.calls] == ["sql_query"]
        assert result.final_skill == "tracker_read"


class TestRecoverableErrors:
    """§7.1/7, §7.1/8 — neither failure is terminal."""

    def test_unknown_skill_name_is_recoverable(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("nonexistent_skill")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "SELECT 1"})]
                ),
                ScriptedModelResponse(text="Rock."),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to("sales_analysis"),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True}},
        )

        result = orch.run_turn(conversation_with("revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.final_skill == "sales_analysis"
        assert result.activations == 0
        payload = tool_result_payload(responder, 1)
        assert '"ok": false' in payload
        assert '"error": "unknown_skill"' in payload
        assert "sales_analysis" in payload and "tracker_read" in payload
        # The view is untouched: the skill's own tools still run.
        assert system_content(responder, 0) == system_content(responder, 1)
        assert [name for name, _ in executor.calls] == ["sql_query"]

    def test_the_reserved_name_is_not_an_activatable_skill(self):
        """§4.7 — `activate_skill("activate_skill")` is `unknown_skill`."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate(ACTIVATE_SKILL_TOOL_NAME)]),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, _ = build_orchestrator(routed_to(None), responder=responder)

        result = orch.run_turn(conversation_with("hello"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert '"error": "unknown_skill"' in tool_result_payload(responder, 1)
        assert result.activations == 0

    def test_activation_cap_is_recoverable_and_keeps_the_active_skill(self):
        """§7.1/8 — with `max_skill_activations=1`, the second attempt fails."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "SELECT 1"})]
                ),
                ScriptedModelResponse(text="Rock."),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to(None),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True}},
            max_skill_activations=1,
        )

        result = orch.run_turn(conversation_with("revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.final_skill == "sales_analysis"
        assert result.activations == 1
        payload = tool_result_payload(responder, 2)
        assert '"error": "activation_limit"' in payload
        assert '"active_skill": "sales_analysis"' in payload
        # The first activation's view survived the refused second one.
        assert system_content(responder, 2) == system_content(responder, 1)
        assert declared_names(responder, 2) == declared_names(responder, 1)
        assert [name for name, _ in executor.calls] == ["sql_query"]

    def test_the_default_cap_allows_two_activations(self):
        """§4.6 — `MAX_SKILL_ACTIVATIONS_PER_TURN` counts replacements too."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, _ = build_orchestrator(routed_to(None), responder=responder)

        result = orch.run_turn(conversation_with("hello"))

        assert result.activations == 2
        assert result.final_skill == "tracker_read"
        # Two activations, no work: they are counted by the activation cap, not
        # by the work budget (SPEC-021 §4.4).
        assert result.outcome.tool_calls_executed == 0


class TestBudgets:
    """SPEC-021 §4.4 — an activation is orchestration and costs no work call."""

    def test_activations_do_not_consume_the_tool_call_budget(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "1"})]
                ),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("python_calculate", {"expression": "2"})]
                ),
                ScriptedModelResponse(text="done"),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to(None),
            responder=responder,
            handlers={
                "sql_query": lambda a: {"ok": True},
                "python_calculate": lambda a: {"ok": True},
            },
            max_tool_calls=2,
        )

        result = orch.run_turn(conversation_with("revenue"))

        # Under SPEC-018 the activation ate one of the two calls and
        # python_calculate never ran. Both work calls now fit.
        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.outcome.reason is TerminationReason.FINAL_ANSWER
        assert result.outcome.tool_calls_executed == 2
        assert [name for name, _ in executor.calls] == ["sql_query", "python_calculate"]

    def test_a_spent_work_budget_answers_instead_of_stopping(self):
        """SPEC-021 §4.1, through the real activation handler."""

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "1"})]
                ),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"q": "2"})]
                ),
                ScriptedModelResponse(text="I ran out of tool calls before finishing."),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to(None),
            responder=responder,
            handlers={"sql_query": lambda a: {"ok": True}},
            max_tool_calls=1,
        )

        result = orch.run_turn(conversation_with("revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.outcome.reason is TerminationReason.BUDGET_EXHAUSTED
        assert result.outcome.final_text == "I ran out of tool calls before finishing."
        assert result.activations == 1
        assert len(executor.calls) == 1

    def test_the_activation_limit_is_still_recoverable(self):
        """SPEC-018 §4.7 survives: the third attempt is refused, not fatal.

        The loop's control budget is deliberately one wider than the activation
        cap, so the handler's own recoverable `activation_limit` result stays
        reachable rather than becoming dead code (SPEC-021 §4.4).
        """

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(tool_calls=[activate("code_workspace")]),
                ScriptedModelResponse(text="continuing with what is loaded"),
            ]
        )
        orch, _ = build_orchestrator(
            routed_to(None), responder=responder, max_skill_activations=2
        )

        result = orch.run_turn(conversation_with("hello"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.outcome.reason is TerminationReason.FINAL_ANSWER
        assert result.activations == 2
        assert result.final_skill == "tracker_read"

    def test_a_fourth_activation_attempt_exceeds_the_control_budget(self):
        """SPEC-021 §4.4 — what actually bounds a thrashing router.

        `MAX_SKILL_ACTIVATIONS_PER_TURN` cannot do this on its own: the handler
        refuses without incrementing its counter, so attempt after attempt would
        be free. The loop's own control-call budget is what closes the loop.
        """

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(tool_calls=[activate("tracker_read")]),
                ScriptedModelResponse(tool_calls=[activate("code_workspace")]),
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="I kept switching and did no work."),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to(None), responder=responder, max_skill_activations=2
        )

        result = orch.run_turn(conversation_with("hello"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.outcome.reason is TerminationReason.BUDGET_EXHAUSTED
        assert result.activations == 2
        assert executor.calls == []


class TestTracing:
    """§7.1/13."""

    def test_activation_emits_the_full_event_set(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        trace = MemoryTraceSink()
        orch, _ = build_orchestrator(
            routed_to("tracker_read"), responder=responder, trace=trace
        )

        result = orch.run_turn(conversation_with("read A-1 then check revenue"))

        activated = [e for e in trace.events if e["event"] == "skill_activated"]
        assert len(activated) == 1
        assert activated[0]["skill"] == "sales_analysis"
        assert activated[0]["skill_version"] == "1"
        assert activated[0]["skill_fingerprint"] == "sha256:sales"
        assert activated[0]["replaced_skill"] == "tracker_read"
        assert activated[0]["activation_index"] == 1
        assert activated[0]["source"] == "tool"
        assert activated[0]["recomposed"] is True
        assert activated[0]["turn_id"] == result.outcome.turn_id
        # The mid-turn view is reconstructable from the trace alone.
        loaded = [e["skill"] for e in trace.events if e["event"] == "skill_loaded"]
        assert loaded == ["tracker_read", "sales_analysis"]
        resolved = [
            e for e in trace.events if e["event"] == "skill_toolset_resolved"
        ]
        assert resolved[-1]["available_tools"] == [
            "sql_query",
            "python_calculate",
            ACTIVATE_SKILL_TOOL_NAME,
        ]
        execution = next(
            e
            for e in trace.events
            if e["event"] == "tool_execution_started"
            and e["tool_name"] == ACTIVATE_SKILL_TOOL_NAME
        )
        assert execution["control_tool"] is True

    def test_turn_finished_reports_initial_final_and_activation_count(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        trace = MemoryTraceSink()
        orch, _ = build_orchestrator(
            routed_to("tracker_read"), responder=responder, trace=trace
        )

        orch.run_turn(conversation_with("read A-1 then check revenue"))

        finished = [e for e in trace.events if e["event"] == "turn_finished"]
        assert len(finished) == 1
        assert finished[0]["initial_skill"] == "tracker_read"
        assert finished[0]["selected_skill"] == "sales_analysis"
        assert finished[0]["skill_version"] == "1"
        assert finished[0]["skill_activations"] == 1

    def test_a_turn_without_activation_reports_zero(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="done")])
        trace = MemoryTraceSink()
        orch, _ = build_orchestrator(
            routed_to("tracker_read"), responder=responder, trace=trace
        )

        orch.run_turn(conversation_with("read A-1"))

        finished = next(e for e in trace.events if e["event"] == "turn_finished")
        assert finished["initial_skill"] == "tracker_read"
        assert finished["selected_skill"] == "tracker_read"
        assert finished["skill_activations"] == 0
        assert not [e for e in trace.events if e["event"] == "skill_activated"]


class TestNoActivationIsByteIdentical:
    """§7.1/10 — SPEC-012's model-facing context, preserved exactly."""

    def test_skill_turn_context_matches_the_spec_012_composition(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="done")])
        orch, _ = build_orchestrator(routed_to("sales_analysis"), responder=responder)
        conversation = conversation_with("revenue by genre")

        result = orch.run_turn(conversation)

        sent, _tools = responder.calls[0]
        expected = conversation.messages_for_model(
            additional_system=compose_active_skill(SALES_SPEC)
        )
        assert sent == expected
        assert result.activations == 0
        assert result.final_skill == "sales_analysis"

    def test_no_skill_turn_context_matches_the_plain_composition(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="done")])
        orch, _ = build_orchestrator(routed_to(None), responder=responder)
        conversation = conversation_with("hello")

        orch.run_turn(conversation)

        sent, _tools = responder.calls[0]
        assert sent == conversation.messages_for_model()


class TestHandlerContract:
    """Unit-level checks of `SkillActivationHandler` itself."""

    def make_handler(self, **overrides) -> SkillActivationHandler:
        registry = skill_registry()
        config = dict(
            skill_registry=registry,
            tool_registry=make_tool_registry(*TOOL_NAMES),
            executor=FakeToolExecutor(),
            declaration=build_activate_skill_declaration(registry.catalog()),
            max_activations=2,
            run_id="run-1",
            turn_id="turn-1",
        )
        config.update(overrides)
        return SkillActivationHandler(**config)

    def test_receipt_shape(self):
        handler = self.make_handler(initial_skill=TRACKER_SPEC)

        control = handler.handle(ACTIVATE_SKILL_TOOL_NAME, {"name": "sales_analysis"})

        assert control.result == {
            "ok": True,
            "skill": "sales_analysis",
            "version": "1",
            "replaced": "tracker_read",
            "available_tools": [
                "sql_query",
                "python_calculate",
                ACTIVATE_SKILL_TOOL_NAME,
            ],
        }
        assert control.tools is not None
        assert control.executor is not None
        assert control.system_suffix == compose_active_skill(SALES_SPEC)

    def test_names_is_exactly_the_reserved_name(self):
        assert self.make_handler().names == frozenset({ACTIVATE_SKILL_TOOL_NAME})

    def test_initial_skill_survives_a_replacement(self):
        handler = self.make_handler(initial_skill=TRACKER_SPEC)

        handler.handle(ACTIVATE_SKILL_TOOL_NAME, {"name": "sales_analysis"})

        assert handler.initial_skill == "tracker_read"
        assert handler.active_skill == "sales_analysis"
        assert handler.active_skill_version == "1"
        assert handler.activations == 1

    def test_an_unknown_name_is_reported_before_the_cap(self):
        """§4.7 — a nonexistent skill is never reported as an exhausted budget."""

        handler = self.make_handler(max_activations=0, initial_skill=TRACKER_SPEC)

        control = handler.handle(ACTIVATE_SKILL_TOOL_NAME, {"name": "nope"})

        assert control.result["error"] == "unknown_skill"
        assert control.result["requested"] == "nope"
        assert control.tools is None
        assert control.executor is None
        assert control.system_suffix is None

    @pytest.mark.parametrize("arguments", [{}, {"name": None}, {"name": 7}, {"skill": "x"}])
    def test_malformed_arguments_are_recoverable(self, arguments):
        handler = self.make_handler()

        control = handler.handle(ACTIVATE_SKILL_TOOL_NAME, arguments)

        assert control.result["ok"] is False
        assert control.result["error"] == "unknown_skill"
        assert handler.activations == 0

    def test_a_refused_activation_does_not_consume_the_budget(self):
        handler = self.make_handler()

        handler.handle(ACTIVATE_SKILL_TOOL_NAME, {"name": "nope"})

        assert handler.activations == 0
        assert handler.active_skill is None


class TestBaselineToolsSurviveActivation:
    """PATCH-012-02 — activation replaces domain tools, never the host baseline."""

    def test_replacement_keeps_the_baseline_and_drops_the_old_domain_tools(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="Rock."),
            ]
        )
        orch, _ = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        result = orch.run_turn(conversation_with("read A-1 then the revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert declared_names(responder, 0) == [
            "mcp_tracker__issue_get",
            "mcp_time__get_current_time",
            ACTIVATE_SKILL_TOOL_NAME,
        ]
        assert declared_names(responder, 1) == [
            "sql_query",
            "python_calculate",
            "mcp_time__get_current_time",
            ACTIVATE_SKILL_TOOL_NAME,
        ]

    def test_baseline_tool_executes_after_a_replacement(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("mcp_time__get_current_time", {})]
                ),
                ScriptedModelResponse(text="Done."),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            handlers={"mcp_time__get_current_time": lambda a: {"ok": True}},
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        result = orch.run_turn(conversation_with("read A-1 then the revenue"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert [name for name, _ in executor.calls] == ["mcp_time__get_current_time"]

    def test_repeated_activation_does_not_duplicate_declarations(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="Rock."),
            ]
        )
        orch, _ = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        orch.run_turn(conversation_with("revenue please"))

        names = declared_names(responder, 2)
        assert names.count("mcp_time__get_current_time") == 1
        assert names.count(ACTIVATE_SKILL_TOOL_NAME) == 1

    def test_replacement_still_forbids_the_previous_skills_domain_tools(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("mcp_tracker__issue_get", {})]
                ),
            ]
        )
        orch, executor = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            handlers={"mcp_tracker__issue_get": lambda a: {"ok": True}},
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        result = orch.run_turn(conversation_with("read A-1"))

        assert result.outcome.reason is TerminationReason.SKILL_POLICY_VIOLATION
        assert executor.calls == []

    def test_toolset_trace_after_activation_separates_the_two_sources(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[activate("sales_analysis")]),
                ScriptedModelResponse(text="Rock."),
            ]
        )
        trace = MemoryTraceSink()
        orch, _ = build_orchestrator(
            routed_to("tracker_read"),
            responder=responder,
            trace=trace,
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        orch.run_turn(conversation_with("revenue please"))

        resolved = [e for e in trace.events if e["event"] == "skill_toolset_resolved"]
        assert resolved[-1]["skill_tools"] == ["sql_query", "python_calculate"]
        assert resolved[-1]["baseline_tools"] == ["mcp_time__get_current_time"]
        assert resolved[-1]["available_tools"] == [
            "sql_query",
            "python_calculate",
            "mcp_time__get_current_time",
            ACTIVATE_SKILL_TOOL_NAME,
        ]

    def test_receipt_reports_the_effective_view_including_baseline(self):
        handler = SkillActivationHandler(
            skill_registry=skill_registry(),
            tool_registry=make_tool_registry(*TOOL_NAMES),
            executor=FakeToolExecutor(),
            declaration=build_activate_skill_declaration(skill_registry().catalog()),
            max_activations=2,
            run_id="run-1",
            turn_id="turn-1",
            initial_skill=TRACKER_SPEC,
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        control = handler.handle(ACTIVATE_SKILL_TOOL_NAME, {"name": "sales_analysis"})

        # The model must not be told Time is gone while it is still declared.
        assert control.result["available_tools"] == [
            "sql_query",
            "python_calculate",
            "mcp_time__get_current_time",
            ACTIVATE_SKILL_TOOL_NAME,
        ]
        assert control.result["available_tools"] == [
            t["function"]["name"] for t in control.tools
        ]

    def test_already_active_receipt_also_reports_the_effective_view(self):
        handler = SkillActivationHandler(
            skill_registry=skill_registry(),
            tool_registry=make_tool_registry(*TOOL_NAMES),
            executor=FakeToolExecutor(),
            declaration=build_activate_skill_declaration(skill_registry().catalog()),
            max_activations=2,
            run_id="run-1",
            turn_id="turn-1",
            initial_skill=SALES_SPEC,
            baseline_tools=BASELINE_TOOL_NAMES,
        )

        control = handler.handle(ACTIVATE_SKILL_TOOL_NAME, {"name": "sales_analysis"})

        # Nothing is recomposed on this branch, but the receipt still has to
        # describe the view the turn is actually running under.
        assert control.tools is None
        assert control.result["available_tools"] == [
            "sql_query",
            "python_calculate",
            "mcp_time__get_current_time",
            ACTIVATE_SKILL_TOOL_NAME,
        ]
