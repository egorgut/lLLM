"""Startup construction of the optional sandbox capability (SPEC-016 §11).

The property under test is that an unusable sandbox produces *nothing* — no
registered tool, no loaded skill, no half-wired handler — rather than a tool the
model can call and that always fails. A capability the model can see but never
use costs a tool call and a retry every time it is offered.
"""

import pytest

from sandbox_runtime.docker_backend import DockerSandboxRuntime
from sandbox_runtime.errors import SandboxImageUnavailable, SandboxUnavailable
from sandbox_tool import SKILL_NAME, build_sandbox_capability
from support_sandbox import FAKE_IMAGE_ID, FakeCommandRunner, make_policy
from tracing import MemoryTraceSink


def build(tmp_path, *, enabled=True, **runner_kwargs):
    policy = make_policy(tmp_path / "tmp")
    runtime = DockerSandboxRuntime(
        run_id="run-1",
        policy=policy,
        trace_sink=MemoryTraceSink(),
        command_runner=FakeCommandRunner(**runner_kwargs),
        tool_execution_timeout_seconds=30,
    )
    return build_sandbox_capability(
        run_id="run-1",
        artifact_root=tmp_path / "artifacts",
        project_root=tmp_path,
        turn_time_margin_seconds=2,
        enabled=enabled,
        policy=policy,
        runtime=runtime,
    )


class TestAvailable:
    def test_a_ready_sandbox_yields_a_wired_capability(self, tmp_path):
        capability, diagnostic = build(tmp_path)

        assert capability is not None
        assert capability.spec.name == "sandbox_execute"
        assert callable(capability.handler)
        assert diagnostic == "[sandbox] ready"

    def test_the_skill_name_is_the_one_app_py_omits(self):
        assert SKILL_NAME == "code_workspace"


class TestUnavailable:
    def test_a_disabled_sandbox_builds_nothing_and_touches_no_docker(self, tmp_path):
        capability, diagnostic = build(tmp_path, enabled=False)

        assert capability is None
        assert diagnostic == "[sandbox] disabled"

    def test_an_unreachable_daemon_omits_the_capability(self, tmp_path):
        capability, diagnostic = build(tmp_path, daemon_available=False)

        assert capability is None
        assert diagnostic.startswith("[sandbox] unavailable:")

    def test_a_missing_image_says_how_to_build_it(self, tmp_path):
        capability, diagnostic = build(tmp_path, image_available=False)

        assert capability is None
        assert "build_sandbox_image" in diagnostic

    def test_a_missing_docker_cli_omits_the_capability(self, tmp_path):
        capability, diagnostic = build(tmp_path, cli_missing=True)

        assert capability is None
        assert diagnostic.startswith("[sandbox] unavailable:")

    def test_no_diagnostic_leaks_a_host_path(self, tmp_path):
        for kwargs in ({"daemon_available": False}, {"image_available": False}):
            _, diagnostic = build(tmp_path, **kwargs)
            assert str(tmp_path) not in diagnostic


class TestEnsureAvailable:
    """The SPEC-015 probe SPEC-016 added, used by the builder above."""

    def _runtime(self, tmp_path, **runner_kwargs):
        return DockerSandboxRuntime(
            run_id="run-1",
            policy=make_policy(tmp_path / "tmp"),
            trace_sink=MemoryTraceSink(),
            command_runner=FakeCommandRunner(**runner_kwargs),
            tool_execution_timeout_seconds=30,
        )

    def test_returns_the_resolved_image_id(self, tmp_path):
        assert self._runtime(tmp_path).ensure_available() == FAKE_IMAGE_ID

    def test_raises_when_the_daemon_is_down(self, tmp_path):
        with pytest.raises(SandboxUnavailable):
            self._runtime(tmp_path, daemon_available=False).ensure_available()

    def test_raises_when_the_image_is_missing(self, tmp_path):
        with pytest.raises(SandboxImageUnavailable):
            self._runtime(tmp_path, image_available=False).ensure_available()

    def test_the_probe_is_memoised_for_the_first_job(self, tmp_path):
        runtime = self._runtime(tmp_path)
        runner = runtime._runner  # the fake we injected

        runtime.ensure_available()
        runtime.ensure_available()

        assert len(runner.argv_starting("docker", "image", "inspect")) == 1
