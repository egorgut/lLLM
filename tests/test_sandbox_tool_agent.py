"""The sandbox tool inside the existing agent loop (SPEC-016 §13-14, §22).

`sandbox_execute` is an ordinary tool call and gets no special treatment. These
tests prove that by pointing the existing reliability machinery at it: the
four-call budget, consecutive-repeat detection, the whole-turn deadline, and the
rule that an unsuccessful turn persists nothing.

The real handler runs behind a real runtime over a fake Docker CLI, so the
arguments the loop fingerprints are the arguments a model would actually send.
"""

from agent import AgentRunner
from conversation import Conversation
from reliability import TerminationReason, TurnStatus
from sandbox_tool import SANDBOX_EXECUTE_SPEC
from skill_runtime.models import SkillSelection
from skill_runtime.orchestrator import SkillTurnOrchestrator
from support import (
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    ScriptedSkillRouter,
    make_tool_call,
)
from support_sandbox import completed_exec
from support_sandbox_tool import RUN_ID, make_harness
from tools.executor import ToolExecutor
from tools.registry import ToolRegistry
from tracing import MemoryTraceSink, NullTraceSink

GOOD_SOURCE = "print('ok')"
BAD_SOURCE = "prin('oops')"


def sandbox_call(source: str):
    return make_tool_call("sandbox_execute", {"language": "python", "source": source})


def build_runner(harness, responder, *, max_tool_calls=4, clock=None, trace_sink=None):
    """An AgentRunner wired to the real sandbox handler, exactly as app.py does."""

    registry = ToolRegistry()
    registry.register(SANDBOX_EXECUTE_SPEC)
    executor = ToolExecutor(registry)
    executor.register_handler("sandbox_execute", harness.handler)
    return AgentRunner(
        respond=responder,
        executor=executor,
        tools=registry.to_ollama_tools(),
        renderer=RecordingRenderer(),
        run_id=RUN_ID,
        max_tool_calls=max_tool_calls,
        max_identical_tool_calls=2,
        model_request_timeout_seconds=5,
        tool_execution_timeout_seconds=5,
        agent_turn_timeout_seconds=30,
        trace_sink=trace_sink or NullTraceSink(),
        clock=clock or harness.clock,
        redacted_argument_tools=frozenset({"sandbox_execute"}),
    )


def run(harness, runner, turn_id="turn-a", *, remaining=120.0):
    context = harness.open_turn(turn_id, remaining=remaining)
    return runner.run_turn(
        [{"role": "user", "content": "make me a file"}], turn_context=context
    )


class TestSuccessfulTurn:
    def test_one_call_answers(self, tmp_path):
        harness = make_harness(
            tmp_path, exec_result=completed_exec(stdout="ok\n"), tar_files={"o.csv": b"1"}
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call(GOOD_SOURCE)]),
                ScriptedModelResponse(text="Created o.csv."),
            ]
        )

        outcome = run(harness, build_runner(harness, responder))

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.tool_calls_executed == 1
        assert harness.turn_dir().joinpath("o.csv").exists()

    def test_a_correction_after_a_failure_succeeds(self, tmp_path):
        """§22.4: two calls, the first publishing nothing."""

        harness = make_harness(tmp_path, tar_files={"o.csv": b"1"})
        # The first exec fails; the second succeeds.
        results = iter(
            [completed_exec(stderr="SyntaxError", exit_code=1), completed_exec()]
        )
        original_stream = harness.runner.stream

        def stream(argv, **kwargs):
            harness.runner._exec_result = next(results)
            return original_stream(argv, **kwargs)

        harness.runner.stream = stream

        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call(BAD_SOURCE)]),
                ScriptedModelResponse(tool_calls=[sandbox_call(GOOD_SOURCE)]),
                ScriptedModelResponse(text="Fixed it and created o.csv."),
            ]
        )

        outcome = run(harness, build_runner(harness, responder))

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.tool_calls_executed == 2
        # Only the second call published anything.
        assert sorted(p.name for p in harness.turn_dir().iterdir()) == ["o.csv"]


class TestLoopGuards:
    def test_an_identical_repeat_stops_the_turn(self, tmp_path):
        harness = make_harness(tmp_path, exec_result=completed_exec(exit_code=1))
        responder = ScriptedResponder(
            [ScriptedModelResponse(tool_calls=[sandbox_call(BAD_SOURCE)])] * 3
        )

        outcome = run(harness, build_runner(harness, responder))

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.reason is TerminationReason.REPEATED_TOOL_CALL
        assert outcome.tool_calls_executed == 2

    def test_a_corrected_source_is_not_an_identical_call(self, tmp_path):
        harness = make_harness(tmp_path, exec_result=completed_exec(exit_code=1))
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call("print(1)")]),
                ScriptedModelResponse(tool_calls=[sandbox_call("print(2)")]),
                ScriptedModelResponse(tool_calls=[sandbox_call("print(3)")]),
                ScriptedModelResponse(text="Giving up; the script keeps failing."),
            ]
        )

        outcome = run(harness, build_runner(harness, responder))

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.tool_calls_executed == 3

    def test_the_four_call_budget_still_applies(self, tmp_path):
        harness = make_harness(tmp_path, exec_result=completed_exec(exit_code=1))
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call(f"print({n})")])
                for n in range(5)
            ]
            + [ScriptedModelResponse(text="The script still fails; I ran out of attempts.")]
        )

        outcome = run(harness, build_runner(harness, responder))

        # The fifth call is still never dispatched. What changed (SPEC-021 §4.1)
        # is that the turn ends with an answer rather than with nothing.
        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.reason is TerminationReason.BUDGET_EXHAUSTED
        assert outcome.tool_calls_executed == 4

    def test_the_handler_refuses_to_start_near_the_deadline(self, tmp_path):
        """§13.3: the tool declines rather than leaving a container behind."""

        harness = make_harness(tmp_path)
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call(GOOD_SOURCE)]),
                ScriptedModelResponse(text="Not enough time remained to run code."),
            ]
        )

        outcome = run(harness, build_runner(harness, responder), remaining=16.0)

        assert outcome.status is TurnStatus.COMPLETED
        assert harness.runner.calls == [], "no container may be created"

    def test_the_whole_turn_deadline_remains_authoritative(self, tmp_path):
        harness = make_harness(tmp_path)
        responder = ScriptedResponder(
            [ScriptedModelResponse(tool_calls=[sandbox_call(GOOD_SOURCE)])] * 3
        )
        runner = build_runner(harness, responder)
        context = harness.open_turn(remaining=30.0)
        harness.clock.advance(31)

        outcome = runner.run_turn(
            [{"role": "user", "content": "hi"}], turn_context=context
        )

        assert outcome.status is TurnStatus.TIMED_OUT
        assert outcome.reason is TerminationReason.TURN_TIMEOUT


class TestArgumentRedaction:
    """§15.3: the generic tool event must not preview a sandbox call's content.

    `sql_query`'s argument is a parameter worth reading in a trace. A
    `sandbox_execute` argument is the user's data expressed as code plus the
    user's files, so the generic preview is suppressed by name.
    """

    def test_the_source_never_reaches_the_generic_tool_event(self, tmp_path):
        harness = make_harness(tmp_path)
        sink = MemoryTraceSink()
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[sandbox_call("SECRET_MARKER = 'in the source'")]
                ),
                ScriptedModelResponse(text="done"),
            ]
        )

        run(harness, build_runner(harness, responder, trace_sink=sink))

        requested = [e for e in sink.events if e["event"] == "tool_call_requested"][0]
        assert requested["tool_name"] == "sandbox_execute"
        assert requested["arguments_redacted"] is True
        assert requested["arguments_preview"] == ""
        assert requested["arguments_sha256"] is None
        assert "SECRET_MARKER" not in repr(sink.events)

    def test_input_file_content_never_reaches_the_generic_tool_event(self, tmp_path):
        harness = make_harness(tmp_path)
        sink = MemoryTraceSink()
        call = make_tool_call(
            "sandbox_execute",
            {
                "language": "python",
                "source": GOOD_SOURCE,
                "input_files": [{"name": "a.txt", "content": "SECRET-FILE-BODY"}],
            },
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[call]),
                ScriptedModelResponse(text="done"),
            ]
        )

        run(harness, build_runner(harness, responder, trace_sink=sink))

        assert "SECRET-FILE-BODY" not in repr(sink.events)

    def test_other_tools_keep_their_preview(self, tmp_path):
        """Redaction is opt-in by name, not a blanket change to tracing."""

        harness = make_harness(tmp_path)
        sink = MemoryTraceSink()
        registry = ToolRegistry()
        registry.register(SANDBOX_EXECUTE_SPEC)
        executor = ToolExecutor(registry)
        executor.register_handler("sandbox_execute", harness.handler)
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call("VISIBLE = 1")]),
                ScriptedModelResponse(text="done"),
            ]
        )
        runner = AgentRunner(
            respond=responder,
            executor=executor,
            tools=registry.to_ollama_tools(),
            renderer=RecordingRenderer(),
            run_id=RUN_ID,
            max_tool_calls=4,
            model_request_timeout_seconds=5,
            tool_execution_timeout_seconds=5,
            agent_turn_timeout_seconds=30,
            trace_sink=sink,
            clock=harness.clock,
            # No redaction configured.
        )

        run(harness, runner)

        requested = [e for e in sink.events if e["event"] == "tool_call_requested"][0]
        assert requested["arguments_redacted"] is False
        assert "VISIBLE = 1" in requested["arguments_preview"]


class TestConversationPersistence:
    """§14, §22.13: sandbox protocol never becomes conversation history."""

    def _orchestrator(self, harness, responder):
        from skill_runtime.loader import SkillPackageLoader
        from config import SKILLS_ROOT
        from pathlib import Path
        from support import make_tool_registry

        registry = make_tool_registry(
            "sandbox_execute",
            "sql_query",
            "python_calculate",
            "mcp_time__get_current_time",
            "mcp_tracker__issue_get",
            "mcp_tracker__issues_find",
            "mcp_tracker__queue_get_metadata",
            "mcp_tracker__issue_get_comments",
        )
        skill_registry = SkillPackageLoader().load_all(Path(SKILLS_ROOT), registry)
        executor = ToolExecutor(registry)
        executor.register_handler("sandbox_execute", harness.handler)
        return SkillTurnOrchestrator(
            skill_registry=skill_registry,
            router=ScriptedSkillRouter(
                SkillSelection("code_workspace", "explicit", "explicit", 0, 1)
            ),
            tool_registry=registry,
            executor=executor,
            respond=responder,
            renderer_factory=RecordingRenderer,
            default_tools=registry.to_ollama_tools(),
            run_id=RUN_ID,
            max_tool_calls=4,
            max_identical_tool_calls=2,
            model_request_timeout_seconds=5,
            tool_execution_timeout_seconds=5,
            agent_turn_timeout_seconds=120,
            trace_sink=NullTraceSink(),
            clock=harness.clock,
            on_turn_context=harness.workspace.begin_turn,
        )

    def test_only_the_final_answer_is_stored(self, tmp_path):
        harness = make_harness(
            tmp_path,
            exec_result=completed_exec(stdout="SECRET-STDOUT"),
            tar_files={"out.csv": b"SECRET-ARTIFACT"},
        )
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[sandbox_call("SECRET_SOURCE = 1\nprint(1)")]
                ),
                ScriptedModelResponse(
                    text=f"Created data/artifacts/{RUN_ID}/turn-a/out.csv."
                ),
            ]
        )
        conversation = Conversation()
        conversation.add_user_message("Make me a CSV.")

        result = self._orchestrator(harness, responder).run_turn(conversation)
        assert result.outcome.status is TurnStatus.COMPLETED
        conversation.add_assistant_message(result.outcome.final_text)

        stored = repr(conversation.stored_messages)
        assert "SECRET_SOURCE" not in stored
        assert "SECRET-STDOUT" not in stored
        assert "SECRET-ARTIFACT" not in stored
        assert "sandbox_execute" not in stored
        assert [message["role"] for message in conversation.stored_messages] == [
            "user",
            "assistant",
        ]
        # An artifact reference is ordinary answer text and may persist.
        assert "out.csv" in conversation.stored_messages[-1]["content"]

    def test_the_turn_context_hook_binds_the_workspace(self, tmp_path):
        harness = make_harness(tmp_path, tar_files={"o.csv": b"1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call(GOOD_SOURCE)]),
                ScriptedModelResponse(text="done"),
            ]
        )
        conversation = Conversation()
        conversation.add_user_message("Make me a CSV.")

        result = self._orchestrator(harness, responder).run_turn(conversation)

        # The workspace adopted the orchestrator's turn id without app.py
        # knowing it in advance.
        assert harness.workspace.turn_id == result.outcome.turn_id
        assert (
            harness.artifact_root / RUN_ID / result.outcome.turn_id / "o.csv"
        ).exists()

    def test_a_failed_turn_rolls_the_artifacts_back(self, tmp_path):
        """The app.py transaction boundary, exercised in the same order."""

        harness = make_harness(tmp_path, tar_files={"o.csv": b"1"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[sandbox_call(GOOD_SOURCE)]),
                # An empty response fails the turn after the file was staged.
                ScriptedModelResponse(text=""),
            ]
        )
        conversation = Conversation()
        conversation.add_user_message("Make me a CSV.")

        result = self._orchestrator(harness, responder).run_turn(conversation)
        assert result.outcome.status is not TurnStatus.COMPLETED

        turn_dir = harness.artifact_root / RUN_ID / result.outcome.turn_id
        assert turn_dir.exists(), "staged during the turn"

        harness.workspace.rollback()
        conversation.remove_last_message()

        assert not turn_dir.exists()
        assert conversation.stored_messages == []
