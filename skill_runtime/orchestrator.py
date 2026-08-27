"""The skill-aware user turn (SPEC-012 §"User turn").

One object owns a whole user turn so the CLI loop and the integration tests share
the exact same path. It creates the single :class:`TurnContext` (before routing),
routes to zero or one skill, composes the active-skill prompt and restricted tool
view, then hands off to the unchanged bounded, observable ``AgentRunner`` with the
shared deadline. Routing and execution therefore share one ``run_id``/``turn_id``
and one whole-turn budget; ``duration_ms`` and ``model_requests`` cover both.

The router remains the mandatory entry decision. What SPEC-018 adds is that the
composed view is no longer frozen for the whole turn: the orchestrator also hands
the runner a :class:`SkillActivationHandler`, so the model can replace the active
skill mid-turn through the host-owned ``activate_skill`` tool. At most one skill
is ever active at an instant — activation replaces, it never stacks.

The orchestrator never persists routing protocol messages and never mutates the
conversation — it only reads the latest user message and a bounded context slice
for routing. Persistence and rollback stay with the caller, keyed off the returned
outcome's status exactly as before.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent import AgentRunner, ControlToolHandler, Renderer, Respond
from config import MAX_SKILL_ACTIVATIONS_PER_TURN
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
from skill_runtime.activation import (
    SkillActivationHandler,
    build_activate_skill_declaration,
)
from skill_runtime.models import SkillSelection, SkillSpec
from skill_runtime.policy import RestrictedToolExecutor, compose_skill_toolset
from skill_runtime.prompting import compose_active_skill
from skill_runtime.registry import SkillRegistry
from skill_runtime.router import SkillRouter
from tools import ToolExecutor, ToolRegistry
from tracing import NullTraceSink, SafeTraceSink, TraceSink, build_event

# How many prior semantic messages the router sees as context.
_ROUTER_CONTEXT_MESSAGES = 6


@dataclass(frozen=True)
class SkillTurnResult:
    """One turn's outcome plus how its skill decision evolved.

    ``selection`` stays the router's decision, unchanged in meaning. ``final_skill``
    is the skill active when the turn ended — the same as ``selection.skill_name``
    unless the model activated something mid-turn (SPEC-018).
    """

    outcome: AgentTurnOutcome
    selection: SkillSelection
    final_skill: str | None = None
    activations: int = 0


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
        on_turn_context: Callable[[TurnContext], None] = lambda _context: None,
        redacted_argument_tools: frozenset[str] = frozenset(),
        max_skill_activations: int = MAX_SKILL_ACTIVATIONS_PER_TURN,
        baseline_tools: Sequence[str] = (),
        on_activation: Callable[[SkillSpec, str | None], None] = (
            lambda _spec, _replaced: None
        ),
        preserve_reasoning: bool = True,
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
        self._on_turn_context = on_turn_context
        self._redacted_argument_tools = redacted_argument_tools
        self._max_skill_activations = max_skill_activations
        # Host-owned tools that survive skill selection and replacement
        # (PATCH-012-02). Injected rather than read from config, because the
        # host owns the baseline and this layer only composes what it is
        # given; an empty default is a view identical to SPEC-012's.
        self._baseline_tools = tuple(baseline_tools)
        self._on_activation = on_activation
        # Passed straight through to every runner this orchestrator builds
        # (SPEC-020 §7.4). The orchestrator has no opinion on reasoning: an
        # `activate_skill` decision preserves its own reasoning exactly like any
        # other tool decision, and the SPEC-018 system-suffix replacement stays
        # the authority on what the model is told afterwards.
        self._preserve_reasoning = preserve_reasoning
        # The registry is frozen at startup, so the declaration is built once.
        # None when there is no skill to activate (SPEC-018 §4.2).
        self._activate_declaration = build_activate_skill_declaration(
            skill_registry.catalog()
        )

    def run_turn(self, conversation: Conversation) -> SkillTurnResult:
        turn_id = self._id_factory()
        started = self._clock()
        deadline = started + self._agent_turn_timeout_seconds
        context = TurnContext(self._run_id, turn_id, started, deadline)
        # Announce the turn's identity and deadline before routing, so a
        # host-owned per-turn resource (SPEC-016's sandbox workspace) is bound to
        # the same turn and the same budget as everything that follows. The
        # orchestrator itself neither creates nor cleans up such a resource: the
        # caller that opens the turn also closes it, keyed off the outcome.
        self._on_turn_context(context)

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

    def _activation_handler(
        self, context: TurnContext, initial_skill: SkillSpec | None
    ) -> SkillActivationHandler | None:
        """A fresh handler for this turn, or ``None`` when there is no catalog.

        Per turn, because everything it holds — which skill is active, how many
        activations are spent — is turn state that must never leak into the next
        one (SPEC-018 §4.10).
        """

        if self._activate_declaration is None:
            return None
        return SkillActivationHandler(
            skill_registry=self._skill_registry,
            tool_registry=self._tool_registry,
            # The original global executor: a mid-turn activation wraps this,
            # never a restricted wrapper already in play.
            executor=self._executor,
            declaration=self._activate_declaration,
            max_activations=self._max_skill_activations,
            run_id=self._run_id,
            turn_id=context.turn_id,
            initial_skill=initial_skill,
            baseline_tools=self._baseline_tools,
            trace=self._trace,
            on_activation=self._on_activation,
        )

    def _turn_fields(
        self, handler: SkillActivationHandler | None, initial_skill: str | None
    ) -> Callable[[], dict[str, Any]]:
        """The skill fields ``turn_finished`` reports, read when the turn ends.

        ``selected_skill`` means "the skill active when the turn ended"; the
        router's own choice is preserved as ``initial_skill``, and every turn
        reports how many times the skill changed (SPEC-018 §4.9).
        """

        if handler is None:
            return lambda: {
                "initial_skill": initial_skill,
                "skill_activations": 0,
            }
        return lambda: {
            "selected_skill": handler.active_skill,
            "skill_version": handler.active_skill_version,
            "initial_skill": handler.initial_skill,
            "skill_activations": handler.activations,
        }

    def _with_activate_skill(
        self, tools: Sequence[dict[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        """Append the host's activation declaration, when there is one."""

        if self._activate_declaration is None:
            return tuple(tools)
        return (*tools, self._activate_declaration)

    def _run_without_skill(
        self,
        conversation: Conversation,
        context: TurnContext,
        selection: SkillSelection,
    ) -> SkillTurnResult:
        self._on_selection(selection)
        handler = self._activation_handler(context, None)
        # The global executor is unrestricted here; `activate_skill` never
        # reaches it, because the loop dispatches a control tool to the handler.
        runner = self._build_runner(
            self._with_activate_skill(self._default_tools),
            self._executor,
            control_handler=handler,
            extra_turn_fields=self._turn_fields(handler, None),
        )
        outcome = runner.run_turn(
            conversation.messages_for_model(),
            turn_context=context,
            selected_skill=None,
            routing_model_requests=selection.routing_requests,
        )
        return SkillTurnResult(
            outcome,
            selection,
            final_skill=handler.active_skill if handler else None,
            activations=handler.activations if handler else 0,
        )

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
        # The skill narrows the domain tools; the host baseline and the reserved
        # activation name are composed on top (PATCH-012-02). One helper builds
        # both the declarations and the allowlist, so the executor can never
        # disagree with what the model was shown — even though the loop
        # intercepts the control call before it reaches dispatch.
        toolset = compose_skill_toolset(
            self._tool_registry,
            spec.allowed_tools,
            self._baseline_tools,
            self._activate_declaration,
        )
        self._trace.emit(
            build_event(
                "skill_toolset_resolved",
                run_id=self._run_id,
                turn_id=context.turn_id,
                skill=spec.name,
                available_tools=list(toolset.names),
                skill_tools=list(toolset.skill_tools),
                baseline_tools=list(toolset.baseline_tools),
            )
        )
        restricted = RestrictedToolExecutor(
            self._executor, toolset.allowed_tools, skill=spec.name
        )
        handler = self._activation_handler(context, spec)
        runner = self._build_runner(
            toolset.declarations,
            restricted,
            control_handler=handler,
            extra_turn_fields=self._turn_fields(handler, spec.name),
        )
        outcome = runner.run_turn(
            # The active-skill block is passed separately rather than baked into
            # the system message, so an activation can replace it mid-turn
            # without the loop having to tell base prompt and skill block apart.
            conversation.messages_for_model(),
            turn_context=context,
            selected_skill=spec.name,
            skill_version=spec.version,
            routing_model_requests=selection.routing_requests,
            system_suffix=compose_active_skill(spec),
        )
        return SkillTurnResult(
            outcome,
            selection,
            final_skill=handler.active_skill if handler else spec.name,
            activations=handler.activations if handler else 0,
        )

    def _build_runner(
        self,
        tools: Sequence[dict[str, Any]],
        executor: Any,
        *,
        control_handler: ControlToolHandler | None = None,
        extra_turn_fields: Callable[[], dict[str, Any]] = lambda: {},
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
            redacted_argument_tools=self._redacted_argument_tools,
            control_handler=control_handler,
            extra_turn_fields=extra_turn_fields,
            preserve_reasoning=self._preserve_reasoning,
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
                initial_skill=None,
                skill_activations=0,
                final_text_chars=0,
                duration_ms=outcome.duration_ms,
            )
        )
        selection = SkillSelection(
            None, str(error), "none", routing_requests, outcome.duration_ms
        )
        return SkillTurnResult(outcome, selection)
