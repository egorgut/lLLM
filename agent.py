"""The bounded, observable agent loop (SPEC-010, SPEC-011).

`AgentRunner` repeatedly gives control back to the model after every tool
result until the model produces a final textual answer, capped by a
host-owned maximum number of tool executions per user turn (SPEC-010). On top
of that, every turn now:

- has host-owned deadlines for one model request, one tool execution, and the
  whole turn (SPEC-011 §10-14, caller-side deadlines only — see
  `reliability.run_with_deadline`);
- detects consecutive identical tool calls (SPEC-011 §15-17);
- emits a structured trace of the decision (SPEC-011 §4-9);
- returns one explicit `AgentTurnOutcome` instead of a bare string or an
  undifferentiated exception (SPEC-011 §"Core architectural decisions" #1).

The runner owns *loop policy only*. It does not own persistent chat storage,
CLI commands, MCP process lifecycle, tool registration, or any tool
implementation — those stay with the caller. Model transport, rendering, the
trace sink, the clock, and the ID factory are all injected so the loop is
deterministically testable without a live model, a live tool, or real time.
"""

import json
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from llm import ModelToolCall
from reliability import (
    STATUS_BY_REASON,
    USER_MESSAGE_BY_REASON,
    AgentRuntimeError,
    AgentTurnOutcome,
    DeadlineExceeded,
    ModelRequestTimeout,
    RepeatedToolCallError,
    TerminationReason,
    ToolExecutionTimeout,
    TurnTimeoutExceeded,
    new_id,
    run_with_deadline,
    tool_call_fingerprint,
    validate_reliability_config,
)
from tools import ToolExecutor
from tracing import NullTraceSink, SafeTraceSink, TraceSink, build_event, preview_and_hash


class ModelResponseLike(Protocol):
    """The slice of :class:`llm.ModelResponse` the loop depends on.

    Declared as a Protocol so tests can inject a scripted response with no live
    Ollama. ``text_chunks()`` streams assistant text as it arrives; ``tool_calls``
    is authoritative once the stream has been consumed.
    """

    def text_chunks(self) -> Iterator[str]: ...

    @property
    def tool_calls(self) -> list[ModelToolCall]: ...


class Renderer(Protocol):
    """Sink for user-visible loop output, injected to keep CLI concerns out."""

    def tool_call(self, call: ModelToolCall, used: int, maximum: int) -> None: ...

    def tool_result(self, result: dict) -> None: ...

    def text(self, chunk: str) -> None: ...


# messages, tool declarations -> one streaming model response.
Respond = Callable[[list[dict[str, Any]], Sequence[dict[str, Any]]], ModelResponseLike]


def assistant_tool_message(call: ModelToolCall) -> dict:
    """The temporary assistant message that records a tool call for the model.

    Part of the ephemeral per-turn transcript only; it is never persisted.
    """

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
    }


def tool_result_message(call: ModelToolCall, result: dict) -> dict:
    """The temporary tool-result observation sent back to the model.

    Part of the ephemeral per-turn transcript only; it is never persisted.
    """

    return {
        "role": "tool",
        "tool_name": call.name,
        "content": json.dumps(result, ensure_ascii=False),
    }


@dataclass
class _Counters:
    model_requests: int = 0
    tool_calls_executed: int = 0


class AgentRunner:
    """Runs one user turn as a bounded, deadline-aware model→tool→model loop.

    The caller supplies a *snapshot* of model-facing messages; the runner never
    receives the mutable ``Conversation``. Temporary tool-protocol messages live
    only in a per-turn working transcript and are discarded when the turn ends —
    the caller persists only a completed outcome's `final_text`.
    """

    def __init__(
        self,
        respond: Respond,
        executor: ToolExecutor,
        tools: Sequence[dict[str, Any]],
        renderer: Renderer,
        *,
        run_id: str,
        max_tool_calls: int,
        max_identical_tool_calls: int = 2,
        model_request_timeout_seconds: float = 120,
        tool_execution_timeout_seconds: float = 30,
        agent_turn_timeout_seconds: float = 180,
        trace_sink: TraceSink = NullTraceSink(),
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = new_id,
        payload_preview_chars: int = 1000,
    ) -> None:
        # All numeric limits are host-owned; reject an incoherent configuration
        # at construction time rather than mid-turn (SPEC-011 §10).
        validate_reliability_config(
            model_request_timeout_seconds=model_request_timeout_seconds,
            tool_execution_timeout_seconds=tool_execution_timeout_seconds,
            agent_turn_timeout_seconds=agent_turn_timeout_seconds,
            max_tool_calls=max_tool_calls,
            max_identical_tool_calls=max_identical_tool_calls,
        )
        self._respond = respond
        self._executor = executor
        self._tools = tools
        self._renderer = renderer
        self._run_id = run_id
        self._max_tool_calls = max_tool_calls
        self._max_identical_tool_calls = max_identical_tool_calls
        self._model_request_timeout_seconds = model_request_timeout_seconds
        self._tool_execution_timeout_seconds = tool_execution_timeout_seconds
        self._agent_turn_timeout_seconds = agent_turn_timeout_seconds
        # Wrapped so a broken trace sink can never break the agent (§19).
        self._trace = SafeTraceSink(trace_sink, run_id)
        self._clock = clock
        self._id_factory = id_factory
        self._payload_preview_chars = payload_preview_chars

    def run_turn(
        self, messages: list[dict[str, Any]], *, turn_id: str | None = None
    ) -> AgentTurnOutcome:
        """Drive the loop until a terminal outcome and return it.

        Every started turn produces exactly one `AgentTurnOutcome` and exactly
        one `turn_finished` trace event, including failures, timeouts, and
        cancellation. An unexpected programming defect is still converted into
        a `failed/internal_error` outcome (with the terminal event emitted)
        before being re-raised, so it remains visible to callers/tests while
        the trace stays complete.
        """

        turn_id = turn_id or self._id_factory()
        start = self._clock()
        deadline = start + self._agent_turn_timeout_seconds
        counters = _Counters()

        self._trace.emit(
            build_event(
                "turn_started",
                run_id=self._run_id,
                turn_id=turn_id,
                message_count=len(messages),
                available_tools=[tool["function"]["name"] for tool in self._tools],
                limits={
                    "max_tool_calls": self._max_tool_calls,
                    "max_identical_tool_calls": self._max_identical_tool_calls,
                    "model_timeout_seconds": self._model_request_timeout_seconds,
                    "tool_timeout_seconds": self._tool_execution_timeout_seconds,
                    "turn_timeout_seconds": self._agent_turn_timeout_seconds,
                },
            )
        )

        try:
            final_text = self._drive_loop(messages, turn_id, deadline, counters)
        except AgentRuntimeError as error:
            outcome = self._outcome(
                turn_id, start, error.reason, None, counters, error_message=str(error)
            )
            self._emit_turn_finished(outcome)
            return outcome
        except KeyboardInterrupt:
            outcome = self._outcome(
                turn_id,
                start,
                TerminationReason.USER_INTERRUPT,
                None,
                counters,
                error_message=USER_MESSAGE_BY_REASON[TerminationReason.USER_INTERRUPT],
            )
            self._emit_turn_finished(outcome)
            return outcome
        except Exception:
            outcome = self._outcome(
                turn_id,
                start,
                TerminationReason.INTERNAL_ERROR,
                None,
                counters,
                error_message=USER_MESSAGE_BY_REASON[TerminationReason.INTERNAL_ERROR],
            )
            self._emit_turn_finished(outcome)
            raise

        outcome = self._outcome(
            turn_id, start, TerminationReason.FINAL_ANSWER, final_text, counters, None
        )
        self._emit_turn_finished(outcome)
        return outcome

    def _outcome(
        self,
        turn_id: str,
        start: float,
        reason: TerminationReason,
        final_text: str | None,
        counters: _Counters,
        error_message: str | None,
    ) -> AgentTurnOutcome:
        return AgentTurnOutcome(
            run_id=self._run_id,
            turn_id=turn_id,
            status=STATUS_BY_REASON[reason],
            reason=reason,
            final_text=final_text,
            tool_calls_executed=counters.tool_calls_executed,
            model_requests=counters.model_requests,
            duration_ms=int((self._clock() - start) * 1000),
            error_message=error_message,
        )

    def _emit_turn_finished(self, outcome: AgentTurnOutcome) -> None:
        self._trace.emit(
            build_event(
                "turn_finished",
                run_id=self._run_id,
                turn_id=outcome.turn_id,
                status=str(outcome.status),
                reason=str(outcome.reason),
                tool_calls_executed=outcome.tool_calls_executed,
                model_requests=outcome.model_requests,
                final_text_chars=len(outcome.final_text) if outcome.final_text else 0,
                duration_ms=outcome.duration_ms,
            )
        )

    def _drive_loop(
        self,
        messages: list[dict[str, Any]],
        turn_id: str,
        deadline: float,
        counters: _Counters,
    ) -> str:
        working_messages = list(messages)
        last_fingerprint: str | None = None
        consecutive_identical_count = 0
        step = 0

        while True:
            step += 1

            # The whole-turn deadline is authoritative: an operation that would
            # start with no turn time remaining must not start at all (§11).
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TurnTimeoutExceeded("Agent turn exceeded its total time limit.")

            counters.model_requests += 1
            request_index = counters.model_requests
            effective_model_timeout = min(self._model_request_timeout_seconds, remaining)

            self._trace.emit(
                build_event(
                    "model_request_started",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    step=step,
                    model_request_index=request_index,
                    working_message_count=len(working_messages),
                    remaining_turn_ms=int(remaining * 1000),
                )
            )

            abandoned = threading.Event()
            model_start = self._clock()
            try:
                text, tool_calls = run_with_deadline(
                    lambda: self._consume_model_response(working_messages, abandoned),
                    timeout_seconds=effective_model_timeout,
                    thread_name=f"model-step-{step}",
                )
            except DeadlineExceeded:
                abandoned.set()
                raise ModelRequestTimeout(
                    "Agent turn timed out while waiting for the model."
                ) from None
            except Exception:
                raise AgentRuntimeError(
                    TerminationReason.MODEL_ERROR, "Model request failed."
                ) from None

            model_duration_ms = int((self._clock() - model_start) * 1000)
            decision = "tool_call" if tool_calls else ("final_answer" if text else "invalid")
            self._trace.emit(
                build_event(
                    "model_response_finished",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    step=step,
                    model_request_index=request_index,
                    decision=decision,
                    tool_call_count=len(tool_calls),
                    text_chars=len(text),
                    duration_ms=model_duration_ms,
                )
            )

            if not tool_calls:
                if not text:
                    raise AgentRuntimeError(
                        TerminationReason.EMPTY_MODEL_RESPONSE,
                        "Model returned an empty response.",
                    )
                return text

            if len(tool_calls) != 1:
                self._trace.emit(
                    build_event(
                        "policy_violation",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        policy="parallel_tool_calls",
                        message="Parallel tool calls are not supported.",
                    )
                )
                raise AgentRuntimeError(
                    TerminationReason.PARALLEL_TOOL_CALLS,
                    "Parallel tool calls are not supported.",
                )

            call = tool_calls[0]
            fingerprint = tool_call_fingerprint(call.name, call.arguments)
            next_count = (
                consecutive_identical_count + 1 if fingerprint == last_fingerprint else 1
            )

            preview, digest, truncated = preview_and_hash(
                call.arguments, limit=self._payload_preview_chars
            )
            self._trace.emit(
                build_event(
                    "tool_call_requested",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    step=step,
                    tool_call_index=counters.tool_calls_executed + 1,
                    tool_name=call.name,
                    arguments_preview=preview,
                    arguments_sha256=digest,
                    arguments_truncated=truncated,
                    consecutive_identical_count=next_count,
                )
            )

            # Repeated-call detection and the tool-call budget are separate
            # policies; a repeated call may stop the turn before the budget is
            # ever reached, so this check runs first (§17).
            if next_count > self._max_identical_tool_calls:
                message = (
                    "Agent stopped after repeating the same tool call "
                    f"{self._max_identical_tool_calls} times."
                )
                self._trace.emit(
                    build_event(
                        "policy_violation",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        policy="repeated_tool_call",
                        message=message,
                    )
                )
                raise RepeatedToolCallError(message, repeat_count=next_count)

            # Enforce the budget before executing: the call that would exceed
            # the limit is never dispatched (SPEC-010 §2).
            if counters.tool_calls_executed >= self._max_tool_calls:
                message = (
                    f"Agent stopped after {self._max_tool_calls} tool calls "
                    "without a final answer."
                )
                self._trace.emit(
                    build_event(
                        "policy_violation",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        policy="tool_call_limit",
                        message=message,
                    )
                )
                raise AgentRuntimeError(TerminationReason.TOOL_CALL_LIMIT, message)

            last_fingerprint, consecutive_identical_count = fingerprint, next_count
            counters.tool_calls_executed += 1
            tool_call_index = counters.tool_calls_executed

            self._renderer.tool_call(call, tool_call_index, self._max_tool_calls)

            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TurnTimeoutExceeded("Agent turn exceeded its total time limit.")
            effective_tool_timeout = min(self._tool_execution_timeout_seconds, remaining)

            self._trace.emit(
                build_event(
                    "tool_execution_started",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    tool_call_index=tool_call_index,
                    tool_name=call.name,
                    effective_timeout_ms=int(effective_tool_timeout * 1000),
                )
            )

            tool_start = self._clock()
            try:
                result = run_with_deadline(
                    lambda: self._executor.execute(call.name, call.arguments),
                    timeout_seconds=effective_tool_timeout,
                    thread_name=f"tool-{tool_call_index}",
                )
            except DeadlineExceeded:
                self._trace.emit(
                    build_event(
                        "tool_execution_finished",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        tool_call_index=tool_call_index,
                        tool_name=call.name,
                        result_ok=None,
                        error_type="timeout",
                        duration_ms=int((self._clock() - tool_start) * 1000),
                    )
                )
                raise ToolExecutionTimeout(f"Tool '{call.name}' timed out.") from None
            except Exception:
                self._trace.emit(
                    build_event(
                        "tool_execution_finished",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        tool_call_index=tool_call_index,
                        tool_name=call.name,
                        result_ok=False,
                        error_type="dispatch_error",
                        duration_ms=int((self._clock() - tool_start) * 1000),
                    )
                )
                raise AgentRuntimeError(
                    TerminationReason.TOOL_EXECUTION_ERROR, "Tool execution failed."
                ) from None

            self._trace.emit(
                build_event(
                    "tool_execution_finished",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    tool_call_index=tool_call_index,
                    tool_name=call.name,
                    result_ok=result.get("ok"),
                    error_type=None,
                    duration_ms=int((self._clock() - tool_start) * 1000),
                )
            )
            self._renderer.tool_result(result)

            # Append the action and its observation to the working transcript
            # so the next model request sees every prior tool result from this
            # turn.
            working_messages.extend(
                [
                    assistant_tool_message(call),
                    tool_result_message(call, result),
                ]
            )

    def _consume_model_response(
        self, working_messages: list[dict[str, Any]], abandoned: threading.Event
    ) -> tuple[str, list[ModelToolCall]]:
        """Runs on the deadline worker thread: stream text, then read tool_calls.

        `abandoned` is set by the calling thread once it has given up waiting
        (a deadline expired); once set, this stops calling the renderer so a
        late-arriving chunk can never print after the timeout error has
        already been shown (the renderer is not thread-safe against that).
        """

        response = self._respond(working_messages, self._tools)
        parts: list[str] = []
        for chunk in response.text_chunks():
            parts.append(chunk)
            if not abandoned.is_set():
                self._renderer.text(chunk)
        return "".join(parts), response.tool_calls
