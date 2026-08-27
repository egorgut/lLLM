"""Mid-turn skill activation: the `activate_skill` control tool (SPEC-018).

SPEC-012 decided the turn's skill once, before any tool had run. This module
lets the model change that decision *while the turn runs*, without giving it any
new authority: the name it supplies is resolved by exact lookup against the
validated registry (never a filesystem path), the new instruction still enters
the system layer inside the host-generated wrapper, and the new tool view is
still only ever a *narrowing* of the global registry.

The router is untouched and remains the mandatory entry decision, so skill
coverage cannot regress; this only removes the ceiling of one skill per turn.

Everything here is host-owned: the declaration is generated from the registry
catalog (never from a skill package), `activate_skill` is a reserved name no
tool and no skill may take, and the per-turn activation cap is configuration the
model never sees.
"""

from collections.abc import Callable, Sequence
from typing import Any

from agent import ControlResult
from skill_runtime.models import SkillCatalogEntry, SkillSpec
from skill_runtime.policy import (
    RestrictedToolExecutor,
    SkillToolset,
    compose_skill_toolset,
)
from skill_runtime.prompting import compose_active_skill
from skill_runtime.registry import SkillRegistry
from tools import ToolExecutor, ToolRegistry
from tracing import NullTraceSink, TraceSink, build_event

# Reserved: the host owns this name. Startup rejects a registered tool or a skill
# package that claims it (see `SkillPackageLoader.load_all`), so the model can
# never be looking at two different things under one name.
ACTIVATE_SKILL_TOOL_NAME = "activate_skill"

_DESCRIPTION = (
    "Load the working procedure for one class of task, replacing any procedure "
    "currently active. Call this when the work turns out to belong to a "
    "different class than the one currently loaded."
)


def build_activate_skill_declaration(
    catalog: Sequence[SkillCatalogEntry],
) -> dict[str, Any] | None:
    """The model-facing declaration, rendered from the validated catalog.

    Returns ``None`` for an empty catalog: with no skill to activate, the tool
    would be an offer the host cannot honor (SPEC-018 §4.2).

    Only the compact name/description pairs the router already sees are used, so
    SPEC-012 §4's two-phase loading holds — no full instruction ever reaches the
    model unactivated.
    """

    if not catalog:
        return None
    return {
        "type": "function",
        "function": {
            "name": ACTIVATE_SKILL_TOOL_NAME,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": [entry.name for entry in catalog],
                        "description": " | ".join(
                            f"{entry.name}: {entry.description}" for entry in catalog
                        ),
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


def reserved_name_conflict(tool_registry: ToolRegistry) -> str | None:
    """Describe a registered tool colliding with the reserved name, else ``None``."""

    if ACTIVATE_SKILL_TOOL_NAME in tool_registry:
        return (
            f"Tool name '{ACTIVATE_SKILL_TOOL_NAME}' is reserved by the host for "
            "mid-turn skill activation and must not be registered as a tool."
        )
    return None


class SkillActivationHandler:
    """Handles `activate_skill` for exactly one turn.

    Created fresh per turn, because everything it holds is per-turn state: which
    skill is active now, and how many activations the turn has spent. It is a
    ``ControlToolHandler`` (``agent.ControlToolHandler``): the agent loop never
    dispatches these calls to the executor, and applies whatever new view this
    returns.

    Two properties make replacement safe:

    - a new restricted executor always wraps the **original global executor**,
      never the previous restricted wrapper, so restrictions cannot accumulate;
    - the new system block *replaces* the previous one (the loop composes it from
      the turn's base prompt), so two `<active_skill>` wrappers never coexist.
    """

    names = frozenset({ACTIVATE_SKILL_TOOL_NAME})

    def __init__(
        self,
        *,
        skill_registry: SkillRegistry,
        tool_registry: ToolRegistry,
        executor: ToolExecutor,
        declaration: dict[str, Any],
        max_activations: int,
        run_id: str,
        turn_id: str,
        initial_skill: SkillSpec | None = None,
        baseline_tools: Sequence[str] = (),
        trace: TraceSink = NullTraceSink(),
        on_activation: Callable[[SkillSpec, str | None], None] = (
            lambda _spec, _replaced: None
        ),
    ) -> None:
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        # The unrestricted global executor, kept for the lifetime of the turn.
        self._executor = executor
        self._declaration = declaration
        # The host baseline survives replacement: an activation swaps which
        # *domain* tools are available, never the host's own (PATCH-012-02).
        self._baseline_tools = tuple(baseline_tools)
        self._max_activations = max_activations
        self._run_id = run_id
        self._turn_id = turn_id
        self._trace = trace
        self._on_activation = on_activation
        self._initial_skill = initial_skill
        self._active: SkillSpec | None = initial_skill
        self._activations = 0

    @property
    def initial_skill(self) -> str | None:
        """The router's selection, preserved even after a replacement."""

        return self._initial_skill.name if self._initial_skill else None

    @property
    def active_skill(self) -> str | None:
        return self._active.name if self._active else None

    @property
    def active_skill_version(self) -> str | None:
        return self._active.version if self._active else None

    @property
    def activations(self) -> int:
        return self._activations

    def handle(self, name: str, arguments: dict[str, Any]) -> ControlResult:
        requested = arguments.get("name") if isinstance(arguments, dict) else None

        # The name is validated before the cap, so a nonexistent skill is never
        # reported as an exhausted budget. Neither failure is terminal: a wrong
        # name is exactly the kind of mistake the loop already recovers from for
        # every other tool (SPEC-018 §4.7).
        if (
            not isinstance(requested, str)
            or requested == ACTIVATE_SKILL_TOOL_NAME
            or not self._skill_registry.contains(requested)
        ):
            return ControlResult(
                result={
                    "ok": False,
                    "error": "unknown_skill",
                    "requested": requested if isinstance(requested, str) else None,
                    "available_skills": [
                        entry.name for entry in self._skill_registry.catalog()
                    ],
                }
            )

        if self._activations >= self._max_activations:
            return ControlResult(
                result={
                    "ok": False,
                    "error": "activation_limit",
                    "limit": self._max_activations,
                    "active_skill": self.active_skill,
                    "message": (
                        "The per-turn skill activation limit is reached. Continue "
                        "with the currently active procedure or answer directly."
                    ),
                }
            )

        replaced = self.active_skill
        spec = self._skill_registry.get(requested)
        self._activations += 1

        # Composed by the same rule the orchestrator used for the router's
        # selection, so the two entry points cannot drift. Computed before the
        # already-active branch below, because the receipt must report the real
        # effective view either way.
        toolset = compose_skill_toolset(
            self._tool_registry,
            spec.allowed_tools,
            self._baseline_tools,
            self._declaration,
        )

        if spec.name == replaced:
            # Already active: acknowledged, counted, but nothing is recomposed —
            # rewriting an identical system block would invalidate the model's
            # cached prefix for no gain (SPEC-018 §4.4).
            self._emit_activated(spec, replaced, recomposed=False)
            return ControlResult(result=self._receipt(spec, replaced, toolset))

        # Always over the original global executor: wrapping the previous
        # restricted wrapper would let one turn's restrictions accumulate.
        executor = RestrictedToolExecutor(
            self._executor, toolset.allowed_tools, skill=spec.name
        )
        self._active = spec

        # The same two events the orchestrator emits for the router's selection,
        # so one trace still reconstructs every view the turn ran under.
        self._trace.emit(
            build_event(
                "skill_loaded",
                run_id=self._run_id,
                turn_id=self._turn_id,
                skill=spec.name,
                skill_version=spec.version,
                skill_fingerprint=spec.fingerprint,
                allowed_tools=list(spec.allowed_tools),
            )
        )
        self._trace.emit(
            build_event(
                "skill_toolset_resolved",
                run_id=self._run_id,
                turn_id=self._turn_id,
                skill=spec.name,
                available_tools=list(toolset.names),
                skill_tools=list(toolset.skill_tools),
                baseline_tools=list(toolset.baseline_tools),
            )
        )
        self._emit_activated(spec, replaced, recomposed=True)
        self._on_activation(spec, replaced)

        return ControlResult(
            result=self._receipt(spec, replaced, toolset),
            tools=toolset.declarations,
            executor=executor,
            # Replaces the previous block entirely; the loop composes it from the
            # turn's base system prompt, so no stale wrapper can survive.
            system_suffix=compose_active_skill(spec),
        )

    def _receipt(
        self, spec: SkillSpec, replaced: str | None, toolset: SkillToolset
    ) -> dict[str, Any]:
        """The model-facing result: a receipt, never the instruction itself.

        The instruction went to the system layer; repeating it here would waste
        context and weaken the precedence SPEC-012 §10 establishes.

        ``available_tools`` is read off the composed toolset rather than rebuilt
        from ``spec.allowed_tools``: telling the model that a host baseline tool
        is gone while it is still declared would be the same contradiction this
        patch exists to remove (PATCH-012-02).
        """

        return {
            "ok": True,
            "skill": spec.name,
            "version": spec.version,
            "replaced": replaced,
            "available_tools": list(toolset.names),
        }

    def _emit_activated(
        self, spec: SkillSpec, replaced: str | None, *, recomposed: bool
    ) -> None:
        self._trace.emit(
            build_event(
                "skill_activated",
                run_id=self._run_id,
                turn_id=self._turn_id,
                skill=spec.name,
                skill_version=spec.version,
                skill_fingerprint=spec.fingerprint,
                replaced_skill=replaced,
                activation_index=self._activations,
                source="tool",
                recomposed=recomposed,
                allowed_tools=list(spec.allowed_tools),
            )
        )
