"""CLI activity indicator regression tests (PATCH-010-01).

Two layers are covered: `ActivityIndicator` itself (TTY drawing, non-TTY
silence, thread discipline) and the lifecycle `CliRenderer` drives around every
durable line of turn output. The ordering assertions use a marker double rather
than real animation, so nothing here depends on frame timing.
"""

import io
import threading

import pytest

from agent import AgentRunner
from app import CliRenderer
from cli_activity import FRAMES, THREAD_NAME, ActivityIndicator
from reliability import TurnStatus
from tests.support import (
    FakeToolExecutor,
    ScriptedModelResponse,
    ScriptedResponder,
    make_tool_call,
)
from tracing import MemoryTraceSink


class FakeTty(io.StringIO):
    """A capturable stream that claims to be an interactive terminal."""

    def isatty(self) -> bool:
        return True


def visible_line(written: str) -> str:
    """What a terminal would still show after replaying `\\r` overwrites.

    The indicator erases itself by writing spaces over its own line, so the
    only way to assert "nothing is left behind" is to interpret the carriage
    returns the way a terminal does.
    """

    line = ""
    column = 0
    for char in written:
        if char == "\r":
            column = 0
        elif char == "\n":
            line, column = "", 0
        else:
            line = line[:column] + char + line[column + 1 :]
            column += 1
    return line.rstrip()


class MarkerIndicator:
    """An `ActivityIndicator` stand-in that prints where it would have drawn.

    Printing through `print` (like `CliRenderer` does) puts the markers in the
    same captured stream as the real output, so a test asserts the *interleaving*
    directly instead of comparing two separately recorded sequences.
    """

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1
        print("<start>")

    def stop(self) -> None:
        self.stops += 1
        print("<stop>")


def indicator_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == THREAD_NAME]


class TestNonTtyIsSilent:
    def test_start_and_stop_write_nothing_to_a_redirected_stream(self):
        stream = io.StringIO()  # a plain StringIO has isatty() -> False
        indicator = ActivityIndicator(stream)

        indicator.start()
        indicator.stop()

        assert indicator.enabled is False
        assert stream.getvalue() == ""
        assert indicator_threads() == []

    def test_stream_without_isatty_is_treated_as_non_tty(self):
        class Bare:
            def write(self, text):  # pragma: no cover - never called
                raise AssertionError("a disabled indicator must not write")

            def flush(self):  # pragma: no cover - never called
                raise AssertionError("a disabled indicator must not flush")

        indicator = ActivityIndicator(Bare())

        indicator.start()
        indicator.stop()

        assert indicator.enabled is False

    def test_no_carriage_return_artifacts_in_captured_turn_output(self, capsys):
        # The end-to-end guarantee for redirected output: a full turn rendered
        # with a disabled indicator contains no control characters at all.
        renderer = CliRenderer(ActivityIndicator(io.StringIO()))

        renderer.tool_call(make_tool_call("sql_query", {"query": "SELECT 1"}), 1, 4)
        renderer.tool_result({"ok": True})
        renderer.text("Rock.")

        captured = capsys.readouterr().out
        assert "\r" not in captured
        assert not any(frame in captured for frame in FRAMES)


class TestTtyDrawing:
    def test_start_draws_the_first_frame_immediately(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        indicator.start()
        try:
            # No tick has elapsed, so this frame must have been drawn by the
            # calling thread: activity is visible the moment the turn starts.
            assert stream.getvalue() == f"\r{FRAMES[0]} Working..."
            assert indicator.active is True
        finally:
            indicator.stop()

    def test_stop_leaves_the_line_empty(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        indicator.start()
        indicator.stop()

        assert visible_line(stream.getvalue()) == ""
        assert indicator.active is False

    def test_durable_output_after_stop_starts_on_a_clean_line(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        indicator.start()
        indicator.stop()
        stream.write("[result] {}")

        assert visible_line(stream.getvalue()) == "[result] {}"

    def test_start_and_stop_are_idempotent(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        indicator.start()
        indicator.start()
        assert len(indicator_threads()) == 1

        indicator.stop()
        indicator.stop()
        assert visible_line(stream.getvalue()) == ""
        assert indicator_threads() == []

    def test_animation_thread_is_a_daemon_and_does_not_survive_the_stop(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        indicator.start()
        assert [t.daemon for t in indicator_threads()] == [True]

        indicator.stop()

        assert indicator_threads() == []

    def test_animation_advances_while_active(self):
        # The one timing-dependent check, kept small and bounded: everything
        # else asserts on the synchronous first frame instead.
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=0.005)

        indicator.start()
        try:
            deadline = threading.Event()
            for _ in range(200):
                if FRAMES[1] in stream.getvalue():
                    break
                deadline.wait(0.01)
            assert FRAMES[1] in stream.getvalue()
        finally:
            indicator.stop()

        assert visible_line(stream.getvalue()) == ""

    def test_stop_from_another_thread_leaves_nothing_behind(self):
        # `CliRenderer.text` stops the indicator from the agent loop's worker
        # thread while the turn's other output is printed from the main one.
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=0.005)
        indicator.start()

        stopper = threading.Thread(target=indicator.stop, name="stopper")
        stopper.start()
        stopper.join()

        length_after_stop = len(stream.getvalue())
        threading.Event().wait(0.05)

        assert len(stream.getvalue()) == length_after_stop
        assert visible_line(stream.getvalue()) == ""
        assert indicator_threads() == []


class TestContextManager:
    def test_exit_clears_the_indicator_on_a_controlled_error(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        with indicator:
            pass  # a turn that returned a CANCELLED/FAILED outcome, no exception

        assert indicator.active is False
        assert visible_line(stream.getvalue()) == ""

    def test_exit_clears_the_indicator_on_an_unexpected_exception(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        with pytest.raises(RuntimeError):
            with indicator:
                raise RuntimeError("unexpected application error")

        assert indicator.active is False
        assert indicator_threads() == []
        assert visible_line(stream.getvalue()) == ""

    def test_exit_clears_the_indicator_on_a_keyboard_interrupt(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        with pytest.raises(KeyboardInterrupt):
            with indicator:
                raise KeyboardInterrupt

        assert indicator.active is False
        assert indicator_threads() == []
        assert visible_line(stream.getvalue()) == ""

    def test_error_text_printed_after_the_turn_is_readable(self):
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60)

        try:
            with indicator:
                raise RuntimeError("boom")
        except RuntimeError:
            stream.write("\nApplication error: Unexpected application error.")

        assert visible_line(stream.getvalue()) == (
            "Application error: Unexpected application error."
        )


class TestRendererLifecycle:
    def test_indicator_stops_before_the_final_answer_and_never_restarts(self, capsys):
        indicator = MarkerIndicator()
        renderer = CliRenderer(indicator)

        renderer.text("Rock ")
        renderer.text("wins.")

        assert capsys.readouterr().out == "<stop>\n\nQwen: Rock wins."
        # Once for the whole streamed answer, not once per chunk.
        assert (indicator.stops, indicator.starts) == (1, 0)

    def test_indicator_wraps_the_tool_block_and_the_result(self, capsys):
        indicator = MarkerIndicator()
        renderer = CliRenderer(indicator)

        renderer.tool_call(make_tool_call("sql_query", {"query": "SELECT 1"}), 1, 4)
        renderer.tool_result({"ok": True})

        assert capsys.readouterr().out == (
            "<stop>\n"
            "\n[tool 1/4] sql_query\n"
            "[args] query=SELECT 1\n"
            "<start>\n"
            "<stop>\n"
            "[result] ok\n"
            "<start>\n"
        )

    def test_default_renderer_prints_no_markers_of_its_own(self, capsys):
        # No indicator injected: nothing but the rendering itself, with no
        # control characters anywhere in it.
        renderer = CliRenderer()

        renderer.tool_call(make_tool_call("sql_query", {"query": "SELECT 1"}), 1, 4)
        renderer.tool_result({"ok": True})
        renderer.text("Rock.")

        assert capsys.readouterr().out == (
            "\n[tool 1/4] sql_query\n"
            "[args] query=SELECT 1\n"
            "[result] ok\n"
            "\nQwen: Rock."
        )


class TestMultiToolTurn:
    def test_activity_resumes_between_every_step_of_a_two_tool_turn(self, capsys):
        first = make_tool_call("python_calculate", {"expression": "1+1"})
        second = make_tool_call("python_calculate", {"expression": "2+2"})
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(tool_calls=[first]),
                ScriptedModelResponse(tool_calls=[second]),
                ScriptedModelResponse(text="Four."),
            ]
        )
        executor = FakeToolExecutor(
            {"python_calculate": lambda arguments: {"ok": True, "result": 4}}
        )
        indicator = MarkerIndicator()
        runner = AgentRunner(
            respond=responder,
            executor=executor,
            tools=[{"type": "function", "function": {"name": "python_calculate"}}],
            renderer=CliRenderer(indicator),
            run_id="run-1",
            max_tool_calls=4,
            max_identical_tool_calls=2,
            model_request_timeout_seconds=5,
            tool_execution_timeout_seconds=5,
            agent_turn_timeout_seconds=30,
            trace_sink=MemoryTraceSink(),
        )

        outcome = runner.run_turn([{"role": "user", "content": "hi"}])

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.final_text == "Four."
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            "<stop>",
            "",
            "[tool 1/4] python_calculate",
            "[args] expression=1+1",
            "<start>",  # tool execution
            "<stop>",
            "[result] ok",
            "  result  4",
            "<start>",  # the model's next decision
            "<stop>",
            "",
            "[tool 2/4] python_calculate",
            "[args] expression=2+2",
            "<start>",
            "<stop>",
            "[result] ok",
            "  result  4",
            "<start>",
            "<stop>",
            "",
            "Qwen: Four.",
        ]
        # Every restart was matched by a stop before durable output.
        assert indicator.stops == indicator.starts + 1

    def test_a_timed_out_turn_leaves_no_indicator_running(self, capsys):
        blocked = threading.Event()
        responder = ScriptedResponder([ScriptedModelResponse(block_on=blocked)])
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=0.005)
        runner = AgentRunner(
            respond=responder,
            executor=FakeToolExecutor(),
            tools=[],
            renderer=CliRenderer(indicator),
            run_id="run-1",
            max_tool_calls=4,
            max_identical_tool_calls=2,
            model_request_timeout_seconds=0.02,
            tool_execution_timeout_seconds=5,
            agent_turn_timeout_seconds=30,
            trace_sink=MemoryTraceSink(),
        )

        with indicator:
            outcome = runner.run_turn([{"role": "user", "content": "hi"}])
        blocked.set()  # release the abandoned worker so it cannot leak

        assert outcome.status is TurnStatus.TIMED_OUT
        assert indicator.active is False
        assert indicator_threads() == []
        assert visible_line(stream.getvalue()) == ""
        assert capsys.readouterr().out == ""
