"""The Docker-backed sandbox runtime (SPEC-015 §6-7, §22-30).

This is the module that owns a container's whole life: preflight, holder start,
the one untrusted exec, bounded output collection, hard termination, and
cleanup. Everything it passes to Docker is host-owned — the job supplies source
and input bytes and nothing else.

Two design points are worth reading before the code.

**Why a holder.** A writable host bind mount would let an untrusted script fill
the developer's disk before the host could look at the result, so there is none.
Instead the container's main process is a trusted idle holder, the job's output
lives in a size-bounded tmpfs that dies with the container, and the host collects
that output through the holder while it is still alive.

**Why termination is real.** ``reliability.run_with_deadline`` (SPEC-011) can
only abandon a thread — Python cannot stop arbitrary running code. Here the
deadline kills the *container*, which takes the job and every process it spawned
with it. That is the whole reason this layer exists.
"""

import hashlib
import io
import tarfile
import time
from pathlib import Path
from typing import Callable, Sequence

from reliability import new_id
from sandbox_runtime.artifacts import ArtifactPolicyViolation, collect_artifacts
from sandbox_runtime.command_runner import (
    CommandRunner,
    DockerCliMissing,
    SubprocessCommandRunner,
)
from sandbox_runtime.errors import (
    SandboxImageUnavailable,
    SandboxRuntimeError,
    SandboxUnavailable,
)
from sandbox_runtime.models import (
    SandboxJob,
    SandboxLanguage,
    SandboxResult,
    SandboxStatus,
)
from sandbox_runtime.paths import (
    JobRejected,
    materialise_job,
    new_job_dir,
    remove_job_dir,
)
from sandbox_runtime.policy import (
    INPUT_MOUNT,
    JOB_GID,
    JOB_UID,
    OUTPUT_MOUNT,
    SOURCE_MOUNT,
    TMP_MOUNT,
    HOLDER_COMMAND,
    SandboxPolicy,
    command_for,
    default_policy,
    policy_fingerprint,
    validate_sandbox_policy,
)
from sandbox_runtime.tracing import JobTracer
from tracing import NullTraceSink, TraceSink

# Headroom above the output tmpfs for tar headers and padding. The physical
# bound on collected data is the tmpfs itself; this only keeps the host from
# reading an unexpectedly long stream.
_TAR_OVERHEAD_BYTES = 64 * 1024

_USER_MESSAGES = {
    "execution_timeout": "The script exceeded its execution time limit.",
    "output_limit": "The script exceeded its output limit.",
    "artifact_policy_violation": "The script produced output that cannot be collected.",
    "nonzero_exit": "The script exited with a non-zero status.",
    "cleanup_unconfirmed": "The sandbox container could not be confirmed removed.",
}


class DockerSandboxRuntime:
    """Executes one :class:`SandboxJob` per disposable container.

    Constructed with host-owned identity and policy — matching the convention
    ``McpClientManager`` already uses — and an injectable
    :class:`~sandbox_runtime.command_runner.CommandRunner` so the whole lifecycle
    is testable without a Docker daemon.
    """

    def __init__(
        self,
        *,
        run_id: str,
        policy: SandboxPolicy | None = None,
        trace_sink: TraceSink = NullTraceSink(),
        command_runner: CommandRunner | None = None,
        tool_execution_timeout_seconds: float | None = None,
        preview_chars: int | None = None,
    ) -> None:
        import config

        self._run_id = run_id
        self._policy = policy if policy is not None else default_policy()
        validate_sandbox_policy(
            self._policy,
            tool_execution_timeout_seconds=(
                tool_execution_timeout_seconds
                if tool_execution_timeout_seconds is not None
                else config.TOOL_EXECUTION_TIMEOUT_SECONDS
            ),
        )
        self._fingerprint = policy_fingerprint(self._policy)
        self._sink = trace_sink
        self._runner = command_runner or SubprocessCommandRunner()
        self._preview_chars = (
            preview_chars
            if preview_chars is not None
            else config.TRACE_PAYLOAD_PREVIEW_CHARS
        )
        self._image_id: str | None = None
        # Create the scratch root once, here, rather than letting the first job
        # create the whole chain moments before it is bind-mounted: Docker
        # Desktop's file-sharing layer caches negative lookups, and a directory
        # created microseconds earlier can still be reported as
        # "bind source path does not exist".
        self._policy.temp_root.mkdir(parents=True, exist_ok=True)

    # -- public API ----------------------------------------------------------

    def ensure_available(self) -> str:
        """Resolve the image now, raising if the sandbox cannot be used.

        Added for SPEC-016, which has to decide at startup whether to expose a
        sandbox tool at all. It performs no new Docker work of its own: it runs
        the same preflight the first job would, so the answer is exactly the one
        that job would have got, and the resolved image ID is memoised for it.

        Raises `SandboxUnavailable` or `SandboxImageUnavailable`; a caller that
        wants a boolean catches `SandboxError`.
        """

        tracer = JobTracer(
            self._sink,
            run_id=self._run_id,
            job_id=new_id(),
            language="none",
            policy_fingerprint=self._fingerprint,
            preview_chars=self._preview_chars,
        )
        return self._resolve_image_id(tracer)

    def execute(self, job: SandboxJob, *, turn_id: str | None = None) -> SandboxResult:
        """Run one job and return exactly one result.

        The terminal-event guarantee follows ``AgentRunner.run_turn``: every
        emitted ``sandbox_job_started`` gets exactly one ``sandbox_job_finished``
        from a ``finally``, including on ``KeyboardInterrupt`` and on a host
        exception.
        """

        job_id = new_id()
        started_at = time.monotonic()
        tracer = JobTracer(
            self._sink,
            run_id=self._run_id,
            job_id=job_id,
            language=str(job.language),
            policy_fingerprint=self._fingerprint,
            turn_id=turn_id,
            preview_chars=self._preview_chars,
        )
        tracer.emit(
            "sandbox_job_started", declared_input_file_count=len(job.input_files)
        )

        result: SandboxResult | None = None
        failure: str | None = None
        try:
            result = self._execute(job, job_id, tracer, started_at)
            return result
        except KeyboardInterrupt:
            # Cleanup already happened in the inner finally; the interrupt keeps
            # its own meaning rather than being flattened into a job failure.
            failure = "keyboard_interrupt"
            raise
        except Exception as error:
            failure = type(error).__name__
            raise
        finally:
            if result is not None:
                tracer.emit(
                    "sandbox_job_finished",
                    status=str(result.status),
                    exit_code=result.exit_code,
                    error_type=result.error_type,
                    artifact_count=len(result.artifacts),
                    duration_ms=result.duration_ms,
                )
            else:
                tracer.emit(
                    "sandbox_job_finished",
                    status=None,
                    error_type=failure,
                    duration_ms=_elapsed_ms(started_at),
                )

    # -- lifecycle -----------------------------------------------------------

    def _execute(
        self, job: SandboxJob, job_id: str, tracer: JobTracer, started_at: float
    ) -> SandboxResult:
        job_dir = new_job_dir(self._policy.temp_root, job_id)
        try:
            try:
                materialised = materialise_job(job, self._policy, job_dir)
            except JobRejected as rejection:
                # Rejected before any Docker command exists: the container that
                # would have run this job is never created.
                tracer.emit(
                    "sandbox_policy_violation",
                    error_type=rejection.error_type,
                    error_message=str(rejection),
                )
                return self._result(
                    job_id,
                    job,
                    SandboxStatus.REJECTED,
                    started_at,
                    exit_code=None,
                    error_type=rejection.error_type,
                    error_message=str(rejection),
                )

            image_id = self._resolve_image_id(tracer)
            tracer.set_image_id(image_id)

            container_id = self._start_holder(job_id, materialised, image_id, tracer)
            tracer.emit(
                "sandbox_container_started",
                source_bytes=len(materialised.source_bytes),
                # The source's hash, never the source itself.
                source_sha256="sha256:"
                + hashlib.sha256(materialised.source_bytes).hexdigest(),
                input_file_count=materialised.input_file_count,
                input_total_bytes=materialised.input_total_bytes,
            )

            result: SandboxResult | None = None
            try:
                result = self._run_and_collect(
                    job, job_id, tracer, started_at, container_id, image_id, job_dir
                )
            finally:
                cleanup_started = time.monotonic()
                cleanup_ok = self._cleanup_container(container_id)
                tracer.emit(
                    "sandbox_cleanup_finished",
                    cleanup_ok=cleanup_ok,
                    duration_ms=_elapsed_ms(cleanup_started),
                )

            # A cleanup failure never overrides a more important primary
            # outcome — a job that already failed stays failed. But a *success*
            # cannot be reported unqualified while a container may still be
            # running, so it is downgraded instead (§29).
            if not cleanup_ok and result.status is SandboxStatus.COMPLETED:
                return self._result(
                    job_id,
                    job,
                    SandboxStatus.STOPPED,
                    started_at,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    stdout_truncated=result.stdout_truncated,
                    stderr_truncated=result.stderr_truncated,
                    image_id=result.image_id,
                    error_type="cleanup_unconfirmed",
                )
            return result
        finally:
            remove_job_dir(job_dir)

    def _run_and_collect(
        self,
        job: SandboxJob,
        job_id: str,
        tracer: JobTracer,
        started_at: float,
        container_id: str,
        image_id: str,
        job_dir: Path,
    ) -> SandboxResult:
        streamed = self._runner.stream(
            self._exec_argv(container_id, command_for(job.language)),
            timeout=self._policy.execution_timeout_seconds,
            max_stdout_bytes=self._policy.max_stdout_bytes,
            max_stderr_bytes=self._policy.max_stderr_bytes,
            on_stop=self._stop_callback(container_id),
        )

        stdout_preview, stdout_hash, stdout_preview_truncated = tracer.output_preview(
            streamed.stdout
        )
        tracer.emit(
            "sandbox_execution_finished",
            exit_code=streamed.exit_code,
            stdout_bytes=streamed.stdout_bytes,
            stderr_bytes=streamed.stderr_bytes,
            stdout_truncated=streamed.stdout_truncated,
            stderr_truncated=streamed.stderr_truncated,
            timed_out=streamed.timed_out,
            output_limit_exceeded=streamed.output_limit_exceeded,
            stdout_preview=stdout_preview,
            stdout_sha256=stdout_hash,
            stdout_preview_truncated=stdout_preview_truncated,
            duration_ms=_elapsed_ms(started_at),
        )

        common = {
            "stdout": streamed.stdout,
            "stderr": streamed.stderr,
            "stdout_truncated": streamed.stdout_truncated,
            "stderr_truncated": streamed.stderr_truncated,
            "image_id": image_id,
        }

        # Order matters: a job that floods output *and* runs long is reported by
        # whichever bound the host acted on first, and both mean the container
        # was killed rather than observed to completion.
        if streamed.timed_out:
            return self._result(
                job_id,
                job,
                SandboxStatus.TIMED_OUT,
                started_at,
                exit_code=None,
                error_type="execution_timeout",
                **common,
            )
        if streamed.output_limit_exceeded:
            return self._result(
                job_id,
                job,
                SandboxStatus.STOPPED,
                started_at,
                exit_code=None,
                error_type="output_limit",
                **common,
            )
        if streamed.exit_code != 0:
            return self._result(
                job_id,
                job,
                SandboxStatus.FAILED,
                started_at,
                exit_code=streamed.exit_code,
                error_type="nonzero_exit",
                **common,
            )

        collection_dir = job_dir / "collect"
        collection_dir.mkdir(parents=True, exist_ok=True)
        collection_started = time.monotonic()
        try:
            artifacts = self._collect_output(container_id, collection_dir)
        except ArtifactPolicyViolation as violation:
            tracer.emit(
                "sandbox_artifact_collection_finished",
                collected=False,
                error_type="artifact_policy_violation",
                error_message=str(violation),
                duration_ms=_elapsed_ms(collection_started),
            )
            return self._result(
                job_id,
                job,
                SandboxStatus.STOPPED,
                started_at,
                exit_code=streamed.exit_code,
                error_type="artifact_policy_violation",
                error_message=str(violation),
                **common,
            )

        tracer.emit(
            "sandbox_artifact_collection_finished",
            collected=True,
            artifact_count=len(artifacts),
            artifact_total_bytes=sum(item.size_bytes for item in artifacts),
            duration_ms=_elapsed_ms(collection_started),
        )
        return self._result(
            job_id,
            job,
            SandboxStatus.COMPLETED,
            started_at,
            exit_code=0,
            artifacts=artifacts,
            **common,
        )

    # -- Docker interaction --------------------------------------------------

    def _resolve_image_id(self, tracer: JobTracer) -> str:
        """Resolve the configured tag to one immutable image ID, once per runtime.

        Tags are mutable; an image ID is not. Creating containers from the
        resolved ID means a rebuild or retag between two jobs cannot silently
        change what the second one executes.
        """

        if self._image_id is not None:
            return self._image_id

        version = self._control(
            ["docker", "version", "--format", "{{.Server.Version}}"]
        )
        if version.exit_code != 0:
            self._trace_preflight_failure(tracer, "daemon_unavailable", version)
            raise SandboxUnavailable()

        inspected = self._control(
            [
                "docker",
                "image",
                "inspect",
                self._policy.image_ref,
                "--format",
                "{{.Id}}",
            ]
        )
        image_id = inspected.stdout.strip()
        if inspected.exit_code != 0 or not image_id.startswith("sha256:"):
            # "Build the image" is only the right advice when the image is
            # actually missing. Any other inspect failure — a daemon restarting,
            # a control-command timeout — would send a developer who *has* built
            # it chasing the wrong problem, so it is reported as unavailability.
            self._trace_preflight_failure(tracer, "image_unavailable", inspected)
            if "No such image" in inspected.stderr:
                raise SandboxImageUnavailable()
            raise SandboxUnavailable()

        self._image_id = image_id
        return image_id

    def _trace_preflight_failure(self, tracer: JobTracer, reason: str, command) -> None:
        """Record the real Docker diagnostics where they belong: in the trace.

        The public exception stays short and stable; the bounded stderr preview
        lives here so a developer can still see what Docker actually said.
        """

        preview, _, _ = tracer.output_preview(command.stderr)
        tracer.emit(
            "sandbox_preflight_failed",
            error_type=reason,
            exit_code=command.exit_code,
            timed_out=command.timed_out,
            docker_stderr_preview=preview,
        )

    def _start_holder(self, job_id: str, materialised, image_id: str, tracer: JobTracer) -> str:
        started = self._control(self._run_argv(job_id, materialised, image_id))
        container_id = started.stdout.strip()
        if started.exit_code != 0 or not container_id:
            # The public message stays stable and path-free, but a start failure
            # is otherwise indistinguishable from a dozen unrelated causes, so
            # Docker's own words go into the trace.
            preview, _, _ = tracer.output_preview(started.stderr)
            tracer.emit(
                "sandbox_container_start_failed",
                exit_code=started.exit_code,
                timed_out=started.timed_out,
                docker_stderr_preview=preview,
            )
            raise SandboxRuntimeError("The sandbox container could not be started.")
        return container_id

    def _run_argv(self, job_id: str, materialised, image_id: str) -> list[str]:
        """The complete holder-container argument vector.

        Every isolation flag is unconditional and none of it is derived from the
        job: no field of :class:`SandboxJob` reaches this list except, indirectly,
        the *contents* of the two read-only mounts.
        """

        policy = self._policy
        argv = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            f"lllm-sandbox-{job_id}",
            "--label",
            "lllm.sandbox=true",
            "--label",
            f"lllm.sandbox.job_id={job_id}",
            "--label",
            "lllm.sandbox.spec=015",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            str(policy.memory_bytes),
            # Equal to --memory: swap would silently restore the headroom the
            # memory limit is there to remove.
            "--memory-swap",
            str(policy.memory_bytes),
            "--cpus",
            str(policy.cpus),
            "--pids-limit",
            str(policy.pids_limit),
            "--ulimit",
            f"nofile={policy.nofile_limit}:{policy.nofile_limit}",
            "--tmpfs",
            f"{TMP_MOUNT}:rw,noexec,nosuid,nodev,size={policy.tmp_bytes}",
            # No noexec on the output mount: it is the job's working directory,
            # and a script writing then running a helper there is ordinary use.
            # The security boundary is the fixed host command, the non-root user,
            # the read-only root, no-new-privileges, and the dropped capabilities.
            "--tmpfs",
            (
                f"{OUTPUT_MOUNT}:rw,nosuid,nodev,size={policy.output_tmpfs_bytes},"
                f"uid={JOB_UID},gid={JOB_GID},mode=0700"
            ),
            "--mount",
            f"type=bind,src={materialised.source_dir},dst={SOURCE_MOUNT},readonly",
            "--mount",
            f"type=bind,src={materialised.input_dir},dst={INPUT_MOUNT},readonly",
        ]
        for key, value in policy.environment.items():
            argv += ["--env", f"{key}={value}"]
        argv.append(image_id)
        argv += list(HOLDER_COMMAND)
        return argv

    def _exec_argv(self, container_id: str, command: Sequence[str]) -> list[str]:
        """The untrusted command, always as a fixed vector under a fixed identity."""

        return [
            "docker",
            "exec",
            "--user",
            f"{JOB_UID}:{JOB_GID}",
            "--workdir",
            OUTPUT_MOUNT,
            container_id,
            *command,
        ]

    def _stop_callback(self, container_id: str) -> Callable[[str], None]:
        """Kill the whole container when a deadline or output cap is crossed."""

        def _stop(reason: str) -> None:  # noqa: ARG001 - reason is for readability
            self._kill_container(container_id)

        return _stop

    def _collect_output(self, container_id: str, collection_dir: Path) -> tuple:
        """Stream ``/sandbox/output`` out of the live holder and validate it.

        ``docker cp`` is deliberately not used: on Docker 29 it resolves paths
        against the host-side rootfs and reads nothing at all from a tmpfs mount
        (verified with both ``--tmpfs`` and ``--mount type=tmpfs``). The output
        therefore comes back as a bounded tar on the exec's stdout.

        Two independent layers then decide what becomes host data: ``tarfile``'s
        ``data`` filter, which refuses absolute paths, ``..`` traversal, links
        pointing outside the archive, and special files; and the ``lstat`` walk in
        ``artifacts.collect_artifacts``, which accepts only bounded regular files.
        """

        max_bytes = self._policy.output_tmpfs_bytes + _TAR_OVERHEAD_BYTES
        collected = self._runner.collect(
            self._exec_argv(container_id, ("tar", "-cf", "-", ".")),
            timeout=self._policy.docker_control_timeout_seconds,
            max_bytes=max_bytes,
        )
        if collected.truncated:
            raise ArtifactPolicyViolation(
                "The job's output exceeds the collectable size limit."
            )
        if collected.timed_out or collected.exit_code != 0:
            raise SandboxRuntimeError("The job's output could not be collected.")

        try:
            with tarfile.open(fileobj=io.BytesIO(collected.stdout), mode="r|") as archive:
                archive.extractall(path=collection_dir, filter="data")
        except tarfile.TarError as error:
            raise ArtifactPolicyViolation(
                f"The job's output could not be read as a safe archive: {error}"
            ) from None

        return collect_artifacts(collection_dir, self._policy)

    def _kill_container(self, container_id: str) -> None:
        """Best-effort immediate termination, called from a reader thread.

        Never raises: this runs while output is still being drained, and an
        exception here would kill the reader instead of the container. A failure
        is picked up by cleanup, which is the authority on whether the container
        is really gone.
        """

        try:
            self._control(
                ["docker", "kill", container_id],
                timeout_override=self._policy.cleanup_timeout_seconds,
            )
        except Exception:
            pass

    def _cleanup_container(self, container_id: str) -> bool:
        """Terminate and remove the container. Idempotent, and never raises.

        Tolerates every ordinary race: already stopped, already removed by
        ``--rm``, or a daemon that disappeared. Returns ``True`` only when the
        container is *confirmed* gone — removed here, or already absent — because
        the caller uses that to decide whether a success can be reported as one.
        """

        timeout = self._policy.cleanup_timeout_seconds
        try:
            self._control(["docker", "kill", container_id], timeout_override=timeout)
            self._control(
                ["docker", "rm", "--force", container_id], timeout_override=timeout
            )
            probe = self._control(
                ["docker", "ps", "--all", "--quiet", "--filter", f"id={container_id}"],
                timeout_override=timeout,
            )
        except Exception:
            return False
        if probe.exit_code != 0 or probe.timed_out:
            return False
        return probe.stdout.strip() == ""

    def _control(self, argv: list[str], *, timeout_override: float | None = None):
        """One bounded Docker control command, with a stable failure shape."""

        timeout = (
            timeout_override
            if timeout_override is not None
            else self._policy.docker_control_timeout_seconds
        )
        try:
            return self._runner.run(argv, timeout=timeout)
        except DockerCliMissing:
            raise SandboxUnavailable() from None

    # -- result construction -------------------------------------------------

    def _result(
        self,
        job_id: str,
        job: SandboxJob,
        status: SandboxStatus,
        started_at: float,
        *,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        artifacts: tuple = (),
        image_id: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> SandboxResult:
        if error_type is not None and error_message is None:
            error_message = _USER_MESSAGES.get(error_type)
        return SandboxResult(
            job_id=job_id,
            status=status,
            language=_language_of(job),
            image_id=image_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            artifacts=artifacts,
            duration_ms=_elapsed_ms(started_at),
            error_type=error_type,
            error_message=error_message,
        )


def _language_of(job: SandboxJob) -> SandboxLanguage | str:
    """The job's language, echoed back verbatim when it is not a supported one."""

    try:
        return SandboxLanguage(job.language)
    except ValueError:
        return str(job.language)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
