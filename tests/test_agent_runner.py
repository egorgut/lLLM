import threading

import pytest

from agent import AgentRunner
from reliability import TerminationReason, TurnStatus
from tests.support import (
    FakeClock,
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    make_tool_call,
)
from tracing import MemoryTraceSink


def make_runner(responder, executor=None, *, trace=None, renderer=None, **overrides):
    config = dict(
        run_id="run-1",
        max_tool_calls=4,
        max_identical_tool_calls=2,
        model_request_timeout_seconds=5,
        tool_execution_timeout_seconds=5,
        agent_turn_timeout_seconds=30,
    )
    config.update(overrides)
    return AgentRunner(
        respond=responder,
        executor=executor or FakeToolExecutor(),
        tools=[{"type": "function", "function": {"name": "python_calculate"}}],
        renderer=renderer or RecordingRenderer(),
        trace_sink=trace if trace is not None else MemoryTraceSink(),
        **config,
    )


BASE_MESSAGES = [{"role": "user", "content": "hi"}]


class TestScenarioA_DirectCompletion:
    def test_final_answer_without_tools(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="Hello there.")])
        runner = make_runner(responder)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.FINAL_ANSWER
        assert outcome.final_text == "Hello there."
        assert outcome.tool_calls_executed == 0
        assert outcome.model_requests == 1


class TestScenarioB_ToolThenCompletion:
    def test_one_successful_tool_call_then_final_answer(self):
        call = make_tool_call("python_calculate", {"expression": "1+1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(text="The result is 2."),
            ]
        )
        executor = FakeToolExecutor({"python_calculate": lambda args: {"ok": True, "result": 2}})
        runner = make_runner(responder, executor)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.FINAL_ANSWER
        assert outcome.final_text == "The result is 2."
        assert outcome.tool_calls_executed == 1
        assert outcome.model_requests == 2
        assert executor.calls == [("python_calculate", {"expression": "1+1"})]

        # The second model request must see the tool result in its transcript.
        second_call_messages = responder.calls[1][0]
        assert any(m.get("role") == "tool" for m in second_call_messages)


class TestScenarioC_StructuredErrorThenRecovery:
    def test_structured_failure_does_not_stop_the_loop(self):
        call_a = make_tool_call("sql_query", {"query": "BAD"})
        call_b = make_tool_call("sql_query", {"query": "GOOD"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call_a]),
                ScriptedModelResponse(tool_calls=[call_b]),
                ScriptedModelResponse(text="Done."),
            ]
        )
        results = iter([{"ok": False, "error": {"type": "bad"}}, {"ok": True, "rows": []}])
        executor = FakeToolExecutor({"sql_query": lambda args: next(results)})
        runner = make_runner(responder, executor)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.tool_calls_executed == 2


class TestMultipleDifferentTools:
    def test_sequential_order_preserved(self):
        call_a = make_tool_call("python_calculate", {"expression": "1+1"})
        call_b = make_tool_call("sql_query", {"query": "SELECT 1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call_a]),
                ScriptedModelResponse(tool_calls=[call_b]),
                ScriptedModelResponse(text="Done."),
            ]
        )
        executor = FakeToolExecutor(
            {
                "python_calculate": lambda args: {"ok": True, "result": 2},
                "sql_query": lambda args: {"ok": True, "rows": []},
            }
        )
        runner = make_runner(responder, executor)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.tool_calls_executed == 2
        assert executor.calls == [
            ("python_calculate", {"expression": "1+1"}),
            ("sql_query", {"query": "SELECT 1"}),
        ]


class TestScenarioE_ToolCallBudgetExhausted:
    def test_fifth_call_not_executed(self):
        # Different arguments per call so repeated-call detection never fires
        # first; only the budget should stop this turn.
        calls = [
            make_tool_call("python_calculate", {"expression": str(i)}) for i in range(5)
        ]
        responder = ScriptedResponder([ScriptedModelResponse(tool_calls=[c]) for c in calls])
        executor = FakeToolExecutor(
            {"python_calculate": lambda args: {"ok": True, "result": 0}}
        )
        runner = make_runner(responder, executor, max_tool_calls=4)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.reason is TerminationReason.TOOL_CALL_LIMIT
        assert outcome.tool_calls_executed == 4
        assert len(executor.calls) == 4


class TestParallelToolCallsRejected:
    def test_none_execute(self):
        call_a = make_tool_call("python_calculate", {"expression": "1"})
        call_b = make_tool_call("python_calculate", {"expression": "2"})
        responder = ScriptedResponder([ScriptedModelResponse(tool_calls=[call_a, call_b])])
        executor = FakeToolExecutor({"python_calculate": lambda args: {"ok": True}})
        runner = make_runner(responder, executor)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.reason is TerminationReason.PARALLEL_TOOL_CALLS
        assert executor.calls == []


class TestEmptyResponse:
    def test_empty_text_and_no_tool_calls_fails(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="")])
        runner = make_runner(responder)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.FAILED
        assert outcome.reason is TerminationReason.EMPTY_MODEL_RESPONSE
        assert outcome.final_text is None


class TestScenarioD_RepeatedIdenticalCall:
    def test_third_identical_call_not_executed(self):
        call = make_tool_call("sql_query", {"query": "SELECT COUNT(*) FROM Track"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(tool_calls=[call]),
            ]
        )
        executor = FakeToolExecutor({"sql_query": lambda args: {"ok": True, "rows": []}})
        runner = make_runner(responder, executor, max_tool_calls=4, max_identical_tool_calls=2)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.reason is TerminationReason.REPEATED_TOOL_CALL
        assert outcome.tool_calls_executed == 2
        assert len(executor.calls) == 2


class TestRepetitionResetsOnDifferentCall:
    def test_a_a_b_a_completes(self):
        call_a = make_tool_call("sql_query", {"query": "A"})
        call_b = make_tool_call("sql_query", {"query": "B"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call_a]),
                ScriptedModelResponse(tool_calls=[call_a]),
                ScriptedModelResponse(tool_calls=[call_b]),
                ScriptedModelResponse(tool_calls=[call_a]),
                ScriptedModelResponse(text="Done."),
            ]
        )
        executor = FakeToolExecutor({"sql_query": lambda args: {"ok": True, "rows": []}})
        runner = make_runner(responder, executor, max_tool_calls=10, max_identical_tool_calls=2)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.tool_calls_executed == 4


class TestCanonicalArgumentOrder:
    def test_key_order_counts_as_the_same_call(self):
        call_a = make_tool_call("sql_query", {"a": 1, "b": 2})
        call_b = make_tool_call("sql_query", {"b": 2, "a": 1})
        call_c = make_tool_call("sql_query", {"b": 2, "a": 1})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call_a]),
                ScriptedModelResponse(tool_calls=[call_b]),
                ScriptedModelResponse(tool_calls=[call_c]),
            ]
        )
        executor = FakeToolExecutor({"sql_query": lambda args: {"ok": True}})
        runner = make_runner(responder, executor, max_identical_tool_calls=2)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.reason is TerminationReason.REPEATED_TOOL_CALL
        assert outcome.tool_calls_executed == 2


class TestScenarioF_ModelTimeout:
    def test_model_request_that_never_returns_times_out(self):
        never = threading.Event()
        responder = ScriptedResponder([ScriptedModelResponse(block_on=never)])
        runner = make_runner(responder, model_request_timeout_seconds=0.02)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.TIMED_OUT
        assert outcome.reason is TerminationReason.MODEL_TIMEOUT
        assert outcome.tool_calls_executed == 0
        assert outcome.final_text is None
        never.set()  # release the abandoned worker thread


class TestScenarioG_ToolTimeout:
    def test_tool_that_never_returns_times_out_without_a_next_model_request(self):
        never = threading.Event()
        call = make_tool_call("sql_query", {"query": "SELECT 1"})
        responder = ScriptedResponder(
            [ScriptedModelResponse(tool_calls=[call]), ScriptedModelResponse(text="unreachable")]
        )
        executor = FakeToolExecutor({"sql_query": lambda args: never.wait() or {"ok": True}})
        runner = make_runner(responder, executor, tool_execution_timeout_seconds=0.02)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.TIMED_OUT
        assert outcome.reason is TerminationReason.TOOL_TIMEOUT
        assert len(responder.calls) == 1  # no second model request was made
        never.set()


class TestMcpLikeHangPrecedence:
    def test_outer_deadline_wins_over_an_inner_fold_to_ok_false(self):
        # Simulates mcp_integration/client.py's own internal catch-all: a
        # handler that would eventually return {"ok": False} on its own
        # internal timeout, but only after blocking well past the outer,
        # host-owned tool-execution deadline. The outer deadline must win.
        never = threading.Event()

        def mcp_like_handler(arguments):
            never.wait()  # never set in this test -- simulates an inner hang
            return {"ok": False, "error": {"type": "mcp_call_failed"}}

        call = make_tool_call("mcp_time__get_current_time", {})
        responder = ScriptedResponder([ScriptedModelResponse(tool_calls=[call])])
        executor = FakeToolExecutor({"mcp_time__get_current_time": mcp_like_handler})
        runner = make_runner(responder, executor, tool_execution_timeout_seconds=0.02)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.TIMED_OUT
        assert outcome.reason is TerminationReason.TOOL_TIMEOUT
        never.set()


class TestScenarioH_TurnDeadline:
    def test_next_operation_does_not_start_once_turn_time_is_exhausted(self):
        # A fake clock lets AgentRunner's own deadline arithmetic see "no time
        # left" with no real waiting: the first tool handler advances the
        # clock past the turn deadline as a side effect, so the *second*
        # model request must never start.
        clock = FakeClock(start=0.0)
        call = make_tool_call("python_calculate", {"expression": "1"})
        responder = ScriptedResponder(
            [ScriptedModelResponse(tool_calls=[call]), ScriptedModelResponse(text="unreachable")]
        )

        def advancing_handler(arguments):
            clock.advance(100)
            return {"ok": True}

        executor = FakeToolExecutor({"python_calculate": advancing_handler})
        runner = make_runner(
            responder,
            executor,
            agent_turn_timeout_seconds=30,
            clock=clock,
        )

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.TIMED_OUT
        assert outcome.reason is TerminationReason.TURN_TIMEOUT
        assert len(responder.calls) == 1  # the second model request never started


class TestScenarioI_TraceFailureDoesNotChangeOutcome:
    def test_agent_still_completes_when_trace_sink_is_broken(self):
        warnings = []

        class BrokenOnce:
            def __init__(self):
                self._failed_once = False

            def emit(self, event):
                if not self._failed_once:
                    self._failed_once = True
                    raise OSError("disk full")

        responder = ScriptedResponder([ScriptedModelResponse(text="Hello.")])
        runner = AgentRunner(
            respond=responder,
            executor=FakeToolExecutor(),
            tools=[{"type": "function", "function": {"name": "python_calculate"}}],
            renderer=RecordingRenderer(),
            trace_sink=BrokenOnce(),
            run_id="run-1",
            max_tool_calls=4,
        )

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.final_text == "Hello."


class TestUserInterrupt:
    def test_keyboard_interrupt_becomes_cancelled_outcome(self):
        responder = ScriptedResponder([KeyboardInterrupt()])
        runner = make_runner(responder)

        outcome = runner.run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.CANCELLED
        assert outcome.reason is TerminationReason.USER_INTERRUPT
        assert outcome.final_text is None


class TestInputSnapshotNotMutated:
    def test_callers_message_list_is_unchanged(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="Hi.")])
        runner = make_runner(responder)
        messages = list(BASE_MESSAGES)
        original = list(messages)

        runner.run_turn(messages)

        assert messages == original


class TestTraceContract:
    def test_exactly_one_turn_finished_event(self):
        responder = ScriptedResponder([ScriptedModelResponse(text="Hi.")])
        trace = MemoryTraceSink()
        runner = make_runner(responder, trace=trace)

        runner.run_turn(BASE_MESSAGES)

        finished = [e for e in trace.events if e["event"] == "turn_finished"]
        assert len(finished) == 1

    def test_config_validation_rejects_bad_values(self):
        responder = ScriptedResponder([])
        with pytest.raises(ValueError):
            make_runner(responder, max_tool_calls=0)
