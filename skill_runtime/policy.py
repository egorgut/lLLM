"""Tool filtering and turn-scoped execution restriction (SPEC-012 §8-9).

Defense-in-depth at both boundaries: only the selected skill's allowed tool
*declarations* are sent to the model, and the *executor* independently rejects a
call outside the allowlist before the underlying handler runs. A skill can only
reduce the global tool set — it can never register, widen, or alter a tool.
"""

import copy
from collections.abc import Sequence
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
