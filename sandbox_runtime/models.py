"""Immutable sandbox contracts (SPEC-015 "Data model").

Frozen dataclasses with no behavior beyond their own invariants: no Docker
calls, no filesystem I/O, no configuration lookups. Mirrors
``skill_runtime/models.py`` — a caller-supplied mapping is defensively copied
behind a read-only view so a holder cannot mutate a registered contract after
validation.

The asymmetry to keep in mind while reading: a :class:`SandboxJob` carries only
*what* to run. Every *how* — image, timeout, memory, mounts, environment,
command, job id — lives in :class:`~sandbox_runtime.policy.SandboxPolicy` and is
owned by the host. There is deliberately no field here a future model-facing
adapter could use to widen its own sandbox.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol


class SandboxLanguage(StrEnum):
    """The only two interpreters this iteration supports."""

    PYTHON = "python"
    BASH = "bash"


class SandboxStatus(StrEnum):
    """The outcome of one job.

    ``COMPLETED`` means the script exited zero *and* the runtime completed its
    own contract around it. ``STOPPED`` is a host-enforced halt (output cap,
    artifact policy, unconfirmed cleanup) as opposed to ``FAILED``, which is the
    script's own non-zero exit.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SandboxJob:
    """One immutable request to execute one script with optional input files.

    ``input_files`` maps a strict relative POSIX path (validated later, by
    ``sandbox_runtime.paths``) to raw bytes. It is defensively copied into a
    read-only mapping at construction, so a caller that keeps and later mutates
    the dict it passed in cannot change what this job means.
    """

    language: SandboxLanguage
    source: str
    input_files: Mapping[str, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "input_files", MappingProxyType(dict(self.input_files))
        )


@dataclass(frozen=True)
class SandboxArtifact:
    """One bounded regular file produced under ``/sandbox/output``.

    ``path`` is relative to that directory and never carries a host path or a
    container id. ``content`` is excluded from ``repr`` (§26) so printing a
    result — in a REPL, a log line, an exception context — can never dump the
    bytes of a generated file.
    """

    path: str
    size_bytes: int
    sha256: str
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.size_bytes != len(self.content):
            raise ValueError("SandboxArtifact.size_bytes must equal len(content).")


@dataclass(frozen=True)
class SandboxResult:
    """The single authoritative outcome of one job.

    Every started job produces exactly one of these (or, for a host defect, an
    exception from ``sandbox_runtime.errors``). The invariants below are checked
    here rather than trusted at each construction site, so a backend bug shows
    up as an immediate error instead of a plausible-looking wrong result — in
    particular, artifacts can never accompany a non-completed job.
    """

    job_id: str
    status: SandboxStatus
    # Normally a SandboxLanguage. A job that named an unsupported language is
    # rejected before anything runs, and its result echoes back the raw string
    # it asked for rather than inventing a language it never requested.
    language: SandboxLanguage | str
    image_id: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: tuple[SandboxArtifact, ...]
    duration_ms: int
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.status is SandboxStatus.COMPLETED:
            if self.exit_code != 0:
                raise ValueError("A completed job must have exit_code == 0.")
            if self.error_type is not None:
                raise ValueError("A completed job must have no error_type.")
        else:
            if self.artifacts:
                raise ValueError("Only a completed job may carry artifacts.")
        if self.status is SandboxStatus.FAILED and not self.exit_code:
            raise ValueError("A failed job must have a non-zero exit_code.")
        if self.status is SandboxStatus.TIMED_OUT and self.exit_code is not None:
            raise ValueError("A timed-out job must have exit_code None.")


class SandboxRuntime(Protocol):
    """The seam SPEC-016 will call.

    ``turn_id`` is optional and unused by SPEC-015's host-only callers; it
    exists now so the future tool adapter can correlate a sandbox job with the
    agent turn that requested it without any signature change.
    """

    def execute(
        self, job: SandboxJob, *, turn_id: str | None = None
    ) -> SandboxResult: ...
