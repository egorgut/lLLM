"""Component-specific model role tests (SPEC-019).

Nothing here contacts Ollama. Both roles are exercised through fake clients that
record what they were asked for, which is also how a split run is *proved*: the
routing call has to land on the router's model and the agent call on the agent's,
under each role's own deadlines.
"""

import time
from types import SimpleNamespace

import pytest

from app import (
    build_model_transports,
    describe_model_roles,
    describe_profile,
    parse_args,
    run_started_event,
    validate_model_roles,
)
from config import (
    MODEL_PROFILES,
    ModelProfile,
    ModelRoles,
    resolve_model_roles,
)
from llm import ROUTING_RESPONSE_SCHEMA, OllamaModel
from reliability import SkillRoutingTimeout
from skill_runtime.models import SkillCatalogEntry
from skill_runtime.router import SkillRouter
from tracing import MemoryTraceSink

CATALOG = (SkillCatalogEntry("sales_analysis", "Analyse sales and revenue data"),)


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


def roles_from(argv: list[str]) -> ModelRoles:
    args = parse_args(argv)
    return resolve_model_roles(args.profile, args.router_profile)


class TestRoleSelection:
    def test_no_arguments_run_both_roles_on_the_host_default(self):
        roles = roles_from([])

        assert roles.agent is MODEL_PROFILES["fast"]
        assert roles.router is MODEL_PROFILES["fast"]
        assert not roles.split

    def test_profile_alone_still_selects_one_profile_for_both_roles(self):
        # SPEC-017's contract, unchanged: --profile owns the whole run unless a
        # router override is supplied.
        roles = roles_from(["--profile", "next"])

        assert roles.agent is MODEL_PROFILES["next"]
        assert roles.router is MODEL_PROFILES["next"]
        assert not roles.split

    def test_router_override_splits_the_two_roles(self):
        roles = roles_from(["--profile", "next", "--router-profile", "fast"])

        assert roles.agent.model == "qwen3.8:27b"
        assert roles.router.model == "qwen3:8b"
        assert roles.split

    def test_router_override_alone_keeps_the_default_agent_profile(self):
        roles = roles_from(["--router-profile", "next"])

        assert roles.agent is MODEL_PROFILES["fast"]
        assert roles.router is MODEL_PROFILES["next"]
        assert roles.split

    def test_naming_the_same_profile_twice_is_not_a_split(self):
        # `MODEL_PROFILES` hands out one object per name, so this collapses back
        # to the historical single-transport path rather than building two.
        assert not roles_from(["--profile", "deep", "--router-profile", "deep"]).split

    def test_unknown_router_profile_never_starts_the_application(self):
        # argparse rejects it before main() does anything, listing the choices.
        with pytest.raises(SystemExit):
            parse_args(["--router-profile", "huge"])

    def test_unknown_router_profile_is_rejected_by_the_resolver_too(self):
        with pytest.raises(ValueError, match="huge"):
            resolve_model_roles("fast", "huge")

    def test_there_is_no_agent_profile_flag(self):
        # --profile already owns that role and must stay backward compatible
        # (SPEC-019 §4.2).
        with pytest.raises(SystemExit):
            parse_args(["--agent-profile", "next"])


class TestRoleTransports:
    def test_one_profile_reuses_one_transport(self):
        transports = build_model_transports(resolve_model_roles("deep"))

        assert transports.router is transports.agent
        assert transports.agent.model == "qwen3:32b"

    def test_a_split_builds_two_transports_bound_to_their_own_models(self):
        transports = build_model_transports(resolve_model_roles("next", "fast"))

        assert transports.router is not transports.agent
        assert transports.agent.model == "qwen3.8:27b"
        assert transports.router.model == "qwen3:8b"


class TestRoleCallableProvenance:
    """Which transport each component actually calls, observed at the client."""

    def test_each_role_reaches_only_its_own_transport(self):
        # The whole claim of a split run, observed at the two clients: routing
        # lands on the router's model, the agent decision on the agent's, and
        # neither call crosses over.
        router_client = FakeClient([text_chunk('{"skill": null, "reason": "n/a"}')])
        agent_client = FakeClient([text_chunk("done")])
        router_model = OllamaModel("qwen3:8b", router_client)
        agent_model = OllamaModel("qwen3.8:27b", agent_client)

        router = SkillRouter(
            route=router_model.text,
            timeout_seconds=5.0,
            max_response_chars=2000,
            repair_attempts=1,
        )
        router.select(
            user_message="hello",
            conversation_context=[],
            catalog=CATALOG,
            deadline=time.monotonic() + 100,
            run_id="run-1",
            turn_id="turn-1",
            trace=MemoryTraceSink(),
        )
        list(agent_model.respond([{"role": "user", "content": "hello"}]).text_chunks())

        assert [call["model"] for call in router_client.calls] == ["qwen3:8b"]
        assert [call["model"] for call in agent_client.calls] == ["qwen3.8:27b"]

    def test_a_split_router_keeps_the_routing_generation_contract(self):
        # PATCH-012-01 is a property of the routing call, not of the profile that
        # happens to serve it: a different router model must not loosen it.
        client = FakeClient([text_chunk('{"skill": null, "reason": "n/a"}')])
        OllamaModel("qwen3:8b", client).text([{"role": "user", "content": "hi"}])

        assert client.calls[0]["think"] is False
        assert client.calls[0]["format"] is ROUTING_RESPONSE_SCHEMA
        assert client.calls[0]["tools"] is None

    def test_the_agent_responds_on_the_agent_model(self):
        agent_client = FakeClient([text_chunk("hi")])
        agent_model = OllamaModel("qwen3.8:27b", agent_client)

        response = agent_model.respond([{"role": "user", "content": "hi"}], [{"tool": 1}])

        assert list(response.text_chunks()) == ["hi"]
        assert agent_client.calls[0]["model"] == "qwen3.8:27b"
        # Unchanged agent behavior: reasoning and format stay the SDK defaults.
        assert agent_client.calls[0]["think"] is None
        assert agent_client.calls[0]["format"] is None


class TestRoleDeadlineOwnership:
    """Which profile each deadline comes from (SPEC-019 §4.4)."""

    def test_the_router_profile_supplies_the_routing_deadline(self):
        assert resolve_model_roles("next", "fast").router.skill_routing_timeout_seconds == 30

    def test_routing_is_abandoned_on_the_router_profiles_deadline(self, monkeypatch):
        # Behavioral, with a synthetic router profile so the test does not have
        # to wait out a committed 30s deadline: routing stops on the *router*
        # profile's own timeout, well inside the agent profile's turn budget.
        brisk = ModelProfile("brisk-router", "qwen3:8b", 120, 180, 0.02)
        monkeypatch.setitem(MODEL_PROFILES, "brisk-router", brisk)
        roles = resolve_model_roles("next", "brisk-router")

        def blocking_route(messages):
            time.sleep(1)
            return '{"skill": null, "reason": "n/a"}'

        router = SkillRouter(
            route=blocking_route,
            timeout_seconds=roles.router.skill_routing_timeout_seconds,
            max_response_chars=2000,
            repair_attempts=1,
        )
        with pytest.raises(SkillRoutingTimeout):
            router.select(
                user_message="hello",
                conversation_context=[],
                catalog=CATALOG,
                # The whole-turn budget comes from the agent profile and is far
                # from exhausted, so only the router deadline can have fired.
                deadline=time.monotonic() + roles.agent.agent_turn_timeout_seconds,
                run_id="run-1",
                turn_id="turn-1",
                trace=MemoryTraceSink(),
            )

    def test_the_agent_owns_the_request_and_whole_turn_budgets(self):
        roles = resolve_model_roles("next", "fast")

        assert roles.agent.model_request_timeout_seconds == 250
        assert roles.agent.agent_turn_timeout_seconds == 500
        # The router's own turn/request numbers never reach the agent loop.
        assert roles.router.agent_turn_timeout_seconds == 180


class TestRolePairValidation:
    def test_every_committed_combination_is_coherent(self):
        for agent in MODEL_PROFILES:
            for router in MODEL_PROFILES:
                validate_model_roles(resolve_model_roles(agent, router))

    def test_a_router_that_cannot_finish_inside_the_turn_is_rejected(self, monkeypatch):
        # Synthetic, because no committed pair is incoherent: a router allowed to
        # route for 400s under a 180s turn would be bounded by the turn budget
        # rather than by its own routing deadline.
        slow_router = ModelProfile("slow-router", "qwen3:8b", 120, 180, 400)
        monkeypatch.setitem(MODEL_PROFILES, "slow-router", slow_router)

        with pytest.raises(ValueError) as error:
            validate_model_roles(resolve_model_roles("fast", "slow-router"))

        message = str(error.value)
        assert "slow-router" in message and "fast" in message


class TestRoleDiagnostics:
    def test_one_profile_prints_the_historical_single_line(self):
        roles = resolve_model_roles("deep")

        assert describe_model_roles(roles) == [
            f"[model] {describe_profile(MODEL_PROFILES['deep'])}"
        ]

    def test_a_split_names_both_roles_unambiguously(self):
        lines = describe_model_roles(resolve_model_roles("next", "fast"))

        assert lines == [
            "[model] agent next: qwen3.8:27b (request 250s, turn 500s)",
            "[router] fast: qwen3:8b (request 120s, routing 30s)",
        ]


class TestRunStartedTrace:
    def test_existing_fields_still_identify_the_agent_model(self):
        event = run_started_event("run-1", resolve_model_roles("next", "fast"))

        assert event["event"] == "run_started"
        assert event["model_name"] == "qwen3.8:27b"
        assert event["model_profile"] == "next"

    def test_router_identity_is_additive_and_recoverable_from_the_trace(self):
        event = run_started_event("run-1", resolve_model_roles("next", "fast"))

        assert event["router_model_name"] == "qwen3:8b"
        assert event["router_model_profile"] == "fast"

    def test_router_identity_is_populated_on_a_monolithic_run_too(self):
        # The documented choice (SPEC-019 §4.9): a consumer never has to infer
        # the router from a missing field.
        event = run_started_event("run-1", resolve_model_roles("deep"))

        assert event["model_profile"] == "deep"
        assert event["router_model_profile"] == "deep"
        assert event["router_model_name"] == "qwen3:32b"
