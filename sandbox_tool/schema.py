"""The model-facing contract of `sandbox_execute` (SPEC-016 §7).

Two things live here: the `ToolSpec` the model sees, and the validation that
turns model-supplied arguments into the three values a `SandboxJob` accepts.

The shape of the schema is the security boundary. `additionalProperties: false`
plus exactly three properties means there is no argument through which a model
could name an image, a mount, a host path, an environment variable, a network
mode, a resource ceiling, or a timeout. Those all stay in the host-owned
`SandboxPolicy` that SPEC-015 owns, and nothing here can widen them.

Validation is deliberately thin. It checks *shape* — types, encodings, duplicate
names — and delegates every *limit* (source size, input count, input bytes) and
the entire relative-path policy to SPEC-015, which enforces them for real before
a container exists. A second copy of those rules in this layer could only ever
drift wider than the runtime's, which is the one direction that matters.
"""

import base64
import binascii
from typing import Any

from sandbox_runtime.models import SandboxLanguage
from sandbox_runtime.paths import JobRejected, validate_input_path
from sandbox_runtime.policy import SandboxPolicy
from tools.registry import ToolSpec

# The model reads this back as `stderr` when a call is rejected before it runs,
# so it stays short enough to be useful and bounded even when it quotes a name
# the model supplied.
MAX_EXPLANATION_CHARS = 500


class InvalidSandboxRequest(Exception):
    """Arguments that must not reach the runtime at all.

    Carries only text safe to show the model: what was wrong with the call and
    how to fix it, never a host path or an internal detail.
    """


SANDBOX_EXECUTE_SPEC = ToolSpec(
    name="sandbox_execute",
    description=(
        "Run one complete Python or Bash script in an isolated sandbox with no "
        "network and no host filesystem access. Read optional supplied input "
        "files from /sandbox/input and write user-facing result files to "
        "/sandbox/output. Returns status, exit code, bounded stdout and stderr, "
        "and metadata for any files the script produced."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["python", "bash"],
                "description": "The interpreter to run the source with.",
            },
            "source": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The complete script to run. Only the standard library and "
                    "commands already present in the image are available: there "
                    "is no network and no package installation. On a retry, "
                    "supply the full corrected script, not a patch."
                ),
            },
            "input_files": {
                "type": "array",
                "description": (
                    "Optional files to place under /sandbox/input before the "
                    "script runs. Omit when the script needs no input."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Relative path under /sandbox/input, e.g. "
                                "'sales.csv'. Absolute paths and '..' are rejected."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "The file content, in the given encoding.",
                        },
                        "encoding": {
                            "type": "string",
                            "enum": ["utf-8", "base64"],
                            "description": (
                                "How 'content' is encoded. Defaults to utf-8; use "
                                "base64 only for binary input."
                            ),
                        },
                    },
                    "required": ["name", "content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["language", "source"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "status": {"type": "string"},
            "exit_code": {"type": ["integer", "null"]},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "artifacts": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["ok", "status", "exit_code", "stdout", "stderr", "artifacts"],
        "additionalProperties": False,
    },
)


def validate_arguments(
    arguments: Any, *, policy: SandboxPolicy
) -> tuple[SandboxLanguage, str, dict[str, bytes]]:
    """Return `(language, source, input_files)` or raise `InvalidSandboxRequest`.

    The returned mapping is exactly what `SandboxJob.input_files` expects:
    validated relative POSIX paths to raw bytes.
    """

    if not isinstance(arguments, dict):
        raise InvalidSandboxRequest("Arguments must be an object.")

    unknown = set(arguments) - {"language", "source", "input_files"}
    if unknown:
        raise InvalidSandboxRequest(
            "Unsupported argument(s): "
            f"{', '.join(sorted(unknown))}. Only 'language', 'source', and "
            "'input_files' are accepted."
        )
    missing = {"language", "source"} - set(arguments)
    if missing:
        raise InvalidSandboxRequest(
            f"Missing required argument(s): {', '.join(sorted(missing))}."
        )

    language = _validate_language(arguments["language"])
    source = _validate_source(arguments["source"])
    input_files = _validate_input_files(arguments.get("input_files"), policy=policy)
    return language, source, input_files


def _validate_language(raw: Any) -> SandboxLanguage:
    if not isinstance(raw, str):
        raise InvalidSandboxRequest("'language' must be a string.")
    try:
        return SandboxLanguage(raw)
    except ValueError:
        raise InvalidSandboxRequest(
            f"Unsupported language {raw!r}. Use 'python' or 'bash'."
        ) from None


def _validate_source(raw: Any) -> str:
    if not isinstance(raw, str):
        raise InvalidSandboxRequest("'source' must be a string.")
    if "\x00" in raw:
        raise InvalidSandboxRequest("'source' must not contain NUL bytes.")
    if not raw.strip():
        raise InvalidSandboxRequest("'source' must not be empty.")
    return raw


def _validate_input_files(
    raw: Any, *, policy: SandboxPolicy
) -> dict[str, bytes]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise InvalidSandboxRequest("'input_files' must be an array.")

    files: dict[str, bytes] = {}
    seen: dict[str, str] = {}
    for index, entry in enumerate(raw):
        name, content = _validate_input_entry(entry, index=index, policy=policy)
        # SPEC-015 rejects two mapping keys that collide case-insensitively, but
        # a list can carry the same name twice, which would silently collapse
        # into one dict entry here — long before the runtime could see it.
        previous = seen.get(name.casefold())
        if previous is not None:
            raise InvalidSandboxRequest(
                f"Duplicate input file name: {_short(previous)!r}. Each input "
                "file needs a distinct name."
            )
        seen[name.casefold()] = name
        files[name] = content
    return files


def _validate_input_entry(
    entry: Any, *, index: int, policy: SandboxPolicy
) -> tuple[str, bytes]:
    where = f"input_files[{index}]"
    if not isinstance(entry, dict):
        raise InvalidSandboxRequest(f"{where} must be an object.")

    unknown = set(entry) - {"name", "content", "encoding"}
    if unknown:
        raise InvalidSandboxRequest(
            f"{where} has unsupported field(s): {', '.join(sorted(unknown))}."
        )
    missing = {"name", "content"} - set(entry)
    if missing:
        raise InvalidSandboxRequest(
            f"{where} is missing {', '.join(sorted(missing))}."
        )

    raw_name = entry["name"]
    if not isinstance(raw_name, str):
        raise InvalidSandboxRequest(f"{where}.name must be a string.")
    try:
        # One path policy, owned by SPEC-015: absolute paths, traversal, '.',
        # '..', backslashes, NUL bytes, empty components, over-long names, and
        # the reserved source filenames are all rejected here, before a job is
        # ever built (SPEC-016 §7.2, §10.2).
        name = validate_input_path(
            raw_name, max_chars=policy.max_artifact_path_chars
        )
    except JobRejected as rejection:
        raise InvalidSandboxRequest(f"{where}.name: {rejection}") from None

    raw_content = entry["content"]
    if not isinstance(raw_content, str):
        raise InvalidSandboxRequest(
            f"{where}.content must be a string (use encoding 'base64' for "
            "binary input)."
        )

    encoding = entry.get("encoding", "utf-8")
    if encoding not in ("utf-8", "base64"):
        raise InvalidSandboxRequest(
            f"{where}.encoding must be 'utf-8' or 'base64', got {encoding!r}."
        )

    if encoding == "utf-8":
        return name, raw_content.encode("utf-8")
    try:
        return name, base64.b64decode(raw_content, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidSandboxRequest(
            f"{where}.content is not valid base64."
        ) from None


def _short(text: str) -> str:
    """Bound a model-supplied value quoted back in an explanation."""

    limit = 80
    return text if len(text) <= limit else text[:limit] + "..."
