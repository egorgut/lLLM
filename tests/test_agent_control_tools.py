"""Control-tool seam tests (SPEC-018 §7.1 items 1, 2, 9; PATCH-018-01).

These exercise `AgentRunner`'s *general* notion of a host-handled tool — a tool
the loop must not dispatch to the `ToolExecutor`, because handling it changes the
turn's own view. Nothing here imports `skill_runtime`: the loop is skill-agnostic
by design (SPEC-018 §3.2), and these tests fail if that ever stops being true.

`tests/test_skill_activation.py` covers the one real implementation of the seam.
"""

from typing import Any

from agent import ControlResult
from reliability import TerminationReason, TurnStatus
from tests.support import (
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    make_tool_call,
)
from tests.test_agent_runner import BASE_MESSAGES, make_runner

CONTROL_TOOL = "control_tool"


class RecordingControlHandler:
    """A minimal `ControlToolHandler` double that records every invocation.

    Returns a preset `ControlResult` per call (holding on the last one), so a
    test can script "this activation replaces the view, that one reports a
    recoverable error and changes nothing".
    """

    names = frozenset({CONTROL_TOOL})

    def __init__(self, results: list[ControlResult] | None = None) -> None:
        self._results = list(results) if results else []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handle(self, name: str, arguments: dict[str, Any]) -> ControlResult:
        self.calls.append((name, dict(arguments)))
        if not self._results:
            return ControlResult(result={"ok": True})
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def control_call(**arguments) -> Any:
    return make_tool_call(CONTROL_TOOL, arguments)


class TestControlDispatch:
    """§7.1/1 — a control call bypasses the executor and is still transcribed."""

    def test_handler_runs_instead_of_the_executor(self):
        handler = RecordingControlHandler(
            [ControlResult(result={"ok": True, "receipt": "yes"})]
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="x")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor()
        runner = make_runner(responder, executor, control_handler=handler)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert handler.calls == [(CONTROL_TOOL, {"name": "x"})]
        # The ordinary dispatch path was never taken.
        assert executor.calls == []

    def test_control_result_is_appended_to_the_turn_transcript(self):
        handler = RecordingControlHandler(
            [ControlResult(result={"ok": True, "receipt": "yes"})]
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="x")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        runner.run_turn(BASE_MESSAGES)

        # The second model request must see the control call and its result,
        # exactly as it would for an ordinary tool.
        second_request_messages, _tools = responder.calls[1]
        assert second_request_messages[-2]["role"] == "assistant"
        assert (
            second_request_messages[-2]["tool_calls"][0]["function"]["name"]
            == CONTROL_TOOL
        )
        assert second_request_messages[-1] == {
            "role": "tool",
            "tool_name": CONTROL_TOOL,
            "content": '{"ok": true, "receipt": "yes"}',
        }

    def test_renderer_sees_the_control_call_and_result(self):
        handler = RecordingControlHandler([ControlResult(result={"ok": True})])
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="x")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        renderer = RecordingRenderer()
        runner = make_runner(responder, control_handler=handler, renderer=renderer)

        runner.run_turn(BASE_MESSAGES)

        # A control call is rendered against the budget it is actually charged
        # against — max_control_calls, not the work budget (SPEC-021 §4.6).
        assert renderer.tool_calls == [(CONTROL_TOOL, 1, 3)]
        assert renderer.tool_results == [{"ok": True}]


class TestPreDispatchPoliciesStillApply:
    """§7.1/2 — every policy that precedes dispatch runs before the handler."""

    def test_parallel_control_calls_are_rejected_before_the_handler(self):
        handler = RecordingControlHandler()
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[control_call(name="a"), control_call(name="b")]
                )
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.reason is TerminationReason.PARALLEL_TOOL_CALLS
        assert handler.calls == []

    def test_repeated_identical_control_call_stops_before_the_handler(self):
        handler = RecordingControlHandler()
        responder = ScriptedResponder(
            [ScriptedModelResponse(tool_calls=[control_call(name="a")])] * 3
        )
        runner = make_runner(responder, control_handler=handler)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.reason is TerminationReason.REPEATED_TOOL_CALL
        # max_identical_tool_calls=2: the third one never reaches the handler.
        assert len(handler.calls) == 2

    def test_the_control_budget_stops_a_control_call(self):
        handler = RecordingControlHandler()
        # Distinct arguments each time, so the repetition guard never fires and
        # the budget is the policy under test.
        responder = ScriptedResponder(
            # Two fit the budget, the third is refused; the fourth response is
            # the forced answer.
            [ScriptedModelResponse(tool_calls=[control_call(name=str(i))]) for i in range(3)]
            + [ScriptedModelResponse(text="out of switches")]
        )
        runner = make_runner(responder, control_handler=handler, max_control_calls=2)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.BUDGET_EXHAUSTED
        assert len(handler.calls) == 2


class TestControlCallsHaveTheirOwnBudget:
    """SPEC-021 §4.4 — an activation is orchestration, and is not charged as work.

    SPEC-018 charged it against `MAX_TOOL_CALLS_PER_TURN` on the reasoning that
    hiding it would let a thrashing model run unbounded. That concern is real and
    is answered here by a second bound rather than by taxing the work budget: a
    turn's capacity for work no longer depends on how well the router guessed.
    """

    def test_control_calls_do_not_consume_the_work_budget(self):
        handler = RecordingControlHandler()
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(tool_calls=[control_call(name="b")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.FINAL_ANSWER
        assert outcome.tool_calls_executed == 0
        assert len(handler.calls) == 2

    def test_a_full_work_budget_is_still_available_after_activations(self):
        """The PATCH-018-02 coupling, gone: activating no longer costs a step."""

        handler = RecordingControlHandler()
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(tool_calls=[make_tool_call("python_calculate", {"expression": "2"})]),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        runner = make_runner(
            responder, executor, control_handler=handler, max_tool_calls=2
        )

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.FINAL_ANSWER
        # Both work calls ran even though an activation happened first.
        assert outcome.tool_calls_executed == 2
        assert len(handler.calls) == 1
        assert len(executor.calls) == 2

    def test_the_two_budgets_are_independent(self):
        """A spent work budget does not stop a control call, and vice versa."""

        handler = RecordingControlHandler()
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        runner = make_runner(
            responder, executor, control_handler=handler, max_tool_calls=1
        )

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.FINAL_ANSWER
        assert len(handler.calls) == 1

    def test_the_loop_is_bounded_by_arithmetic_under_an_adversarial_script(self):
        """SPEC-021 §7.2/9 — the closed-form maximum on model requests holds.

        The script asks for a tool at every single opportunity, alternating
        control and work calls with distinct arguments so neither the repetition
        guard nor either budget's own refusal can be avoided.
        """

        max_tool_calls, max_control_calls = 3, 2
        handler = RecordingControlHandler()
        executor = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})

        counter = iter(range(1000))

        def responder(messages, tools):
            n = next(counter)
            if not list(tools):
                # The forced request: the only way this turn can end.
                return ScriptedModelResponse(text="stopping here")
            call = (
                control_call(name=f"c{n}")
                if n % 2
                else make_tool_call("python_calculate", {"expression": str(n)})
            )
            return ScriptedModelResponse(tool_calls=[call])

        runner = make_runner(
            responder,
            executor,
            control_handler=handler,
            max_tool_calls=max_tool_calls,
            max_control_calls=max_control_calls,
        )
        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.BUDGET_EXHAUSTED
        # model requests in the loop
        #   <= max_tool_calls + max_control_calls + 1 (the refused step) + 1 (forced)
        bound = max_tool_calls + max_control_calls + 2
        assert outcome.model_requests <= bound
        assert len(executor.calls) <= max_tool_calls
        assert len(handler.calls) <= max_control_calls


class TestViewReplacement:
    """Each `ControlResult` field is independent; ``None`` means unchanged."""

    def test_new_declarations_reach_the_next_model_request(self):
        new_tools = ({"type": "function", "function": {"name": "sql_query"}},)
        handler = RecordingControlHandler(
            [ControlResult(result={"ok": True}, tools=new_tools)]
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        runner.run_turn(BASE_MESSAGES)

        first_tools = responder.calls[0][1]
        second_tools = responder.calls[1][1]
        assert [t["function"]["name"] for t in first_tools] == ["python_calculate"]
        assert [t["function"]["name"] for t in second_tools] == ["sql_query"]

    def test_new_executor_receives_every_subsequent_call(self):
        replacement = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        handler = RecordingControlHandler(
            [ControlResult(result={"ok": True}, executor=replacement)]
        )
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(text="done"),
            ]
        )
        original = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        runner = make_runner(responder, original, control_handler=handler)

        runner.run_turn(BASE_MESSAGES)

        assert original.calls == []
        assert [name for name, _ in replacement.calls] == ["python_calculate"]

    def test_system_suffix_replaces_rather_than_stacks(self):
        handler = RecordingControlHandler(
            [
                ControlResult(result={"ok": True}, system_suffix="FIRST"),
                ControlResult(result={"ok": True}, system_suffix="SECOND"),
            ]
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(tool_calls=[control_call(name="b")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        runner.run_turn(
            [{"role": "system", "content": "BASE"}, {"role": "user", "content": "hi"}]
        )

        contents = [messages[0]["content"] for messages, _ in responder.calls]
        assert contents == ["BASE", "BASE\n\nFIRST", "BASE\n\nSECOND"]

    def test_a_result_only_control_call_changes_nothing(self):
        handler = RecordingControlHandler(
            [ControlResult(result={"ok": False, "error": "recoverable"})]
        )
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[control_call(name="a")]),
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        runner = make_runner(responder, executor, control_handler=handler)

        outcome = runner.run_turn(
            [{"role": "system", "content": "BASE"}, {"role": "user", "content": "hi"}]
        )

        assert outcome.status is TurnStatus.COMPLETED
        # Same system block, same declarations, same executor throughout.
        assert {messages[0]["content"] for messages, _ in responder.calls} == {"BASE"}
        assert {
            tuple(t["function"]["name"] for t in tools) for _m, tools in responder.calls
        } == {("python_calculate",)}
        assert [name for name, _ in executor.calls] == ["python_calculate"]


class TestControlCallsPreserveTheirReasoning:
    """SPEC-020 §4.7 — a control tool is an ordinary decision for preservation.

    The loop must not learn that some tool calls are "the host's" for this
    purpose: whatever a decision reasoned about, it reasoned before choosing the
    call it chose, and the next decision needs that either way. What preservation
    must *not* do is grant that reasoning any authority -- it travels as
    historical assistant state, beside a call the loop had already accepted under
    every policy it applies to any other.
    """

    def test_the_reasoning_behind_a_control_call_travels_with_it(self):
        handler = RecordingControlHandler(
            [ControlResult(result={"ok": True}, system_suffix="new view")]
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    thinking="this is really a sales question",
                    tool_calls=[control_call(skill="sales_analysis")],
                ),
                ScriptedModelResponse(text="Done."),
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        runner.run_turn(BASE_MESSAGES)

        second_request = responder.calls[1][0]
        [assistant] = [m for m in second_request if m.get("role") == "assistant"]
        assert assistant["thinking"] == "this is really a sales question"
        # And the host block is still the authority on what the model is told.
        assert second_request[0]["content"].endswith("new view")

    def test_preserved_reasoning_does_not_stack_or_bypass_the_host_block(self):
        # Two activations in one turn: the system message must still carry
        # exactly one host block, chosen by SPEC-018's replacement rule and not
        # by anything the model reasoned in between.
        handler = RecordingControlHandler(
            [
                ControlResult(result={"ok": True}, system_suffix="first view"),
                ControlResult(result={"ok": True}, system_suffix="second view"),
            ]
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    thinking="try sales", tool_calls=[control_call(skill="a")]
                ),
                ScriptedModelResponse(
                    thinking="no, tracker", tool_calls=[control_call(skill="b")]
                ),
                ScriptedModelResponse(text="Done."),
            ]
        )
        runner = make_runner(responder, control_handler=handler)

        runner.run_turn([{"role": "system", "content": "base"}, *BASE_MESSAGES])

        third_request = responder.calls[2][0]
        assert third_request[0]["content"] == "base\n\nsecond view"
        assert [m["thinking"] for m in third_request if m.get("role") == "assistant"] == [
            "try sales",
            "no, tracker",
        ]


class TestControlToolIsOptional:
    def test_a_runner_without_a_handler_dispatches_normally(self):
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        runner = make_runner(responder, executor)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert [name for name, _ in executor.calls] == ["python_calculate"]

    def test_a_non_control_name_still_reaches_the_executor(self):
        handler = RecordingControlHandler()
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor({"python_calculate": lambda a: {"ok": True}})
        runner = make_runner(responder, executor, control_handler=handler)

        runner.run_turn(BASE_MESSAGES)

        assert handler.calls == []
        assert [name for name, _ in executor.calls] == ["python_calculate"]
