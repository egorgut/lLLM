"""Bounded, type-checked collection of a job's output (SPEC-015 §25-26).

``docker cp`` faithfully reproduces whatever the container's ``/sandbox/output``
contained — including symlinks, FIFOs, and device nodes if a job created them.
This module is the boundary that decides which of that is allowed to become
host data.

Two properties matter most: the walk never *follows* a link (every entry is
``lstat``-ed, and a symlink is rejected rather than resolved, so a link to
``/etc/passwd`` in the copied tree can never be read back as an artifact), and
every bound is checked before the bytes are read into memory.
"""

import hashlib
import os
import stat
from pathlib import Path

from sandbox_runtime.models import SandboxArtifact
from sandbox_runtime.policy import SandboxPolicy


class ArtifactPolicyViolation(Exception):
    """The copied output tree is not acceptable as host data.

    The backend maps this to ``status=stopped``,
    ``error_type="artifact_policy_violation"``, and no artifacts at all — a
    partial set is never returned, because a caller cannot tell a bounded
    subset from a complete result.
    """


def collect_artifacts(
    collection_dir: Path, policy: SandboxPolicy
) -> tuple[SandboxArtifact, ...]:
    """Walk the copied output tree and return bounded regular-file artifacts.

    Directories are traversed but are not artifacts themselves; an empty
    directory is simply ignored. The result is sorted by path so two identical
    jobs produce identical output regardless of filesystem iteration order.
    """

    root = Path(collection_dir)
    artifacts: list[SandboxArtifact] = []
    total_bytes = 0

    for absolute in _walk(root):
        relative = absolute.relative_to(root).as_posix()
        if len(relative) > policy.max_artifact_path_chars:
            raise ArtifactPolicyViolation(
                f"An output path exceeds {policy.max_artifact_path_chars} characters."
            )

        entry_stat = os.lstat(absolute)
        _reject_non_regular(relative, entry_stat)

        # A hard link to a file outside the copied tree would let one artifact
        # be counted once but reference content the bounds never measured.
        if entry_stat.st_nlink > 1:
            raise ArtifactPolicyViolation(
                f"Output file {relative!r} is a hard link, which is not collected."
            )

        if len(artifacts) >= policy.max_artifact_files:
            raise ArtifactPolicyViolation(
                f"A job may produce at most {policy.max_artifact_files} output files."
            )
        if entry_stat.st_size > policy.max_artifact_file_bytes:
            raise ArtifactPolicyViolation(
                f"Output file {relative!r} exceeds "
                f"{policy.max_artifact_file_bytes} bytes."
            )
        total_bytes += entry_stat.st_size
        if total_bytes > policy.max_artifact_total_bytes:
            raise ArtifactPolicyViolation(
                f"Output files exceed {policy.max_artifact_total_bytes} bytes in total."
            )

        content = _read_regular_file(absolute, entry_stat.st_size)
        artifacts.append(
            SandboxArtifact(
                path=relative,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        )

    artifacts.sort(key=lambda artifact: artifact.path)
    return tuple(artifacts)


def _walk(root: Path) -> list[Path]:
    """Every non-directory entry under ``root``, deterministically ordered.

    Uses ``scandir`` with ``follow_symlinks=False`` so a symlinked *directory*
    is returned as an entry to be rejected, never descended into.
    """

    entries: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as scan:
            for entry in sorted(scan, key=lambda item: item.name):
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                else:
                    entries.append(path)
    return entries


def _reject_non_regular(relative: str, entry_stat: os.stat_result) -> None:
    """Reject every file type that is not a plain regular file."""

    mode = entry_stat.st_mode
    if stat.S_ISLNK(mode):
        raise ArtifactPolicyViolation(
            f"Output entry {relative!r} is a symlink, which is not collected."
        )
    if stat.S_ISFIFO(mode):
        raise ArtifactPolicyViolation(f"Output entry {relative!r} is a FIFO.")
    if stat.S_ISSOCK(mode):
        raise ArtifactPolicyViolation(f"Output entry {relative!r} is a socket.")
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        raise ArtifactPolicyViolation(f"Output entry {relative!r} is a device node.")
    if not stat.S_ISREG(mode):
        raise ArtifactPolicyViolation(
            f"Output entry {relative!r} is not a regular file."
        )


def _read_regular_file(path: Path, expected_size: int) -> bytes:
    """Read one file without following a link, re-checking its type on the descriptor.

    ``O_NOFOLLOW`` closes the gap between the ``lstat`` above and this read: a
    path that became a symlink in between cannot be opened here.
    """

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ArtifactPolicyViolation("An output entry changed type while reading.")
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        # The copied tree is host-private, so a size that no longer matches the
        # lstat above means something is wrong with the runtime, not the job.
        if len(content) != expected_size:
            raise ArtifactPolicyViolation("An output file changed size while reading.")
    finally:
        os.close(descriptor)
    return content
