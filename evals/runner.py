"""Scripted and live agent evaluations (SPEC-011).

Unit tests (`tests/test_agent_runner.py`) verify that the harness enforces its
own policies. This module verifies that the *assembled* agent completes a
small set of representative tasks acceptably:

    unit test:  did the harness enforce the policy?
    evaluation: did the model + harness complete the task acceptably?

The scripted suite drives `AgentRunner` with the same deterministic fixtures
as the committed tests (`tests/support.py`) and requires no live Ollama, no
live MCP server, and no real database — it is safe for a default CI-style
gate. The live suite exercises the real model, the real local tools, and the
real MCP server; it is optional, run manually, and never part of that gate.

Usage:

    python -m evals.runner --suite scripted
    python -m evals.runner --suite live
"""

import argparse
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import AgentRunner
from config import (
    MAX_IDENTICAL_TOOL_CALLS,
    MAX_SKILL_ACTIVATIONS_PER_TURN,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_INSTRUCTION_CHARS,
    MAX_SKILL_ROUTING_RESPONSE_CHARS,
    MAX_SKILL_SCHEMA_BYTES,
    MAX_SKILLS,
    MAX_TOOL_CALLS_PER_TURN,
    MODEL_PROFILES,
    SKILL_ROUTING_REPAIR_ATTEMPTS,
    SKILLS_ROOT,
    TOOL_EXECUTION_TIMEOUT_SECONDS,
    TRACE_PAYLOAD_PREVIEW_CHARS,
    ModelProfile,
    resolve_model_profile,
)
from conversation import Conversation
from reliability import TurnStatus, new_id
from skill_runtime import (
    SkillPackageLoader,
    SkillRouter,
    SkillTurnOrchestrator,
    validate_skill_config,
)
from tests.support import (
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    ScriptedRouteFn,
    make_tool_call,
    make_tool_registry,
)
from tracing import MemoryTraceSink, NullTraceSink

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The scripted suite never contacts a model, so it is pinned to one profile's
# deadlines regardless of --profile (SPEC-017 §4.3): its committed results must
# stay comparable across runs, and its scripted doubles have no latency at all.
SCRIPTED_PROFILE = MODEL_PROFILES["fast"]
DEFAULT_CASES_PATH = PROJECT_ROOT / "evals" / "cases.json"
RESULTS_DIR = PROJECT_ROOT / "data" / "evals"
SCHEMA_VERSION = 1


@dataclass
class CaseResult:
    id: str
    passed: bool
    status: str
    reason: str
    tool_calls: list[str]
    duration_ms: int
    failures: list[str] = field(default_factory=list)
    selected_skill: str | None = None
    selection_source: str | None = None
    routing_requests: int | None = None
    # SPEC-018: the router's choice is no longer necessarily the turn's last
    # word, so a skill case reports both ends of the decision.
    final_skill: str | None = None
    skill_activations: int | None = None
    # PATCH-018-01, live evidence. `tool_sequence` is read from the trace rather
    # than from the recording executor, because a control tool never reaches an
    # executor and would otherwise be invisible in the very measurement it is
    # the subject of. The remaining fields are what SPEC-018 §7.2 asked to
    # record per profile.
    profile: str | None = None
    model_requests: int | None = None
    tool_sequence: list[str] = field(default_factory=list)
    activation_events: list[dict[str, Any]] = field(default_factory=list)
    model_request_ms: list[int] = field(default_factory=list)


def load_cases(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_expectation(
    outcome,
    tool_calls_used: list[str],
    expectation: dict[str, Any],
    selection=None,
    final_skill: str | None = None,
    activations: int | None = None,
) -> list[str]:
    """Objective, deterministic assertions only -- no LLM judge (SPEC-011 §6).

    `selection` (a `SkillSelection`, present only for skill cases) enables the
    skill-specific keys `expected_selection` and `selection_source` (SPEC-012).
    `final_skill`/`activations` enable `expected_final_skill` and
    `expected_activations`, which describe how the skill decision *ended*
    (SPEC-018): the router's choice alone no longer says what a turn ran under.
    """

    failures: list[str] = []

    if "expected_selection" in expectation:
        got = selection.skill_name if selection is not None else None
        if got != expectation["expected_selection"]:
            failures.append(
                f"expected selection={expectation['expected_selection']!r}, got {got!r}"
            )

    if "expected_final_skill" in expectation:
        if final_skill != expectation["expected_final_skill"]:
            failures.append(
                f"expected final_skill={expectation['expected_final_skill']!r}, "
                f"got {final_skill!r}"
            )

    if "expected_activations" in expectation:
        if activations != expectation["expected_activations"]:
            failures.append(
                f"expected skill_activations={expectation['expected_activations']!r}, "
                f"got {activations!r}"
            )

    if "selection_source" in expectation and selection is not None:
        if selection.source != expectation["selection_source"]:
            failures.append(
                f"expected selection_source={expectation['selection_source']!r}, "
                f"got {selection.source!r}"
            )

    if "forbidden_tools" in expectation:
        forbidden = set(expectation["forbidden_tools"])
        used = [t for t in tool_calls_used if t in forbidden]
        if used:
            failures.append(f"forbidden tool calls used: {used}")

    if "status" in expectation and str(outcome.status) != expectation["status"]:
        failures.append(f"expected status={expectation['status']!r}, got {outcome.status!r}")

    if "reason" in expectation and str(outcome.reason) != expectation["reason"]:
        failures.append(f"expected reason={expectation['reason']!r}, got {outcome.reason!r}")

    if "required_tools" in expectation:
        missing = [t for t in expectation["required_tools"] if t not in tool_calls_used]
        if missing:
            failures.append(f"missing required tool calls: {missing}")

    if "allowed_tools" in expectation:
        allowed = set(expectation["allowed_tools"])
        disallowed = [t for t in tool_calls_used if t not in allowed]
        if disallowed:
            failures.append(f"disallowed tool calls used: {disallowed}")

    if "min_tool_calls" in expectation and len(tool_calls_used) < expectation["min_tool_calls"]:
        failures.append(
            f"expected at least {expectation['min_tool_calls']} tool calls, "
            f"got {len(tool_calls_used)}"
        )

    if "max_tool_calls" in expectation and len(tool_calls_used) > expectation["max_tool_calls"]:
        failures.append(
            f"expected at most {expectation['max_tool_calls']} tool calls, "
            f"got {len(tool_calls_used)}"
        )

    if outcome.status is TurnStatus.COMPLETED:
        text = outcome.final_text or ""
        for substring in expectation.get("answer_contains", []):
            if substring.lower() not in text.lower():
                failures.append(f"answer does not contain {substring!r}")
        pattern = expectation.get("answer_matches")
        if pattern and not re.search(pattern, text):
            failures.append(f"answer does not match pattern {pattern!r}")

    if "max_duration_ms" in expectation and outcome.duration_ms > expectation["max_duration_ms"]:
        failures.append(
            f"expected duration <= {expectation['max_duration_ms']}ms, "
            f"got {outcome.duration_ms}ms"
        )

    return failures


def _declared_tool_names(case: dict[str, Any]) -> list[str]:
    names = set(case.get("tool_results", {}).keys())
    for item in case.get("script", []):
        if "tool_call" in item:
            names.add(item["tool_call"]["name"])
    return sorted(names)


def _tool_result_handler(results: Any):
    """A fake handler returning `results` verbatim, or cycling a list of them
    (holding on the last entry once exhausted) -- needed for cases like
    sql-recovery-001 where the same tool name must return different results on
    successive calls."""

    if isinstance(results, list):
        remaining = list(results)

        def handler(arguments: dict) -> dict:
            if remaining:
                return remaining.pop(0)
            return results[-1]

        return handler
    return lambda arguments: results


def _build_scripted_response(item: dict[str, Any]) -> ScriptedModelResponse:
    if "text" in item:
        return ScriptedModelResponse(text=item["text"])
    if "tool_call" in item:
        call = make_tool_call(item["tool_call"]["name"], item["tool_call"]["arguments"])
        return ScriptedModelResponse(tool_calls=[call])
    if item.get("block"):
        return ScriptedModelResponse(block_on=threading.Event())
    raise ValueError(f"Unrecognized scripted response item: {item}")


def run_scripted_case(case: dict[str, Any]) -> CaseResult:
    responder = ScriptedResponder(
        [_build_scripted_response(item) for item in case.get("script", [])]
    )
    executor = FakeToolExecutor(
        {
            name: _tool_result_handler(results)
            for name, results in case.get("tool_results", {}).items()
        }
    )

    runner_config = dict(
        max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
        max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
        model_request_timeout_seconds=SCRIPTED_PROFILE.model_request_timeout_seconds,
        tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        agent_turn_timeout_seconds=SCRIPTED_PROFILE.agent_turn_timeout_seconds,
    )
    runner_config.update(case.get("runner_overrides", {}))

    runner = AgentRunner(
        respond=responder,
        executor=executor,
        tools=[
            {"type": "function", "function": {"name": name}}
            for name in _declared_tool_names(case)
        ],
        renderer=RecordingRenderer(),
        run_id="eval-scripted",
        trace_sink=NullTraceSink(),
        **runner_config,
    )

    outcome = runner.run_turn(
        [{"role": "user", "content": case["prompt"]}], turn_id=case["id"]
    )
    tool_calls_used = [name for name, _ in executor.calls]
    failures = evaluate_expectation(outcome, tool_calls_used, case["expectation"])
    return CaseResult(
        id=case["id"],
        passed=not failures,
        status=str(outcome.status),
        reason=str(outcome.reason),
        tool_calls=tool_calls_used,
        duration_ms=outcome.duration_ms,
        failures=failures,
    )


def run_scripted_skill_case(case: dict[str, Any]) -> CaseResult:
    """A scripted skill turn through the real `SkillTurnOrchestrator`.

    The router is the real one driven by a scripted `RouteFn` (or bypassed by an
    explicit request in the prompt); the reference skills under `skills/` are
    loaded and validated for real. No live Ollama, MCP, or database is involved.
    """

    tool_registry = make_tool_registry(
        "sql_query",
        "python_calculate",
        "sandbox_execute",
        "mcp_time__get_current_time",
        "mcp_tracker__issue_get",
        "mcp_tracker__issues_find",
        "mcp_tracker__queue_get_metadata",
        "mcp_tracker__issue_get_comments",
    )
    skill_registry = SkillPackageLoader().load_all(SKILLS_ROOT, tool_registry)
    executor = FakeToolExecutor(
        {
            name: _tool_result_handler(results)
            for name, results in case.get("tool_results", {}).items()
        }
    )
    router = SkillRouter(
        ScriptedRouteFn(list(case.get("route", []))),
        timeout_seconds=SCRIPTED_PROFILE.skill_routing_timeout_seconds,
        max_response_chars=MAX_SKILL_ROUTING_RESPONSE_CHARS,
        repair_attempts=SKILL_ROUTING_REPAIR_ATTEMPTS,
    )
    responder = ScriptedResponder(
        [_build_scripted_response(item) for item in case.get("script", [])]
    )
    orchestrator = SkillTurnOrchestrator(
        skill_registry=skill_registry,
        router=router,
        tool_registry=tool_registry,
        executor=executor,
        respond=responder,
        renderer_factory=RecordingRenderer,
        default_tools=tool_registry.to_ollama_tools(),
        run_id="eval-scripted",
        max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
        max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
        model_request_timeout_seconds=SCRIPTED_PROFILE.model_request_timeout_seconds,
        tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        agent_turn_timeout_seconds=SCRIPTED_PROFILE.agent_turn_timeout_seconds,
        trace_sink=NullTraceSink(),
    )

    conversation = Conversation()
    conversation.add_user_message(case["prompt"])
    result = orchestrator.run_turn(conversation)

    tool_calls_used = [name for name, _ in executor.calls]
    failures = evaluate_expectation(
        result.outcome,
        tool_calls_used,
        case["expectation"],
        selection=result.selection,
        final_skill=result.final_skill,
        activations=result.activations,
    )
    return CaseResult(
        id=case["id"],
        passed=not failures,
        status=str(result.outcome.status),
        reason=str(result.outcome.reason),
        tool_calls=tool_calls_used,
        duration_ms=result.outcome.duration_ms,
        failures=failures,
        selected_skill=result.selection.skill_name,
        selection_source=result.selection.source,
        routing_requests=result.selection.routing_requests,
        final_skill=result.final_skill,
        skill_activations=result.activations,
    )


class _RecordingExecutorWrapper:
    """Wraps the real `ToolExecutor` so the live suite can see which tools ran,
    without changing the production dispatch path itself."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments) if isinstance(arguments, dict) else arguments))
        return self._inner.execute(name, arguments)


def _render_live_prompt(template: str) -> str:
    """Substitute operator-supplied Tracker smoke fixtures into a live prompt.

    A live case must never hardcode a real issue/queue/query -- those are
    operator-specific (SPEC-013 §"Live smoke suite"). A placeholder left
    unset renders as a visible "not set" marker rather than a plausible-looking
    fake identifier, so a misconfigured run fails obviously instead of quietly
    exercising the wrong Tracker object.
    """

    values = {
        "tracker_issue_id": os.environ.get(
            "TRACKER_SMOKE_ISSUE_ID", "<TRACKER_SMOKE_ISSUE_ID not set>"
        ),
        "tracker_queue_id": os.environ.get(
            "TRACKER_SMOKE_QUEUE_ID", "<TRACKER_SMOKE_QUEUE_ID not set>"
        ),
        "tracker_search_query": os.environ.get(
            "TRACKER_SMOKE_SEARCH_QUERY", "<TRACKER_SMOKE_SEARCH_QUERY not set>"
        ),
    }
    return template.format(**values)


def run_live_case(
    case: dict[str, Any], executor, tools, respond, run_id: str, profile: ModelProfile
) -> CaseResult:
    from prompts import SYSTEM_PROMPT

    runner = AgentRunner(
        respond=respond,
        executor=executor,
        tools=tools,
        renderer=RecordingRenderer(),
        run_id=run_id,
        max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
        max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
        model_request_timeout_seconds=profile.model_request_timeout_seconds,
        tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        agent_turn_timeout_seconds=profile.agent_turn_timeout_seconds,
        trace_sink=NullTraceSink(),
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _render_live_prompt(case["prompt"])},
    ]

    executor.calls.clear()
    try:
        outcome = runner.run_turn(messages, turn_id=case["id"])
    except Exception as error:
        return CaseResult(
            id=case["id"],
            passed=False,
            status="failed",
            reason="internal_error",
            tool_calls=[],
            duration_ms=0,
            failures=[f"live case raised: {error}"],
        )

    tool_calls_used = [name for name, _ in executor.calls]
    failures = evaluate_expectation(outcome, tool_calls_used, case["expectation"])
    return CaseResult(
        id=case["id"],
        passed=not failures,
        status=str(outcome.status),
        reason=str(outcome.reason),
        tool_calls=tool_calls_used,
        duration_ms=outcome.duration_ms,
        failures=failures,
        profile=profile.name,
        model_requests=outcome.model_requests,
    )


def _turn_events(trace: MemoryTraceSink, turn_id: str) -> list[dict[str, Any]]:
    return [event for event in trace.events if event.get("turn_id") == turn_id]


def run_live_skill_case(
    case: dict[str, Any],
    orchestrator: SkillTurnOrchestrator,
    trace: MemoryTraceSink,
    executor,
    profile: ModelProfile,
) -> CaseResult:
    """One live case through the *real* skill-aware turn (PATCH-018-01).

    SPEC-018 §7.2 could not be answered by the pre-existing live suite: it drives
    a raw `AgentRunner`, so no live turn ever routed a skill, and none could
    activate one. This runs the case through the same `SkillTurnOrchestrator`
    `app.py` builds, with the same registry, router, skills, limits, and profile
    deadlines — the eval reimplements no routing or activation semantics of its
    own, it only observes.
    """

    conversation = Conversation()
    conversation.add_user_message(_render_live_prompt(case["prompt"]))
    executor.calls.clear()

    try:
        result = orchestrator.run_turn(conversation)
    except Exception as error:  # pragma: no cover - live-only safety net
        return CaseResult(
            id=case["id"],
            passed=False,
            status="failed",
            reason="internal_error",
            tool_calls=[],
            duration_ms=0,
            failures=[f"live skill case raised: {error}"],
            profile=profile.name,
        )

    events = _turn_events(trace, result.outcome.turn_id)
    # The model-decided order, control tools included: `tool_call_requested` is
    # emitted for every call the loop accepted, before dispatch chooses between
    # the executor and the host's control handler.
    tool_sequence = [
        event["tool_name"] for event in events if event["event"] == "tool_call_requested"
    ]
    activation_events = [
        {
            "skill": event["skill"],
            "replaced_skill": event["replaced_skill"],
            "activation_index": event["activation_index"],
            "recomposed": event["recomposed"],
        }
        for event in events
        if event["event"] == "skill_activated"
    ]
    model_request_ms = [
        event["duration_ms"]
        for event in events
        if event["event"] == "model_response_finished"
    ]

    tool_calls_used = [name for name, _ in executor.calls]
    failures = evaluate_expectation(
        result.outcome,
        tool_calls_used,
        case["expectation"],
        selection=result.selection,
        final_skill=result.final_skill,
        activations=result.activations,
    )
    return CaseResult(
        id=case["id"],
        passed=not failures,
        status=str(result.outcome.status),
        reason=str(result.outcome.reason),
        tool_calls=tool_calls_used,
        duration_ms=result.outcome.duration_ms,
        failures=failures,
        selected_skill=result.selection.skill_name,
        selection_source=result.selection.source,
        routing_requests=result.selection.routing_requests,
        final_skill=result.final_skill,
        skill_activations=result.activations,
        profile=profile.name,
        model_requests=result.outcome.model_requests,
        tool_sequence=tool_sequence,
        activation_events=activation_events,
        model_request_ms=model_request_ms,
    )


def _build_live_orchestrator(
    registry,
    executor,
    mcp_servers,
    model,
    profile: ModelProfile,
    run_id: str,
    trace: MemoryTraceSink,
    sandbox,
) -> SkillTurnOrchestrator:
    """Assemble the production skill-aware turn exactly as `app.py` does.

    Every component here is the production one — `app.omitted_skills`, the real
    `SkillPackageLoader`, the real `SkillRouter` on the profile's own routing
    deadline, the real `SkillTurnOrchestrator` under the host's own limits. The
    only differences from `app.py:main()` are the ones an eval must have: a
    recording renderer instead of the CLI one, and an in-memory trace sink the
    runner can read the evidence back out of.
    """

    from app import omitted_skills

    validate_skill_config(
        skill_routing_timeout_seconds=profile.skill_routing_timeout_seconds,
        skill_routing_repair_attempts=SKILL_ROUTING_REPAIR_ATTEMPTS,
        max_skill_routing_response_chars=MAX_SKILL_ROUTING_RESPONSE_CHARS,
        max_skill_instruction_chars=MAX_SKILL_INSTRUCTION_CHARS,
        max_skill_schema_bytes=MAX_SKILL_SCHEMA_BYTES,
        max_skills=MAX_SKILLS,
        max_skill_description_chars=MAX_SKILL_DESCRIPTION_CHARS,
        max_skill_activations_per_turn=MAX_SKILL_ACTIVATIONS_PER_TURN,
    )
    skill_registry = SkillPackageLoader().load_all(
        SKILLS_ROOT, registry, omit=omitted_skills(mcp_servers, sandbox)
    )
    router = SkillRouter(
        route=model.text,
        timeout_seconds=profile.skill_routing_timeout_seconds,
        max_response_chars=MAX_SKILL_ROUTING_RESPONSE_CHARS,
        repair_attempts=SKILL_ROUTING_REPAIR_ATTEMPTS,
        payload_preview_chars=TRACE_PAYLOAD_PREVIEW_CHARS,
    )
    return SkillTurnOrchestrator(
        skill_registry=skill_registry,
        router=router,
        tool_registry=registry,
        executor=executor,
        respond=model.respond,
        renderer_factory=RecordingRenderer,
        default_tools=registry.to_ollama_tools(),
        run_id=run_id,
        max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
        max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
        model_request_timeout_seconds=profile.model_request_timeout_seconds,
        tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        agent_turn_timeout_seconds=profile.agent_turn_timeout_seconds,
        trace_sink=trace,
        payload_preview_chars=TRACE_PAYLOAD_PREVIEW_CHARS,
        max_skill_activations=MAX_SKILL_ACTIVATIONS_PER_TURN,
        on_turn_context=(
            sandbox.workspace.begin_turn if sandbox else lambda _context: None
        ),
        redacted_argument_tools=(
            frozenset({sandbox.spec.name}) if sandbox else frozenset()
        ),
    )


def _run_live_cases(cases: list[dict[str, Any]], profile: ModelProfile) -> list[CaseResult]:
    # Imported lazily: the live suite is the only path that needs a real
    # Ollama connection, real local tools, and a real MCP server. Keeping this
    # import out of the module top level means the scripted suite (and the
    # rest of the test/import graph) never depends on any of that.
    from app import build_executor, build_mcp_servers, load_dotenv_if_present, register_mcp_tools
    from config import (
        PROJECT_ROOT,
        SANDBOX_ARTIFACT_ROOT,
        SANDBOX_TOOL_ENABLED,
        SANDBOX_TURN_TIME_MARGIN_SECONDS,
    )
    from llm import OllamaModel
    from mcp_integration import McpClientManager, McpStartupError
    from sandbox_tool import build_sandbox_capability

    # Same gap-filling as app.py's main(): a local .env supplies Tracker
    # credentials for this manual live run without needing them re-exported.
    load_dotenv_if_present()

    registry, executor = build_executor()
    recording_executor = _RecordingExecutorWrapper(executor)
    run_id = new_id()
    trace = MemoryTraceSink()
    skill_cases = [case for case in cases if case.get("skill_case")]
    # The sandbox is built only for the skill-aware path (PATCH-018-01): it adds
    # a tool declaration, and the pre-existing live suite's model-facing tool
    # list must stay exactly what it was before this patch.
    sandbox = None
    if skill_cases:
        sandbox, _diagnostic = build_sandbox_capability(
            run_id=run_id,
            artifact_root=SANDBOX_ARTIFACT_ROOT,
            project_root=PROJECT_ROOT,
            turn_time_margin_seconds=SANDBOX_TURN_TIME_MARGIN_SECONDS,
            enabled=SANDBOX_TOOL_ENABLED,
            trace_sink=trace,
        )
        if sandbox is not None:
            registry.register(sandbox.spec)
            executor.register_handler(sandbox.spec.name, sandbox.handler)
    # Reuses exactly the same effective server map (static local servers +
    # conditional Tracker) that app.py's main() builds, so a live Tracker
    # eval and a live `python app.py` session see identical MCP configuration.
    mcp_servers = build_mcp_servers()
    manager = McpClientManager(
        mcp_servers,
        call_timeout=TOOL_EXECUTION_TIMEOUT_SECONDS,
        run_id=run_id,
        trace_sink=NullTraceSink(),
    )

    try:
        try:
            manager.start()
            register_mcp_tools(registry, executor, manager, mcp_servers)
        except McpStartupError as error:
            return [
                CaseResult(
                    id=case["id"],
                    passed=False,
                    status="failed",
                    reason="internal_error",
                    tool_calls=[],
                    duration_ms=0,
                    failures=[f"MCP startup failed: {error}"],
                )
                for case in cases
            ]

        tools = registry.to_ollama_tools()

        model = OllamaModel.for_profile(profile)

        orchestrator = (
            _build_live_orchestrator(
                registry,
                recording_executor,
                mcp_servers,
                model,
                profile,
                run_id,
                trace,
                sandbox,
            )
            if skill_cases
            else None
        )

        return [
            run_live_skill_case(case, orchestrator, trace, recording_executor, profile)
            if case.get("skill_case")
            else run_live_case(
                case, recording_executor, tools, model.respond, run_id, profile
            )
            for case in cases
        ]
    finally:
        manager.close()


def run_suite(
    suite: str,
    cases_path: Path,
    category: str | None = None,
    profile: ModelProfile | None = None,
) -> tuple[dict[str, int], list[CaseResult]]:
    cases = load_cases(cases_path)
    applicable = [case for case in cases if suite in case.get("modes", ["scripted", "live"])]
    if category is not None:
        applicable = [case for case in applicable if case.get("category", "").startswith(category)]

    if suite == "scripted":
        results = [
            run_scripted_skill_case(case)
            if case.get("skill_case")
            else run_scripted_case(case)
            for case in applicable
        ]
    elif suite == "live":
        results = _run_live_cases(applicable, profile or resolve_model_profile())
    else:
        raise ValueError(f"Unknown suite: {suite}")

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
    }
    return summary, results


def write_results(
    suite: str, summary: dict[str, int], results: list[CaseResult], model_name: str
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{timestamp}-{suite}.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "suite": suite,
        "started_at": started_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "model": model_name,
        "summary": summary,
        "cases": [
            {
                "id": result.id,
                "passed": result.passed,
                "status": result.status,
                "reason": result.reason,
                "selected_skill": result.selected_skill,
                "selection_source": result.selection_source,
                "routing_requests": result.routing_requests,
                "final_skill": result.final_skill,
                "skill_activations": result.skill_activations,
                "profile": result.profile,
                "model_requests": result.model_requests,
                "tool_calls": result.tool_calls,
                "tool_sequence": result.tool_sequence,
                "activation_events": result.activation_events,
                "model_request_ms": result.model_request_ms,
                "duration_ms": result.duration_ms,
                "failures": result.failures,
            }
            for result in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the lLLM agent evaluation suite.")
    parser.add_argument("--suite", choices=["scripted", "live"], default="scripted")
    parser.add_argument(
        "--profile",
        choices=sorted(MODEL_PROFILES),
        default=None,
        help="Model profile for the live suite (SPEC-017); ignored by the scripted suite.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Only run cases whose category starts with this prefix (e.g. 'tracker').",
    )
    args = parser.parse_args(argv)

    profile = resolve_model_profile(args.profile)
    summary, results = run_suite(args.suite, args.cases, args.category, profile)
    model_name = "scripted" if args.suite == "scripted" else profile.model
    result_path = write_results(args.suite, summary, results, model_name)

    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        print(f"[{marker}] {result.id} ({result.status}/{result.reason}, {result.duration_ms}ms)")
        for failure in result.failures:
            print(f"    - {failure}")

    print(
        f"\n{summary['passed']}/{summary['total']} passed "
        f"({summary['failed']} failed). Results: {result_path}"
    )

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
