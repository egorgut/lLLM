"""The skill-aware user turn (SPEC-012 §"User turn").

One object owns a whole user turn so the CLI loop and the integration tests share
the exact same path. It creates the single :class:`TurnContext` (before routing),
routes to zero or one skill, composes the active-skill prompt and restricted tool
view, then hands off to the unchanged bounded, observable ``AgentRunner`` with the
shared deadline. Routing and execution therefore share one ``run_id``/``turn_id``
and one whole-turn budget; ``duration_ms`` and ``model_requests`` cover both.

The orchestrator never persists routing protocol messages and never mutates the
conversation — it only reads the latest user message and a bounded context slice
for routing. Persistence and rollback stay with the caller, keyed off the returned
outcome's status exactly as before.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent import AgentRunner, Renderer, Respond
from conversation import Conversation
from reliability import (
    STATUS_BY_REASON,
    USER_MESSAGE_BY_REASON,
    AgentRuntimeError,
    AgentTurnOutcome,
    SkillLoadError,
    TurnContext,
    new_id,
)
from skill_runtime.models import SkillSelection
from skill_runtime.policy import RestrictedToolExecutor, declarations_for_names
from skill_runtime.prompting import compose_active_skill
from skill_runtime.registry import SkillRegistry
from skill_runtime.router import SkillRouter
from tools import ToolExecutor, ToolRegistry
from tracing import NullTraceSink, SafeTraceSink, TraceSink, build_event

# How many prior semantic messages the router sees as context.
_ROUTER_CONTEXT_MESSAGES = 6


@dataclass(frozen=True)
class SkillTurnResult:
    outcome: AgentTurnOutcome
    selection: SkillSelection


class SkillTurnOrchestrator:
    def __init__(
        self,
        *,
        skill_registry: SkillRegistry,
        router: SkillRouter,
        tool_registry: ToolRegistry,
        executor: ToolExecutor,
        respond: Respond,
        renderer_factory: Callable[[], Renderer],
        default_tools: Sequence[dict[str, Any]],
        run_id: str,
        max_tool_calls: int,
        max_identical_tool_calls: int,
        model_request_timeout_seconds: float,
        tool_execution_timeout_seconds: float,
        agent_turn_timeout_seconds: float,
        trace_sink: TraceSink = NullTraceSink(),
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = new_id,
        payload_preview_chars: int = 1000,
        on_selection: Callable[[SkillSelection], None] = lambda _selection: None,
    ) -> None:
        self._skill_registry = skill_registry
        self._router = router
        self._tool_registry = tool_registry
        self._executor = executor
        self._respond = respond
        self._renderer_factory = renderer_factory
        self._default_tools = tuple(default_tools)
        self._run_id = run_id
        self._max_tool_calls = max_tool_calls
        self._max_identical_tool_calls = max_identical_tool_calls
        self._model_request_timeout_seconds = model_request_timeout_seconds
        self._tool_execution_timeout_seconds = tool_execution_timeout_seconds
        self._agent_turn_timeout_seconds = agent_turn_timeout_seconds
        self._trace = SafeTraceSink(trace_sink, run_id)
        self._clock = clock
        self._id_factory = id_factory
        self._payload_preview_chars = payload_preview_chars
        self._on_selection = on_selection

    def run_turn(self, conversation: Conversation) -> SkillTurnResult:
        turn_id = self._id_factory()
        started = self._clock()
        deadline = started + self._agent_turn_timeout_seconds
        context = TurnContext(self._run_id, turn_id, started, deadline)

        catalog = self._skill_registry.catalog()
        try:
            selection = self._router.select(
                user_message=conversation.latest_user_message,
                conversation_context=conversation.stored_messages[:-1][
                    -_ROUTER_CONTEXT_MESSAGES:
                ],
                catalog=catalog,
                deadline=deadline,
                run_id=self._run_id,
                turn_id=turn_id,
                catalog_fingerprint=self._skill_registry.catalog_fingerprint(),
                trace=self._trace,
            )
        except AgentRuntimeError as error:
            return self._routing_failure(context, error)

        if selection.skill_name is None:
            return self._run_without_skill(conversation, context, selection)
        return self._run_with_skill(conversation, context, selection)

    def _run_without_skill(
        self,
        conversation: Conversation,
        context: TurnContext,
        selection: SkillSelection,
    ) -> SkillTurnResult:
        self._on_selection(selection)
        runner = self._build_runner(self._default_tools, self._executor)
        outcome = runner.run_turn(
            conversation.messages_for_model(),
            turn_context=context,
            selected_skill=None,
            routing_model_requests=selection.routing_requests,
        )
        return SkillTurnResult(outcome, selection)

    def _run_with_skill(
        self,
        conversation: Conversation,
        context: TurnContext,
        selection: SkillSelection,
    ) -> SkillTurnResult:
        try:
            spec = self._skill_registry.get(selection.skill_name)
        except KeyError as error:
            # Unreachable after validated startup; defense in depth.
            return self._routing_failure(
                context,
                SkillLoadError(
                    f"Selected skill '{selection.skill_name}' is not registered."
                ),
                routing_requests=selection.routing_requests,
                _error=error,
            )

        self._on_selection(selection)
        self._trace.emit(
            build_event(
                "skill_loaded",
                run_id=self._run_id,
                turn_id=context.turn_id,
                skill=spec.name,
                skill_version=spec.version,
                skill_fingerprint=spec.fingerprint,
                allowed_tools=list(spec.allowed_tools),
            )
        )
        tools = declarations_for_names(self._tool_registry, spec.allowed_tools)
        self._trace.emit(
            build_event(
                "skill_toolset_resolved",
                run_id=self._run_id,
                turn_id=context.turn_id,
                skill=spec.name,
                available_tools=[t["function"]["name"] for t in tools],
            )
        )
        restricted = RestrictedToolExecutor(
            self._executor, frozenset(spec.allowed_tools), skill=spec.name
        )
        runner = self._build_runner(tools, restricted)
        outcome = runner.run_turn(
            conversation.messages_for_model(
                additional_system=compose_active_skill(spec)
            ),
            turn_context=context,
            selected_skill=spec.name,
            skill_version=spec.version,
            routing_model_requests=selection.routing_requests,
        )
        return SkillTurnResult(outcome, selection)

    def _build_runner(
        self, tools: Sequence[dict[str, Any]], executor: Any
    ) -> AgentRunner:
        return AgentRunner(
            respond=self._respond,
            executor=executor,
            tools=tools,
            renderer=self._renderer_factory(),
            run_id=self._run_id,
            max_tool_calls=self._max_tool_calls,
            max_identical_tool_calls=self._max_identical_tool_calls,
            model_request_timeout_seconds=self._model_request_timeout_seconds,
            tool_execution_timeout_seconds=self._tool_execution_timeout_seconds,
            agent_turn_timeout_seconds=self._agent_turn_timeout_seconds,
            trace_sink=self._trace,
            clock=self._clock,
            id_factory=self._id_factory,
            payload_preview_chars=self._payload_preview_chars,
        )

    def _routing_failure(
        self,
        context: TurnContext,
        error: AgentRuntimeError,
        *,
        routing_requests: int | None = None,
        _error: Exception | None = None,
    ) -> SkillTurnResult:
        """Build the terminal outcome for a turn that failed before the agent ran.

        No agent loop executed, so the orchestrator emits the single terminal
        ``turn_finished`` itself. ``model_requests`` reflects the routing requests
        already spent; ``duration_ms`` covers routing from the shared turn start.
        """

        reason = error.reason
        if routing_requests is None:
            routing_requests = getattr(error, "routing_requests", 0)
        outcome = AgentTurnOutcome(
            run_id=self._run_id,
            turn_id=context.turn_id,
            status=STATUS_BY_REASON[reason],
            reason=reason,
            final_text=None,
            tool_calls_executed=0,
            model_requests=routing_requests,
            duration_ms=int((self._clock() - context.started_at) * 1000),
            error_message=USER_MESSAGE_BY_REASON.get(reason) or str(error),
        )
        self._trace.emit(
            build_event(
                "turn_finished",
                run_id=self._run_id,
                turn_id=context.turn_id,
                status=str(outcome.status),
                reason=str(outcome.reason),
                tool_calls_executed=0,
                model_requests=outcome.model_requests,
                routing_model_requests=routing_requests,
                agent_model_requests=0,
                selected_skill=None,
                skill_version=None,
                final_text_chars=0,
                duration_ms=outcome.duration_ms,
            )
        )
        selection = SkillSelection(
            None, str(error), "none", routing_requests, outcome.duration_ms
        )
        return SkillTurnResult(outcome, selection)
