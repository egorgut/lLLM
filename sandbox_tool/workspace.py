"""The turn-scoped workspace (SPEC-016 §9).

One object owns everything about a sandbox turn that is not a single job: which
turn is running, how much of its deadline is left, which artifacts it has
staged, and whether those artifacts survive.

The temporary workspace is not here on purpose. SPEC-015 already creates
`data/sandbox/tmp/<job_id>/`, mounts it read-only, and removes it in a `finally`
whatever the job does — source, inputs, and collected output all reach this
layer in memory. What SPEC-016 adds around it is *ownership*: a turn identity to
bind jobs to, and a published artifact set whose fate follows the turn's.

Publication writes straight into the turn's final directory rather than staging
somewhere else and moving on commit. The model has to quote a usable path in the
same turn it creates the file, so the path it quotes must already be the real
one. "Staged" therefore means "written into a directory whose survival is still
undecided": a completed turn keeps it, and every other outcome removes the whole
directory (§9.5). Nothing else ever lives in that directory, so removing it
cannot take an unrelated file with it.
"""

import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from reliability import TurnContext
from sandbox_runtime.models import SandboxArtifact
from sandbox_tool.artifacts import (
    ArtifactPublicationError,
    media_type_for,
    unique_relative_path,
    write_artifact,
)
from tracing import NullTraceSink, SafeTraceSink, TraceSink, build_event

_TURN_DIR_MODE = 0o700


class TurnWorkspace:
    """Turn identity, remaining time, and artifact commit/rollback for one run."""

    def __init__(
        self,
        *,
        run_id: str,
        artifact_root: Path,
        project_root: Path,
        trace_sink: TraceSink = NullTraceSink(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run_id = run_id
        self._artifact_root = Path(artifact_root)
        self._project_root = Path(project_root)
        self._trace = SafeTraceSink(trace_sink, run_id)
        self._clock = clock
        # `publish` runs on the agent's tool worker thread while begin/commit/
        # rollback run on the chat loop's thread, so the turn state below is
        # guarded rather than assumed to be touched by one thread at a time.
        self._lock = threading.Lock()
        self._context: TurnContext | None = None
        self._generation = 0
        self._call_index = 0
        self._published: list[str] = []
        self._published_bytes = 0

    # -- turn lifecycle ------------------------------------------------------

    def begin_turn(self, context: TurnContext) -> None:
        """Adopt a new turn. Wired to the orchestrator's `on_turn_context` hook.

        A previous turn that was never committed is rolled back first. That
        cannot happen through the normal CLI path — the chat loop always commits
        or rolls back — but it keeps the invariant "at most one turn's artifacts
        are ever undecided" true for any other caller.
        """

        self.rollback()
        with self._lock:
            self._context = context
            self._generation += 1
            self._call_index = 0
            self._published = []
            self._published_bytes = 0

    def commit(self) -> None:
        """Keep this turn's artifacts. Called only for a completed turn."""

        with self._lock:
            context, published, total = self._context, self._published, self._published_bytes
            self._clear()
        if context is None:
            return
        if published:
            self._emit(
                "sandbox_artifacts_committed",
                context.turn_id,
                artifact_count=len(published),
                artifact_total_bytes=total,
            )

    def rollback(self) -> None:
        """Discard this turn's artifacts. Safe to call when no turn is active."""

        with self._lock:
            context, published = self._context, self._published
            turn_dir = self._turn_dir() if context is not None else None
            self._clear()
        if context is None:
            return
        if turn_dir is not None and turn_dir.exists():
            shutil.rmtree(turn_dir, ignore_errors=True)
            self._prune_run_dir()
        if published:
            self._emit(
                "sandbox_artifacts_rolled_back",
                context.turn_id,
                artifact_count=len(published),
            )

    # -- state a job needs ---------------------------------------------------

    @property
    def turn_id(self) -> str | None:
        return self._context.turn_id if self._context is not None else None

    @property
    def generation(self) -> int:
        """Monotonic turn counter, used to detect a result arriving too late."""

        return self._generation

    def next_call_index(self) -> int:
        """A 1-based counter of sandbox calls within the current turn."""

        with self._lock:
            self._call_index += 1
            return self._call_index

    def remaining_seconds(self) -> float | None:
        """Time left in the current turn, or None when no turn is active."""

        if self._context is None:
            return None
        return self._context.deadline - self._clock()

    # -- publication ---------------------------------------------------------

    def publish(
        self, artifacts: Sequence[SandboxArtifact], *, generation: int, job_id: str
    ) -> list[dict[str, Any]]:
        """Write a successful job's artifacts and return bounded metadata.

        Raises `ArtifactPublicationError` if anything cannot be written, after
        removing the partial files this call created — a turn never keeps half
        of a job's output.
        """

        with self._lock:
            if self._context is None or generation != self._generation:
                # The turn this job belonged to is already over: its outer
                # deadline fired and the caller abandoned the worker thread.
                # Whatever it produced belongs to nobody, so it is never written.
                raise ArtifactPublicationError(
                    "The turn ended before its artifacts could be published."
                )
            turn_id = self._context.turn_id
            turn_dir = self._turn_dir()
            taken = set(self._published)

        if not artifacts:
            return []

        entries: list[dict[str, Any]] = []
        written: list[Path] = []
        try:
            turn_dir.mkdir(parents=True, exist_ok=True, mode=_TURN_DIR_MODE)
            for artifact in artifacts:
                name = unique_relative_path(artifact.path, taken)
                written.append(write_artifact(turn_dir, name, artifact.content))
                taken.add(name)
                entries.append(
                    {
                        "name": name,
                        "media_type": media_type_for(name),
                        "size_bytes": artifact.size_bytes,
                        "path": self._user_path(name, turn_id),
                    }
                )
        except (ArtifactPublicationError, OSError) as error:
            for path in written:
                path.unlink(missing_ok=True)
            if isinstance(error, OSError):
                raise ArtifactPublicationError(
                    "The artifact directory could not be prepared."
                ) from error
            raise

        with self._lock:
            if generation != self._generation:
                for path in written:
                    path.unlink(missing_ok=True)
                raise ArtifactPublicationError(
                    "The turn ended before its artifacts could be published."
                )
            self._published.extend(entry["name"] for entry in entries)
            self._published_bytes += sum(entry["size_bytes"] for entry in entries)

        self._emit(
            "sandbox_artifacts_staged",
            turn_id,
            job_id=job_id,
            artifact_count=len(entries),
            artifact_total_bytes=sum(entry["size_bytes"] for entry in entries),
        )
        return entries

    # -- internals -----------------------------------------------------------

    def _turn_dir(self) -> Path:
        assert self._context is not None
        return self._artifact_root / self._run_id / self._context.turn_id

    def _user_path(self, name: str, turn_id: str) -> str:
        """The path the model may quote: repo-relative, never a host path."""

        try:
            root = self._artifact_root.relative_to(self._project_root).as_posix()
        except ValueError:
            root = self._artifact_root.name
        return f"{root}/{self._run_id}/{turn_id}/{name}"

    def _prune_run_dir(self) -> None:
        """Remove the per-run directory once its last turn directory is gone."""

        run_dir = self._artifact_root / self._run_id
        try:
            run_dir.rmdir()
        except OSError:
            pass  # not empty, or already gone — either way, nothing to do

    def _clear(self) -> None:
        self._context = None
        self._call_index = 0
        self._published = []
        self._published_bytes = 0

    def _emit(self, event: str, turn_id: str | None, **fields: Any) -> None:
        self._trace.emit(
            build_event(event, run_id=self._run_id, turn_id=turn_id, **fields)
        )
