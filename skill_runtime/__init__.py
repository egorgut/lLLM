"""The filesystem-backed, host-controlled skill layer above tools (SPEC-012).

A *tool* answers "how do I perform one operation?"; a *skill* answers "how do I
solve one defined class of tasks?" using a restricted subset of registered
tools. Skills are declarative packages under the repository ``skills/`` root —
never executable plugins. This package owns the runtime code (models, loader,
registry, router, prompt composition, tool policy, and turn orchestration); the
declarative ``skills/`` directory holds the packages themselves.

The dependency direction is strictly ``SkillRegistry -> ToolRegistry`` (by
name); tools never depend on skills, and a skill can only ever *reduce* the
global tool set, never widen capability.
"""

from skill_runtime.config_validation import validate_skill_config
from skill_runtime.loader import SkillPackageError, SkillPackageLoader
from skill_runtime.models import SkillCatalogEntry, SkillSelection, SkillSpec
from skill_runtime.orchestrator import SkillTurnOrchestrator
from skill_runtime.policy import RestrictedToolExecutor, declarations_for_names
from skill_runtime.prompting import compose_active_skill
from skill_runtime.registry import SkillRegistry
from skill_runtime.router import SkillRouter, parse_explicit_selection

__all__ = [
    "RestrictedToolExecutor",
    "SkillCatalogEntry",
    "SkillPackageError",
    "SkillPackageLoader",
    "SkillRegistry",
    "SkillRouter",
    "SkillSelection",
    "SkillSpec",
    "SkillTurnOrchestrator",
    "compose_active_skill",
    "declarations_for_names",
    "parse_explicit_selection",
    "validate_skill_config",
]
