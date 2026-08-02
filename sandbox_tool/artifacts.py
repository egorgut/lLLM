"""Artifact naming, typing, and safe host writes (SPEC-016 §9.4, §17).

Pure helpers with no turn state — `sandbox_tool.workspace` owns the lifecycle
and calls into these. Keeping them separate makes the two hard parts testable on
their own: deciding *where* an artifact lands, and writing it without trusting
anything about the name it arrived with.

SPEC-015 has already validated every artifact path (relative, bounded, no
symlink, no traversal) before it reaches this module. These checks are the
second half of that guarantee rather than a replacement for it: the write is
still `O_EXCL | O_NOFOLLOW` under a re-verified containment check, so a defect
anywhere upstream cannot turn into a write outside the turn directory.
"""

import os
from pathlib import Path

# Conservative, extension-only inference (§17). An artifact is untrusted output;
# nothing here opens or parses it to guess better.
_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_DEFAULT_MEDIA_TYPE = "application/octet-stream"

# Artifacts are the user's files, inside a directory private to this user.
_ARTIFACT_DIR_MODE = 0o700
_ARTIFACT_FILE_MODE = 0o600


class ArtifactPublicationError(Exception):
    """The host could not publish a successful job's artifacts.

    A job that ran perfectly but whose output could not be written is not a
    success: the tool reports a host failure rather than an empty artifact list
    that would read like "the script produced nothing".
    """


def media_type_for(relative_path: str) -> str:
    """The media type for one artifact, by extension only."""

    suffix = Path(relative_path).suffix.lower()
    return _MEDIA_TYPES.get(suffix, _DEFAULT_MEDIA_TYPE)


def unique_relative_path(relative_path: str, taken: set[str]) -> str:
    """Resolve a name collision deterministically (§9.4).

    The first artifact to claim a name keeps it; later collisions in the same
    turn get `-2`, `-3`, and so on, so two successful calls in one turn can both
    write `report.csv` without either silently overwriting the other.
    """

    if relative_path not in taken:
        return relative_path

    parent, _, name = relative_path.rpartition("/")
    prefix = f"{parent}/" if parent else ""
    # `Path.suffix` semantics: "report.csv" splits, "report" and ".gitignore"
    # do not — a leading dot is part of the name, not an extension.
    suffix = Path(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name

    counter = 2
    while True:
        candidate = f"{prefix}{stem}-{counter}{suffix}"
        if candidate not in taken:
            return candidate
        counter += 1


def write_artifact(directory: Path, relative_path: str, content: bytes) -> Path:
    """Write one artifact under `directory`, or raise `ArtifactPublicationError`.

    `relative_path` has already been validated by SPEC-015; the containment check
    below is defence in depth, not the primary control.
    """

    target = directory / relative_path
    root = os.path.normpath(str(directory))
    if os.path.commonpath([root, os.path.normpath(str(target))]) != root:
        raise ArtifactPublicationError(
            f"Artifact path escapes the turn directory: {relative_path!r}."
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=_ARTIFACT_DIR_MODE)
        # O_EXCL | O_NOFOLLOW refuses to follow anything already at this path,
        # so publication can never write through a symlink or clobber a file
        # from an earlier call that the collision policy meant to preserve.
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _ARTIFACT_FILE_MODE,
        )
        try:
            os.write(descriptor, content)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ArtifactPublicationError(
            f"Could not write artifact {relative_path!r}: {error.strerror}."
        ) from error
    return target
