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

SPEC-020 adds one more: the model's hidden reasoning is no longer discarded.
Within one active turn it is captured separately from user-visible content,
attached to the transient assistant tool-call message, and handed back to the
next model decision — then dropped with the rest of the working transcript when
the turn ends. It never reaches the renderer, the conversation, or the trace as
text.

SPEC-018 adds one general notion on top: a *control tool* — a tool the host
handles itself, because handling it changes the turn's own view (its tool
declarations, its executor, its system-level block). The loop knows only that
such a tool exists and how to apply what its handler returns; it knows nothing
about what any control tool *means*.

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
from dataclasses import dataclass, field
from typing import Any, Protocol

from llm import ModelResponseMetrics, ModelStreamChunk, ModelToolCall
from reliability import (
    STATUS_BY_REASON,
    USER_MESSAGE_BY_REASON,
    AgentActionReceipt,
    AgentRuntimeError,
    AgentTurnOutcome,
    DeadlineExceeded,
    ModelRequestTimeout,
    RepeatedToolCallError,
    SkillPolicyViolation,
    TerminationReason,
    ToolExecutionTimeout,
    TurnContext,
    TurnTimeoutExceeded,
    canonical_json,
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
    Ollama. ``chunks()`` streams reasoning and assistant text as they arrive, kept
    apart; ``tool_calls`` is authoritative once the stream has been consumed;
    ``metrics`` is the transport's own timing metadata, or ``None`` when the
    stream ended before Ollama reported any.
    """

    def chunks(self) -> Iterator[ModelStreamChunk]: ...

    @property
    def tool_calls(self) -> list[ModelToolCall]: ...

    @property
    def metrics(self) -> ModelResponseMetrics | None: ...


class Renderer(Protocol):
    """Sink for user-visible loop output, injected to keep CLI concerns out."""

    def tool_call(self, call: ModelToolCall, used: int, maximum: int) -> None: ...

    def tool_result(self, result: dict) -> None: ...

    def text(self, chunk: str) -> None: ...


# messages, tool declarations -> one streaming model response.
Respond = Callable[[list[dict[str, Any]], Sequence[dict[str, Any]]], ModelResponseLike]


@dataclass(frozen=True)
class ControlResult:
    """What a control tool returns: a model-facing result, plus optional new view.

    Each of the three view fields is ``None`` when that part of the turn is
    unchanged, so a handler that only wants to report something (a recoverable
    error, say) leaves the running turn exactly as it was.
    """

    result: dict[str, Any]
    # New model-facing tool declarations; None leaves the current ones in place.
    tools: tuple[dict[str, Any], ...] | None = None
    # New executor for every subsequent call; None keeps the current one.
    executor: Any | None = None
    # New host-owned system-level block; None keeps the current one.
    system_suffix: str | None = None


class ControlToolHandler(Protocol):
    """A host-handled tool the loop must not dispatch to the ``ToolExecutor``.

    ``names`` is the closed set of tool names this handler owns. The loop still
    applies every pre-dispatch policy (parallel calls, repeated calls, the
    tool-call budget) and the same tool-execution deadline; only the dispatch
    target differs. The loop never inspects what a control tool means.
    """

    names: frozenset[str]

    def handle(self, name: str, arguments: dict[str, Any]) -> ControlResult: ...


def assistant_tool_message(call: ModelToolCall, thinking: str = "") -> dict:
    """The temporary assistant message that records a tool call for the model.

    Part of the ephemeral per-turn transcript only; it is never persisted.

    When the decision that produced this call also produced reasoning, that
    reasoning rides along on the same message (SPEC-020 §4.5) — the installed
    Ollama SDK already models an assistant message that way, so this needs no
    second memory mechanism and no hidden message type. Empty reasoning is
    omitted entirely rather than sent as an empty field, so a non-thinking model
    produces exactly the transcript it produced before SPEC-020.
    """

    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": call.name, "arguments": call.arguments}}],
    }
    if thinking:
        message["thinking"] = thinking
    return message


def tool_result_message(call: ModelToolCall, result: dict) -> dict:
    """The temporary tool-result observation sent back to the model.

    Part of the ephemeral per-turn transcript only; it is never persisted.
    """

    return {
        "role": "tool",
        "tool_name": call.name,
        "content": json.dumps(result, ensure_ascii=False),
    }


@dataclass(frozen=True)
class _ModelDecision:
    """What one consumed model response amounted to, for the loop's own use.

    Deliberately internal: `thinking` is transient turn state, and exposing it
    through `AgentTurnOutcome` would make it the caller's problem to remember not
    to persist. The loop is the only thing that ever sees it.

    Every ``*_ms`` field is measured from the same origin as the request's own
    ``duration_ms``, so the two are directly comparable. ``None`` means "that
    never happened in this response" — a model that emitted no reasoning has no
    `first_thinking_ms`, one that went straight to a tool call has no
    `first_content_ms`.
    """

    text: str
    thinking: str
    tool_calls: list[ModelToolCall]
    first_model_output_ms: int | None = None
    first_thinking_ms: int | None = None
    first_content_ms: int | None = None
    first_tool_call_ms: int | None = None
    metrics: ModelResponseMetrics | None = None

    @property
    def visible_ttft_ms(self) -> int | None:
        """Request start -> the first thing the harness could *show* as output.

        The number the user actually experiences as "time to first token", which
        is not the same as the model starting to generate: hidden reasoning is
        model output the terminal never renders. Keeping the two apart is the
        whole point of SPEC-020 §4.9 — an 18 s silence is a different problem
        depending on which of them it was.
        """

        candidates = [
            value
            for value in (self.first_content_ms, self.first_tool_call_ms)
            if value is not None
        ]
        return min(candidates) if candidates else None

    def trace_fields(self) -> dict[str, Any]:
        """The additive reasoning/latency fields for `model_response_finished`.

        Counts and timings only. The reasoning text never appears here, and
        neither does a preview or a hash of it (SPEC-020 §4.11): a digest of a
        short chain of thought is not much of a secret.
        """

        return {
            "thinking_chars": len(self.thinking),
            "first_model_output_ms": self.first_model_output_ms,
            "first_thinking_ms": self.first_thinking_ms,
            "first_content_ms": self.first_content_ms,
            "first_tool_call_ms": self.first_tool_call_ms,
            "visible_ttft_ms": self.visible_ttft_ms,
            **(
                self.metrics.as_trace_fields()
                if self.metrics is not None
                else ModelResponseMetrics().as_trace_fields()
            ),
        }


@dataclass
class _Counters:
    model_requests: int = 0
    tool_calls_executed: int = 0
    # One receipt per ordinary tool the turn actually executed, in execution
    # order (PATCH-010-05). Reported only by a completed turn.
    action_receipts: list[AgentActionReceipt] = field(default_factory=list)


class AgentRunner:
    """Runs one user turn as a bounded, deadline-aware model→tool→model loop.

    The caller supplies a *snapshot* of model-facing messages; the runner never
    receives the mutable ``Conversation``. Temporary tool-protocol messages live
    only in a per-turn working transcript and are discarded when the turn ends —
    the caller persists only a completed outcome's `final_text`, now alongside a
    bounded receipt per executed tool (PATCH-010-05) so the *fact* of each action
    outlives the transcript that carried its payload.
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
        receipt_argument_chars: int = 500,
        redacted_argument_tools: frozenset[str] = frozenset(),
        control_handler: ControlToolHandler | None = None,
        extra_turn_fields: Callable[[], dict[str, Any]] = lambda: {},
        preserve_reasoning: bool = True,
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
        # The bound on one action receipt's argument preview (PATCH-010-05).
        # Separate from the trace bound above on purpose: a receipt is semantic
        # memory the next turn depends on, and it must not shrink, grow, or
        # vanish because tracing configuration changed.
        self._receipt_argument_chars = receipt_argument_chars
        # Tools whose arguments are content rather than parameters, and must
        # therefore never be previewed or hashed into the trace (SPEC-016 §15.3).
        # A `sql_query` string is a parameter worth seeing in a trace; a
        # `sandbox_execute` source is the user's data expressed as code, and its
        # input files are the user's files. Host-owned and name-based, so this
        # stays one generic rule rather than sandbox-specific loop logic.
        self._redacted_argument_tools = redacted_argument_tools
        # Tools the host handles itself because they change the turn's own view
        # (SPEC-018 §4.1). None means every tool call goes to the executor.
        self._control_handler = control_handler
        # Extra fields the host contributes to the terminal trace event, read at
        # emit time so they can reflect state the turn changed while running.
        self._extra_turn_fields = extra_turn_fields
        # Whether a decision's reasoning travels forward to the next decision of
        # the same turn (SPEC-020 §4.5). True is the production behavior; False
        # exists so the evaluation harness can measure preservation against its
        # own absence on identical prompts (SPEC-020 §7.4), and reproduces the
        # pre-SPEC-020 transcript exactly. It is not a user-facing runtime mode.
        self._preserve_reasoning = preserve_reasoning
        # Per-turn skill context (SPEC-012); (re)assigned at the top of run_turn.
        self._selected_skill: str | None = None
        self._skill_version: str | None = None
        self._routing_model_requests = 0
        # The content of the caller's leading system message, if any, kept so a
        # control tool can replace the host block appended to it without ever
        # stacking two blocks (SPEC-018 §4.8). None = the caller sent none.
        self._system_base: str | None = None

    def run_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        turn_id: str | None = None,
        turn_context: TurnContext | None = None,
        selected_skill: str | None = None,
        skill_version: str | None = None,
        routing_model_requests: int = 0,
        system_suffix: str | None = None,
    ) -> AgentTurnOutcome:
        """Drive the loop until a terminal outcome and return it.

        Every started turn produces exactly one `AgentTurnOutcome` and exactly
        one `turn_finished` trace event, including failures, timeouts, and
        cancellation. An unexpected programming defect is still converted into
        a `failed/internal_error` outcome (with the terminal event emitted)
        before being re-raised, so it remains visible to callers/tests while
        the trace stays complete.

        When a `turn_context` is supplied (SPEC-012), the turn adopts its
        `turn_id`, absolute `started_at`, and shared whole-turn `deadline` rather
        than minting fresh ones — so skill routing and agent execution share one
        budget and `duration_ms` covers both. `routing_model_requests` is added
        to the agent's own model requests so `model_requests` means "all model
        requests made for the user turn"; `selected_skill`/`skill_version` enrich
        the trace only.

        Any reasoning the model produces along the way lives only in this call's
        working transcript and is unreachable once it returns (SPEC-020 §4.6) —
        on the final-answer path and on every failure path alike.

        `system_suffix` (SPEC-018) is a host-owned block appended to the caller's
        leading system message for this turn. It is passed separately rather than
        baked into `messages` so a control tool can *replace* it later without
        the loop having to tell base prompt and host block apart.
        """

        if turn_context is not None:
            turn_id = turn_context.turn_id
            start = turn_context.started_at
            deadline = turn_context.deadline
        else:
            turn_id = turn_id or self._id_factory()
            start = self._clock()
            deadline = start + self._agent_turn_timeout_seconds
        self._selected_skill = selected_skill
        self._skill_version = skill_version
        self._routing_model_requests = routing_model_requests
        self._system_base = (
            messages[0]["content"]
            if messages and messages[0].get("role") == "system"
            else None
        )
        counters = _Counters()

        self._trace.emit(
            build_event(
                "turn_started",
                run_id=self._run_id,
                turn_id=turn_id,
                message_count=len(messages),
                available_tools=[tool["function"]["name"] for tool in self._tools],
                selected_skill=selected_skill,
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
            final_text = self._drive_loop(
                messages, turn_id, deadline, counters, system_suffix
            )
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

        # Only here do the receipts leave the turn: the answer above is the one
        # the user actually received, so it is the only one whose provenance the
        # caller may remember (PATCH-010-05). Every failure path above builds its
        # outcome without them, even when a tool did run before the turn ended.
        outcome = self._outcome(
            turn_id,
            start,
            TerminationReason.FINAL_ANSWER,
            final_text,
            counters,
            None,
            action_receipts=tuple(counters.action_receipts),
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
        action_receipts: tuple[AgentActionReceipt, ...] = (),
    ) -> AgentTurnOutcome:
        return AgentTurnOutcome(
            run_id=self._run_id,
            turn_id=turn_id,
            status=STATUS_BY_REASON[reason],
            reason=reason,
            final_text=final_text,
            tool_calls_executed=counters.tool_calls_executed,
            # "all model requests made for the user turn" = routing + agent.
            model_requests=counters.model_requests + self._routing_model_requests,
            duration_ms=int((self._clock() - start) * 1000),
            error_message=error_message,
            action_receipts=action_receipts,
        )

    def _emit_turn_finished(self, outcome: AgentTurnOutcome) -> None:
        fields: dict[str, Any] = {
            "status": str(outcome.status),
            "reason": str(outcome.reason),
            "tool_calls_executed": outcome.tool_calls_executed,
            "model_requests": outcome.model_requests,
            "routing_model_requests": self._routing_model_requests,
            "agent_model_requests": outcome.model_requests - self._routing_model_requests,
            "selected_skill": self._selected_skill,
            "skill_version": self._skill_version,
            "final_text_chars": len(outcome.final_text) if outcome.final_text else 0,
            "duration_ms": outcome.duration_ms,
        }
        # Host-contributed fields win: a control tool may have changed something
        # the loop reported at the start of the turn (SPEC-018 §4.9). Guarded,
        # because exactly one terminal event is guaranteed for every turn and a
        # defect in the caller's closure must not be what prevents it.
        try:
            fields.update(self._extra_turn_fields())
        except Exception:  # noqa: BLE001 - the terminal event matters more
            pass
        self._trace.emit(
            build_event(
                "turn_finished",
                run_id=self._run_id,
                turn_id=outcome.turn_id,
                **fields,
            )
        )

    def _set_system_suffix(
        self, working_messages: list[dict[str, Any]], suffix: str | None
    ) -> None:
        """Rewrite the leading system message as base + *at most one* host block.

        Always composed from the base captured at the start of the turn, never
        from the current content, so a replacement can neither stack a second
        block nor leave a stale one behind (SPEC-018 §4.8). The message object is
        replaced rather than mutated, so the caller's own list is untouched.
        """

        if self._system_base is None and not suffix:
            return
        base = self._system_base or ""
        content = f"{base}\n\n{suffix}" if base and suffix else (suffix or base)
        if working_messages and working_messages[0].get("role") == "system":
            working_messages[0] = {**working_messages[0], "content": content}
        else:
            working_messages.insert(0, {"role": "system", "content": content})

    def _action_receipt(
        self, call: ModelToolCall, result: dict, *, redacted: bool
    ) -> AgentActionReceipt:
        """One bounded receipt for a tool this turn actually executed.

        Generic by construction (PATCH-010-05): the tool's name, the canonical
        JSON of the arguments the model chose, and whether the host observed a
        structured `ok`. No result body, no summarizer, nothing tool-specific —
        the loop cannot learn what Time, Tracker, SQL, or the sandbox mean.

        A redacted tool keeps its identity and status and loses everything else,
        including any hash: the trace's privacy boundary is reused as-is rather
        than a second, weaker one being invented for cross-turn memory.
        """

        if redacted:
            preview, truncated = "", False
        else:
            encoded = canonical_json(call.arguments)
            truncated = len(encoded) > self._receipt_argument_chars
            preview = encoded[: self._receipt_argument_chars]
        return AgentActionReceipt(
            tool_name=call.name,
            arguments_preview=preview,
            arguments_truncated=truncated,
            arguments_redacted=redacted,
            result_ok=bool(result.get("ok")),
        )

    def _drive_loop(
        self,
        messages: list[dict[str, Any]],
        turn_id: str,
        deadline: float,
        counters: _Counters,
        system_suffix: str | None = None,
    ) -> str:
        working_messages = list(messages)
        # The view a control tool may replace. Loop-local, not instance state:
        # one turn's activation must never leak into the next (SPEC-018 §4.10).
        working_tools: Sequence[dict[str, Any]] = self._tools
        working_executor = self._executor
        if system_suffix:
            self._set_system_suffix(working_messages, system_suffix)
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
                decision = run_with_deadline(
                    lambda: self._consume_model_response(
                        working_messages, working_tools, abandoned, model_start
                    ),
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

            text, tool_calls = decision.text, decision.tool_calls
            model_duration_ms = int((self._clock() - model_start) * 1000)
            outcome_kind = (
                "tool_call" if tool_calls else ("final_answer" if text else "invalid")
            )
            self._trace.emit(
                build_event(
                    "model_response_finished",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    step=step,
                    model_request_index=request_index,
                    decision=outcome_kind,
                    tool_call_count=len(tool_calls),
                    text_chars=len(text),
                    duration_ms=model_duration_ms,
                    # Reasoning and latency, as counts and timings only.
                    **decision.trace_fields(),
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

            redacted = call.name in self._redacted_argument_tools
            if redacted:
                # Size and identity still make the call traceable; the content
                # does not appear, and neither does a hash of it — a hash of a
                # short script is not much of a secret.
                preview, digest, truncated = "", None, False
            else:
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
                    arguments_redacted=redacted,
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

            # A control tool is handled by the host instead of the executor,
            # because handling it changes this turn's own view. Everything else
            # about the call — every policy above, the deadline below, the trace
            # events, the transcript append — is identical (SPEC-018 §4.1).
            is_control = (
                self._control_handler is not None
                and call.name in self._control_handler.names
            )

            self._trace.emit(
                build_event(
                    "tool_execution_started",
                    run_id=self._run_id,
                    turn_id=turn_id,
                    tool_call_index=tool_call_index,
                    tool_name=call.name,
                    control_tool=is_control,
                    effective_timeout_ms=int(effective_tool_timeout * 1000),
                )
            )

            control: ControlResult | None = None
            tool_start = self._clock()
            try:
                if is_control:
                    handler = self._control_handler
                    control = run_with_deadline(
                        lambda: handler.handle(call.name, call.arguments),
                        timeout_seconds=effective_tool_timeout,
                        thread_name=f"control-{tool_call_index}",
                    )
                    result = control.result
                else:
                    executor = working_executor
                    result = run_with_deadline(
                        lambda: executor.execute(call.name, call.arguments),
                        timeout_seconds=effective_tool_timeout,
                        thread_name=f"tool-{tool_call_index}",
                    )
            except SkillPolicyViolation as violation:
                # A disallowed tool never reached its handler (the restricted
                # executor raised before dispatch). This is a deliberate policy
                # stop, not a tool failure, so it must not be folded into
                # tool_execution_error (SPEC-012 §9).
                self._trace.emit(
                    build_event(
                        "policy_violation",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        policy="skill_tool_allowlist",
                        skill=violation.skill,
                        requested_tool=violation.requested_tool,
                        message=str(violation),
                    )
                )
                self._trace.emit(
                    build_event(
                        "tool_execution_finished",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        tool_call_index=tool_call_index,
                        tool_name=call.name,
                        result_ok=False,
                        error_type="policy_violation",
                        duration_ms=int((self._clock() - tool_start) * 1000),
                    )
                )
                raise
            except AgentRuntimeError:
                # A control handler may fail the turn with a controlled reason of
                # its own. It already carries the right TerminationReason, so it
                # passes through rather than being flattened into a generic
                # tool_execution_error. Nothing on the ordinary executor path
                # raises this (a handler contract breach is ToolExecutionError).
                self._trace.emit(
                    build_event(
                        "tool_execution_finished",
                        run_id=self._run_id,
                        turn_id=turn_id,
                        tool_call_index=tool_call_index,
                        tool_name=call.name,
                        result_ok=False,
                        error_type="control_error",
                        duration_ms=int((self._clock() - tool_start) * 1000),
                    )
                )
                raise
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

            if not is_control:
                # The action really happened, so it earns a receipt — success or
                # structured failure alike (PATCH-010-05). A control tool does
                # not: it changes *this* turn's own view and is discarded with
                # it, so remembering it across turns could make a later turn
                # infer that a skill is still active. The loop applies that rule
                # generically, through the same `is_control` flag it already
                # dispatched on; it still knows nothing about any tool's meaning.
                counters.action_receipts.append(
                    self._action_receipt(call, result, redacted=redacted)
                )

            if control is not None:
                # Each field is independent, and None means "unchanged" — a
                # handler reporting a recoverable error changes nothing at all.
                if control.tools is not None:
                    working_tools = control.tools
                if control.executor is not None:
                    working_executor = control.executor
                if control.system_suffix is not None:
                    self._set_system_suffix(working_messages, control.system_suffix)

            # Append the action, the reasoning that chose it, and its
            # observation to the working transcript, so the next model request
            # sees every prior decision of this turn rather than having to infer
            # its own intent back from the action alone (SPEC-020 §4.5). Only
            # this decision's reasoning goes onto this decision's message; the
            # tool result is untouched, because reasoning is assistant state and
            # not observation data.
            working_messages.extend(
                [
                    assistant_tool_message(
                        call, decision.thinking if self._preserve_reasoning else ""
                    ),
                    tool_result_message(call, result),
                ]
            )

    def _consume_model_response(
        self,
        working_messages: list[dict[str, Any]],
        working_tools: Sequence[dict[str, Any]],
        abandoned: threading.Event,
        started: float,
    ) -> _ModelDecision:
        """Runs on the deadline worker thread: stream the response, then read
        tool_calls.

        Reasoning and content are accumulated independently and only content ever
        reaches the renderer — the terminal stays reasoning-blind by construction
        rather than by a filter someone has to remember to apply (SPEC-020 §4.4).

        `abandoned` is set by the calling thread once it has given up waiting
        (a deadline expired); once set, this stops calling the renderer so a
        late-arriving chunk can never print after the timeout error has
        already been shown (the renderer is not thread-safe against that).

        `started` is the caller's own request-start stamp, reused rather than
        re-read so every latency below shares an origin with the request's
        `duration_ms`.
        """

        response = self._respond(working_messages, working_tools)
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        first_thinking_ms: int | None = None
        first_content_ms: int | None = None

        def elapsed_ms() -> int:
            return int((self._clock() - started) * 1000)

        for chunk in response.chunks():
            if chunk.thinking:
                if first_thinking_ms is None:
                    first_thinking_ms = elapsed_ms()
                thinking_parts.append(chunk.thinking)
            if chunk.content:
                if first_content_ms is None:
                    first_content_ms = elapsed_ms()
                content_parts.append(chunk.content)
                if not abandoned.is_set():
                    self._renderer.text(chunk.content)

        # `chunks()` stops on the chunk that carried the tool call, so the moment
        # the loop above ends is the moment that chunk arrived.
        first_tool_call_ms = elapsed_ms() if response.tool_calls else None
        starts = [
            value
            for value in (first_thinking_ms, first_content_ms, first_tool_call_ms)
            if value is not None
        ]
        return _ModelDecision(
            text="".join(content_parts),
            thinking="".join(thinking_parts),
            tool_calls=response.tool_calls,
            first_model_output_ms=min(starts) if starts else None,
            first_thinking_ms=first_thinking_ms,
            first_content_ms=first_content_ms,
            first_tool_call_ms=first_tool_call_ms,
            metrics=response.metrics,
        )
