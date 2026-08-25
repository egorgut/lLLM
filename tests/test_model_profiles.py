"""Model profile regression tests (SPEC-017).

Nothing here contacts Ollama: the transport is exercised through a fake client
that records what it was asked for.
"""

from types import SimpleNamespace

import pytest

import config
import llm
from app import describe_profile, parse_args, validate_model_profiles
from config import (
    DEFAULT_MODEL_PROFILE,
    MODEL_PROFILES,
    ModelProfile,
    resolve_model_profile,
)
from llm import ModelResponse, OllamaModel


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


def tool_chunk(name: str, arguments: dict):
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))


class TestCommittedProfiles:
    def test_fast_profile_preserves_the_historical_configuration(self):
        # Every journal from SPEC-005 through PATCH-010-01 was recorded under
        # these exact values; changing them silently would make them
        # irreproducible (SPEC-017 §3.2).
        fast = MODEL_PROFILES["fast"]
        assert fast.model == "qwen3:8b"
        assert fast.model_request_timeout_seconds == 120
        assert fast.agent_turn_timeout_seconds == 180
        assert fast.skill_routing_timeout_seconds == 30

    def test_fast_is_the_default_profile(self):
        assert DEFAULT_MODEL_PROFILE == "fast"
        assert resolve_model_profile() is MODEL_PROFILES["fast"]

    def test_deep_profile_runs_the_larger_model_with_larger_deadlines(self):
        fast, deep = MODEL_PROFILES["fast"], MODEL_PROFILES["deep"]
        assert deep.model == "qwen3:32b"
        assert deep.model_request_timeout_seconds > fast.model_request_timeout_seconds
        assert deep.agent_turn_timeout_seconds > fast.agent_turn_timeout_seconds
        assert deep.skill_routing_timeout_seconds > fast.skill_routing_timeout_seconds

    def test_next_profile_runs_the_new_model_generation(self):
        # PATCH-017-02 pins only what the patch actually claims: the model binds,
        # and adding it did not move the host default (which every journal from
        # SPEC-005 onwards depends on).
        assert MODEL_PROFILES["next"].model == "qwen3.8:27b"
        assert resolve_model_profile() is MODEL_PROFILES["fast"]

    def test_next_mlx_profile_binds_the_mlx_package_with_next_deadlines(self):
        # PATCH-019-01 pins only what the patch claims: the experimental profile
        # binds the MLX-backed package, and copies `next`'s deadlines rather than
        # measuring its own so that timeout policy is not a second variable in the
        # comparison. Adding it did not move the host default either.
        base, mlx = MODEL_PROFILES["next"], MODEL_PROFILES["next-mlx"]
        assert mlx.model == "qwen3.8:27b-mlx"
        assert base.model == "qwen3.8:27b"
        assert (
            mlx.model_request_timeout_seconds,
            mlx.agent_turn_timeout_seconds,
            mlx.skill_routing_timeout_seconds,
        ) == (
            base.model_request_timeout_seconds,
            base.agent_turn_timeout_seconds,
            base.skill_routing_timeout_seconds,
        )
        assert resolve_model_profile() is MODEL_PROFILES["fast"]

    def test_every_committed_profile_is_internally_coherent(self):
        # The same host-owned rules SPEC-011 §10 applies to one configuration.
        validate_model_profiles()

    def test_profiles_are_immutable(self):
        with pytest.raises(Exception):
            MODEL_PROFILES["fast"].model = "other"


class TestResolveModelProfile:
    def test_named_profile_is_returned(self):
        assert resolve_model_profile("deep") is MODEL_PROFILES["deep"]

    def test_unknown_profile_names_the_valid_ones(self):
        with pytest.raises(ValueError) as error:
            resolve_model_profile("huge")
        message = str(error.value)
        assert "huge" in message
        for name in MODEL_PROFILES:
            assert name in message

    def test_incoherent_profile_is_rejected_by_name(self, monkeypatch):
        broken = ModelProfile("broken", "qwen3:8b", 120, 10, 30)  # turn < request
        monkeypatch.setitem(MODEL_PROFILES, "broken", broken)

        with pytest.raises(ValueError, match="broken"):
            validate_model_profiles()


class TestCliSelection:
    def test_no_flag_selects_the_host_default(self):
        assert parse_args([]).profile is None
        assert resolve_model_profile(parse_args([]).profile) is MODEL_PROFILES["fast"]

    def test_flag_selects_the_named_profile(self):
        assert resolve_model_profile(parse_args(["--profile", "deep"]).profile) is (
            MODEL_PROFILES["deep"]
        )

    def test_unknown_profile_never_starts_the_application(self):
        # argparse rejects it before main() does anything, listing the choices.
        with pytest.raises(SystemExit):
            parse_args(["--profile", "huge"])

    def test_startup_line_names_the_model_and_its_deadlines(self):
        line = describe_profile(MODEL_PROFILES["deep"])
        assert "deep" in line and "qwen3:32b" in line
        assert "300s" in line and "600s" in line and "60s" in line


class TestTransportBinding:
    def test_module_builds_no_client_and_reads_no_model_at_import(self):
        # The whole point of SPEC-017 §4.4: the transport is injected, not
        # resolved from a constant when llm.py happens to be imported.
        assert not hasattr(llm, "client")
        assert not hasattr(config, "MODEL_NAME")

    def test_respond_sends_the_profile_model(self):
        client = FakeClient([text_chunk("hi")])
        model = OllamaModel("qwen3:32b", client)

        response = model.respond([{"role": "user", "content": "hi"}], [{"tool": 1}])

        assert list(response.text_chunks()) == ["hi"]
        assert client.calls[0]["model"] == "qwen3:32b"
        assert client.calls[0]["tools"] == [{"tool": 1}]
        assert client.calls[0]["stream"] is True

    def test_router_call_sends_no_tool_declarations(self):
        client = FakeClient([text_chunk('{"skill": '), text_chunk('"sales_analysis"}')])
        model = OllamaModel("qwen3:8b", client)

        assert model.text([{"role": "user", "content": "hi"}]) == '{"skill": "sales_analysis"}'
        assert client.calls[0]["tools"] is None

    def test_tool_calls_are_still_collected(self):
        client = FakeClient([tool_chunk("sql_query", {"query": "SELECT 1"})])
        model = OllamaModel("qwen3:32b", client)

        response = model.respond([{"role": "user", "content": "hi"}])
        assert list(response.text_chunks()) == []
        assert [call.name for call in response.tool_calls] == ["sql_query"]

    def test_for_profile_binds_the_profiles_model(self):
        model = OllamaModel.for_profile(MODEL_PROFILES["deep"])
        assert model.model == "qwen3:32b"

    def test_empty_history_is_still_rejected(self):
        with pytest.raises(ValueError):
            ModelResponse([], None, model="qwen3:8b", client=FakeClient())
