"""SkillRouter tests (SPEC-012 §"Unit tests: explicit selection" and "router").

The real router is driven with a scripted `RouteFn`, so no live Ollama is used.
Deadlines use real monotonic time but are generous; the one timeout test uses a
tiny component timeout plus a blocking route function.
"""

import threading
import time

import pytest

from reliability import (
    InvalidSkillSelection,
    SkillRoutingError,
    SkillRoutingTimeout,
)
from skill_runtime.models import SkillCatalogEntry
from skill_runtime.router import SkillRouter, parse_explicit_selection
from tests.support import ScriptedRouteFn
from tracing import MemoryTraceSink

CATALOG = (
    SkillCatalogEntry("sales_analysis", "Analyse sales and revenue data"),
    SkillCatalogEntry("database_exploration", "Inspect and explain database contents"),
)


def make_router(responses, **overrides):
    config = dict(
        timeout_seconds=5.0,
        max_response_chars=2000,
        repair_attempts=1,
    )
    config.update(overrides)
    route = ScriptedRouteFn(responses)
    return SkillRouter(route, **config), route


def select(router, message, *, context=None, catalog=CATALOG, trace=None):
    return router.select(
        user_message=message,
        conversation_context=context or [],
        catalog=catalog,
        deadline=time.monotonic() + 100,
        run_id="run-1",
        turn_id="turn-1",
        catalog_fingerprint="sha256:catalog",
        trace=trace or MemoryTraceSink(),
    )


# ---- explicit selection (parser + bypass) --------------------------------


def test_explicit_exact_phrase():
    names = ["sales_analysis"]
    assert parse_explicit_selection("use the sales_analysis skill now", names) == "sales_analysis"


def test_explicit_case_insensitive():
    names = ["sales_analysis"]
    assert parse_explicit_selection("USE THE Sales_Analysis SKILL", names) == "sales_analysis"


def test_explicit_alternate_wrappers():
    names = ["sales_analysis"]
    assert parse_explicit_selection("use skill sales_analysis", names) == "sales_analysis"
    assert parse_explicit_selection("with the sales_analysis skill", names) == "sales_analysis"


def test_unknown_name_not_explicit():
    assert parse_explicit_selection("use the financial_agent skill", ["sales_analysis"]) is None


def test_near_match_not_accepted():
    assert parse_explicit_selection("use the sales_analysis_v2 skill", ["sales_analysis"]) is None


def test_path_like_name_not_matched():
    assert parse_explicit_selection("use the ../sales skill", ["sales_analysis"]) is None


def test_ordinary_mention_not_misclassified():
    assert parse_explicit_selection("what does the sales_analysis skill do?", ["sales_analysis"]) is None


def test_explicit_selection_bypasses_model():
    router, route = make_router([])  # no scripted response available
    selection = select(router, "please use the sales_analysis skill")
    assert selection.skill_name == "sales_analysis"
    assert selection.source == "explicit"
    assert selection.routing_requests == 0
    assert route.calls == []  # the routing model was never called


# ---- model routing --------------------------------------------------------


def test_model_selects_valid_skill():
    router, route = make_router(['{"skill": "sales_analysis", "reason": "revenue"}'])
    selection = select(router, "Which genre earns the most?")
    assert selection.skill_name == "sales_analysis"
    assert selection.source == "model"
    assert selection.routing_requests == 1
    assert len(route.calls) == 1


def test_model_selects_none():
    router, _ = make_router(['{"skill": null, "reason": "general chat"}'])
    selection = select(router, "Explain what an agent loop is.")
    assert selection.skill_name is None
    assert selection.source == "model"


def test_malformed_json_repaired_once():
    router, route = make_router(
        ["not json at all", '{"skill": "sales_analysis", "reason": "ok"}']
    )
    selection = select(router, "revenue by genre")
    assert selection.skill_name == "sales_analysis"
    assert selection.routing_requests == 2
    # The repair request must carry the allowed names and the required shape.
    repair_messages = route.calls[1]
    assert any("sales_analysis" in m["content"] for m in repair_messages)
    assert any("invalid" in m["content"].lower() for m in repair_messages)


def test_unknown_name_repaired_once():
    router, _ = make_router(
        ['{"skill": "financial_super_agent"}', '{"skill": null, "reason": "none"}']
    )
    selection = select(router, "hello")
    assert selection.skill_name is None
    assert selection.routing_requests == 2


def test_second_invalid_response_fails():
    router, _ = make_router(["garbage one", "garbage two"])
    with pytest.raises(InvalidSkillSelection):
        select(router, "revenue")


def test_oversized_response_fails_after_repair():
    big = '{"skill": "sales_analysis", "reason": "' + "x" * 5000 + '"}'
    router, _ = make_router([big, big], max_response_chars=100)
    with pytest.raises(InvalidSkillSelection):
        select(router, "revenue")


def test_transport_failure():
    router, _ = make_router([RuntimeError("connection refused")])
    with pytest.raises(SkillRoutingError):
        select(router, "revenue")


def test_routing_timeout():
    blocker = threading.Event()  # never set

    def blocking_route(_messages):
        blocker.wait()
        return "{}"

    router = SkillRouter(
        blocking_route, timeout_seconds=0.02, max_response_chars=2000, repair_attempts=1
    )
    with pytest.raises(SkillRoutingTimeout):
        select(router, "revenue")


def test_empty_catalog_skips_model():
    router, route = make_router([])
    selection = select(router, "anything", catalog=())
    assert selection.skill_name is None
    assert selection.source == "none"
    assert route.calls == []


def test_router_never_receives_full_instructions():
    router, route = make_router(['{"skill": null}'])
    select(router, "revenue")
    sent = str(route.calls[0])
    # Only catalog descriptions are exposed, never a full SKILL.md body.
    assert "Analyse sales and revenue data" in sent
    assert "## Procedure" not in sent


def test_bounded_conversation_context():
    router, route = make_router(['{"skill": null}'])
    long_message = {"role": "user", "content": "z" * 2000}
    select(router, "revenue", context=[long_message])
    sent = str(route.calls[0])
    assert "…" in sent  # per-message context is truncated


def test_repair_started_traced():
    trace = MemoryTraceSink()
    router, _ = make_router(["bad", '{"skill": null}'])
    select(router, "hi", trace=trace)
    events = [e["event"] for e in trace.events]
    assert "skill_routing_started" in events
    assert "skill_routing_repair_started" in events
    assert "skill_routing_finished" in events
