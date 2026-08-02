"""A fake Docker CLI for deterministic sandbox-runtime tests (SPEC-015).

Real usage of the runtime is a fixed sequence of Docker commands:

    docker version                  -> daemon reachable?
    docker image inspect <tag>      -> immutable image ID
    docker run --detach ...         -> holder container ID
    docker exec ... <interpreter>   -> the untrusted job (streamed)
    docker exec ... tar -cf - .     -> bounded output (binary)
    docker kill / rm / ps           -> cleanup and confirmation

:class:`FakeCommandRunner` answers all of those from a script, records every
argument vector it was given, and lets a test make any step fail, hang, or flood
its output. No Docker daemon, no container, and no subprocess is involved, so
the entire lifecycle — including timeout kills and cleanup ordering — is
testable on a machine with Docker uninstalled.
"""

import io
import tarfile
from pathlib import Path
from typing import Callable, Sequence

from sandbox_runtime.command_runner import (
    CollectedBytes,
    CompletedCommand,
    DockerCliMissing,
    StreamedCommand,
)
from sandbox_runtime.policy import SandboxPolicy

FAKE_IMAGE_ID = "sha256:" + "a1" * 32
FAKE_CONTAINER_ID = "c0" * 32


def make_policy(temp_root: Path, **overrides) -> SandboxPolicy:
    """A valid policy pointing at a test-owned temporary root.

    Defaults mirror ``config.py`` closely enough that a test asserting on the
    generated Docker arguments is asserting on realistic values, while every
    field stays overridable for the bound-enforcement cases.
    """

    defaults = dict(
        image_ref="lllm-sandbox:test",
        temp_root=temp_root,
        execution_timeout_seconds=10,
        docker_control_timeout_seconds=10,
        cleanup_timeout_seconds=5,
        memory_bytes=256 * 1024 * 1024,
        cpus=1.0,
        pids_limit=64,
        nofile_limit=64,
        tmp_bytes=16 * 1024 * 1024,
        output_tmpfs_bytes=8 * 1024 * 1024,
        max_source_bytes=100_000,
        max_input_files=20,
        max_input_file_bytes=1_000_000,
        max_input_total_bytes=2_000_000,
        max_stdout_bytes=100_000,
        max_stderr_bytes=100_000,
        max_artifact_files=20,
        max_artifact_file_bytes=2_000_000,
        max_artifact_total_bytes=5_000_000,
        max_artifact_path_chars=240,
    )
    defaults.update(overrides)
    return SandboxPolicy(**defaults)


def make_tar(files: dict[str, bytes]) -> bytes:
    """Build the uncompressed tar payload the real `tar -cf - .` would produce."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def completed_exec(
    stdout: str = "", stderr: str = "", exit_code: int = 0
) -> StreamedCommand:
    """A job exec that ran to completion within every bound."""

    return StreamedCommand(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=len(stdout.encode()),
        stderr_bytes=len(stderr.encode()),
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        output_limit_exceeded=False,
    )


def timed_out_exec() -> StreamedCommand:
    """A job exec the host abandoned at its deadline."""

    return StreamedCommand(
        exit_code=None,
        stdout="",
        stderr="",
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=True,
        output_limit_exceeded=False,
    )


def flooded_exec(*, stream: str = "stdout") -> StreamedCommand:
    """A job exec stopped because one stream crossed its byte cap."""

    return StreamedCommand(
        exit_code=None,
        stdout="x" * 100 if stream == "stdout" else "",
        stderr="x" * 100 if stream == "stderr" else "",
        stdout_bytes=10_000_000 if stream == "stdout" else 0,
        stderr_bytes=10_000_000 if stream == "stderr" else 0,
        stdout_truncated=stream == "stdout",
        stderr_truncated=stream == "stderr",
        timed_out=False,
        output_limit_exceeded=True,
    )


class FakeCommandRunner:
    """A scripted stand-in for the trusted Docker CLI boundary."""

    def __init__(
        self,
        *,
        image_id: str = FAKE_IMAGE_ID,
        container_id: str = FAKE_CONTAINER_ID,
        exec_result: StreamedCommand | None = None,
        tar_files: dict[str, bytes] | None = None,
        tar_payload: bytes | None = None,
        daemon_available: bool = True,
        image_available: bool = True,
        run_succeeds: bool = True,
        cleanup_confirmed: bool = True,
        cli_missing: bool = False,
        stop_on_stream: str | None = None,
        tar_exit_code: int = 0,
        inspect_fails_transiently: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.stop_reasons: list[str] = []
        self._image_id = image_id
        self._container_id = container_id
        self._exec_result = exec_result or completed_exec()
        self._tar_payload = (
            tar_payload
            if tar_payload is not None
            else make_tar(tar_files or {})
        )
        self._daemon_available = daemon_available
        self._image_available = image_available
        self._run_succeeds = run_succeeds
        self._cleanup_confirmed = cleanup_confirmed
        self._cli_missing = cli_missing
        # When set, the fake exec calls back into the runtime the way a real
        # flood or deadline would, so cleanup ordering can be asserted.
        self._stop_on_stream = stop_on_stream
        self._tar_exit_code = tar_exit_code
        self._inspect_fails_transiently = inspect_fails_transiently

    # -- CommandRunner protocol ---------------------------------------------

    def run(self, argv: Sequence[str], *, timeout: float) -> CompletedCommand:
        argv = list(argv)
        self.calls.append(argv)
        if self._cli_missing:
            raise DockerCliMissing("fake: docker not installed")

        if argv[:2] == ["docker", "version"]:
            if not self._daemon_available:
                return CompletedCommand(1, "", "Cannot connect to the Docker daemon")
            return CompletedCommand(0, "29.6.1\n", "")

        if argv[:3] == ["docker", "image", "inspect"]:
            if not self._image_available:
                return CompletedCommand(
                    1, "", f"Error response from daemon: No such image: {argv[3]}"
                )
            if self._inspect_fails_transiently:
                return CompletedCommand(1, "", "error during connect: EOF")
            return CompletedCommand(0, self._image_id + "\n", "")

        if argv[:2] == ["docker", "run"]:
            if not self._run_succeeds:
                return CompletedCommand(125, "", "docker: invalid reference format")
            return CompletedCommand(0, self._container_id + "\n", "")

        if argv[:2] in (["docker", "kill"], ["docker", "rm"]):
            return CompletedCommand(0, "", "")

        if argv[:2] == ["docker", "ps"]:
            # An empty stdout means "no such container", i.e. confirmed gone.
            return CompletedCommand(
                0, "" if self._cleanup_confirmed else self._container_id + "\n", ""
            )

        return CompletedCommand(0, "", "")

    def stream(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        on_stop: Callable[[str], None],
    ) -> StreamedCommand:
        self.calls.append(list(argv))
        if self._cli_missing:
            raise DockerCliMissing("fake: docker not installed")
        if self._stop_on_stream is not None:
            self.stop_reasons.append(self._stop_on_stream)
            on_stop(self._stop_on_stream)
        return self._exec_result

    def collect(
        self, argv: Sequence[str], *, timeout: float, max_bytes: int
    ) -> CollectedBytes:
        self.calls.append(list(argv))
        if self._cli_missing:
            raise DockerCliMissing("fake: docker not installed")
        return CollectedBytes(
            exit_code=self._tar_exit_code,
            stdout=self._tar_payload,
            stderr="",
            truncated=len(self._tar_payload) > max_bytes,
            timed_out=False,
        )

    # -- assertions helpers --------------------------------------------------

    def argv_starting(self, *prefix: str) -> list[list[str]]:
        """Every recorded call whose argument vector starts with ``prefix``."""

        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]

    def one_argv_starting(self, *prefix: str) -> list[str]:
        matches = self.argv_starting(*prefix)
        assert len(matches) == 1, f"expected exactly one {prefix!r} call, got {len(matches)}"
        return matches[0]

    def command_order(self) -> list[str]:
        """A compact ``docker <verb>`` trace of the whole lifecycle."""

        order = []
        for call in self.calls:
            if call[:3] == ["docker", "image", "inspect"]:
                order.append("image inspect")
            elif call[:2] == ["docker", "exec"] and "tar" in call:
                order.append("exec tar")
            elif call[:2] == ["docker", "exec"]:
                order.append("exec job")
            else:
                order.append(" ".join(call[:2]).replace("docker ", ""))
        return order
