"""Tool filtering and turn-scoped execution restriction (SPEC-012 §8-9).

Defense-in-depth at both boundaries: only the tools composed for the turn are
*declared* to the model, and the *executor* independently rejects a call outside
the same allowlist before the underlying handler runs. A skill can only reduce
the global tool set — it can never register, widen, or alter a tool.

What the skill reduces is the *domain* subset. PATCH-012-02 separates that from
the host's own baseline utilities: the effective view is the skill's tools
composed with a small, host-owned baseline set, plus the host's control tool.
:func:`compose_skill_toolset` is the single place that union is computed, for
both the router's initial selection and SPEC-018 mid-turn replacement, and it
returns the declarations and the executor policy together so the two can never
disagree about what the turn is allowed to do.
"""

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from reliability import SkillPolicyViolation
from tools import ToolExecutor, ToolRegistry


def declarations_for_names(
    registry: ToolRegistry, names: Sequence[str]
) -> tuple[dict[str, Any], ...]:
    """Ollama tool declarations for exactly ``names``, in that order.

    Each declaration is a deep copy (a caller mutating the result cannot corrupt
    the registry), and an unknown name is rejected rather than silently dropped.
    Mirrors the declaration shape of ``ToolRegistry.to_ollama_tools`` but filtered
    and reordered to the skill's allowlist.
    """

    declarations: list[dict[str, Any]] = []
    for name in names:
        spec = registry.get(name)  # raises KeyError on an unknown name
        declarations.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": copy.deepcopy(spec.input_schema),
                },
            }
        )
    return tuple(declarations)


class RestrictedToolExecutor:
    """A turn-scoped wrapper that gates dispatch by a skill's allowlist.

    Duck-types :meth:`ToolExecutor.execute` so it drops into ``AgentRunner`` with
    no loop changes. A disallowed call raises :class:`SkillPolicyViolation` before
    reaching the real executor, so the tool never runs (mapped by the runner to
    ``stopped/skill_policy_violation``). It only ever *narrows* access — an allowed
    call is delegated unchanged, preserving every tool's own safety controls.
    """

    def __init__(
        self, executor: ToolExecutor, allowed_tools: frozenset[str], *, skill: str
    ) -> None:
        self._executor = executor
        self._allowed_tools = allowed_tools
        self._skill = skill

    def execute(self, name: str, arguments: dict[str, Any]) -> dict:
        if name not in self._allowed_tools:
            raise SkillPolicyViolation(
                f"Skill '{self._skill}' is not permitted to call tool '{name}'.",
                requested_tool=name,
                skill=self._skill,
            )
        return self._executor.execute(name, arguments)


@dataclass(frozen=True)
class SkillToolset:
    """One active skill's effective tool view: declarations and policy together.

    Built only by :func:`compose_skill_toolset`. ``declarations`` is what the
    model is shown and ``allowed_tools`` is what the executor permits; both are
    derived from ``names``, so a tool can never be declared without being
    executable, nor silently permitted without being declared.

    ``skill_tools`` and ``baseline_tools`` keep the two sources distinguishable
    after the union, which is what lets a trace say *why* a tool was available.
    """

    declarations: tuple[dict[str, Any], ...]
    allowed_tools: frozenset[str]
    names: tuple[str, ...]
    skill_tools: tuple[str, ...]
    baseline_tools: tuple[str, ...]


def compose_skill_toolset(
    registry: ToolRegistry,
    skill_tools: Sequence[str],
    baseline_tools: Sequence[str] = (),
    control_declaration: dict[str, Any] | None = None,
) -> SkillToolset:
    """Compose the effective tool view for one active skill.

    ``skill_tools`` keeps its declared order first (so an existing skill's view is
    unchanged apart from what is appended), then the baseline names it does not
    already contain, then the host's control declaration last. Order is fully
    deterministic and duplicates collapse to their first occurrence, so a baseline
    tool a skill also declares appears exactly once, in its skill position.

    ``control_declaration`` is passed in rather than imported because the control
    tool is not a registry tool: it is host-generated (``activate_skill``), and
    taking it as an argument keeps this module free of a dependency on the
    activation layer that depends on it.

    An unknown name raises ``KeyError`` (via :func:`declarations_for_names`)
    rather than being dropped — a missing skill tool is a startup defect the
    loader already rejects, and a missing baseline tool is one
    ``validate_baseline_tools`` rejects.
    """

    registry_names: list[str] = []
    for name in (*skill_tools, *baseline_tools):
        if name not in registry_names:
            registry_names.append(name)

    declarations = declarations_for_names(registry, registry_names)
    names = list(registry_names)
    if control_declaration is not None:
        declarations = (*declarations, control_declaration)
        control_name = control_declaration["function"]["name"]
        if control_name not in names:
            names.append(control_name)

    return SkillToolset(
        declarations=declarations,
        allowed_tools=frozenset(names),
        names=tuple(names),
        skill_tools=tuple(skill_tools),
        baseline_tools=tuple(baseline_tools),
    )
