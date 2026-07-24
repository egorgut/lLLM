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
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import AgentRunner
from config import (
    AGENT_TURN_TIMEOUT_SECONDS,
    MAX_IDENTICAL_TOOL_CALLS,
    MAX_TOOL_CALLS_PER_TURN,
    MCP_SERVERS,
    MODEL_NAME,
    MODEL_REQUEST_TIMEOUT_SECONDS,
    TOOL_EXECUTION_TIMEOUT_SECONDS,
)
from reliability import TurnStatus, new_id
from tests.support import (
    FakeToolExecutor,
    RecordingRenderer,
    ScriptedModelResponse,
    ScriptedResponder,
    make_tool_call,
)
from tracing import NullTraceSink

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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


def load_cases(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_expectation(
    outcome, tool_calls_used: list[str], expectation: dict[str, Any]
) -> list[str]:
    """Objective, deterministic assertions only -- no LLM judge (SPEC-011 §6)."""

    failures: list[str] = []

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
        model_request_timeout_seconds=MODEL_REQUEST_TIMEOUT_SECONDS,
        tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        agent_turn_timeout_seconds=AGENT_TURN_TIMEOUT_SECONDS,
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


class _RecordingExecutorWrapper:
    """Wraps the real `ToolExecutor` so the live suite can see which tools ran,
    without changing the production dispatch path itself."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments) if isinstance(arguments, dict) else arguments))
        return self._inner.execute(name, arguments)


def run_live_case(case: dict[str, Any], executor, tools, respond, run_id: str) -> CaseResult:
    from prompts import SYSTEM_PROMPT

    runner = AgentRunner(
        respond=respond,
        executor=executor,
        tools=tools,
        renderer=RecordingRenderer(),
        run_id=run_id,
        max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
        max_identical_tool_calls=MAX_IDENTICAL_TOOL_CALLS,
        model_request_timeout_seconds=MODEL_REQUEST_TIMEOUT_SECONDS,
        tool_execution_timeout_seconds=TOOL_EXECUTION_TIMEOUT_SECONDS,
        agent_turn_timeout_seconds=AGENT_TURN_TIMEOUT_SECONDS,
        trace_sink=NullTraceSink(),
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": case["prompt"]},
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
    )


def _run_live_cases(cases: list[dict[str, Any]]) -> list[CaseResult]:
    # Imported lazily: the live suite is the only path that needs a real
    # Ollama connection, real local tools, and a real MCP server. Keeping this
    # import out of the module top level means the scripted suite (and the
    # rest of the test/import graph) never depends on any of that.
    from app import build_executor, register_mcp_tools
    from llm import ModelResponse
    from mcp_integration import McpClientManager, McpStartupError

    registry, executor = build_executor()
    recording_executor = _RecordingExecutorWrapper(executor)
    manager = McpClientManager(MCP_SERVERS, call_timeout=TOOL_EXECUTION_TIMEOUT_SECONDS)

    try:
        try:
            manager.start()
            register_mcp_tools(registry, executor, manager)
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
        run_id = new_id()

        def respond(messages, declarations):
            return ModelResponse(messages, declarations)

        return [
            run_live_case(case, recording_executor, tools, respond, run_id)
            for case in cases
        ]
    finally:
        manager.close()


def run_suite(suite: str, cases_path: Path) -> tuple[dict[str, int], list[CaseResult]]:
    cases = load_cases(cases_path)
    applicable = [case for case in cases if suite in case.get("modes", ["scripted", "live"])]

    if suite == "scripted":
        results = [run_scripted_case(case) for case in applicable]
    elif suite == "live":
        results = _run_live_cases(applicable)
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
                "tool_calls": result.tool_calls,
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
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    args = parser.parse_args(argv)

    summary, results = run_suite(args.suite, args.cases)
    model_name = "scripted" if args.suite == "scripted" else MODEL_NAME
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
