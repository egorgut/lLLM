"""Input validation and safe host materialisation (SPEC-015 §19-20, §27).

This module owns everything the host writes *before* a container exists: the
per-job scratch directory, the fixed source file, and the validated input tree.
It performs no Docker call and knows nothing about containers.

The rule that shapes it: a caller supplies bytes and relative names, and the
host decides where those land. Nothing supplied by a caller is ever used as an
absolute path, a directory to escape into, a symlink, or a name that could
displace the source file the runtime is about to execute.
"""

import os
import shutil
from pathlib import Path
from typing import Mapping

from sandbox_runtime.models import SandboxJob, SandboxLanguage
from sandbox_runtime.policy import SandboxPolicy, source_filename_for

# Host-side modes. The job directory is private to this user; the mounted
# subtrees must stay readable by the container's fixed non-root account, which
# does not share the host uid.
_JOB_DIR_MODE = 0o700
_MOUNT_DIR_MODE = 0o755
_MOUNT_FILE_MODE = 0o644


class JobRejected(Exception):
    """A job that must not reach Docker at all.

    Carries a stable ``error_type`` from the SPEC-015 error taxonomy so the
    backend can build a ``REJECTED`` result without inspecting the message.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def new_job_dir(temp_root: Path, job_id: str) -> Path:
    """Create the private scratch directory for one job.

    The name comes only from the host-generated ``job_id`` — never from source,
    a filename, or any other caller text (§27-28).
    """

    job_dir = Path(temp_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(job_dir, _JOB_DIR_MODE)
    return job_dir


def remove_job_dir(job_dir: Path) -> None:
    """Delete a job's scratch tree. Tolerates an already-absent directory."""

    shutil.rmtree(job_dir, ignore_errors=True)


def validate_input_path(raw: str, *, max_chars: int) -> str:
    """Validate one input path against the strict relative POSIX subset (§19).

    Rejects — rather than sanitises — anything outside the subset. Silently
    rewriting a suspicious path would make the mount contents differ from what
    the caller believes it supplied, which is worse than a clear rejection.
    """

    if not isinstance(raw, str) or not raw:
        raise JobRejected("input_path_invalid", "An input path must be a non-empty string.")
    if len(raw) > max_chars:
        raise JobRejected(
            "input_path_invalid", f"An input path exceeds {max_chars} characters."
        )
    if "\x00" in raw:
        raise JobRejected("input_path_invalid", "An input path contains a NUL byte.")
    if "\\" in raw:
        raise JobRejected("input_path_invalid", "An input path contains a backslash.")
    if raw.startswith("/"):
        raise JobRejected("input_path_invalid", "An input path must be relative.")

    components = raw.split("/")
    for component in components:
        if component == "":
            raise JobRejected("input_path_invalid", "An input path has an empty component.")
        if component in (".", ".."):
            raise JobRejected(
                "input_path_invalid", "An input path contains a '.' or '..' component."
            )

    # An input must never occupy the fixed source filename's slot. The mounts are
    # separate directories today, so this cannot actually collide — but the check
    # keeps the guarantee true if a future step ever merges them.
    if components[-1] in {source_filename_for(lang) for lang in SandboxLanguage}:
        raise JobRejected(
            "input_path_invalid",
            f"An input path may not use the reserved source filename: {components[-1]}.",
        )
    return "/".join(components)


def validate_input_files(
    job: SandboxJob, policy: SandboxPolicy
) -> Mapping[str, bytes]:
    """Validate every input path and size; return the normalized mapping."""

    if len(job.input_files) > policy.max_input_files:
        raise JobRejected(
            "input_file_limit",
            f"A job may carry at most {policy.max_input_files} input files.",
        )

    validated: dict[str, bytes] = {}
    seen_casefolded: dict[str, str] = {}
    total_bytes = 0

    for raw_path, content in job.input_files.items():
        path = validate_input_path(
            raw_path, max_chars=policy.max_artifact_path_chars
        )
        # Case-insensitive collision detection: the host filesystem here is
        # macOS APFS, which is case-insensitive by default, so two paths that
        # differ only in case would silently overwrite one another instead of
        # producing the two files the caller asked for.
        previous = seen_casefolded.get(path.casefold())
        if previous is not None:
            raise JobRejected(
                "input_path_invalid",
                f"Two input paths resolve to the same file: {previous!r} and {path!r}.",
            )
        seen_casefolded[path.casefold()] = path

        if not isinstance(content, (bytes, bytearray)):
            raise JobRejected(
                "invalid_job", f"Input file {path!r} must be supplied as bytes."
            )
        if len(content) > policy.max_input_file_bytes:
            raise JobRejected(
                "input_size_limit",
                f"Input file {path!r} exceeds {policy.max_input_file_bytes} bytes.",
            )
        total_bytes += len(content)
        if total_bytes > policy.max_input_total_bytes:
            raise JobRejected(
                "input_size_limit",
                f"Input files exceed {policy.max_input_total_bytes} bytes in total.",
            )
        validated[path] = bytes(content)

    return validated


def encode_source(job: SandboxJob, policy: SandboxPolicy) -> bytes:
    """Encode and bound the job source (§20)."""

    try:
        SandboxLanguage(job.language)
    except ValueError:
        raise JobRejected(
            "unsupported_language", f"Unsupported sandbox language: {job.language!r}."
        ) from None
    if not isinstance(job.source, str) or not job.source.strip():
        raise JobRejected("invalid_job", "Job source must be non-empty text.")
    try:
        encoded = job.source.encode("utf-8")
    except UnicodeEncodeError:
        raise JobRejected("invalid_job", "Job source must be encodable as UTF-8.") from None
    if len(encoded) > policy.max_source_bytes:
        raise JobRejected(
            "source_too_large",
            f"Job source exceeds {policy.max_source_bytes} bytes.",
        )
    return encoded


class MaterialisedJob:
    """The host directories prepared for one job's read-only mounts."""

    def __init__(self, source_dir: Path, input_dir: Path, source_bytes: bytes) -> None:
        self.source_dir = source_dir
        self.input_dir = input_dir
        self.source_bytes = source_bytes
        self.input_file_count = 0
        self.input_total_bytes = 0


def materialise_job(job: SandboxJob, policy: SandboxPolicy, job_dir: Path) -> MaterialisedJob:
    """Validate the job and write its source and inputs under ``job_dir``.

    Validation happens in full *before* the first byte is written, so a rejected
    job never leaves a partial tree behind. Only regular files are created:
    nothing a caller supplies can produce a symlink, device node, FIFO, socket,
    or executable bit.
    """

    source_bytes = encode_source(job, policy)
    input_files = validate_input_files(job, policy)

    source_dir = job_dir / "source"
    input_dir = job_dir / "input"
    for directory in (source_dir, input_dir):
        directory.mkdir(parents=True, exist_ok=False)
        os.chmod(directory, _MOUNT_DIR_MODE)

    source_path = source_dir / source_filename_for(job.language)
    _write_regular_file(source_path, source_bytes)

    materialised = MaterialisedJob(source_dir, input_dir, source_bytes)
    for path, content in input_files.items():
        target = input_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in _parents_below(target.parent, input_dir):
            os.chmod(parent, _MOUNT_DIR_MODE)
        _write_regular_file(target, content)
        materialised.input_file_count += 1
        materialised.input_total_bytes += len(content)

    return materialised


def _parents_below(directory: Path, root: Path) -> list[Path]:
    """Every directory from ``root`` (exclusive) down to ``directory``."""

    parents = []
    current = directory
    while current != root:
        parents.append(current)
        current = current.parent
    return parents


def _write_regular_file(path: Path, content: bytes) -> None:
    """Create one regular file with fixed non-executable permissions.

    ``O_CREAT | O_EXCL`` refuses to follow an existing path of any kind, so this
    can never write through a symlink that appeared in the scratch tree.
    """

    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _MOUNT_FILE_MODE
    )
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    os.chmod(path, _MOUNT_FILE_MODE)
