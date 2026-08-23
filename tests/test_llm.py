"""Model transport request-shape tests (PATCH-012-01).

Nothing here contacts Ollama: the transport is exercised through a fake client
that records what it was asked for, the same way `tests/test_model_profiles.py`
does.

What these protect is one asymmetry. Skill routing is a narrow classification
(SPEC-012 §"Core architectural decisions" 5), and letting the model deliberate
over it cost an unbounded amount of wall-clock -- enough to blow every profile's
routing deadline on a multi-intent request. Routing therefore asks for no
thinking; the agent loop, where deliberation may be earning its cost, keeps it.
A regression in either direction is a real defect, so both sides are asserted.
"""

import time
from types import SimpleNamespace

from llm import ROUTING_RESPONSE_SCHEMA, ModelResponse, OllamaModel
from skill_runtime.models import SkillCatalogEntry
from skill_runtime.router import SkillRouter


class FakeClient:
    """Records every chat() call and replays canned stream chunks."""

    def __init__(self, chunks=()) -> None:
        self.calls: list[dict] = []
        self._chunks = list(chunks)

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._chunks)


def text_chunk(text: str):
    return SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))


MESSAGES = [{"role": "user", "content": "hi"}]


class TestRoutingDisablesThinking:
    def test_text_asks_for_no_thinking(self):
        client = FakeClient([text_chunk('{"skill": null}')])
        model = OllamaModel("qwen3:8b", client)

        model.text(MESSAGES)

        assert client.calls[0]["think"] is False

    def test_text_still_returns_the_joined_response(self):
        client = FakeClient([text_chunk('{"skill": '), text_chunk('"sales"}')])
        model = OllamaModel("qwen3:8b", client)

        assert model.text(MESSAGES) == '{"skill": "sales"}'

    def test_text_declares_no_tools(self):
        # Routing must not be able to call anything -- unchanged by this patch,
        # asserted here because `text()` now builds a request of its own shape.
        client = FakeClient([text_chunk("{}")])

        OllamaModel("qwen3:8b", client).text(MESSAGES)

        assert client.calls[0]["tools"] is None


class TestRoutingConstrainsTheResponseShape:
    def test_text_constrains_generation_to_the_routing_schema(self):
        client = FakeClient([text_chunk('{"skill": null, "reason": ""}')])

        OllamaModel("qwen3:8b", client).text(MESSAGES)

        assert client.calls[0]["format"] == ROUTING_RESPONSE_SCHEMA

    def test_the_schema_matches_what_the_router_parses(self):
        # `_parse` requires an object carrying `skill`, and treats a null skill
        # as "no skill needed" -- so the schema must permit exactly that, or a
        # constrained response could never express it.
        assert ROUTING_RESPONSE_SCHEMA["type"] == "object"
        assert set(ROUTING_RESPONSE_SCHEMA["required"]) == {"skill", "reason"}
        assert ROUTING_RESPONSE_SCHEMA["properties"]["skill"]["type"] == [
            "string",
            "null",
        ]

    def test_a_schema_constrained_response_survives_the_router(self):
        # The end the schema exists for: the shapes it permits are shapes
        # SkillRouter accepts, including the null selection.
        router = SkillRouter(
            route=lambda _messages: '{"skill": null, "reason": "none needed"}',
            timeout_seconds=5,
            max_response_chars=2_000,
            repair_attempts=1,
        )

        selection = router.select(
            user_message="what is a bounded agent loop?",
            conversation_context=[],
            catalog=[SkillCatalogEntry(name="sales_analysis", description="sales")],
            deadline=time.monotonic() + 5,
            run_id="run",
            turn_id="turn",
        )

        assert selection.skill_name is None
        assert selection.routing_requests == 1


class TestAgentLoopKeepsThinking:
    def test_respond_leaves_thinking_to_the_model(self):
        # None is the SDK's own default and means "the model decides"; the agent
        # loop's behavior is unchanged by PATCH-012-01.
        client = FakeClient([text_chunk("hello")])
        model = OllamaModel("qwen3:8b", client)

        list(model.respond(MESSAGES).text_chunks())

        assert client.calls[0]["think"] is None

    def test_respond_with_tools_leaves_thinking_to_the_model(self):
        client = FakeClient([text_chunk("hello")])
        model = OllamaModel("qwen3:8b", client)
        tools = [{"type": "function", "function": {"name": "sql_query"}}]

        list(model.respond(MESSAGES, tools).text_chunks())

        assert client.calls[0]["think"] is None
        assert client.calls[0]["tools"] == tools

    def test_respond_leaves_the_response_shape_unconstrained(self):
        # The routing schema must never reach a tool-calling request: it would
        # forbid the tool calls the agent loop exists to make.
        client = FakeClient([text_chunk("hello")])
        model = OllamaModel("qwen3:8b", client)

        list(model.respond(MESSAGES).text_chunks())

        assert client.calls[0]["format"] is None

    def test_respond_can_be_asked_to_disable_thinking_explicitly(self):
        client = FakeClient([text_chunk("hello")])
        model = OllamaModel("qwen3:8b", client)

        list(model.respond(MESSAGES, think=False).text_chunks())

        assert client.calls[0]["think"] is False


class TestRequestShapeIsOtherwiseUnchanged:
    def test_every_request_streams(self):
        client = FakeClient([text_chunk("x")])
        model = OllamaModel("qwen3:8b", client)

        model.text(MESSAGES)
        list(model.respond(MESSAGES).text_chunks())

        assert [call["stream"] for call in client.calls] == [True, True]

    def test_no_generation_cap_is_imposed(self):
        # A `num_predict` cap was considered and rejected for routing: measured
        # against qwen3:8b it truncated the hidden reasoning rather than the
        # answer, returning an empty response for the router to parse. The
        # profile's routing deadline remains the backstop (PATCH-012-01).
        client = FakeClient([text_chunk("x")])

        OllamaModel("qwen3:8b", client).text(MESSAGES)

        assert "options" not in client.calls[0]

    def test_the_model_name_reaches_every_request(self):
        client = FakeClient([text_chunk("x")])
        model = OllamaModel("qwen3:32b", client)

        model.text(MESSAGES)
        list(model.respond(MESSAGES).text_chunks())

        assert {call["model"] for call in client.calls} == {"qwen3:32b"}

    def test_empty_history_is_rejected_before_a_request_is_made(self):
        client = FakeClient()

        try:
            ModelResponse([], None, model="qwen3:8b", client=client)
        except ValueError:
            pass
        else:  # pragma: no cover - the guard is expected to fire
            raise AssertionError("empty history should raise")

        assert client.calls == []
