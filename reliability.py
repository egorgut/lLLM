"""Reliability primitives for the agent loop (SPEC-011).

This module is deliberately framework-free and has no dependency on the model
transport, the tool executor, or the CLI: it holds only the outcome/status
vocabulary, the typed internal exceptions the agent loop uses as control flow,
the canonical tool-call fingerprint used for repeated-call detection, and the
caller-side deadline helper used to bound model and tool calls. Keeping these
pure makes them testable without any of `agent.py`'s wiring.
"""

import hashlib
import json
import queue
import threading
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class TerminationReason(StrEnum):
    FINAL_ANSWER = "final_answer"
    EMPTY_MODEL_RESPONSE = "empty_model_response"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    TOOL_CALL_LIMIT = "tool_call_limit"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    MODEL_TIMEOUT = "model_timeout"
    TOOL_TIMEOUT = "tool_timeout"
    TURN_TIMEOUT = "turn_timeout"
    MODEL_ERROR = "model_error"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    USER_INTERRUPT = "user_interrupt"
    INTERNAL_ERROR = "internal_error"


# The authoritative status for every termination reason (SPEC-011, "Detailed
# agent-turn contract"). A reason never appears with more than one status.
STATUS_BY_REASON: dict[TerminationReason, TurnStatus] = {
    TerminationReason.FINAL_ANSWER: TurnStatus.COMPLETED,
    TerminationReason.EMPTY_MODEL_RESPONSE: TurnStatus.FAILED,
    TerminationReason.PARALLEL_TOOL_CALLS: TurnStatus.STOPPED,
    TerminationReason.TOOL_CALL_LIMIT: TurnStatus.STOPPED,
    TerminationReason.REPEATED_TOOL_CALL: TurnStatus.STOPPED,
    TerminationReason.MODEL_TIMEOUT: TurnStatus.TIMED_OUT,
    TerminationReason.TOOL_TIMEOUT: TurnStatus.TIMED_OUT,
    TerminationReason.TURN_TIMEOUT: TurnStatus.TIMED_OUT,
    TerminationReason.MODEL_ERROR: TurnStatus.FAILED,
    TerminationReason.TOOL_EXECUTION_ERROR: TurnStatus.FAILED,
    TerminationReason.USER_INTERRUPT: TurnStatus.CANCELLED,
    TerminationReason.INTERNAL_ERROR: TurnStatus.FAILED,
}


# User-facing messages for every termination reason (SPEC-011 "Error
# taxonomy"). `final_answer` has no error message: a completed turn is not an
# error. Reasons that need a runtime value (e.g. the tool name, a configured
# limit) are formatted by the caller; this table holds only the static ones.
USER_MESSAGE_BY_REASON: dict[TerminationReason, str | None] = {
    TerminationReason.FINAL_ANSWER: None,
    TerminationReason.EMPTY_MODEL_RESPONSE: "Model returned an empty response.",
    TerminationReason.PARALLEL_TOOL_CALLS: "Parallel tool calls are not supported.",
    TerminationReason.MODEL_TIMEOUT: "Agent turn timed out while waiting for the model.",
    TerminationReason.TURN_TIMEOUT: "Agent turn exceeded its total time limit.",
    TerminationReason.MODEL_ERROR: "Model request failed.",
    TerminationReason.TOOL_EXECUTION_ERROR: "Tool execution failed.",
    TerminationReason.USER_INTERRUPT: "Generation interrupted.",
    TerminationReason.INTERNAL_ERROR: "Unexpected application error.",
}


@dataclass(frozen=True)
class AgentTurnOutcome:
    """The single authoritative result of one user turn.

    Every started turn produces exactly one outcome, including failures. A
    `final_text` is present only for `status == COMPLETED`; every other status
    carries `final_text = None` so a partial or failed turn can never be
    mistaken for a persistable answer.
    """

    run_id: str
    turn_id: str
    status: TurnStatus
    reason: TerminationReason
    final_text: str | None
    tool_calls_executed: int
    model_requests: int
    duration_ms: int
    error_message: str | None = None


class AgentRuntimeError(Exception):
    """Internal control flow for a controlled turn failure.

    Raised inside the agent loop and always caught at the `AgentRunner.run_turn`
    boundary, which converts it into an `AgentTurnOutcome` via
    `STATUS_BY_REASON`. It is not meant to escape the agent module.
    """

    def __init__(self, reason: TerminationReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ModelRequestTimeout(AgentRuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(TerminationReason.MODEL_TIMEOUT, message)


class ToolExecutionTimeout(AgentRuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(TerminationReason.TOOL_TIMEOUT, message)


class TurnTimeoutExceeded(AgentRuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(TerminationReason.TURN_TIMEOUT, message)


class RepeatedToolCallError(AgentRuntimeError):
    def __init__(self, message: str, *, repeat_count: int) -> None:
        super().__init__(TerminationReason.REPEATED_TOOL_CALL, message)
        self.repeat_count = repeat_count


def new_id() -> str:
    """An opaque, host-generated identifier for a run or a turn.

    Never derived from user or model text (SPEC-011 §3). A standard UUID4
    string is sufficient for local diagnostic correlation.
    """

    return str(uuid.uuid4())


def canonical_json(value: Any) -> str:
    """A stable JSON encoding used for fingerprinting and hashing.

    Sorted keys make argument key order irrelevant to the result; compact
    separators keep the fingerprint deterministic and free of incidental
    whitespace differences.
    """

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tool_call_fingerprint(name: str, arguments: dict[str, Any]) -> str:
    """A structural identity for one tool call, used for repeated-call detection.

    Two calls are the same fingerprint only if the tool name matches and the
    arguments are structurally identical once canonicalized. This is not SQL or
    semantic equivalence — `SELECT 1` and `SELECT   1` fingerprint differently.
    """

    return f"{name}:{canonical_json(arguments)}"


def sha256_of(text: str) -> str:
    """A stable hash used for trace correlation, not for security."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DeadlineExceeded(Exception):
    """A caller-side deadline expired while waiting for `run_with_deadline`.

    Carries no `TerminationReason`: the caller knows which phase (model
    request, tool execution) it was waiting on and wraps this into the
    matching `AgentRuntimeError` subclass.
    """


def run_with_deadline(
    fn: Callable[[], T], *, timeout_seconds: float, thread_name: str
) -> T:
    """Run `fn` to completion on a background thread, bounded by a deadline.

    This is a **caller-side deadline only** (SPEC-011 §14): Python cannot
    safely terminate arbitrary running code, so on expiry this function raises
    `DeadlineExceeded` in the calling thread without stopping the worker. The
    worker is a daemon thread, so an abandoned one never blocks process exit;
    any result or exception it later produces is discarded by dropping the
    reference to its result queue.
    """

    result_box: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            result_box.put(("ok", fn()))
        except BaseException as error:  # noqa: BLE001 - forwarded, not swallowed
            result_box.put(("error", error))

    threading.Thread(target=_worker, name=thread_name, daemon=True).start()

    try:
        kind, payload = result_box.get(timeout=timeout_seconds)
    except queue.Empty:
        raise DeadlineExceeded(
            f"{thread_name} did not complete within {timeout_seconds:.3f}s "
            "(caller-side deadline only; the worker thread was not terminated)."
        ) from None

    if kind == "error":
        raise payload
    return payload


def validate_reliability_config(
    *,
    model_request_timeout_seconds: float,
    tool_execution_timeout_seconds: float,
    agent_turn_timeout_seconds: float,
    max_tool_calls: int,
    max_identical_tool_calls: int,
) -> None:
    """Reject an internally incoherent host configuration at startup (§10).

    All values are host-owned; the model never supplies or changes them. A
    misconfiguration here is a programming/deployment defect, not a turn-time
    failure, so this raises a plain `ValueError` rather than an
    `AgentRuntimeError`.
    """

    if model_request_timeout_seconds <= 0:
        raise ValueError(
            "model_request_timeout_seconds must be > 0, got "
            f"{model_request_timeout_seconds}."
        )
    if tool_execution_timeout_seconds <= 0:
        raise ValueError(
            "tool_execution_timeout_seconds must be > 0, got "
            f"{tool_execution_timeout_seconds}."
        )
    if agent_turn_timeout_seconds <= 0:
        raise ValueError(
            f"agent_turn_timeout_seconds must be > 0, got {agent_turn_timeout_seconds}."
        )
    if max_tool_calls < 1:
        raise ValueError(f"max_tool_calls must be at least 1, got {max_tool_calls}.")
    if max_identical_tool_calls < 1:
        raise ValueError(
            "max_identical_tool_calls must be at least 1, got "
            f"{max_identical_tool_calls}."
        )
    smallest_component_timeout = min(
        model_request_timeout_seconds, tool_execution_timeout_seconds
    )
    if agent_turn_timeout_seconds < smallest_component_timeout:
        raise ValueError(
            "agent_turn_timeout_seconds "
            f"({agent_turn_timeout_seconds}) must be at least as large as the "
            f"smallest component timeout ({smallest_component_timeout})."
        )
