"""Test seams for the SPEC-016 sandbox tool.

Builds a real `sandbox_execute` handler over a real `DockerSandboxRuntime`
driven by SPEC-015's `FakeCommandRunner`, so the adapter is exercised against
the genuine runtime path — the same job materialisation, the same result
invariants, the same error taxonomy — without a Docker daemon anywhere.

Only the two things the adapter cannot supply for itself are faked: the Docker
CLI beneath the runtime, and the clock the turn deadline is measured against.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from reliability import TurnContext
from sandbox_runtime.docker_backend import DockerSandboxRuntime
from sandbox_tool.handler import create_sandbox_execute_handler
from sandbox_tool.workspace import TurnWorkspace
from support import FakeClock
from support_sandbox import FakeCommandRunner, make_policy
from tracing import MemoryTraceSink

RUN_ID = "run-016"
TURN_ID = "turn-a"
TURN_TIME_MARGIN_SECONDS = 2
# Comfortably above execution (10) + cleanup (5) + margin (2).
DEFAULT_REMAINING_SECONDS = 120.0


@dataclass
class SandboxToolHarness:
    """One wired sandbox tool plus everything a test needs to assert on it."""

    handler: Callable[[dict[str, Any]], dict[str, Any]]
    workspace: TurnWorkspace
    runner: FakeCommandRunner
    trace: MemoryTraceSink
    clock: FakeClock
    artifact_root: Path
    project_root: Path

    def open_turn(
        self, turn_id: str = TURN_ID, *, remaining: float = DEFAULT_REMAINING_SECONDS
    ) -> TurnContext:
        """Start a turn with `remaining` seconds left on its deadline."""

        now = self.clock()
        context = TurnContext(RUN_ID, turn_id, now, now + remaining)
        self.workspace.begin_turn(context)
        return context

    def turn_dir(self, turn_id: str = TURN_ID) -> Path:
        return self.artifact_root / RUN_ID / turn_id

    def events(self, name: str) -> list[dict[str, Any]]:
        return [event for event in self.trace.events if event["event"] == name]

    def trace_text(self) -> str:
        """The whole trace as one string, for "must not appear" assertions."""

        return repr(self.trace.events)


def make_harness(tmp_path: Path, **runner_kwargs: Any) -> SandboxToolHarness:
    """Build a sandbox tool over a fake Docker CLI with the given behaviour."""

    runner = FakeCommandRunner(**runner_kwargs)
    trace = MemoryTraceSink()
    clock = FakeClock()
    policy = make_policy(tmp_path / "sandbox-tmp")
    runtime = DockerSandboxRuntime(
        run_id=RUN_ID,
        policy=policy,
        trace_sink=trace,
        command_runner=runner,
        tool_execution_timeout_seconds=30,
    )
    artifact_root = tmp_path / "artifacts"
    workspace = TurnWorkspace(
        run_id=RUN_ID,
        artifact_root=artifact_root,
        project_root=tmp_path,
        trace_sink=trace,
        clock=clock,
    )
    handler = create_sandbox_execute_handler(
        runtime=runtime,
        policy=policy,
        workspace=workspace,
        turn_time_margin_seconds=TURN_TIME_MARGIN_SECONDS,
        trace_sink=trace,
        run_id=RUN_ID,
    )
    return SandboxToolHarness(
        handler=handler,
        workspace=workspace,
        runner=runner,
        trace=trace,
        clock=clock,
        artifact_root=artifact_root,
        project_root=tmp_path,
    )


def python_call(source: str = "print('hi')", **extra: Any) -> dict[str, Any]:
    """A minimal valid `sandbox_execute` argument object."""

    return {"language": "python", "source": source, **extra}
