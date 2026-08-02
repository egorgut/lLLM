"""The `sandbox_execute` handler (SPEC-016 §7-8, §13, §15-16).

This is the whole adapter between `ToolExecutor` and SPEC-015, and its job is
narrow by design: validate the model's arguments, refuse to start a job that
cannot finish inside the turn, build a `SandboxJob`, call the runtime, publish
artifacts if the job actually succeeded, and normalise everything into one
stable envelope.

What it deliberately does *not* do is reimplement any part of SPEC-015. No
Docker command, no image resolution, no timeout enforcement, no container
cleanup, no artifact extraction rule lives here. The runtime owns all of it, and
this layer only translates its typed outcome into something a model can act on.

Two rules shape the translation:

**A non-zero exit is never a success.** `ok` is true only when the job completed
with exit code zero *and* its artifacts were published. Anything else returns a
false `ok` with a status the model can reason about, so it can decide between
correcting its script and explaining a limitation it cannot fix.

**The envelope is uniform.** Every outcome — a validation mistake, a syntax
error, a timeout, an unavailable daemon — returns the same six fields, so the
model reads one shape rather than branching on the failure's origin. For
failures that never reached a process, `stderr` carries a short safe
explanation; there is no host path, container id, or Docker output in it.
"""

import time
from typing import Any

from sandbox_runtime.errors import SandboxImageUnavailable, SandboxUnavailable
from sandbox_runtime.models import (
    SandboxJob,
    SandboxResult,
    SandboxRuntime,
    SandboxStatus,
)
from sandbox_runtime.policy import SandboxPolicy
from sandbox_tool.artifacts import ArtifactPublicationError
from sandbox_tool.schema import (
    MAX_EXPLANATION_CHARS,
    InvalidSandboxRequest,
    validate_arguments,
)
from sandbox_tool.workspace import TurnWorkspace
from tools.executor import ToolArguments, ToolHandler, ToolResult
from tracing import NullTraceSink, SafeTraceSink, TraceSink, build_event

# The model-facing status vocabulary (§7.5). Nine values come from the spec; the
# tenth, `insufficient_time`, reports the §13.3 refusal to start a job that
# cannot finish before the whole-turn deadline. Reusing `runtime_unavailable`
# for it would tell the model the sandbox is broken when it is merely late, and
# `runtime_error` would hide a deliberate policy decision inside a host-defect
# bucket — neither is a distinction worth losing in a trace.
STATUS_SUCCEEDED = "succeeded"
STATUS_NON_ZERO_EXIT = "non_zero_exit"
STATUS_TIMED_OUT = "timed_out"
STATUS_STDOUT_LIMIT = "stdout_limit_exceeded"
STATUS_STDERR_LIMIT = "stderr_limit_exceeded"
STATUS_ARTIFACT_LIMIT = "artifact_limit_exceeded"
STATUS_INVALID_REQUEST = "invalid_request"
STATUS_RUNTIME_UNAVAILABLE = "runtime_unavailable"
STATUS_RUNTIME_ERROR = "runtime_error"
STATUS_INSUFFICIENT_TIME = "insufficient_time"

# Stable, host-owned explanations. The runtime's own `error_message` values are
# already safe and specific, so they are preferred where they exist; these cover
# the outcomes the runtime never sees.
_UNAVAILABLE_MESSAGE = (
    "The sandbox runtime is not available. Answer without running code, and say "
    "that code execution is unavailable."
)
_RUNTIME_ERROR_MESSAGE = "The sandbox job could not be completed."
_INSUFFICIENT_TIME_MESSAGE = (
    "Not enough time remains in this turn to run a sandbox job. Answer with what "
    "you already have instead of retrying."
)


def create_sandbox_execute_handler(
    *,
    runtime: SandboxRuntime,
    policy: SandboxPolicy,
    workspace: TurnWorkspace,
    turn_time_margin_seconds: float,
    trace_sink: TraceSink = NullTraceSink(),
    run_id: str,
) -> ToolHandler:
    """Build the `sandbox_execute` handler bound to one runtime and workspace.

    A closure for the same reason `create_sql_query_handler` is one: a tool
    handler receives only the model's arguments, so everything host-owned — the
    runtime, the policy, the turn workspace, the trace sink — is captured here
    and can never arrive as a tool argument.
    """

    trace = SafeTraceSink(trace_sink, run_id)
    # The floor a job must clear before it is allowed to start: SPEC-015 needs
    # this long, worst case, to run the script and then kill and remove the
    # container. Below it, the outer deadline could fire mid-job and leave the
    # container behind (§13.3).
    minimum_seconds = (
        policy.execution_timeout_seconds
        + policy.cleanup_timeout_seconds
        + turn_time_margin_seconds
    )

    def emit(event: str, **fields: Any) -> None:
        trace.emit(
            build_event(
                event, run_id=run_id, turn_id=workspace.turn_id, **fields
            )
        )

    def sandbox_execute(arguments: ToolArguments) -> ToolResult:
        call_index = workspace.next_call_index()

        try:
            language, source, input_files = validate_arguments(
                arguments, policy=policy
            )
        except InvalidSandboxRequest as error:
            emit(
                "sandbox_tool_result_returned",
                sandbox_call_index=call_index,
                sandbox_job_id=None,
                status=STATUS_INVALID_REQUEST,
                exit_code=None,
                artifact_count=0,
            )
            return _envelope(STATUS_INVALID_REQUEST, stderr=str(error))

        source_bytes = len(source.encode("utf-8"))
        emit(
            "sandbox_tool_requested",
            sandbox_call_index=call_index,
            language=str(language),
            source_bytes=source_bytes,
            input_file_count=len(input_files),
            input_total_bytes=sum(len(content) for content in input_files.values()),
        )

        remaining = workspace.remaining_seconds()
        if remaining is None:
            # No turn is active. Unreachable through the orchestrator, which
            # opens a turn before routing; a host defect, not a model mistake.
            return _finish(
                call_index, None, STATUS_RUNTIME_ERROR, stderr=_RUNTIME_ERROR_MESSAGE
            )
        if remaining <= minimum_seconds:
            return _finish(
                call_index,
                None,
                STATUS_INSUFFICIENT_TIME,
                stderr=_INSUFFICIENT_TIME_MESSAGE,
            )

        generation = workspace.generation
        started = time.monotonic()
        try:
            result = runtime.execute(
                SandboxJob(
                    language=language, source=source, input_files=input_files
                ),
                turn_id=workspace.turn_id,
            )
        except (SandboxUnavailable, SandboxImageUnavailable):
            return _finish(
                call_index,
                None,
                STATUS_RUNTIME_UNAVAILABLE,
                stderr=_UNAVAILABLE_MESSAGE,
                duration_ms=_elapsed_ms(started),
            )
        except Exception:
            # Both a declared host failure (`SandboxRuntimeError`) and an
            # undeclared defect are the same thing to the model: something on
            # the host went wrong, and changing the script will not fix it.
            # Never leak a traceback either way; the runtime's own terminal
            # trace event already records what actually happened.
            return _finish(
                call_index,
                None,
                STATUS_RUNTIME_ERROR,
                stderr=_RUNTIME_ERROR_MESSAGE,
                duration_ms=_elapsed_ms(started),
            )

        status = classify(result)
        artifacts: list[dict[str, Any]] = []
        if status == STATUS_SUCCEEDED:
            try:
                artifacts = workspace.publish(
                    result.artifacts, generation=generation, job_id=result.job_id
                )
            except ArtifactPublicationError:
                # The script ran perfectly but its output could not be kept.
                # Reporting success with an empty artifact list would tell the
                # model its files exist somewhere; this says they do not.
                return _finish(
                    call_index,
                    result.job_id,
                    STATUS_RUNTIME_ERROR,
                    stderr=(
                        "The script finished, but its output files could not be "
                        "saved."
                    ),
                    duration_ms=result.duration_ms,
                )

        return _finish(
            call_index,
            result.job_id,
            status,
            # An exit code is reported only when a process really produced one
            # (§7.6). A rejected, timed-out, or host-stopped job has none, even
            # where the runtime happened to observe one before killing it.
            exit_code=(
                result.exit_code
                if status in (STATUS_SUCCEEDED, STATUS_NON_ZERO_EXIT)
                else None
            ),
            stdout=result.stdout,
            stderr=_stderr_for(result, status),
            artifacts=artifacts,
            duration_ms=result.duration_ms,
        )

    def _finish(
        call_index: int,
        job_id: str | None,
        status: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        duration_ms: int | None = None,
    ) -> ToolResult:
        """Emit the result event and build the envelope, in that order."""

        artifacts = artifacts or []
        emit(
            "sandbox_tool_result_returned",
            sandbox_call_index=call_index,
            sandbox_job_id=job_id,
            status=status,
            exit_code=exit_code,
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
            artifact_count=len(artifacts),
            artifact_total_bytes=sum(entry["size_bytes"] for entry in artifacts),
            duration_ms=duration_ms,
        )
        return _envelope(
            status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
        )

    return sandbox_execute


def classify(result: SandboxResult) -> str:
    """Map one SPEC-015 outcome onto the model-facing status vocabulary (§7.5).

    The runtime's taxonomy is richer than the model needs and is phrased for a
    developer reading a trace. This is the single place it is narrowed, so the
    public tool contract stays stable even if SPEC-015 adds an `error_type`.
    """

    if result.status is SandboxStatus.COMPLETED:
        return STATUS_SUCCEEDED
    if result.status is SandboxStatus.TIMED_OUT:
        return STATUS_TIMED_OUT
    if result.status is SandboxStatus.FAILED:
        return STATUS_NON_ZERO_EXIT
    if result.status is SandboxStatus.REJECTED:
        # Every rejection reason — unsupported language, oversized source, a bad
        # input path, too many inputs — is something the model itself supplied.
        return STATUS_INVALID_REQUEST
    if result.status is SandboxStatus.STOPPED:
        if result.error_type == "output_limit":
            # SPEC-015 reports one `output_limit`; the truncation flags say which
            # stream actually crossed it, which is what the model needs to fix it.
            return STATUS_STDOUT_LIMIT if result.stdout_truncated else STATUS_STDERR_LIMIT
        if result.error_type == "artifact_policy_violation":
            return STATUS_ARTIFACT_LIMIT
        # `cleanup_unconfirmed` and anything added later: a host-side problem the
        # model cannot correct by changing its script.
        return STATUS_RUNTIME_ERROR
    return STATUS_RUNTIME_ERROR


def _stderr_for(result: SandboxResult, status: str) -> str:
    """What the model sees as `stderr` for a completed runtime call.

    A job that reached a process returns that process's real stderr. A job the
    runtime stopped or rejected never produced meaningful stderr, so its stable
    explanation takes that slot instead of an empty string.
    """

    if status in (STATUS_SUCCEEDED, STATUS_NON_ZERO_EXIT):
        return result.stderr
    explanation = result.error_message or _RUNTIME_ERROR_MESSAGE
    if result.stderr:
        explanation = f"{explanation}\n{result.stderr}"
    return explanation[:MAX_EXPLANATION_CHARS]


def _envelope(
    status: str,
    *,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    artifacts: list[dict[str, Any]] | None = None,
) -> ToolResult:
    return {
        "ok": status == STATUS_SUCCEEDED,
        "status": status,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "artifacts": artifacts or [],
    }


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
