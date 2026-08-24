"""CLI activity indicator regression tests (PATCH-010-01, PATCH-010-03).

Two layers are covered: `ActivityIndicator` itself (TTY drawing, non-TTY
silence, thread discipline) and the lifecycle `CliRenderer` drives around every
durable line of turn output. The ordering assertions use a marker double rather
than real animation, so nothing here depends on frame timing.

The elapsed counter added by PATCH-010-03 is driven by an injected fake clock
for the same reason: what the line *says* must be assertable without waiting for
real seconds to pass.
"""

import io
import re
import threading

import pytest

from agent import AgentRunner
from app import CliRenderer, print_turn_time
from cli_activity import (
    FRAMES,
    THREAD_NAME,
    ActivityIndicator,
    format_elapsed,
    format_turn_time,
)
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


class FakeClock:
    """A monotonic clock the test moves by hand, never by sleeping."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MarkerIndicator:
    """An `ActivityIndicator` stand-in that prints where it would have drawn.

    Printing through `print` (like `CliRenderer` does) puts the markers in the
    same captured stream as the real output, so a test asserts the *interleaving*
    directly instead of comparing two separately recorded sequences.

    It models the real class's idempotency (PATCH-010-04): stopping something
    already stopped writes nothing and counts for nothing, exactly as
    `ActivityIndicator.stop()` returns without touching the stream when no
    animation thread is running. Without that, a marker would appear where the
    real terminal stays silent, and the interleaving assertions would be
    measuring the double rather than the renderer.

    It also starts out *running*, which is the state a renderer always meets in
    production: the chat loop enters `with indicator:` before `run_turn`, since
    the turn's first silence -- skill routing -- precedes any renderer.
    """

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self._active = True

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.starts += 1
        print("<start>")

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
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
            # The counter is part of that first frame (PATCH-010-03), starting
            # at zero because no measurable time has passed yet.
            assert stream.getvalue() == f"\r{FRAMES[0]} Working... 0.0s"
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


class TestElapsedFormatting:
    """The shared formatter behind both the counter and the footer."""

    def test_sub_minute_durations_keep_one_decimal(self):
        assert format_elapsed(0) == "0.0s"
        assert format_elapsed(4.23) == "4.2s"
        assert format_elapsed(52.61) == "52.6s"

    def test_a_minute_and_over_splits_into_minutes_and_seconds(self):
        assert format_elapsed(60) == "1m 00.0s"
        assert format_elapsed(201.37) == "3m 21.4s"

    def test_the_minute_boundary_does_not_render_sixty_seconds(self):
        # 59.96 rounds to 60.0 at one decimal; the line must not read "60.0s"
        # for a frame before flipping to "1m 00.0s".
        assert format_elapsed(59.94) == "59.9s"
        assert format_elapsed(59.96) == "1m 00.0s"

    def test_a_negative_clock_reading_never_renders_a_negative_duration(self):
        assert format_elapsed(-0.5) == "0.0s"

    def test_the_footer_carries_the_same_formatting(self):
        assert format_turn_time(52.61) == "[time] 52.6s"
        assert format_turn_time(201.37) == "[time] 3m 21.4s"


class TestElapsedCounter:
    def test_the_drawn_line_carries_the_elapsed_time(self):
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            # `start()` draws one frame synchronously on the calling thread, so
            # a stop/start pair redraws with the current reading -- the same
            # path a real turn takes around every durable line, and no sleep.
            clock.advance(4.2)
            indicator.stop()
            indicator.start()

            assert visible_line(stream.getvalue()) == f"{FRAMES[0]} Working... 4.2s"

    def test_the_counter_measures_the_turn_not_the_current_silence(self):
        # The indicator stops and restarts once per durable line. If the origin
        # were stamped by start(), the counter would reset at every [skill],
        # [tool ...] and [result] line and measure nothing useful.
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            clock.advance(10)
            indicator.stop()
            indicator.start()
            clock.advance(5)
            indicator.stop()
            indicator.start()

            assert visible_line(stream.getvalue()).endswith("15.0s")

    def test_a_new_turn_starts_from_zero_again(self):
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            clock.advance(30)
        clock.advance(120)  # the user reading the answer, between turns

        with indicator:
            assert visible_line(stream.getvalue()).endswith("0.0s")

    def test_between_turns_the_elapsed_reading_is_zero(self):
        clock = FakeClock()
        indicator = ActivityIndicator(FakeTty(), interval_seconds=60, clock=clock)

        assert indicator.elapsed_seconds == 0.0
        with indicator:
            clock.advance(7)
            assert indicator.elapsed_seconds == 7
        assert indicator.elapsed_seconds == 0.0

    def test_a_bare_start_outside_a_turn_still_counts_from_itself(self):
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        indicator.start()
        try:
            clock.advance(3)
            indicator.stop()
            indicator.start()
            assert visible_line(stream.getvalue()).endswith("3.0s")
        finally:
            indicator.stop()


class TestEraseWidthFollowsTheCounter:
    def test_stop_clears_a_line_that_grew_past_the_label(self):
        # The erase width used to be fixed at construction. A counter that grows
        # to "1m 41.0s" draws a wider line than that, so a fixed width would
        # leave its tail on screen after stop().
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            clock.advance(101)
            indicator.stop()
            indicator.start()
            assert visible_line(stream.getvalue()).endswith("1m 41.0s")

        assert visible_line(stream.getvalue()) == ""

    def test_durable_output_after_a_wide_counter_starts_on_a_clean_line(self, capsys):
        clock = FakeClock()
        indicator = ActivityIndicator(interval_seconds=60, enabled=True, clock=clock)
        renderer = CliRenderer(indicator)

        with indicator:
            clock.advance(3661)  # "61m 01.0s", far wider than the label
            indicator.stop()
            indicator.start()
            renderer.text("Rock wins.")

        assert visible_line(capsys.readouterr().out.split("\n")[0]) == ""

    def test_the_next_turn_does_not_inherit_the_previous_erase_width(self):
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            clock.advance(3661)
            indicator.stop()
            indicator.start()
        first_turn_bytes = len(stream.getvalue())

        with indicator:
            pass

        # A short turn erases a short line: the width is measured per turn, not
        # carried over from the widest line the session has ever drawn.
        second_turn = stream.getvalue()[first_turn_bytes:]
        widest_erase = max(len(run) for run in re.findall(r" +", second_turn))
        assert widest_erase == len(f"{FRAMES[0]} Working... 0.0s")


class TestNonTtySilenceCoversTheCounter:
    def test_a_redirected_stream_gets_nothing_however_long_the_turn_ran(self):
        # PATCH-010-01's guarantee, now covering the timer: a non-TTY capture
        # must stay byte-for-byte what it was, which is why the footer in
        # app.py is gated on `enabled` too.
        clock = FakeClock()
        stream = io.StringIO()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            clock.advance(3600)
            indicator.stop()
            indicator.start()

        assert stream.getvalue() == ""
        assert indicator.enabled is False


class TestTurnTimeFooter:
    """The footer `app.py` prints once a turn is over (PATCH-010-03)."""

    def test_an_interactive_terminal_gets_the_footer(self, capsys):
        indicator = ActivityIndicator(FakeTty(), interval_seconds=60)

        print_turn_time(indicator, 52.61)

        assert capsys.readouterr().out == "[time] 52.6s\n"

    def test_a_redirected_stream_gets_no_footer(self, capsys):
        # The same gate as the indicator itself, so a piped transcript stays
        # byte-for-byte what it was before this patch.
        indicator = ActivityIndicator(io.StringIO(), interval_seconds=60)

        print_turn_time(indicator, 52.61)

        assert capsys.readouterr().out == ""

    def test_the_footer_matches_what_the_counter_was_showing(self, capsys):
        # One formatter for both, so the last frame the user saw and the footer
        # they are left with cannot disagree.
        clock = FakeClock()
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=60, clock=clock)

        with indicator:
            clock.advance(101)
            indicator.stop()
            indicator.start()
            last_frame = visible_line(stream.getvalue())
            elapsed = indicator.elapsed_seconds
        print_turn_time(indicator, elapsed)

        assert last_frame.endswith("1m 41.0s")
        assert capsys.readouterr().out == "[time] 1m 41.0s\n"


class TestTextAfterAToolCall:
    """A turn shaped text -> tool call -> text (PATCH-010-04).

    The model comments before reaching for another tool, so answer text arrives
    twice in one turn. Observed live on a Tracker search whose first query found
    nothing: the model said so, then issued a second `issues_find`.
    """

    def _turn(self, indicator, capsys):
        call = make_tool_call("sql_query", {"query": "SELECT 1"})
        responder = ScriptedResponder(
            [
                # Text *and* a tool call in one response. This is the shape that
                # exposes the defect; a response that only calls a tool never
                # streams text at all, which is why it stayed hidden.
                ScriptedModelResponse(text="Ничего не нашёл, попробую иначе.",
                                      tool_calls=[call]),
                ScriptedModelResponse(text="Вот таблица."),
            ]
        )
        runner = AgentRunner(
            respond=responder,
            executor=FakeToolExecutor({"sql_query": lambda arguments: {"ok": True}}),
            tools=[{"type": "function", "function": {"name": "sql_query"}}],
            renderer=CliRenderer(indicator),
            run_id="run-1",
            max_tool_calls=4,
            model_request_timeout_seconds=5,
            tool_execution_timeout_seconds=5,
            agent_turn_timeout_seconds=30,
            trace_sink=MemoryTraceSink(),
        )
        outcome = runner.run_turn([{"role": "user", "content": "hi"}])
        return outcome, capsys.readouterr().out.splitlines()

    def test_the_indicator_is_stopped_before_the_second_answer_segment(self, capsys):
        indicator = MarkerIndicator()

        outcome, lines = self._turn(indicator, capsys)

        assert outcome.status is TurnStatus.COMPLETED
        assert lines == [
            "<stop>",
            "",
            "Qwen: Ничего не нашёл, попробую иначе.",
            "[tool 1/4] sql_query",
            "[args] query=SELECT 1",
            "<start>",  # tool execution
            "<stop>",
            "[result] ok",
            "<start>",  # the model's next decision
            # The regression: this stop used to be skipped, because the prefix
            # had already been printed, and the spinner kept animating over the
            # answer below it.
            "<stop>",
            "Вот таблица.",
        ]
        assert indicator.stops == indicator.starts + 1

    def test_the_answer_prefix_is_still_printed_exactly_once(self, capsys):
        # The fix must not be mistaken for "print `Qwen: ` again": the prefix
        # contract is per turn and unchanged.
        _, lines = self._turn(MarkerIndicator(), capsys)

        assert sum(line.startswith("Qwen: ") for line in lines) == 1

    def test_no_frame_reaches_the_terminal_while_the_answer_streams(self, capsys):
        # The same turn against the real indicator drawing into a fake terminal:
        # once the answer begins, nothing further may be written to that line.
        stream = FakeTty()
        indicator = ActivityIndicator(stream, interval_seconds=0.001)
        indicator.start()

        self._turn(indicator, capsys)
        drawn = stream.getvalue()
        indicator.stop()

        assert indicator.active is False
        # The last thing written must be an erase, not a frame: the indicator
        # was stopped for the answer and never resumed behind it.
        assert drawn.endswith("\r")
        assert not any(drawn.rstrip("\r ").endswith(frame) for frame in FRAMES)
