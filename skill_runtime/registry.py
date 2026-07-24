"""The in-memory, exact-name skill registry (SPEC-012 "Skill registry").

Owns validated skill identities, exact-name lookup, duplicate rejection, and the
compact catalog used for routing. It does not do model routing, tool execution,
conversation state, tracing, hot reload, or agent-loop policy. Registration order
is deterministic (the loader sorts packages by name before registering), which
keeps the catalog JSON and its fingerprint stable across runs.
"""

from reliability import canonical_json, sha256_of
from skill_runtime.models import SkillCatalogEntry, SkillSpec


class SkillRegistry:
    def __init__(self) -> None:
        # A dict preserves insertion order, giving deterministic enumeration and
        # a reproducible catalog.
        self._skills: dict[str, SkillSpec] = {}

    def register(self, skill: SkillSpec) -> None:
        """Add a validated skill; reject a duplicate name with ``ValueError``."""

        if skill.name in self._skills:
            raise ValueError(f"Skill '{skill.name}' is already registered.")
        self._skills[skill.name] = skill

    def get(self, name: str) -> SkillSpec:
        """Return the skill for an exact name, or raise ``KeyError``.

        The model may select only a name already present here; a lookup never
        touches the filesystem with model-generated text (SPEC-012 §7).
        """

        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"Unknown skill: {name}") from None

    def contains(self, name: str) -> bool:
        return name in self._skills

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    def list_skills(self) -> tuple[SkillSpec, ...]:
        return tuple(self._skills.values())

    def catalog(self) -> tuple[SkillCatalogEntry, ...]:
        """The compact routing view: name + description only, in order."""

        return tuple(skill.catalog_entry() for skill in self._skills.values())

    def catalog_fingerprint(self) -> str:
        """A stable hash over the ordered catalog, stamped into routing traces."""

        payload = [
            {"name": entry.name, "description": entry.description}
            for entry in self.catalog()
        ]
        return "sha256:" + sha256_of(canonical_json(payload))
