"""Cross-turn action provenance (PATCH-010-05).

A completed turn's tool executions leave bounded, session-only receipts, so the
next turn can still see *that* a tool ran after the working transcript that
carried its payload is gone. These tests pin the whole path: what the loop
collects, what the outcome reports, what the conversation remembers, what the
model is shown, and — just as importantly — everything that must stay out of it
(raw results, reasoning, disk, the router).

Deterministic throughout: scripted model, fake executor, no live Ollama, MCP,
sandbox, or real waits.
"""

import json
from pathlib import Path

from agent import AgentRunner
from config import MAX_ACTION_RECEIPTS_IN_CONTEXT
from conversation import Conversation
from reliability import AgentActionReceipt, TurnStatus
from skill_runtime.activation import ACTIVATE_SKILL_TOOL_NAME
from skill_runtime.models import SkillSelection, SkillSpec
from skill_runtime.orchestrator import SkillTurnOrchestrator
from skill_runtime.registry import SkillRegistry
from storage import STORE_VERSION, JsonConversationStore
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

RECEIPT_MARKER = "<host_action_receipts>"

TOOL_NAMES = (
    "sql_query",
    "python_calculate",
    "mcp_time__get_current_time",
    "sandbox_execute",
)


def make_runner(responder, executor=None, *, trace=None, tools=None, **overrides):
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
        tools=tools or [{"type": "function", "function": {"name": "python_calculate"}}],
        renderer=RecordingRenderer(),
        trace_sink=trace if trace is not None else MemoryTraceSink(),
        **config,
    )


BASE_MESSAGES = [{"role": "user", "content": "hi"}]


def ok(**fields):
    return lambda _arguments: {"ok": True, **fields}


def failed(**fields):
    return lambda _arguments: {"ok": False, **fields}


def receipt(name, preview, *, ok=True, truncated=False, redacted=False):
    return AgentActionReceipt(
        tool_name=name,
        arguments_preview=preview,
        arguments_truncated=truncated,
        arguments_redacted=redacted,
        result_ok=ok,
    )


class TestCollection:
    """1-3: what the loop records for the actions it actually executed."""

    def test_one_successful_action_produces_one_receipt(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[
                        make_tool_call(
                            "mcp_time__get_current_time", {"timezone": "Asia/Tokyo"}
                        )
                    ]
                ),
                ScriptedModelResponse(text="It is 05:20 in Tokyo."),
            ]
        )
        executor = FakeToolExecutor(
            {"mcp_time__get_current_time": ok(datetime="2026-08-27T05:20:26+09:00")}
        )

        outcome = make_runner(responder, executor).run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.COMPLETED
        assert outcome.action_receipts == (
            receipt("mcp_time__get_current_time", '{"timezone":"Asia/Tokyo"}'),
        )

    def test_several_actions_keep_execution_order(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})]
                ),
                ScriptedModelResponse(
                    tool_calls=[
                        make_tool_call("python_calculate", {"expression": "1+1"})
                    ]
                ),
                ScriptedModelResponse(text="Two."),
            ]
        )
        executor = FakeToolExecutor(
            {"sql_query": ok(rows=[]), "python_calculate": ok(value=2)}
        )

        outcome = make_runner(responder, executor).run_turn(BASE_MESSAGES)

        assert [item.tool_name for item in outcome.action_receipts] == [
            "sql_query",
            "python_calculate",
        ]

    def test_recovered_tool_error_keeps_both_executions(self):
        results = iter([{"ok": False, "error": "syntax"}, {"ok": True, "rows": []}])
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"query": "SELEC 1"})]
                ),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})]
                ),
                ScriptedModelResponse(text="Fixed it."),
            ]
        )
        executor = FakeToolExecutor({"sql_query": lambda _arguments: next(results)})

        outcome = make_runner(responder, executor).run_turn(BASE_MESSAGES)

        assert [item.result_ok for item in outcome.action_receipts] == [False, True]
        # A structured failure is an action, not an observation: the error text
        # itself never travels with the receipt.
        assert "syntax" not in "".join(
            item.arguments_preview for item in outcome.action_receipts
        )

    def test_rejected_call_is_never_receipted(self):
        """A call stopped by policy before dispatch executed nothing."""

        call = make_tool_call("sql_query", {"query": "SELECT 1"})
        responder = ScriptedResponder(
            [ScriptedModelResponse(tool_calls=[call])] * 2
            + [ScriptedModelResponse(text="unreachable")]
        )
        executor = FakeToolExecutor({"sql_query": ok(rows=[])})

        outcome = make_runner(responder, executor, max_tool_calls=1).run_turn(
            BASE_MESSAGES
        )

        # The first call ran (1 of 1); the second was refused before dispatch and
        # stopped the turn, so the whole turn reports nothing.
        assert outcome.status is TurnStatus.STOPPED
        assert outcome.tool_calls_executed == 1
        assert outcome.action_receipts == ()


class TestRollback:
    """4: an answer the user never received leaves no provenance."""

    def test_stopped_turn_reports_no_receipts(self):
        call = make_tool_call("sql_query", {"query": "SELECT 1"})
        responder = ScriptedResponder([ScriptedModelResponse(tool_calls=[call])] * 3)
        executor = FakeToolExecutor({"sql_query": ok(rows=[])})

        outcome = make_runner(responder, executor).run_turn(BASE_MESSAGES)

        assert outcome.status is TurnStatus.STOPPED
        assert outcome.tool_calls_executed >= 1
        assert outcome.action_receipts == ()

    def test_rolled_back_user_turn_leaves_no_receipt_in_the_conversation(self):
        conversation = Conversation()
        conversation.add_user_message("first")
        conversation.add_assistant_message(
            "answer", [receipt("sql_query", '{"query":"SELECT 1"}')]
        )
        conversation.add_user_message("second, which will fail")

        conversation.remove_last_message()
        conversation.remove_last_message()
        conversation.remove_last_message()
        conversation.add_user_message("a fresh turn")

        assert RECEIPT_MARKER not in _projection(conversation)


class TestProjection:
    """5, 7: what the next model request is shown, and how much of it."""

    def test_prior_assistant_message_carries_the_receipt_suffix(self):
        conversation = Conversation()
        conversation.add_user_message("сколько времени сейчас в Токио?")
        conversation.add_assistant_message(
            "Сейчас в Токио 05:20.",
            [receipt("mcp_time__get_current_time", '{"timezone":"Asia/Tokyo"}')],
        )
        conversation.add_user_message("ты правда вызывал инструмент?")

        messages = conversation.messages_for_model()
        assistant = messages[2]

        assert assistant["role"] == "assistant"
        assert assistant["content"].startswith("Сейчас в Токио 05:20.")
        assert RECEIPT_MARKER in assistant["content"]
        assert "tool=mcp_time__get_current_time" in assistant["content"]
        assert 'args={"timezone":"Asia/Tokyo"}' in assistant["content"]
        assert "result_ok=true" in assistant["content"]
        # Provenance, not observation: no raw tool result reaches the model.
        assert "2026-08-27T05:20:26+09:00" not in assistant["content"]

    def test_messages_without_receipts_are_untouched(self):
        conversation = Conversation()
        conversation.add_user_message("hi")
        conversation.add_assistant_message("hello")

        messages = conversation.messages_for_model()

        assert messages[1:] == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_failure_receipt_is_projected_as_such(self):
        conversation = Conversation()
        conversation.add_user_message("q")
        conversation.add_assistant_message(
            "I could not read it.",
            [receipt("sql_query", '{"query":"SELEC 1"}', ok=False)],
        )

        assert "result_ok=false" in _projection(conversation)

    def test_projection_is_bounded_and_keeps_the_newest(self):
        conversation = Conversation()
        total = MAX_ACTION_RECEIPTS_IN_CONTEXT + 3
        for index in range(total):
            conversation.add_user_message(f"ask {index}")
            conversation.add_assistant_message(
                f"answer {index}",
                [receipt("sql_query", f'{{"query":"SELECT {index}"}}')],
            )

        projected = _projection(conversation)

        assert projected.count("tool=sql_query") == MAX_ACTION_RECEIPTS_IN_CONTEXT
        dropped = range(total - MAX_ACTION_RECEIPTS_IN_CONTEXT)
        for index in dropped:
            assert f'"SELECT {index}"' not in projected
        kept = range(total - MAX_ACTION_RECEIPTS_IN_CONTEXT, total)
        positions = [projected.index(f'"SELECT {index}"') for index in kept]
        assert positions == sorted(positions)

    def test_receipts_outside_the_context_window_cannot_be_projected(self):
        from config import MAX_CONTEXT_MESSAGES

        conversation = Conversation()
        conversation.add_user_message("the oldest question")
        conversation.add_assistant_message(
            "the oldest answer",
            [receipt("sql_query", '{"query":"SELECT oldest"}')],
        )
        for index in range(MAX_CONTEXT_MESSAGES):
            conversation.add_user_message(f"filler {index}")

        assert "SELECT oldest" not in _projection(conversation)

    def test_a_giant_argument_cannot_dominate_the_context(self):
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"query": "x" * 5_000})]
                ),
                ScriptedModelResponse(text="done"),
            ]
        )
        executor = FakeToolExecutor({"sql_query": ok(rows=[])})

        outcome = make_runner(
            responder, executor, receipt_argument_chars=50
        ).run_turn(BASE_MESSAGES)

        (item,) = outcome.action_receipts
        assert item.arguments_truncated is True
        assert len(item.arguments_preview) == 50

        conversation = Conversation()
        conversation.add_assistant_message("done", outcome.action_receipts)
        assert "(truncated)" in _projection(conversation)


class TestRedaction:
    """8: the trace's privacy boundary, reused rather than weakened."""

    def test_redacted_tool_keeps_identity_and_status_only(self):
        source = "print('the user's own data as code')"
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[
                        make_tool_call(
                            "sandbox_execute",
                            {"source": source, "input_files": {"a.csv": "secret"}},
                        )
                    ]
                ),
                ScriptedModelResponse(text="Ran it."),
            ]
        )
        executor = FakeToolExecutor({"sandbox_execute": ok(stdout="hi")})

        outcome = make_runner(
            responder,
            executor,
            redacted_argument_tools=frozenset({"sandbox_execute"}),
        ).run_turn(BASE_MESSAGES)

        (item,) = outcome.action_receipts
        assert item.tool_name == "sandbox_execute"
        assert item.result_ok is True
        assert item.arguments_preview == ""
        assert item.arguments_redacted is True
        assert item.arguments_truncated is False
        # No hash of redacted content either — the receipt has no field for one,
        # and nothing derived from the source may appear in the projection.
        assert not hasattr(item, "arguments_sha256")

        conversation = Conversation()
        conversation.add_assistant_message("Ran it.", outcome.action_receipts)
        projected = _projection(conversation)
        assert "args=<redacted>" in projected
        assert source not in projected
        assert "secret" not in projected


class TestSessionOnlyMemory:
    """6, 9: what never reaches disk, and what /reset takes with it."""

    def test_saved_history_keeps_the_existing_schema(self, tmp_path: Path):
        conversation = Conversation()
        conversation.add_user_message("сколько времени в Токио?")
        conversation.add_assistant_message(
            "05:20.",
            [receipt("mcp_time__get_current_time", '{"timezone":"Asia/Tokyo"}')],
        )
        store = JsonConversationStore(str(tmp_path / "chat_history.json"))

        store.save(conversation.stored_messages)
        payload = json.loads((tmp_path / "chat_history.json").read_text("utf-8"))

        assert payload["version"] == STORE_VERSION
        assert payload["messages"] == [
            {"role": "user", "content": "сколько времени в Токио?"},
            {"role": "assistant", "content": "05:20."},
        ]
        assert RECEIPT_MARKER not in (tmp_path / "chat_history.json").read_text("utf-8")

    def test_stored_messages_stay_semantic_content_only(self):
        conversation = Conversation()
        conversation.add_assistant_message(
            "05:20.", [receipt("mcp_time__get_current_time", "{}")]
        )

        (message,) = conversation.stored_messages
        assert message == {"role": "assistant", "content": "05:20."}

    def test_a_restart_does_not_reconstruct_receipts(self, tmp_path: Path):
        conversation = Conversation()
        conversation.add_user_message("q")
        conversation.add_assistant_message(
            "a", [receipt("mcp_time__get_current_time", "{}")]
        )
        store = JsonConversationStore(str(tmp_path / "chat_history.json"))
        store.save(conversation.stored_messages)

        restarted = Conversation(messages=store.load())

        assert RECEIPT_MARKER not in _projection(restarted)

    def test_reset_clears_every_receipt(self):
        conversation = Conversation()
        conversation.add_user_message("q")
        conversation.add_assistant_message(
            "a", [receipt("mcp_time__get_current_time", "{}")]
        )

        conversation.reset()
        conversation.add_user_message("q")
        conversation.add_assistant_message("a")

        assert RECEIPT_MARKER not in _projection(conversation)


class TestTurnIntegration:
    """10-12: the whole orchestrated turn — router, control tools, reasoning."""

    def test_router_never_sees_receipt_markup(self):
        router = ScriptedSkillRouter(SkillSelection(None, "no skill", "model", 1, 5))
        responder = ScriptedResponder([ScriptedModelResponse(text="Second answer.")])
        orchestrator, _ = _build_orchestrator(router, responder=responder)

        conversation = Conversation()
        conversation.add_user_message("сколько времени в Токио?")
        conversation.add_assistant_message(
            "05:20.",
            [receipt("mcp_time__get_current_time", '{"timezone":"Asia/Tokyo"}')],
        )
        conversation.add_user_message("ты правда вызывал инструмент?")

        orchestrator.run_turn(conversation)

        context = router.calls[0]["conversation_context"]
        assert context == [
            {"role": "user", "content": "сколько времени в Токио?"},
            {"role": "assistant", "content": "05:20."},
        ]
        assert RECEIPT_MARKER not in json.dumps(context, ensure_ascii=False)

    def test_activate_skill_is_traced_but_never_receipted(self):
        router = ScriptedSkillRouter(SkillSelection(None, "no skill", "model", 1, 5))
        responder = ScriptedResponder(
            [
                ScriptedModelResponse(
                    tool_calls=[
                        make_tool_call(ACTIVATE_SKILL_TOOL_NAME, {"name": "sales"})
                    ]
                ),
                ScriptedModelResponse(
                    tool_calls=[make_tool_call("sql_query", {"query": "SELECT 1"})]
                ),
                ScriptedModelResponse(text="Done."),
            ]
        )
        trace = MemoryTraceSink()
        orchestrator, _ = _build_orchestrator(
            router,
            responder=responder,
            handlers={"sql_query": ok(rows=[])},
            trace=trace,
        )

        result = orchestrator.run_turn(_conversation_with("analyse sales"))

        assert result.outcome.status is TurnStatus.COMPLETED
        assert result.activations == 1
        # The control tool stays fully visible where it belongs — the trace.
        traced = [
            event["tool_name"]
            for event in trace.events
            if event["event"] == "tool_execution_finished"
        ]
        assert traced == [ACTIVATE_SKILL_TOOL_NAME, "sql_query"]
        # ...and nowhere in cross-turn memory: remembering a turn-scoped
        # activation could make a later turn infer the skill is still active.
        assert [item.tool_name for item in result.outcome.action_receipts] == [
            "sql_query"
        ]

    def test_reasoning_changes_neither_the_receipt_nor_the_context(self):
        def run(thinking: str):
            responder = ScriptedResponder(
                [
                    ScriptedModelResponse(
                        thinking=thinking,
                        tool_calls=[
                            make_tool_call(
                                "mcp_time__get_current_time", {"timezone": "Asia/Tokyo"}
                            )
                        ],
                    ),
                    ScriptedModelResponse(text="05:20."),
                ]
            )
            executor = FakeToolExecutor({"mcp_time__get_current_time": ok(t="05:20")})
            return make_runner(responder, executor).run_turn(BASE_MESSAGES)

        secret = "The user probably wants JST; I will call the time tool."
        with_reasoning = run(secret)
        without_reasoning = run("")

        assert with_reasoning.action_receipts == without_reasoning.action_receipts

        conversation = Conversation()
        conversation.add_user_message("сколько времени в Токио?")
        conversation.add_assistant_message("05:20.", with_reasoning.action_receipts)
        assert secret not in _projection(conversation)


def _projection(conversation: Conversation) -> str:
    """Everything the model would be shown, as one searchable string."""

    return "\n".join(
        message["content"] for message in conversation.messages_for_model()
    )


def _conversation_with(user_message: str) -> Conversation:
    conversation = Conversation()
    conversation.add_user_message(user_message)
    return conversation


SALES_SPEC = SkillSpec(
    name="sales",
    description="Analyse sales data",
    version="1",
    allowed_tools=("sql_query",),
    instruction="# Sales\nProcedure body.",
    input_schema={"type": "object", "properties": {}},
    package_path=Path("/skills/sales"),
    fingerprint="sha256:sales",
)


def _build_orchestrator(router, *, responder, handlers=None, trace=None):
    registry = SkillRegistry()
    registry.register(SALES_SPEC)
    tool_registry = make_tool_registry(*TOOL_NAMES)
    executor = FakeToolExecutor(handlers or {})
    orchestrator = SkillTurnOrchestrator(
        skill_registry=registry,
        router=router,
        tool_registry=tool_registry,
        executor=executor,
        respond=responder,
        renderer_factory=RecordingRenderer,
        default_tools=tool_registry.to_ollama_tools(),
        run_id="run-1",
        max_tool_calls=4,
        max_identical_tool_calls=2,
        model_request_timeout_seconds=5,
        tool_execution_timeout_seconds=5,
        agent_turn_timeout_seconds=30,
        trace_sink=trace or MemoryTraceSink(),
    )
    return orchestrator, executor
