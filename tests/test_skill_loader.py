"""Loader validation tests (SPEC-012 §"Unit tests: package loader").

Every case builds a package on a temporary directory and asserts the loader
either produces a valid `SkillSpec` or fails fast with a `SkillPackageError`.
No live model, MCP, or network is involved.
"""

import json
import os

import pytest

from skill_runtime.loader import SkillPackageError, SkillPackageLoader
from tests.support import make_tool_registry

VALID_FRONT_MATTER = {
    "name": "sample_skill",
    "description": "A sample skill for tests",
    "version": '"1"',
    "allowed_tools": ["alpha", "beta"],
}

VALID_BODY = "\n".join(
    [
        "# Sample Skill",
        "",
        "## Use when",
        "When testing the loader.",
        "",
        "## Do not use when",
        "Never in production tests.",
        "",
        "## Input",
        "None required.",
        "",
        "## Available tools",
        "- alpha",
        "",
        "## Procedure",
        "1. Do the thing.",
        "",
        "## Constraints",
        "- Be deterministic.",
        "",
        "## Completion criteria",
        "Return a result.",
    ]
)

VALID_SCHEMA = {
    "type": "object",
    "properties": {"metric": {"type": "string"}},
    "required": ["metric"],
}


def _render_front_matter(front_matter: dict) -> str:
    lines = ["---"]
    for key, value in front_matter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def write_package(
    root,
    name="sample_skill",
    *,
    front_matter=None,
    body=VALID_BODY,
    schema=None,
    raw_skill_md=None,
    skill_md_bytes=None,
    schema_text=None,
    include_skill_md=True,
    include_schema=True,
):
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    if include_skill_md:
        skill_md = package / "SKILL.md"
        if skill_md_bytes is not None:
            skill_md.write_bytes(skill_md_bytes)
        elif raw_skill_md is not None:
            skill_md.write_text(raw_skill_md, encoding="utf-8")
        else:
            if front_matter is None:
                fm = dict(VALID_FRONT_MATTER)
                fm["name"] = name  # keep declared name aligned with the directory
            else:
                fm = dict(front_matter)
            skill_md.write_text(
                _render_front_matter(fm) + "\n\n" + body + "\n", encoding="utf-8"
            )
    if include_schema:
        schema_path = package / "input.schema.json"
        if schema_text is not None:
            schema_path.write_text(schema_text, encoding="utf-8")
        else:
            schema_path.write_text(
                json.dumps(VALID_SCHEMA if schema is None else schema), encoding="utf-8"
            )
    return package


def loader(**overrides):
    return SkillPackageLoader(**overrides)


def tool_registry():
    return make_tool_registry("alpha", "beta")


# 1. Valid package loads.
def test_valid_package_loads(tmp_path):
    write_package(tmp_path)
    registry = loader().load_all(tmp_path, tool_registry())
    spec = registry.get("sample_skill")
    assert spec.version == "1"
    assert spec.allowed_tools == ("alpha", "beta")
    assert "# Sample Skill" in spec.instruction
    assert spec.input_schema["required"] == ["metric"]


def test_missing_root_returns_empty_registry(tmp_path):
    registry = loader().load_all(tmp_path / "does_not_exist", tool_registry())
    assert len(registry) == 0


# 2. Missing SKILL.md.
def test_missing_skill_md(tmp_path):
    write_package(tmp_path, include_skill_md=False)
    with pytest.raises(SkillPackageError, match="missing SKILL.md"):
        loader().load_all(tmp_path, tool_registry())


# 3. Missing input schema.
def test_missing_input_schema(tmp_path):
    write_package(tmp_path, include_schema=False)
    with pytest.raises(SkillPackageError, match="missing input.schema.json"):
        loader().load_all(tmp_path, tool_registry())


# 4. Invalid YAML / malformed front matter.
def test_malformed_front_matter(tmp_path):
    write_package(tmp_path, raw_skill_md="no front matter here\n# Title\n")
    with pytest.raises(SkillPackageError, match="front matter"):
        loader().load_all(tmp_path, tool_registry())


# 5. Unsafe YAML tags rejected.
def test_unsafe_yaml_tag_rejected(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["description"] = "!!python/object/apply:os.system ['echo hi']"
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="unsupported YAML feature"):
        loader().load_all(tmp_path, tool_registry())


# 6. Missing metadata field.
def test_missing_metadata_field(tmp_path):
    fm = {k: v for k, v in VALID_FRONT_MATTER.items() if k != "version"}
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="missing required field: version"):
        loader().load_all(tmp_path, tool_registry())


# 7. Unknown metadata field.
def test_unknown_metadata_field(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["author"] = "someone"
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="unknown field: author"):
        loader().load_all(tmp_path, tool_registry())


# 8. Invalid skill name.
def test_invalid_skill_name(tmp_path):
    write_package(tmp_path, name="Bad-Name")
    with pytest.raises(SkillPackageError, match="Invalid skill package directory name"):
        loader().load_all(tmp_path, tool_registry())


# 9. Directory / name mismatch.
def test_directory_name_mismatch(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["name"] = "other_name"
    write_package(tmp_path, name="sample_skill", front_matter=fm)
    with pytest.raises(SkillPackageError, match="does not match declared name"):
        loader().load_all(tmp_path, tool_registry())


# 10. Duplicate allowed tool.
def test_duplicate_allowed_tool(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["allowed_tools"] = ["alpha", "alpha"]
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="duplicate"):
        loader().load_all(tmp_path, tool_registry())


# 11. Unknown allowed tool.
def test_unknown_allowed_tool(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["allowed_tools"] = ["alpha", "write_text_file"]
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="unknown tool 'write_text_file'"):
        loader().load_all(tmp_path, tool_registry())


def test_empty_allowed_tools(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["allowed_tools"] = []
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="non-empty allowed_tools"):
        loader().load_all(tmp_path, tool_registry())


# 12. Empty description.
def test_empty_description(tmp_path):
    fm = dict(VALID_FRONT_MATTER)
    fm["description"] = '""'
    write_package(tmp_path, front_matter=fm)
    with pytest.raises(SkillPackageError, match="empty or non-string description"):
        loader().load_all(tmp_path, tool_registry())


# 13. Oversized description.
def test_oversized_description(tmp_path):
    write_package(tmp_path)
    with pytest.raises(SkillPackageError, match="description exceeds"):
        loader(max_description_chars=5).load_all(tmp_path, tool_registry())


# 14. Oversized instruction.
def test_oversized_instruction(tmp_path):
    write_package(tmp_path)
    with pytest.raises(SkillPackageError, match="instruction exceeds"):
        loader(max_instruction_chars=10).load_all(tmp_path, tool_registry())


# 15. Missing required heading.
def test_missing_required_heading(tmp_path):
    body = VALID_BODY.replace("## Constraints", "## Notes")
    write_package(tmp_path, body=body)
    with pytest.raises(SkillPackageError, match="missing required heading '## Constraints'"):
        loader().load_all(tmp_path, tool_registry())


# 16. Duplicate required heading.
def test_duplicate_required_heading(tmp_path):
    body = VALID_BODY + "\n\n## Procedure\nAgain.\n"
    write_package(tmp_path, body=body)
    with pytest.raises(SkillPackageError, match="duplicate heading '## Procedure'"):
        loader().load_all(tmp_path, tool_registry())


def test_multiple_h1_rejected(tmp_path):
    body = "# One\n\n# Two\n\n" + VALID_BODY.split("\n", 1)[1]
    write_package(tmp_path, body=body)
    with pytest.raises(SkillPackageError, match="exactly one H1"):
        loader().load_all(tmp_path, tool_registry())


# 17. Invalid UTF-8.
def test_invalid_utf8(tmp_path):
    write_package(tmp_path, skill_md_bytes=b"---\nname: x\n---\n\xff\xfe body")
    with pytest.raises(SkillPackageError, match="not valid UTF-8"):
        loader().load_all(tmp_path, tool_registry())


# 18. NUL byte.
def test_nul_byte_rejected(tmp_path):
    write_package(tmp_path, skill_md_bytes=b"---\nname: sample_skill\n---\n\x00body")
    with pytest.raises(SkillPackageError, match="NUL byte"):
        loader().load_all(tmp_path, tool_registry())


# 19. Invalid JSON schema.
def test_invalid_json_schema(tmp_path):
    write_package(tmp_path, schema_text="{not valid json")
    with pytest.raises(SkillPackageError, match="not valid JSON"):
        loader().load_all(tmp_path, tool_registry())


def test_schema_not_object_root(tmp_path):
    write_package(tmp_path, schema={"type": "array", "items": {}})
    with pytest.raises(SkillPackageError, match="'type' must be 'object'"):
        loader().load_all(tmp_path, tool_registry())


# 20. External schema reference rejected.
def test_external_schema_ref_rejected(tmp_path):
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "https://example.com/x.json"}},
    }
    write_package(tmp_path, schema=schema)
    with pytest.raises(SkillPackageError, match=r"\$ref"):
        loader().load_all(tmp_path, tool_registry())


# 21. Symlink policy: a symlinked package directory is rejected.
def test_symlinked_package_dir_rejected(tmp_path):
    real = tmp_path / "_real"
    write_package(real, name="sample_skill")
    root = tmp_path / "skills"
    root.mkdir()
    os.symlink(real / "sample_skill", root / "sample_skill")
    with pytest.raises(SkillPackageError, match="Symlinked entry"):
        loader().load_all(root, tool_registry())


# 22. Path traversal defense: a symlinked file escaping the root is rejected.
def test_symlinked_file_escaping_root_rejected(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "skills"
    package = write_package(root, name="sample_skill")
    (package / "SKILL.md").unlink()
    os.symlink(outside, package / "SKILL.md")
    with pytest.raises(SkillPackageError, match="symlinked file"):
        loader().load_all(root, tool_registry())


def test_unexpected_non_directory_entry_rejected(tmp_path):
    write_package(tmp_path, name="sample_skill")
    (tmp_path / "stray.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(SkillPackageError, match="Unexpected non-directory entry"):
        loader().load_all(tmp_path, tool_registry())


def test_readme_at_root_is_allowed(tmp_path):
    write_package(tmp_path, name="sample_skill")
    (tmp_path / "README.md").write_text("# skills", encoding="utf-8")
    registry = loader().load_all(tmp_path, tool_registry())
    assert registry.contains("sample_skill")


def test_hidden_os_artifacts_are_skipped(tmp_path):
    write_package(tmp_path, name="sample_skill")
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x01mac junk")
    (tmp_path / ".hidden_dir").mkdir()
    registry = loader().load_all(tmp_path, tool_registry())
    assert [s.name for s in registry.list_skills()] == ["sample_skill"]


def test_max_skills_enforced(tmp_path):
    write_package(tmp_path, name="alpha_skill")
    write_package(tmp_path, name="beta_skill")
    with pytest.raises(SkillPackageError, match="Too many skill packages"):
        loader(max_skills=1).load_all(tmp_path, tool_registry())


# 23. Deterministic fingerprint.
def test_deterministic_fingerprint(tmp_path):
    write_package(tmp_path, name="sample_skill")
    first = loader().load_all(tmp_path, tool_registry()).get("sample_skill")
    second = loader().load_all(tmp_path, tool_registry()).get("sample_skill")
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.startswith("sha256:")


def test_fingerprint_changes_with_body(tmp_path):
    write_package(tmp_path, name="sample_skill")
    base = loader().load_all(tmp_path, tool_registry()).get("sample_skill")

    other = tmp_path / "other"
    write_package(other, name="sample_skill", body=VALID_BODY + "\n\nExtra line.")
    changed = loader().load_all(other, tool_registry()).get("sample_skill")
    assert base.fingerprint != changed.fingerprint


def test_deterministic_registration_order(tmp_path):
    for name in ("gamma_skill", "alpha_skill", "beta_skill"):
        write_package(tmp_path, name=name)
    registry = loader().load_all(tmp_path, make_tool_registry("alpha", "beta"))
    assert [s.name for s in registry.list_skills()] == [
        "alpha_skill",
        "beta_skill",
        "gamma_skill",
    ]
