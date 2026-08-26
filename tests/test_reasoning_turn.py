"""Reasoning as transient state of one agent turn (SPEC-020 §4.4-§4.11).

The claim under test is narrow and one-directional. Reasoning produced by a
model decision may travel *forward inside the same turn* — to the next model
request, attached to the assistant tool-call message it belongs to — and
nowhere else. Not to the terminal, not to `Conversation`, not to
`chat_history.json`, not to a tool result, not into the next turn, and not into
a trace as text, preview, or digest.

Everything here is deterministic: scripted model decisions, a fake executor, an
in-memory trace sink. No live Ollama, no real clock dependence.
"""

import json
import threading

import pytest

from agent import AgentRunner
from conversation import Conversation
from reliability import TerminationReason, TurnStatus
from storage import JsonConversationStore
from tests.support import (
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    make_tool_call,
)
from tracing import MemoryTraceSink

# A string no other part of the fixture could produce, so "does the reasoning
# appear here?" can be answered by a substring search over a whole artifact.
SECRET = "PLAN-7f3a: pull both periods, then diff them"
BASE_MESSAGES = [{"role": "user", "content": "how did sales change?"}]


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
        tools=[{"type": "function", "function": {"name": "sql_query"}}],
        renderer=renderer or RecordingRenderer(),
        trace_sink=trace if trace is not None else MemoryTraceSink(),
        **config,
    )


def sql_executor():
    return FakeToolExecutor({"sql_query": lambda args: {"ok": True, "rows": [[1]]}})


def one_tool_then_answer(thinking=SECRET, second_thinking="", text="Sales fell 12%."):
    """The canonical shape: reason -> tool -> reason -> final answer."""

    return ScriptedResponder(
        [
            ScriptedModelResponse(
                thinking=thinking,
                tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})],
            ),
            ScriptedModelResponse(thinking=second_thinking, text=text),
        ]
    )


def assistant_messages(messages):
    return [m for m in messages if m.get("role") == "assistant"]


class TestPreservationWithinTheTurn:
    def test_reasoning_is_attached_to_the_tool_call_it_produced(self):
        responder = one_tool_then_answer()

        make_runner(responder, sql_executor()).run_turn(BASE_MESSAGES)

        second_request = responder.calls[1][0]
        [assistant] = assistant_messages(second_request)
        assert assistant["thinking"] == SECRET
        assert assistant["tool_calls"][0]["function"]["name"] == "sql_query"

    def test_the_next_request_sees_reasoning_then_action_then_observation(self):
        # The ordering is the point of the whole step: the model gets its own
        # prior plan back *beside* what it did and what came of it, instead of
        # having to re-derive the plan from the action alone.
        responder = one_tool_then_answer()

        make_runner(responder, sql_executor()).run_turn(BASE_MESSAGES)

        roles = [m.get("role") for m in responder.calls[1][0]]
        assert roles[-2:] == ["assistant", "tool"]
        assert responder.calls[1][0][-2]["thinking"] == SECRET

    def test_reasoning_never_enters_the_tool_result(self):
        # Reasoning is assistant state, not observation data. A tool result is
        # the one message in the transcript the model may treat as fact.
        responder = one_tool_then_answer()

        make_runner(responder, sql_executor()).run_turn(BASE_MESSAGES)

        [tool_message] = [m for m in responder.calls[1][0] if m.get("role") == "tool"]
        assert SECRET not in tool_message["content"]
        assert "thinking" not in tool_message

    def test_each_decision_of_a_multi_tool_turn_keeps_its_own_reasoning(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    thinking="first: get this period",
                    tool_calls=[make_tool_call("sql_query", {"query": "A"})],
                ),
                ScriptedModelResponse(
                    thinking="second: now the prior period",
                    tool_calls=[make_tool_call("sql_query", {"query": "B"})],
                ),
                ScriptedModelResponse(text="Down 12%."),
            ]
        )

        outcome = make_runner(responder, sql_executor()).run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        third_request = responder.calls[2][0]
        assert [m["thinking"] for m in assistant_messages(third_request)] == [
            "first: get this period",
            "second: now the prior period",
        ]

    def test_a_non_thinking_model_adds_no_thinking_field_at_all(self):
        # Absent, not empty: the transcript a non-thinking model produces is
        # byte-for-byte the one it produced before SPEC-020.
        responder = one_tool_then_answer(thinking="")

        make_runner(responder, sql_executor()).run_turn(BASE_MESSAGES)

        [assistant] = assistant_messages(responder.calls[1][0])
        assert "thinking" not in assistant


class TestPreservationSwitch:
    def test_disabling_preservation_reproduces_the_pre_spec_transcript(self):
        # The evaluation-only path (SPEC-020 §7.4). Reasoning is still generated
        # and still costs what it costs -- it simply does not travel -- which is
        # what makes the A/B a measurement of preservation rather than of effort.
        responder = one_tool_then_answer()

        make_runner(
            responder, sql_executor(), preserve_reasoning=False
        ).run_turn(BASE_MESSAGES)

        [assistant] = assistant_messages(responder.calls[1][0])
        assert "thinking" not in assistant

    def test_the_answer_is_unaffected_by_the_switch(self):
        for preserve in (True, False):
            outcome = make_runner(
                one_tool_then_answer(), sql_executor(), preserve_reasoning=preserve
            ).run_turn(BASE_MESSAGES)

            assert outcome.final_text == "Sales fell 12%."


class TestTheUserNeverSeesReasoning:
    def test_no_reasoning_chunk_reaches_the_renderer(self):
        renderer = RecordingRenderer()

        make_runner(
            one_tool_then_answer(second_thinking="now phrase it"),
            sql_executor(),
            renderer=renderer,
        ).run_turn(BASE_MESSAGES)

        assert renderer.text_chunks == ["Sales fell 12%."]

    def test_content_still_streams_chunk_by_chunk(self):
        renderer = RecordingRenderer()
        responder = ScriptedResponder(
            [ScriptedModelResponse(thinking=SECRET, text="Hello there.")]
        )

        make_runner(responder, renderer=renderer).run_turn(BASE_MESSAGES)

        assert renderer.text_chunks == ["Hello there."]

    def test_the_final_text_carries_only_content(self):
        outcome = make_runner(
            ScriptedResponder(
                [ScriptedModelResponse(thinking=SECRET, text="Hello there.")]
            )
        ).run_turn(BASE_MESSAGES)

        assert outcome.final_text == "Hello there."
        assert not hasattr(outcome, "thinking")


class TestReasoningNeverPersists:
    def test_stored_history_holds_only_semantic_messages(self, tmp_path):
        # What the caller does after a completed turn, exactly as `app.py` does
        # it: the outcome's final text is the only thing that survives.
        conversation = Conversation()
        conversation.add_user_message("how did sales change?")
        outcome = make_runner(one_tool_then_answer(), sql_executor()).run_turn(
            conversation.messages_for_model()
        )
        conversation.add_assistant_message(outcome.final_text)

        store = JsonConversationStore(tmp_path / "chat_history.json")
        store.save(conversation.stored_messages)

        assert all("thinking" not in m for m in conversation.stored_messages)
        assert SECRET not in (tmp_path / "chat_history.json").read_text(encoding="utf-8")

    def test_the_callers_own_message_list_is_never_mutated(self):
        # The turn transcript is a copy; reasoning cannot leak back into the
        # snapshot the caller keeps and re-sends on the next turn.
        messages = [dict(m) for m in BASE_MESSAGES]

        make_runner(one_tool_then_answer(), sql_executor()).run_turn(messages)

        assert messages == BASE_MESSAGES

    def test_a_second_turn_starts_with_no_reasoning_from_the_first(self):
        runner_responder = one_tool_then_answer()
        runner = make_runner(runner_responder, sql_executor())
        runner.run_turn(BASE_MESSAGES)

        second_responder = ScriptedResponder([ScriptedModelResponse(text="Sure.")])
        make_runner(second_responder, sql_executor()).run_turn(BASE_MESSAGES)

        assert second_responder.calls[0][0] == BASE_MESSAGES

    @pytest.mark.parametrize(
        "failure,reason",
        [
            (RuntimeError("transport died"), TerminationReason.MODEL_ERROR),
            (KeyboardInterrupt(), TerminationReason.USER_INTERRUPT),
        ],
    )
    def test_a_failed_turn_leaves_no_reasoning_behind(self, failure, reason):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    thinking=SECRET,
                    tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})],
                ),
                failure,
            ]
        )

        outcome = make_runner(responder, sql_executor()).run_turn(BASE_MESSAGES)

        assert outcome.reason is reason
        assert outcome.final_text is None
        assert SECRET not in json.dumps(outcome.__dict__, default=str)

    def test_a_timed_out_turn_leaves_no_reasoning_behind(self):
        blocked = threading.Event()
        responder = ScriptedResponder(
            [ScriptedModelResponse(thinking=SECRET, block_on=blocked)]
        )
        trace = MemoryTraceSink()

        outcome = make_runner(
            responder, trace=trace, model_request_timeout_seconds=0.05
        ).run_turn(BASE_MESSAGES)
        blocked.set()

        assert outcome.status is TurnStatus.TIMED_OUT
        assert SECRET not in json.dumps(trace.events, ensure_ascii=False)


class TestTracePrivacyAndMetrics:
    def test_no_event_carries_reasoning_text_a_preview_or_a_digest(self):
        import hashlib

        trace = MemoryTraceSink()

        make_runner(one_tool_then_answer(), sql_executor(), trace=trace).run_turn(
            BASE_MESSAGES
        )

        serialized = json.dumps(trace.events, ensure_ascii=False)
        assert SECRET not in serialized
        assert hashlib.sha256(SECRET.encode()).hexdigest() not in serialized
        # A partial leak is still a leak: no prefix long enough to be meaningful.
        assert SECRET[:20] not in serialized

    def test_the_counts_and_timings_are_reported_instead(self):
        trace = MemoryTraceSink()

        make_runner(one_tool_then_answer(), sql_executor(), trace=trace).run_turn(
            BASE_MESSAGES
        )

        finished = [
            event for event in trace.events if event["event"] == "model_response_finished"
        ]
        assert [event["thinking_chars"] for event in finished] == [len(SECRET), 0]
        assert finished[0]["first_thinking_ms"] is not None
        # First request went straight from reasoning to a tool call: nothing
        # visible was ever streamed, so visible TTFT is the tool call itself.
        assert finished[0]["first_content_ms"] is None
        assert finished[0]["visible_ttft_ms"] == finished[0]["first_tool_call_ms"]
        # Second request produced content and no tool call.
        assert finished[1]["first_tool_call_ms"] is None
        assert finished[1]["visible_ttft_ms"] == finished[1]["first_content_ms"]

    def test_the_ollama_metadata_fields_are_always_present(self):
        # Present-and-null rather than absent, so a consumer reads every request
        # the same way whether or not Ollama reported timings for it.
        trace = MemoryTraceSink()

        make_runner(one_tool_then_answer(), sql_executor(), trace=trace).run_turn(
            BASE_MESSAGES
        )

        finished = next(
            e for e in trace.events if e["event"] == "model_response_finished"
        )
        for field in (
            "ollama_load_ms",
            "ollama_prompt_eval_ms",
            "ollama_prompt_eval_count",
            "ollama_eval_ms",
            "ollama_eval_count",
        ):
            assert field in finished

    def test_the_pre_existing_fields_keep_their_meaning(self):
        trace = MemoryTraceSink()

        make_runner(one_tool_then_answer(), sql_executor(), trace=trace).run_turn(
            BASE_MESSAGES
        )

        finished = [
            e for e in trace.events if e["event"] == "model_response_finished"
        ]
        assert [e["decision"] for e in finished] == ["tool_call", "final_answer"]
        # text_chars counts *visible* text only -- reasoning does not inflate it.
        assert [e["text_chars"] for e in finished] == [0, len("Sales fell 12%.")]
