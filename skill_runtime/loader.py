"""Filesystem discovery and strict validation of skill packages (SPEC-012).

Startup is fail-fast: every package under the ``skills/`` root is discovered,
validated, and frozen into a :class:`SkillRegistry` before the chat loop starts,
so a malformed package can never surface mid-turn. All failures raise
:class:`SkillPackageError` with a stable message naming the package.

Front matter is parsed by a small in-house parser for the *documented constrained
subset* below — not a general YAML engine — so the loader needs no third-party
dependency and can never construct arbitrary Python objects from tags (a
deliberate deviation from a general ``yaml.safe_load``, matching the project's
framework-free, minimal-dependency ethos; SPEC-012 §"SKILL.md front matter" allows
``yaml.safe_load`` *or equivalent*). The supported subset is exactly:

- top-level ``key: value`` scalars (optionally single/double quoted);
- one block list of scalars (``key:`` then indented ``- item`` lines);
- nothing else — anchors, aliases, tags, nested maps, and block scalars are
  rejected.

The input-schema validator is likewise a documented structural check over a
Draft 2020-12 *subset* (object root, ``properties`` object, ``required`` array of
strings, no ``$ref``), mirroring ``tools/registry._validate_schema``. It performs
no network access and resolves no references.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import (
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_INSTRUCTION_CHARS,
    MAX_SKILL_SCHEMA_BYTES,
    MAX_SKILLS,
)
from reliability import canonical_json, sha256_of
from skill_runtime.models import SkillSpec, read_only_schema
from skill_runtime.registry import SkillRegistry
from tools import ToolRegistry


class SkillPackageError(Exception):
    """A skill package is malformed or references something unavailable.

    Raised only at startup discovery/validation; it never becomes a turn outcome
    because no turn has started (SPEC-012 §17).
    """


_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ALLOWED_FRONT_MATTER_KEYS = frozenset({"name", "description", "version", "allowed_tools"})
_MAX_VERSION_CHARS = 64
# Required H2 headings, validated case-sensitively and each exactly once.
_REQUIRED_HEADINGS = (
    "Use when",
    "Do not use when",
    "Input",
    "Available tools",
    "Procedure",
    "Constraints",
    "Completion criteria",
)
_README_NAMES = frozenset({"readme", "readme.md", "readme.txt"})


@dataclass(frozen=True)
class SkillPackageLoader:
    """Loads and validates every package under a ``skills/`` root.

    Bounds default to the host configuration but are injectable so tests can
    exercise the oversize paths deterministically without huge fixtures.
    """

    max_instruction_chars: int = MAX_SKILL_INSTRUCTION_CHARS
    max_schema_bytes: int = MAX_SKILL_SCHEMA_BYTES
    max_skills: int = MAX_SKILLS
    max_description_chars: int = MAX_SKILL_DESCRIPTION_CHARS

    def load_all(self, skills_root: Path, tool_registry: ToolRegistry) -> SkillRegistry:
        registry = SkillRegistry()
        root = Path(skills_root)
        # A missing root is a valid development state: no skills, no routing
        # model call, ordinary agent behavior continues (SPEC-012 §"Empty skill
        # registry").
        if not root.exists():
            return registry

        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise SkillPackageError(f"Skills root is not a directory: {skills_root}")

        package_dirs: list[Path] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            # Skip hidden OS/VCS artifacts (e.g. macOS `.DS_Store`, `.git`); a
            # valid skill name can never start with a dot, so these are never
            # packages and must not fail startup.
            if child.name.startswith("."):
                continue
            if child.is_symlink():
                raise SkillPackageError(
                    f"Symlinked entry under skills/ is not allowed: {child.name}"
                )
            if child.is_dir():
                package_dirs.append(child)
            elif child.name.lower() not in _README_NAMES:
                raise SkillPackageError(
                    f"Unexpected non-directory entry under skills/: {child.name}"
                )

        if len(package_dirs) > self.max_skills:
            raise SkillPackageError(
                f"Too many skill packages: {len(package_dirs)} exceeds "
                f"MAX_SKILLS={self.max_skills}."
            )

        for package_dir in package_dirs:
            spec = self._load_package(package_dir, resolved_root, tool_registry)
            try:
                registry.register(spec)
            except ValueError as error:  # duplicate skill name
                raise SkillPackageError(str(error)) from error
        return registry

    def _load_package(
        self, package_dir: Path, resolved_root: Path, tool_registry: ToolRegistry
    ) -> SkillSpec:
        package_name = package_dir.name
        if not _NAME_PATTERN.fullmatch(package_name):
            raise SkillPackageError(
                f"Invalid skill package directory name: {package_name!r} "
                f"(must match {_NAME_PATTERN.pattern})."
            )

        skill_md = self._require_inside(package_dir / "SKILL.md", resolved_root, package_name)
        schema_path = self._require_inside(
            package_dir / "input.schema.json", resolved_root, package_name
        )
        if not skill_md.is_file():
            raise SkillPackageError(f"Skill '{package_name}' is missing SKILL.md.")
        if not schema_path.is_file():
            raise SkillPackageError(
                f"Skill '{package_name}' is missing input.schema.json."
            )

        text = _read_text(skill_md, package_name, "SKILL.md")
        front_matter, body = _parse_front_matter(text, package_name)
        metadata = _validate_metadata(
            front_matter,
            package_name,
            tool_registry,
            max_description_chars=self.max_description_chars,
        )
        instruction = _validate_body(body, package_name, self.max_instruction_chars)
        schema = _load_schema(
            schema_path, package_name, resolved_root, self.max_schema_bytes
        )

        fingerprint = "sha256:" + sha256_of(
            canonical_json(
                {
                    "name": metadata["name"],
                    "description": metadata["description"],
                    "version": metadata["version"],
                    "allowed_tools": list(metadata["allowed_tools"]),
                    "instruction": instruction,
                    "schema": schema,
                }
            )
        )

        return SkillSpec(
            name=metadata["name"],
            description=metadata["description"],
            version=metadata["version"],
            allowed_tools=metadata["allowed_tools"],
            instruction=instruction,
            input_schema=read_only_schema(schema),
            package_path=package_dir,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _require_inside(path: Path, resolved_root: Path, package_name: str) -> Path:
        """Reject a symlinked file and any path escaping the skills root."""

        if path.is_symlink():
            raise SkillPackageError(
                f"Skill '{package_name}' contains a symlinked file: {path.name}."
            )
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise SkillPackageError(
                f"Skill '{package_name}' path escapes the skills root: {path.name}."
            )
        return path


def _read_text(path: Path, package_name: str, label: str) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise SkillPackageError(
            f"Skill '{package_name}' {label} contains a NUL byte."
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillPackageError(
            f"Skill '{package_name}' {label} is not valid UTF-8."
        ) from error


def _parse_scalar(value: str, package_name: str) -> str:
    if value[:1] in ("!", "&", "*", "|", ">", "{", "["):
        raise SkillPackageError(
            f"Skill '{package_name}' front matter uses an unsupported YAML feature."
        )
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_front_matter(text: str, package_name: str) -> tuple[dict[str, Any], str]:
    """Parse the constrained front-matter subset; return (mapping, body)."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillPackageError(
            f"Skill '{package_name}' SKILL.md must begin with '---' front matter."
        )

    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise SkillPackageError(
            f"Skill '{package_name}' SKILL.md has unterminated front matter."
        )

    data: dict[str, Any] = {}
    fm_lines = lines[1:end]
    i = 0
    while i < len(fm_lines):
        raw = fm_lines[i]
        if not raw.strip():
            i += 1
            continue
        if raw[0] in (" ", "\t"):
            raise SkillPackageError(
                f"Skill '{package_name}' front matter has unexpected indentation."
            )
        if ":" not in raw:
            raise SkillPackageError(
                f"Skill '{package_name}' front matter line is not 'key: value': {raw!r}."
            )
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        if key in data:
            raise SkillPackageError(
                f"Skill '{package_name}' front matter has a duplicate key: {key}."
            )
        if key not in _ALLOWED_FRONT_MATTER_KEYS:
            raise SkillPackageError(
                f"Skill '{package_name}' front matter has an unknown field: {key}."
            )
        if rest == "":
            # A block list: consume the following indented '- item' lines.
            items: list[str] = []
            i += 1
            while i < len(fm_lines) and fm_lines[i][:1] in (" ", "\t"):
                item = fm_lines[i].strip()
                if item:
                    if not item.startswith("- "):
                        raise SkillPackageError(
                            f"Skill '{package_name}' front matter list item is "
                            f"not '- value': {item!r}."
                        )
                    items.append(_parse_scalar(item[2:].strip(), package_name))
                i += 1
            data[key] = items
        else:
            data[key] = _parse_scalar(rest, package_name)
            i += 1

    body = "\n".join(lines[end + 1 :]).strip()
    return data, body


def _validate_metadata(
    data: dict[str, Any],
    package_name: str,
    tool_registry: ToolRegistry,
    *,
    max_description_chars: int,
) -> dict[str, Any]:
    for key in ("name", "description", "version", "allowed_tools"):
        if key not in data:
            raise SkillPackageError(
                f"Skill '{package_name}' front matter is missing required field: {key}."
            )

    name = data["name"]
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise SkillPackageError(
            f"Skill '{package_name}' declares an invalid name: {name!r}."
        )
    if name != package_name:
        raise SkillPackageError(
            f"Skill directory '{package_name}' does not match declared name '{name}'."
        )

    description = data["description"]
    if not isinstance(description, str) or not description.strip():
        raise SkillPackageError(
            f"Skill '{package_name}' has an empty or non-string description."
        )
    if len(description) > max_description_chars:
        raise SkillPackageError(
            f"Skill '{package_name}' description exceeds "
            f"{max_description_chars} characters."
        )

    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise SkillPackageError(
            f"Skill '{package_name}' has an empty or non-string version."
        )
    if len(version) > _MAX_VERSION_CHARS:
        raise SkillPackageError(
            f"Skill '{package_name}' version exceeds {_MAX_VERSION_CHARS} characters."
        )

    allowed = data["allowed_tools"]
    if not isinstance(allowed, list) or not allowed:
        raise SkillPackageError(
            f"Skill '{package_name}' must declare a non-empty allowed_tools list."
        )
    if not all(isinstance(item, str) and item for item in allowed):
        raise SkillPackageError(
            f"Skill '{package_name}' allowed_tools must be non-empty tool names."
        )
    if len(set(allowed)) != len(allowed):
        raise SkillPackageError(
            f"Skill '{package_name}' allowed_tools contains a duplicate."
        )
    for tool_name in allowed:
        if tool_name not in tool_registry:
            raise SkillPackageError(
                f"Skill '{package_name}' references unknown tool '{tool_name}'."
            )

    return {
        "name": name,
        "description": description.strip(),
        "version": version.strip(),
        "allowed_tools": tuple(allowed),
    }


def _validate_body(body: str, package_name: str, max_instruction_chars: int) -> str:
    if not body.strip():
        raise SkillPackageError(f"Skill '{package_name}' SKILL.md has an empty body.")
    if len(body) > max_instruction_chars:
        raise SkillPackageError(
            f"Skill '{package_name}' instruction exceeds "
            f"{max_instruction_chars} characters."
        )

    lines = body.splitlines()
    h1_count = sum(1 for line in lines if line.startswith("# "))
    if h1_count != 1:
        raise SkillPackageError(
            f"Skill '{package_name}' SKILL.md must contain exactly one H1 heading."
        )

    headings = [line[3:].strip() for line in lines if line.startswith("## ")]
    for required in _REQUIRED_HEADINGS:
        count = headings.count(required)
        if count == 0:
            raise SkillPackageError(
                f"Skill '{package_name}' SKILL.md is missing required heading "
                f"'## {required}'."
            )
        if count > 1:
            raise SkillPackageError(
                f"Skill '{package_name}' SKILL.md has a duplicate heading "
                f"'## {required}'."
            )
    return body


def _contains_ref(node: Any) -> bool:
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        return any(_contains_ref(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_ref(item) for item in node)
    return False


def _load_schema(
    path: Path, package_name: str, resolved_root: Path, max_schema_bytes: int
) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > max_schema_bytes:
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json exceeds {max_schema_bytes} bytes."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json is not valid UTF-8."
        ) from error
    try:
        schema = json.loads(text)
    except json.JSONDecodeError as error:
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json is not valid JSON: {error}."
        ) from error

    if not isinstance(schema, dict):
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json must be a JSON object."
        )
    if schema.get("type") != "object":
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json top-level 'type' must be 'object'."
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json must define a 'properties' object."
        )
    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise SkillPackageError(
                f"Skill '{package_name}' input.schema.json 'required' must be a "
                "list of strings."
            )
    if _contains_ref(schema):
        raise SkillPackageError(
            f"Skill '{package_name}' input.schema.json uses '$ref', which is not "
            "supported (no external reference resolution)."
        )
    return schema
