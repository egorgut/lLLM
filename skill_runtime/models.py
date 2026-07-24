"""Immutable skill contracts (SPEC-012 "Data model").

These dataclasses are frozen and carry no behavior. ``SkillSpec`` is the full
validated identity of one package; ``SkillCatalogEntry`` is the compact
routing-only view (name + description, nothing else); ``SkillSelection`` is the
router's decision for one turn.
"""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class SkillSpec:
    """The complete, validated identity of one skill package.

    ``allowed_tools`` preserves the order declared in ``SKILL.md``. The input
    schema is stored behind a read-only mapping so a holder cannot mutate the
    registered contract. ``fingerprint`` changes whenever relevant package
    content changes, giving deterministic trace correlation. Instances are only
    ever produced by the loader, after full validation.
    """

    name: str
    description: str
    version: str
    allowed_tools: tuple[str, ...]
    instruction: str
    input_schema: Mapping[str, Any]
    package_path: Path
    fingerprint: str

    def catalog_entry(self) -> "SkillCatalogEntry":
        return SkillCatalogEntry(name=self.name, description=self.description)


@dataclass(frozen=True)
class SkillCatalogEntry:
    """The only skill data the routing model ever sees.

    Deliberately excludes package paths, the full instruction, the input schema,
    allowed-tool details, and fingerprints (SPEC-012 "SkillCatalogEntry"). This
    keeps the routing prompt proportional to name + description, not full bodies.
    """

    name: str
    description: str


@dataclass(frozen=True)
class SkillSelection:
    """The router's zero-or-one decision for a single user turn.

    ``skill_name`` is ``None`` when no skill applies. ``source`` is one of
    ``"explicit"`` (the user named an exact skill), ``"model"`` (the routing
    model chose), or ``"none"`` (no catalog / no model call needed). ``reason``
    is bounded diagnostic text — not hidden chain-of-thought. ``routing_requests``
    counts model requests spent routing (0 for explicit/none, 1 or 2 for model).
    """

    skill_name: str | None
    reason: str
    source: str
    routing_requests: int
    duration_ms: int


def read_only_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """Wrap a parsed schema so downstream holders cannot mutate it in place."""

    return MappingProxyType(dict(schema))
