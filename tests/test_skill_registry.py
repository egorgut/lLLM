"""SkillRegistry tests (SPEC-012 §"Unit tests: registry")."""

from pathlib import Path

import pytest

from skill_runtime.models import SkillCatalogEntry, SkillSpec
from skill_runtime.registry import SkillRegistry


def make_spec(name, *, description="A skill.", version="1", instruction="# X\nbody"):
    return SkillSpec(
        name=name,
        description=description,
        version=version,
        allowed_tools=("alpha",),
        instruction=instruction,
        input_schema={"type": "object", "properties": {}},
        package_path=Path("/skills") / name,
        fingerprint=f"sha256:{name}",
    )


def test_register_and_exact_lookup():
    registry = SkillRegistry()
    spec = make_spec("sales_analysis")
    registry.register(spec)
    assert registry.get("sales_analysis") is spec
    assert registry.contains("sales_analysis")
    assert "sales_analysis" in registry
    assert len(registry) == 1


def test_reject_duplicate():
    registry = SkillRegistry()
    registry.register(make_spec("sales_analysis"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_spec("sales_analysis"))


def test_unknown_lookup_raises():
    registry = SkillRegistry()
    with pytest.raises(KeyError, match="Unknown skill"):
        registry.get("missing")
    assert not registry.contains("missing")


def test_deterministic_order():
    registry = SkillRegistry()
    for name in ("alpha_skill", "beta_skill", "gamma_skill"):
        registry.register(make_spec(name))
    assert [s.name for s in registry.list_skills()] == [
        "alpha_skill",
        "beta_skill",
        "gamma_skill",
    ]


def test_catalog_excludes_full_instruction():
    registry = SkillRegistry()
    registry.register(make_spec("sales_analysis", instruction="SECRET FULL BODY"))
    catalog = registry.catalog()
    assert catalog == (
        SkillCatalogEntry(name="sales_analysis", description="A skill."),
    )
    serialized = str(catalog)
    assert "SECRET FULL BODY" not in serialized
    entry = catalog[0]
    assert not hasattr(entry, "instruction")
    assert not hasattr(entry, "input_schema")


def test_catalog_fingerprint_stable_and_content_sensitive():
    registry = SkillRegistry()
    registry.register(make_spec("a_skill", description="one"))
    first = registry.catalog_fingerprint()
    assert first == registry.catalog_fingerprint()
    assert first.startswith("sha256:")

    other = SkillRegistry()
    other.register(make_spec("a_skill", description="two"))
    assert other.catalog_fingerprint() != first


def test_returns_are_immutable_tuples():
    registry = SkillRegistry()
    registry.register(make_spec("a_skill"))
    assert isinstance(registry.list_skills(), tuple)
    assert isinstance(registry.catalog(), tuple)
