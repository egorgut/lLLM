"""Transport-level reasoning contract (SPEC-020 §4.1-§4.3, §4.10).

Nothing here contacts Ollama. The transport is exercised through the shared
fake client (`tests/support.FakeOllamaClient`) that records what it was asked
for and replays canned chunks shaped like the installed SDK's own.

Two properties are worth protecting separately. The first is *separation*: a
streamed message carries hidden reasoning and user-visible content in different
fields, and the transport must never let one become the other -- everything
above it, from the terminal to `chat_history.json`, relies on that boundary
holding here. The second is *ownership*: the agent-side reasoning mode is run
configuration applied at this composition boundary, and skill routing must be
unable to inherit it under any mode (PATCH-012-01 remains in force).
"""

import pytest

from config import MODEL_PROFILES, REASONING_MODES, resolve_reasoning_think
from llm import ModelResponseMetrics, ModelStreamChunk, OllamaModel
from tests.support import FakeOllamaClient, sdk_chunk

MESSAGES = [{"role": "user", "content": "hi"}]


def chunks_of(*stream):
    return list(OllamaModel("qwen3.8:27b", FakeOllamaClient(stream)).respond(MESSAGES).chunks())


class TestStreamSeparation:
    def test_a_content_only_stream_yields_only_content(self):
        # The pre-SPEC-020 shape: a non-thinking model behaves exactly as before,
        # and produces no reasoning for anything downstream to preserve.
        assert chunks_of(sdk_chunk(content="Hello "), sdk_chunk(content="there.")) == [
            ModelStreamChunk(content="Hello "),
            ModelStreamChunk(content="there."),
        ]

    def test_thinking_then_content_stays_two_separate_streams(self):
        assert chunks_of(
            sdk_chunk(thinking="I should just answer. "),
            sdk_chunk(thinking="No tool is needed."),
            sdk_chunk(content="42."),
        ) == [
            ModelStreamChunk(thinking="I should just answer. "),
            ModelStreamChunk(thinking="No tool is needed."),
            ModelStreamChunk(content="42."),
        ]

    def test_reasoning_is_never_concatenated_into_content(self):
        # The single failure this whole SPEC has to prevent: if reasoning ever
        # arrives as content, it is rendered to the terminal and persisted to
        # chat history, and no rule further up can put it back.
        chunks = chunks_of(sdk_chunk(thinking="secret plan", content="visible"))

        assert "".join(chunk.content for chunk in chunks) == "visible"
        assert "".join(chunk.thinking for chunk in chunks) == "secret plan"

    def test_thinking_then_a_tool_call_yields_the_reasoning_and_stops(self):
        response = OllamaModel(
            "qwen3.8:27b",
            FakeOllamaClient(
                [
                    sdk_chunk(thinking="I need the sales figures first."),
                    sdk_chunk(tool_calls=[("sql_query", {"query": "SELECT 1"})]),
                    sdk_chunk(content="never read"),
                ]
            ),
        ).respond(MESSAGES)

        chunks = list(response.chunks())

        assert chunks == [ModelStreamChunk(thinking="I need the sales figures first.")]
        assert [call.name for call in response.tool_calls] == ["sql_query"]

    def test_text_chunks_is_the_content_only_view(self):
        response = OllamaModel(
            "qwen3.8:27b",
            FakeOllamaClient(
                [sdk_chunk(thinking="deliberating"), sdk_chunk(content="answer")]
            ),
        ).respond(MESSAGES)

        assert list(response.text_chunks()) == ["answer"]


class TestOllamaTimingMetadata:
    def test_the_final_chunks_metadata_is_captured(self):
        response = OllamaModel(
            "qwen3.8:27b",
            FakeOllamaClient(
                [
                    sdk_chunk(content="hi"),
                    sdk_chunk(
                        load_duration=1_500_000_000,
                        prompt_eval_duration=2_000_000_000,
                        prompt_eval_count=812,
                        eval_duration=3_000_000_000,
                        eval_count=64,
                    ),
                ]
            ),
        ).respond(MESSAGES)

        list(response.chunks())

        assert response.metrics == ModelResponseMetrics(
            load_ms=1500,
            prompt_eval_ms=2000,
            prompt_eval_count=812,
            eval_ms=3000,
            eval_count=64,
        )

    def test_a_stream_without_metadata_records_none(self):
        # Absent, not fabricated: a caller reading `null` knows Ollama said
        # nothing, which is a different fact from "it took zero milliseconds".
        response = OllamaModel(
            "qwen3.8:27b", FakeOllamaClient([sdk_chunk(content="hi")])
        ).respond(MESSAGES)

        list(response.chunks())

        assert response.metrics is None
        assert set(ModelResponseMetrics().as_trace_fields().values()) == {None}


class TestReasoningModeMapping:
    @pytest.mark.parametrize(
        "mode,expected",
        [("auto", None), ("off", False), ("low", "low"), ("medium", "medium")],
    )
    def test_each_mode_reaches_the_request_as_its_native_think_value(self, mode, expected):
        client = FakeOllamaClient([sdk_chunk(content="hi")])
        model = OllamaModel(
            "qwen3.8:27b", client, reasoning_think=resolve_reasoning_think(mode)
        )

        list(model.respond(MESSAGES).chunks())

        assert client.calls[0]["think"] == expected

    def test_the_default_is_auto_and_sends_nothing(self):
        # `auto` is not "some default effort we picked" -- it is the absence of a
        # `think` argument, which is what every journal before SPEC-020 recorded.
        client = FakeOllamaClient([sdk_chunk(content="hi")])

        list(OllamaModel("qwen3.8:27b", client).respond(MESSAGES).chunks())

        assert client.calls[0]["think"] is None

    def test_for_profile_binds_the_mode_for_the_whole_run(self):
        # The client is swapped after construction because `for_profile` builds a
        # real one bound to a host; what is under test is that the *mode* survived
        # the profile composition, not the connection.
        model = OllamaModel.for_profile(MODEL_PROFILES["next"], reasoning_mode="medium")
        client = FakeOllamaClient([sdk_chunk(content="hi")])
        model._client = client

        list(model.respond(MESSAGES).chunks())

        assert client.calls[0]["think"] == "medium"

    def test_an_unknown_mode_fails_before_any_request_is_built(self):
        with pytest.raises(ValueError, match="Unknown reasoning mode"):
            OllamaModel.for_profile(MODEL_PROFILES["next"], reasoning_mode="xhigh")

    def test_an_explicit_argument_overrides_the_configured_mode(self):
        client = FakeOllamaClient([sdk_chunk(content="hi")])
        model = OllamaModel("qwen3.8:27b", client, reasoning_think="medium")

        list(model.respond(MESSAGES, think=False).chunks())

        assert client.calls[0]["think"] is False


class TestRoutingIgnoresTheAgentMode:
    @pytest.mark.parametrize("mode", REASONING_MODES)
    def test_routing_asks_for_no_thinking_under_every_agent_mode(self, mode):
        # Why this matters even for `off`: routing's contract is that it is
        # *always* think=False, not that it happens to agree with the agent's
        # setting on some runs. One transport object serves both roles whenever
        # SPEC-019 resolves them to the same profile.
        client = FakeOllamaClient([sdk_chunk(content='{"skill": null, "reason": ""}')])
        model = OllamaModel(
            "qwen3.8:27b", client, reasoning_think=resolve_reasoning_think(mode)
        )

        model.text(MESSAGES)

        assert client.calls[0]["think"] is False

    def test_the_agent_response_shape_stays_unconstrained_under_every_mode(self):
        client = FakeOllamaClient([sdk_chunk(content="hi")])
        model = OllamaModel("qwen3.8:27b", client, reasoning_think="low")

        list(model.respond(MESSAGES).chunks())

        assert client.calls[0]["format"] is None
