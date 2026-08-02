"""The injectable boundary over the trusted local Docker CLI (SPEC-015 §3, §21).

Two things live here and nothing else: how a Docker command is invoked, and how
its output is bounded. No sandbox policy, no container lifecycle, no knowledge of
jobs — that belongs to ``docker_backend``. The split exists so the whole runtime
can be unit-tested against a fake runner, with no Docker daemon anywhere.

Every invocation is an argument *vector* with ``shell=False``. There is no code
path in this project that builds a Docker command as a shell string, so no part
of a job — a filename, a container id, a path — can ever be reinterpreted as
shell syntax.

One distinction worth stating plainly, because it looks like a contradiction of
§18: the ``docker`` CLI process on the *host* inherits the host environment,
because that is how it finds the daemon socket and the active context. The
*container* environment is unrelated to it — it is exactly the ``--env`` flags
the backend passes, built from scratch. Nothing here forwards host environment
into a container.
"""

import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

# Read granularity for the streamed pipes. Small enough that a flooding job is
# stopped promptly after crossing its limit, large enough to avoid syscall churn.
_CHUNK_BYTES = 8192


@dataclass(frozen=True)
class CompletedCommand:
    """The result of a short control command (``image inspect``, ``kill``, ...)."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class StreamedCommand:
    """The result of the one long-running command: the untrusted job's exec.

    ``exit_code`` is ``None`` whenever the host aborted the command — on a
    deadline or an output limit — because the exit status of a process we killed
    says nothing about what the script would have done.

    ``stdout_bytes``/``stderr_bytes`` count everything *seen*, which can exceed
    what was retained; the truncation flags say which of the two happened.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    output_limit_exceeded: bool


@dataclass(frozen=True)
class CollectedBytes:
    """The result of a command whose stdout is binary and bounded (the tar stream)."""

    exit_code: int | None
    stdout: bytes
    stderr: str
    truncated: bool
    timed_out: bool


class CommandRunner(Protocol):
    """The three shapes of Docker invocation this runtime needs."""

    def run(self, argv: Sequence[str], *, timeout: float) -> CompletedCommand: ...

    def stream(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        on_stop: Callable[[str], None],
    ) -> StreamedCommand: ...

    def collect(
        self, argv: Sequence[str], *, timeout: float, max_bytes: int
    ) -> CollectedBytes: ...


class DockerCliMissing(Exception):
    """The ``docker`` executable is not on the host PATH."""


class SubprocessCommandRunner:
    """Runs Docker through ``subprocess``, the only such place in the project."""

    def run(self, argv: Sequence[str], *, timeout: float) -> CompletedCommand:
        try:
            completed = subprocess.run(
                list(argv),
                shell=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise DockerCliMissing("The docker executable was not found.") from None
        except subprocess.TimeoutExpired:
            return CompletedCommand(exit_code=124, stdout="", stderr="", timed_out=True)
        return CompletedCommand(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def stream(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        on_stop: Callable[[str], None],
    ) -> StreamedCommand:
        """Run one command, draining both pipes concurrently under byte caps.

        Both pipes get their own reader thread, so neither can fill its buffer
        and deadlock the other — the classic failure of reading stdout to
        completion before touching stderr.

        Crossing a cap does not merely truncate: ``on_stop`` fires immediately,
        and the backend's callback kills the whole container. A job that would
        print forever is therefore stopped at its source rather than being
        drained politely into a discard.
        """

        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise DockerCliMissing("The docker executable was not found.") from None

        stop_signalled = threading.Event()

        def _signal_stop(reason: str) -> None:
            # Whichever condition trips first owns the outcome: a job that floods
            # stdout right as its deadline expires must not be reported twice.
            if not stop_signalled.is_set():
                stop_signalled.set()
                on_stop(reason)

        stdout_reader = _BoundedReader(process.stdout, max_stdout_bytes)
        stderr_reader = _BoundedReader(process.stderr, max_stderr_bytes)
        threads = [
            threading.Thread(
                target=reader.drain,
                args=(lambda: _signal_stop("output_limit"),),
                name=name,
                daemon=True,
            )
            for reader, name in (
                (stdout_reader, "sandbox-stdout"),
                (stderr_reader, "sandbox-stderr"),
            )
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_stop("timeout")
            # The callback kills the container, which makes this exec exit on its
            # own; terminating the client is only a backstop for the case where
            # the kill itself did not take effect.
            try:
                process.wait(timeout=_grace_seconds(timeout))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        for thread in threads:
            thread.join(timeout=1.0)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

        limit_exceeded = stdout_reader.truncated or stderr_reader.truncated
        aborted = timed_out or limit_exceeded
        return StreamedCommand(
            exit_code=None if aborted else process.returncode,
            stdout=stdout_reader.text(),
            stderr=stderr_reader.text(),
            stdout_bytes=stdout_reader.total_bytes,
            stderr_bytes=stderr_reader.total_bytes,
            stdout_truncated=stdout_reader.truncated,
            stderr_truncated=stderr_reader.truncated,
            timed_out=timed_out,
            output_limit_exceeded=limit_exceeded,
        )

    def collect(
        self, argv: Sequence[str], *, timeout: float, max_bytes: int
    ) -> CollectedBytes:
        """Read a bounded binary stdout (the output tar) without decoding it."""

        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise DockerCliMissing("The docker executable was not found.") from None

        # One byte over the cap is enough to know the cap was exceeded; reading
        # further would defeat the point of having one.
        stdout_reader = _BoundedReader(process.stdout, max_bytes + 1)
        stderr_reader = _BoundedReader(process.stderr, 4096)
        threads = [
            threading.Thread(target=reader.drain, args=(None,), daemon=True)
            for reader in (stdout_reader, stderr_reader)
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()

        for thread in threads:
            thread.join(timeout=1.0)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

        payload = stdout_reader.data()
        return CollectedBytes(
            exit_code=None if timed_out else process.returncode,
            stdout=payload[:max_bytes],
            stderr=stderr_reader.text(),
            truncated=len(payload) > max_bytes,
            timed_out=timed_out,
        )


def _grace_seconds(timeout: float) -> float:
    """A short window for an exec to notice its container was killed."""

    return min(5.0, max(1.0, timeout / 4))


class _BoundedReader:
    """Drains one pipe, counting every byte but retaining at most ``limit``."""

    def __init__(self, pipe, limit: int) -> None:
        self._pipe = pipe
        self._limit = limit
        self._chunks: list[bytes] = []
        self._retained = 0
        self.total_bytes = 0
        self.truncated = False

    def drain(self, on_limit: Callable[[], None] | None) -> None:
        if self._pipe is None:
            return
        try:
            while True:
                chunk = self._pipe.read1(_CHUNK_BYTES)
                if not chunk:
                    return
                self.total_bytes += len(chunk)
                if self._retained < self._limit:
                    room = self._limit - self._retained
                    self._chunks.append(chunk[:room])
                    self._retained += min(room, len(chunk))
                if self.total_bytes > self._limit and not self.truncated:
                    self.truncated = True
                    if on_limit is not None:
                        on_limit()
        except (ValueError, OSError):
            # The pipe was closed underneath us because the process was killed.
            return

    def data(self) -> bytes:
        return b"".join(self._chunks)

    def text(self) -> str:
        """Bounded text, with deterministic replacement for invalid bytes.

        Decoding happens once, at the end, over the retained bytes: a chunk
        boundary can split a multi-byte character, so decoding per chunk would
        corrupt otherwise valid output.
        """

        return self.data().decode("utf-8", errors="replace")
