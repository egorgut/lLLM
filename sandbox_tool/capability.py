"""Optional sandbox capability construction and startup probe (SPEC-016 §11).

The sandbox depends on something the application cannot provide for itself: a
reachable Docker daemon holding a built image. SPEC-013 already established what
to do with a dependency like that — a disabled or unreachable Tracker omits its
tools and its skill, prints one line, and leaves the rest of the app working.
This follows the same shape rather than inventing a second policy.

The probe runs once, at startup, and decides between three states: disabled by
configuration, enabled but unavailable, or ready. Both failure states end with
`sandbox_execute` unregistered and `code_workspace` omitted, because a tool the
model can see but that can only ever fail is worse than no tool at all — it
invites retries that spend the turn's budget on a capability that does not
exist.

The image is never built here (§11.3). Building is an operator action through
`scripts/build_sandbox_image.py`; a runtime that built its own image would be
deciding what code it is about to run.
"""

from dataclasses import dataclass
from pathlib import Path

from sandbox_runtime.docker_backend import DockerSandboxRuntime
from sandbox_runtime.errors import SandboxImageUnavailable, SandboxUnavailable
from sandbox_runtime.policy import SandboxPolicy, default_policy
from sandbox_tool.handler import create_sandbox_execute_handler
from sandbox_tool.schema import SANDBOX_EXECUTE_SPEC
from sandbox_tool.workspace import TurnWorkspace
from tools.executor import ToolHandler
from tools.registry import ToolSpec
from tracing import NullTraceSink, TraceSink

SKILL_NAME = "code_workspace"


@dataclass(frozen=True)
class SandboxCapability:
    """Everything the application needs to wire the sandbox into one run."""

    spec: ToolSpec
    handler: ToolHandler
    workspace: TurnWorkspace


def build_sandbox_capability(
    *,
    run_id: str,
    artifact_root: Path,
    project_root: Path,
    turn_time_margin_seconds: float,
    enabled: bool,
    trace_sink: TraceSink = NullTraceSink(),
    policy: SandboxPolicy | None = None,
    runtime: DockerSandboxRuntime | None = None,
) -> tuple[SandboxCapability | None, str]:
    """Return `(capability, diagnostic)`; `capability` is None when unusable.

    `runtime` is injectable so tests can exercise both branches against the real
    `DockerSandboxRuntime` driven by a fake Docker CLI, without a daemon.
    """

    if not enabled:
        return None, "[sandbox] disabled"

    policy = policy if policy is not None else default_policy()
    try:
        if runtime is None:
            runtime = DockerSandboxRuntime(
                run_id=run_id, policy=policy, trace_sink=trace_sink
            )
        runtime.ensure_available()
    except SandboxImageUnavailable as error:
        return None, f"[sandbox] unavailable: {error}"
    except SandboxUnavailable as error:
        return None, f"[sandbox] unavailable: {error}"

    workspace = TurnWorkspace(
        run_id=run_id,
        artifact_root=artifact_root,
        project_root=project_root,
        trace_sink=trace_sink,
    )
    handler = create_sandbox_execute_handler(
        runtime=runtime,
        policy=policy,
        workspace=workspace,
        turn_time_margin_seconds=turn_time_margin_seconds,
        trace_sink=trace_sink,
        run_id=run_id,
    )
    return (
        SandboxCapability(
            spec=SANDBOX_EXECUTE_SPEC, handler=handler, workspace=workspace
        ),
        "[sandbox] ready",
    )
