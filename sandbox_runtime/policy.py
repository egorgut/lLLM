"""The host-owned sandbox policy (SPEC-015 "Sandbox policy", §15-18).

Everything a job cannot choose lives here: image identity, resource ceilings,
filesystem layout, the fixed interpreter command, and the fixed container
environment. :class:`SandboxJob` carries no override field for any of it, and
this module never reads the job.

Validation follows the project's existing convention for host-owned limits
(``reliability.validate_reliability_config``,
``skill_runtime.config_validation``): an incoherent value is a deployment
defect, so it raises a plain ``ValueError`` before anything reaches Docker,
rather than degrading into a runtime failure later.
"""

from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from reliability import canonical_json, sha256_of
from sandbox_runtime.models import SandboxLanguage

# Container-side layout. Fixed constants rather than policy fields: the whole
# point is that neither the job nor a future tool adapter can relocate them.
SOURCE_MOUNT = "/sandbox/source"
INPUT_MOUNT = "/sandbox/input"
OUTPUT_MOUNT = "/sandbox/output"
TMP_MOUNT = "/tmp"

# The identity the untrusted command runs as (§12). Matches the account baked
# into sandbox/image/Dockerfile and the ownership of the output tmpfs.
JOB_UID = 65532
JOB_GID = 65532

# The trusted idle process that keeps the container — and therefore the output
# tmpfs — alive while the host runs and collects one job (§7).
HOLDER_COMMAND = ("/usr/local/bin/lllm-sandbox-hold",)

# The fixed source filename per language. The job never chooses it, so a script
# can neither guess a second source path nor overwrite the one being run.
SOURCE_FILENAMES: Mapping[SandboxLanguage, str] = MappingProxyType(
    {
        SandboxLanguage.PYTHON: "main.py",
        SandboxLanguage.BASH: "main.sh",
    }
)

# The complete argument vector per language (§17). Isolated Python mode, no
# bytecode writes, no user site-packages; bash with no profile and no rc file.
# Note there is no `-c`: model text is never a command argument, only a file
# the host wrote.
_COMMANDS: Mapping[SandboxLanguage, tuple[str, ...]] = MappingProxyType(
    {
        SandboxLanguage.PYTHON: (
            "python3",
            "-I",
            "-B",
            f"{SOURCE_MOUNT}/{SOURCE_FILENAMES[SandboxLanguage.PYTHON]}",
        ),
        SandboxLanguage.BASH: (
            "/bin/bash",
            "--noprofile",
            "--norc",
            f"{SOURCE_MOUNT}/{SOURCE_FILENAMES[SandboxLanguage.BASH]}",
        ),
    }
)

# The complete environment the job receives (§18). Built from scratch — never
# derived from os.environ or .env — and deliberately free of anything that
# selects code to load or a network route to take.
DEFAULT_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "HOME": OUTPUT_MOUNT,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
)

# Environment names that must never appear in the fixed set, even if a future
# edit adds them by accident: each one either loads code, redirects the network,
# or carries a credential.
_FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "IFS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "DOCKER_HOST",
        "OLLAMA_HOST",
        "TRACKER_TOKEN",
    }
)

_MIN_MEMORY_BYTES = 64 * 1024 * 1024
_MIN_PIDS_LIMIT = 8
_MIN_NOFILE_LIMIT = 16
_MIN_ARTIFACT_PATH_CHARS = 64


@dataclass(frozen=True)
class SandboxPolicy:
    """The complete set of host-owned rules applied to every job.

    ``temp_root`` is the one host path here. It is excluded from
    :func:`policy_fingerprint` because fingerprints are written to traces, and
    traces must not record host filesystem paths.
    """

    image_ref: str
    temp_root: Path
    execution_timeout_seconds: float
    docker_control_timeout_seconds: float
    cleanup_timeout_seconds: float
    memory_bytes: int
    cpus: float
    pids_limit: int
    nofile_limit: int
    tmp_bytes: int
    output_tmpfs_bytes: int
    max_source_bytes: int
    max_input_files: int
    max_input_file_bytes: int
    max_input_total_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_artifact_files: int
    max_artifact_file_bytes: int
    max_artifact_total_bytes: int
    max_artifact_path_chars: int
    environment: Mapping[str, str] = DEFAULT_ENVIRONMENT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )


def command_for(language: SandboxLanguage) -> tuple[str, ...]:
    """The fixed interpreter argument vector for ``language``."""

    try:
        return _COMMANDS[language]
    except KeyError:
        raise ValueError(f"Unsupported sandbox language: {language!r}.") from None


def source_filename_for(language: SandboxLanguage) -> str:
    """The fixed source filename the host writes for ``language``."""

    try:
        return SOURCE_FILENAMES[language]
    except KeyError:
        raise ValueError(f"Unsupported sandbox language: {language!r}.") from None


def policy_fingerprint(policy: SandboxPolicy) -> str:
    """A stable identity for the effective policy, for trace correlation.

    Two runs with the same fingerprint applied the same limits, so a trace can
    be read months later without guessing which constants were in force. Host
    paths are excluded (see :class:`SandboxPolicy`).
    """

    payload = {
        field.name: getattr(policy, field.name)
        for field in fields(policy)
        if field.name not in ("temp_root", "environment")
    }
    payload["environment"] = dict(policy.environment)
    return "sha256:" + sha256_of(canonical_json(payload))


def default_policy() -> SandboxPolicy:
    """Build the policy from the host-owned constants in ``config.py``."""

    import config

    return SandboxPolicy(
        image_ref=config.SANDBOX_IMAGE_REF,
        temp_root=config.SANDBOX_TEMP_ROOT,
        execution_timeout_seconds=config.SANDBOX_EXECUTION_TIMEOUT_SECONDS,
        docker_control_timeout_seconds=config.SANDBOX_DOCKER_CONTROL_TIMEOUT_SECONDS,
        cleanup_timeout_seconds=config.SANDBOX_CLEANUP_TIMEOUT_SECONDS,
        memory_bytes=config.SANDBOX_MEMORY_BYTES,
        cpus=config.SANDBOX_CPUS,
        pids_limit=config.SANDBOX_PIDS_LIMIT,
        nofile_limit=config.SANDBOX_NOFILE_LIMIT,
        tmp_bytes=config.SANDBOX_TMP_BYTES,
        output_tmpfs_bytes=config.SANDBOX_OUTPUT_TMPFS_BYTES,
        max_source_bytes=config.SANDBOX_MAX_SOURCE_BYTES,
        max_input_files=config.SANDBOX_MAX_INPUT_FILES,
        max_input_file_bytes=config.SANDBOX_MAX_INPUT_FILE_BYTES,
        max_input_total_bytes=config.SANDBOX_MAX_INPUT_TOTAL_BYTES,
        max_stdout_bytes=config.SANDBOX_MAX_STDOUT_BYTES,
        max_stderr_bytes=config.SANDBOX_MAX_STDERR_BYTES,
        max_artifact_files=config.SANDBOX_MAX_ARTIFACT_FILES,
        max_artifact_file_bytes=config.SANDBOX_MAX_ARTIFACT_FILE_BYTES,
        max_artifact_total_bytes=config.SANDBOX_MAX_ARTIFACT_TOTAL_BYTES,
        max_artifact_path_chars=config.SANDBOX_MAX_ARTIFACT_PATH_CHARS,
    )


def validate_sandbox_policy(
    policy: SandboxPolicy, *, tool_execution_timeout_seconds: float
) -> None:
    """Reject an internally incoherent host policy before any Docker call.

    ``tool_execution_timeout_seconds`` is the existing outer tool budget
    (``config.TOOL_EXECUTION_TIMEOUT_SECONDS``). SPEC-016 will drive this
    runtime from one ordinary tool execution, so the sandbox must be able to
    time out *and* clean up its container strictly inside that budget (§16) —
    otherwise the caller-side deadline fires first and abandons a live container.
    """

    if not policy.image_ref.strip():
        raise ValueError("image_ref must not be empty.")
    if policy.image_ref.endswith(":latest") or ":" not in policy.image_ref:
        raise ValueError(
            "image_ref must name an explicit non-'latest' tag, got "
            f"{policy.image_ref!r}."
        )

    for name, value in (
        ("execution_timeout_seconds", policy.execution_timeout_seconds),
        ("docker_control_timeout_seconds", policy.docker_control_timeout_seconds),
        ("cleanup_timeout_seconds", policy.cleanup_timeout_seconds),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}.")

    budget = policy.execution_timeout_seconds + policy.cleanup_timeout_seconds
    if budget >= tool_execution_timeout_seconds:
        raise ValueError(
            "execution_timeout_seconds + cleanup_timeout_seconds "
            f"({budget}) must be below tool_execution_timeout_seconds "
            f"({tool_execution_timeout_seconds})."
        )

    if policy.memory_bytes < _MIN_MEMORY_BYTES:
        raise ValueError(
            f"memory_bytes must be at least {_MIN_MEMORY_BYTES}, got "
            f"{policy.memory_bytes}."
        )
    if policy.cpus <= 0:
        raise ValueError(f"cpus must be > 0, got {policy.cpus}.")
    if policy.pids_limit < _MIN_PIDS_LIMIT:
        raise ValueError(
            f"pids_limit must be at least {_MIN_PIDS_LIMIT}, got {policy.pids_limit}."
        )
    if policy.nofile_limit < _MIN_NOFILE_LIMIT:
        raise ValueError(
            f"nofile_limit must be at least {_MIN_NOFILE_LIMIT}, got "
            f"{policy.nofile_limit}."
        )

    for name, value in (
        ("tmp_bytes", policy.tmp_bytes),
        ("output_tmpfs_bytes", policy.output_tmpfs_bytes),
        ("max_source_bytes", policy.max_source_bytes),
        ("max_input_files", policy.max_input_files),
        ("max_input_file_bytes", policy.max_input_file_bytes),
        ("max_input_total_bytes", policy.max_input_total_bytes),
        ("max_stdout_bytes", policy.max_stdout_bytes),
        ("max_stderr_bytes", policy.max_stderr_bytes),
        ("max_artifact_files", policy.max_artifact_files),
        ("max_artifact_file_bytes", policy.max_artifact_file_bytes),
        ("max_artifact_total_bytes", policy.max_artifact_total_bytes),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}.")

    if policy.max_input_file_bytes > policy.max_input_total_bytes:
        raise ValueError(
            "max_input_file_bytes must not exceed max_input_total_bytes "
            f"({policy.max_input_file_bytes} > {policy.max_input_total_bytes})."
        )
    if policy.max_artifact_file_bytes > policy.max_artifact_total_bytes:
        raise ValueError(
            "max_artifact_file_bytes must not exceed max_artifact_total_bytes "
            f"({policy.max_artifact_file_bytes} > {policy.max_artifact_total_bytes})."
        )
    # A total artifact budget above the writable tmpfs is unreachable by
    # construction: the container physically cannot produce that much output.
    # Allowing it would advertise a bound the runtime never actually enforces.
    if policy.max_artifact_total_bytes > policy.output_tmpfs_bytes:
        raise ValueError(
            "max_artifact_total_bytes must not exceed output_tmpfs_bytes "
            f"({policy.max_artifact_total_bytes} > {policy.output_tmpfs_bytes})."
        )
    if policy.max_artifact_path_chars < _MIN_ARTIFACT_PATH_CHARS:
        raise ValueError(
            "max_artifact_path_chars must be at least "
            f"{_MIN_ARTIFACT_PATH_CHARS}, got {policy.max_artifact_path_chars}."
        )

    for language in SandboxLanguage:
        command_for(language)
        source_filename_for(language)

    _validate_environment(policy.environment)


def _validate_environment(environment: Mapping[str, str]) -> None:
    """Reject any fixed environment entry that could load code or leak a secret."""

    for key, value in environment.items():
        if not key or not all(char.isascii() for char in key):
            raise ValueError(f"Unsafe sandbox environment key: {key!r}.")
        if not (key[0].isalpha() or key[0] == "_") or not all(
            char.isalnum() or char == "_" for char in key
        ):
            raise ValueError(f"Unsafe sandbox environment key: {key!r}.")
        if key in _FORBIDDEN_ENVIRONMENT_KEYS:
            raise ValueError(f"Forbidden sandbox environment key: {key!r}.")
        if not value.isascii() or any(char in value for char in "\x00\n\r"):
            raise ValueError(f"Unsafe sandbox environment value for {key!r}.")
