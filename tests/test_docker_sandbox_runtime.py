"""The Docker sandbox lifecycle, without Docker (SPEC-015 §6-7, §22-30).

Every case here runs against :class:`tests.support_sandbox.FakeCommandRunner`,
so the argument vectors, the isolation flags, the kill-on-limit behavior, the
cleanup ordering, and the trace contract are all verified on a machine with no
Docker daemon at all.

Two groups deserve special attention when reading: the flag assertions, which
are the machine-checkable form of the SPEC-015 security invariants, and the
cleanup assertions, which must hold on *every* exit path including
``KeyboardInterrupt``.
"""

import pathlib

import pytest

from sandbox_runtime.command_runner import StreamedCommand
from sandbox_runtime.docker_backend import DockerSandboxRuntime
from sandbox_runtime.errors import SandboxImageUnavailable, SandboxUnavailable
from sandbox_runtime.models import SandboxJob, SandboxLanguage, SandboxStatus
from sandbox_runtime.policy import JOB_GID, JOB_UID, OUTPUT_MOUNT
from tests.support_sandbox import (
    FAKE_CONTAINER_ID,
    FAKE_IMAGE_ID,
    FakeCommandRunner,
    completed_exec,
    flooded_exec,
    make_policy,
    make_tar,
    timed_out_exec,
)
from tracing import MemoryTraceSink

PYTHON_JOB = SandboxJob(language=SandboxLanguage.PYTHON, source="print(1)")


def build_runtime(tmp_path, runner, sink=None, **policy_overrides):
    return DockerSandboxRuntime(
        run_id="run-1",
        policy=make_policy(tmp_path, **policy_overrides),
        trace_sink=sink if sink is not None else MemoryTraceSink(),
        command_runner=runner,
        tool_execution_timeout_seconds=30,
    )


def flags(argv: list[str]) -> set[str]:
    return {token for token in argv if token.startswith("--")}


# -- image preflight ---------------------------------------------------------


def test_the_tag_is_resolved_to_an_immutable_image_id(tmp_path):
    runner = FakeCommandRunner()
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    inspect = runner.one_argv_starting("docker", "image", "inspect")
    assert inspect[3] == "lllm-sandbox:test"
    assert result.image_id == FAKE_IMAGE_ID


def test_the_container_is_created_from_the_image_id_not_the_tag(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    run_argv = runner.one_argv_starting("docker", "run")
    assert FAKE_IMAGE_ID in run_argv
    assert "lllm-sandbox:test" not in run_argv


def test_the_image_id_is_resolved_once_per_runtime(tmp_path):
    runner = FakeCommandRunner()
    runtime = build_runtime(tmp_path, runner)
    runtime.execute(PYTHON_JOB)
    runtime.execute(PYTHON_JOB)

    assert len(runner.argv_starting("docker", "image", "inspect")) == 1


def test_an_unavailable_daemon_raises_a_sanitised_exception(tmp_path):
    runner = FakeCommandRunner(daemon_available=False)
    with pytest.raises(SandboxUnavailable) as error:
        build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    message = str(error.value)
    assert message == "Docker sandbox is unavailable."
    # No raw Docker stderr, no host path, no traceback text in the public message.
    assert "Cannot connect" not in message and str(tmp_path) not in message


def test_a_missing_image_points_at_the_build_script(tmp_path):
    runner = FakeCommandRunner(image_available=False)
    with pytest.raises(SandboxImageUnavailable) as error:
        build_runtime(tmp_path, runner).execute(PYTHON_JOB)
    assert "scripts/build_sandbox_image.py" in str(error.value)


def test_a_missing_docker_cli_raises_sandbox_unavailable(tmp_path):
    runner = FakeCommandRunner(cli_missing=True)
    with pytest.raises(SandboxUnavailable):
        build_runtime(tmp_path, runner).execute(PYTHON_JOB)


def test_a_transient_inspect_failure_is_not_reported_as_a_missing_image(tmp_path):
    # "Build the image" is the wrong advice for a developer who has built it and
    # hit a restarting daemon, so only Docker's actual missing-image response
    # produces that message.
    runner = FakeCommandRunner(inspect_fails_transiently=True)
    with pytest.raises(SandboxUnavailable):
        build_runtime(tmp_path, runner).execute(PYTHON_JOB)


def test_preflight_failures_put_docker_diagnostics_in_the_trace(tmp_path):
    sink = MemoryTraceSink()
    runner = FakeCommandRunner(image_available=False)
    with pytest.raises(SandboxImageUnavailable):
        build_runtime(tmp_path, runner, sink).execute(PYTHON_JOB)

    preflight = next(e for e in sink.events if e["event"] == "sandbox_preflight_failed")
    assert preflight["error_type"] == "image_unavailable"
    assert "No such image" in preflight["docker_stderr_preview"]


# -- container arguments (the security invariants) ---------------------------


def test_every_required_isolation_flag_is_present(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)
    argv = runner.one_argv_starting("docker", "run")

    assert argv[:4] == ["docker", "run", "--detach", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--memory") + 1] == str(256 * 1024 * 1024)
    assert argv[argv.index("--memory-swap") + 1] == str(256 * 1024 * 1024)
    assert argv[argv.index("--cpus") + 1] == "1.0"
    assert argv[argv.index("--pids-limit") + 1] == "64"
    assert argv[argv.index("--ulimit") + 1] == "nofile=64:64"


def test_no_forbidden_flag_or_mount_is_ever_passed(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    for argv in runner.calls:
        joined = " ".join(argv)
        for forbidden in (
            "--privileged",
            "--device",
            "--cap-add",
            "--userns",
            "docker.sock",
            "network host",
            "--pid host",
            "--ipc host",
            "--publish",
        ):
            assert forbidden not in joined, f"forbidden token {forbidden!r} in {joined}"


def test_writable_output_is_container_tmpfs_owned_by_the_job_user(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)
    argv = runner.one_argv_starting("docker", "run")

    tmpfs = [argv[index + 1] for index, token in enumerate(argv) if token == "--tmpfs"]
    output = next(entry for entry in tmpfs if entry.startswith(f"{OUTPUT_MOUNT}:"))
    assert f"size={8 * 1024 * 1024}" in output
    assert f"uid={JOB_UID}" in output and f"gid={JOB_GID}" in output
    assert "mode=0700" in output
    assert "nosuid" in output and "nodev" in output

    tmp_entry = next(entry for entry in tmpfs if entry.startswith("/tmp:"))
    assert "noexec" in tmp_entry


def test_source_and_input_are_mounted_read_only_and_output_is_not_a_host_mount(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)
    argv = runner.one_argv_starting("docker", "run")

    mounts = [argv[index + 1] for index, token in enumerate(argv) if token == "--mount"]
    assert len(mounts) == 2
    assert all(mount.startswith("type=bind,") and mount.endswith(",readonly") for mount in mounts)
    assert any("dst=/sandbox/source" in mount for mount in mounts)
    assert any("dst=/sandbox/input" in mount for mount in mounts)
    # The one writable location must never be a host bind mount.
    assert not any("dst=/sandbox/output" in mount for mount in mounts)


def test_no_host_environment_is_forwarded(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_TOKEN", "super-secret-value")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")

    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)
    argv = runner.one_argv_starting("docker", "run")

    envs = [argv[index + 1] for index, token in enumerate(argv) if token == "--env"]
    assert sorted(envs) == sorted(
        [
            "HOME=/sandbox/output",
            "LANG=C.UTF-8",
            "LC_ALL=C.UTF-8",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED=1",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
    )
    assert "super-secret-value" not in " ".join(argv)
    assert "proxy.internal" not in " ".join(argv)


def test_the_container_is_labelled_and_named_from_the_host_job_id(tmp_path):
    runner = FakeCommandRunner()
    result = build_runtime(tmp_path, runner).execute(
        SandboxJob(language=SandboxLanguage.PYTHON, source="print('unique-source-text')")
    )
    argv = runner.one_argv_starting("docker", "run")

    assert argv[argv.index("--name") + 1] == f"lllm-sandbox-{result.job_id}"
    labels = [argv[index + 1] for index, token in enumerate(argv) if token == "--label"]
    assert labels == [
        "lllm.sandbox=true",
        f"lllm.sandbox.job_id={result.job_id}",
        "lllm.sandbox.spec=015",
    ]
    # The identity never derives from job content.
    assert "unique-source-text" not in " ".join(argv)


def test_docker_is_never_invoked_through_a_shell():
    # Structural: no module in the package may build a shell command string.
    package = pathlib.Path(__file__).resolve().parent.parent / "sandbox_runtime"
    for module in package.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "shell=True" not in text, f"{module.name} uses shell=True"
        assert "os.system" not in text, f"{module.name} uses os.system"


def test_every_docker_call_is_an_argument_vector(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    for argv in runner.calls:
        assert isinstance(argv, list)
        assert all(isinstance(token, str) for token in argv)
        # A token carrying a shell metacharacter would be a sign that separate
        # arguments had been collapsed into one string.
        assert not any(token.count(" ") and token.startswith("docker") for token in argv)


# -- the job command ---------------------------------------------------------


def test_python_executes_the_exact_fixed_command_as_the_job_user(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    argv = next(call for call in runner.calls if call[:2] == ["docker", "exec"] and "python3" in call)
    assert argv == [
        "docker",
        "exec",
        "--user",
        f"{JOB_UID}:{JOB_GID}",
        "--workdir",
        OUTPUT_MOUNT,
        FAKE_CONTAINER_ID,
        "python3",
        "-I",
        "-B",
        "/sandbox/source/main.py",
    ]


def test_bash_executes_the_exact_fixed_command_as_the_job_user(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(
        SandboxJob(language=SandboxLanguage.BASH, source="echo hi")
    )

    argv = next(call for call in runner.calls if call[:2] == ["docker", "exec"] and "/bin/bash" in call)
    assert argv[-4:] == ["/bin/bash", "--noprofile", "--norc", "/sandbox/source/main.sh"]
    assert argv[argv.index("--user") + 1] == f"{JOB_UID}:{JOB_GID}"


# -- outcomes ----------------------------------------------------------------


def test_a_successful_job_reports_completed_with_its_output(tmp_path):
    runner = FakeCommandRunner(exec_result=completed_exec(stdout="4950\n"))
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == "4950\n"
    assert result.error_type is None


def test_a_non_zero_exit_is_a_failed_job_not_an_exception(tmp_path):
    runner = FakeCommandRunner(
        exec_result=completed_exec(stderr="RuntimeError: boom\n", exit_code=1)
    )
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.FAILED
    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert result.error_type == "nonzero_exit"
    assert result.artifacts == ()


def test_a_timeout_kills_the_container_and_returns_timed_out(tmp_path):
    runner = FakeCommandRunner(exec_result=timed_out_exec(), stop_on_stream="timeout")
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.TIMED_OUT
    assert result.exit_code is None
    assert result.error_type == "execution_timeout"
    # The kill happens while the job is still streaming, not only in cleanup.
    assert runner.command_order().index("kill") < runner.command_order().index("rm")
    assert len(runner.argv_starting("docker", "kill")) >= 2


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_an_output_cap_stops_the_job_and_kills_the_container(tmp_path, stream):
    runner = FakeCommandRunner(
        exec_result=flooded_exec(stream=stream), stop_on_stream="output_limit"
    )
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.STOPPED
    assert result.error_type == "output_limit"
    assert getattr(result, f"{stream}_truncated") is True
    assert result.artifacts == ()
    assert runner.argv_starting("docker", "kill")


def test_a_holder_that_cannot_be_started_is_a_runtime_error(tmp_path):
    from sandbox_runtime.errors import SandboxRuntimeError

    runner = FakeCommandRunner(run_succeeds=False)
    sink = MemoryTraceSink()
    with pytest.raises(SandboxRuntimeError) as error:
        build_runtime(tmp_path, runner, sink).execute(PYTHON_JOB)

    assert str(error.value) == "The sandbox container could not be started."
    # Docker's own words belong in the trace, not in the public message.
    failure = next(e for e in sink.events if e["event"] == "sandbox_container_start_failed")
    assert "invalid reference format" in failure["docker_stderr_preview"]


# -- rejection (no Docker at all) --------------------------------------------


@pytest.mark.parametrize(
    "job,expected",
    [
        (SandboxJob(language="ruby", source="puts 1"), "unsupported_language"),
        (SandboxJob(language=SandboxLanguage.PYTHON, source="   "), "invalid_job"),
        (
            SandboxJob(
                language=SandboxLanguage.PYTHON, source="print(1)", input_files={"/etc/passwd": b"x"}
            ),
            "input_path_invalid",
        ),
        (
            SandboxJob(
                language=SandboxLanguage.PYTHON, source="print(1)", input_files={"../x": b"x"}
            ),
            "input_path_invalid",
        ),
    ],
)
def test_an_invalid_job_is_rejected_before_any_docker_command(tmp_path, job, expected):
    runner = FakeCommandRunner()
    result = build_runtime(tmp_path, runner).execute(job)

    assert result.status is SandboxStatus.REJECTED
    assert result.error_type == expected
    assert result.image_id is None
    assert runner.calls == []


def test_a_source_over_the_limit_is_rejected_before_any_docker_command(tmp_path):
    runner = FakeCommandRunner()
    result = build_runtime(tmp_path, runner, max_source_bytes=100).execute(
        SandboxJob(language=SandboxLanguage.PYTHON, source="x" * 2000)
    )
    assert result.status is SandboxStatus.REJECTED
    assert result.error_type == "source_too_large"
    assert runner.calls == []


# -- artifacts ---------------------------------------------------------------


def test_artifacts_are_collected_only_after_a_zero_exit(tmp_path):
    runner = FakeCommandRunner(
        exec_result=completed_exec(exit_code=2), tar_files={"report.csv": b"x"}
    )
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.FAILED
    assert result.artifacts == ()
    # The tar stream is never even requested for a failed job.
    assert "exec tar" not in runner.command_order()


def test_output_is_collected_while_the_holder_is_still_alive(tmp_path):
    runner = FakeCommandRunner(tar_files={"report.csv": b"name,value\n"})
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    order = runner.command_order()
    assert order.index("exec tar") < order.index("kill")
    assert order.index("exec tar") < order.index("rm")
    assert [artifact.path for artifact in result.artifacts] == ["report.csv"]


def test_collected_artifacts_carry_content_size_and_digest(tmp_path):
    import hashlib

    payload = b"name,value\nalpha,42\n"
    runner = FakeCommandRunner(tar_files={"report.csv": payload})
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    artifact = result.artifacts[0]
    assert artifact.content == payload
    assert artifact.size_bytes == len(payload)
    assert artifact.sha256 == hashlib.sha256(payload).hexdigest()


def test_artifacts_are_sorted_deterministically(tmp_path):
    runner = FakeCommandRunner(
        tar_files={"zebra.txt": b"z", "alpha.txt": b"a", "nested/mid.txt": b"m"}
    )
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert [artifact.path for artifact in result.artifacts] == [
        "alpha.txt",
        "nested/mid.txt",
        "zebra.txt",
    ]


def test_an_artifact_bound_violation_stops_the_job_and_returns_nothing(tmp_path):
    runner = FakeCommandRunner(tar_files={f"f{index}.txt": b"x" for index in range(5)})
    result = build_runtime(tmp_path, runner, max_artifact_files=2).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.STOPPED
    assert result.error_type == "artifact_policy_violation"
    assert result.artifacts == ()


def test_an_unreadable_output_archive_is_an_artifact_policy_violation(tmp_path):
    runner = FakeCommandRunner(tar_payload=b"this is not a tar archive at all")
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.STOPPED
    assert result.error_type == "artifact_policy_violation"


# -- cleanup -----------------------------------------------------------------


@pytest.mark.parametrize(
    "runner_kwargs",
    [
        {},  # success
        {"exec_result": completed_exec(exit_code=3)},  # non-zero exit
        {"exec_result": timed_out_exec(), "stop_on_stream": "timeout"},
        {"exec_result": flooded_exec(), "stop_on_stream": "output_limit"},
        {"tar_payload": b"not a tar"},  # artifact policy failure
    ],
)
def test_cleanup_runs_on_every_outcome(tmp_path, runner_kwargs):
    runner = FakeCommandRunner(**runner_kwargs)
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert runner.argv_starting("docker", "kill")
    assert runner.one_argv_starting("docker", "rm") == [
        "docker",
        "rm",
        "--force",
        FAKE_CONTAINER_ID,
    ]


def test_cleanup_targets_the_exact_container_id_not_a_name_pattern(tmp_path):
    runner = FakeCommandRunner()
    build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    for argv in runner.argv_starting("docker", "kill") + runner.argv_starting("docker", "rm"):
        assert FAKE_CONTAINER_ID in argv
        assert not any(token.startswith("lllm.sandbox") for token in argv)


def test_cleanup_runs_before_a_keyboard_interrupt_propagates(tmp_path):
    class InterruptingRunner(FakeCommandRunner):
        def stream(self, argv, **kwargs):
            self.calls.append(list(argv))
            raise KeyboardInterrupt

    runner = InterruptingRunner()
    sink = MemoryTraceSink()
    with pytest.raises(KeyboardInterrupt):
        build_runtime(tmp_path, runner, sink).execute(PYTHON_JOB)

    assert runner.argv_starting("docker", "rm")
    # The interrupt keeps its own meaning rather than becoming a job failure.
    events = [event["event"] for event in sink.events]
    assert events[-1] == "sandbox_job_finished"
    assert sink.events[-1]["error_type"] == "keyboard_interrupt"


def test_an_unconfirmed_cleanup_downgrades_a_successful_job(tmp_path):
    # A success cannot be reported unqualified while a container may still be
    # running, but the job's own exit code is preserved (§29).
    runner = FakeCommandRunner(cleanup_confirmed=False, tar_files={"a.txt": b"x"})
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.STOPPED
    assert result.error_type == "cleanup_unconfirmed"
    assert result.exit_code == 0
    assert result.artifacts == ()


def test_a_cleanup_failure_never_replaces_a_more_important_outcome(tmp_path):
    runner = FakeCommandRunner(
        cleanup_confirmed=False, exec_result=timed_out_exec(), stop_on_stream="timeout"
    )
    result = build_runtime(tmp_path, runner).execute(PYTHON_JOB)

    assert result.status is SandboxStatus.TIMED_OUT
    assert result.error_type == "execution_timeout"


def test_host_temporary_directories_are_removed_on_every_path(tmp_path):
    # Source, input, and collected output are all deleted in a finally block, so
    # no job's data outlives the result object built from it.
    temp_root = tmp_path / "sandbox-tmp"
    temp_root.mkdir()

    cases = [
        (FakeCommandRunner(tar_files={"a.txt": b"x"}), PYTHON_JOB),
        (FakeCommandRunner(exec_result=completed_exec(exit_code=1)), PYTHON_JOB),
        (FakeCommandRunner(exec_result=timed_out_exec(), stop_on_stream="timeout"), PYTHON_JOB),
        (FakeCommandRunner(), SandboxJob(language="ruby", source="puts 1")),
    ]
    for runner, job in cases:
        DockerSandboxRuntime(
            run_id="run-1",
            policy=make_policy(temp_root),
            trace_sink=MemoryTraceSink(),
            command_runner=runner,
            tool_execution_timeout_seconds=30,
        ).execute(job)
        assert list(temp_root.iterdir()) == []


def test_two_jobs_receive_distinct_ids_and_container_names(tmp_path):
    runner = FakeCommandRunner()
    runtime = build_runtime(tmp_path, runner)
    first = runtime.execute(PYTHON_JOB)
    second = runtime.execute(PYTHON_JOB)

    assert first.job_id != second.job_id
    names = [
        argv[argv.index("--name") + 1] for argv in runner.argv_starting("docker", "run")
    ]
    assert names == [f"lllm-sandbox-{first.job_id}", f"lllm-sandbox-{second.job_id}"]


# -- tracing -----------------------------------------------------------------


def test_a_successful_job_emits_the_required_events_in_order(tmp_path):
    sink = MemoryTraceSink()
    build_runtime(tmp_path, FakeCommandRunner(tar_files={"a.txt": b"x"}), sink).execute(
        PYTHON_JOB
    )

    assert [event["event"] for event in sink.events] == [
        "sandbox_job_started",
        "sandbox_container_started",
        "sandbox_execution_finished",
        "sandbox_artifact_collection_finished",
        "sandbox_cleanup_finished",
        "sandbox_job_finished",
    ]


def test_a_rejected_job_emits_a_policy_violation_and_a_terminal_event(tmp_path):
    sink = MemoryTraceSink()
    build_runtime(tmp_path, FakeCommandRunner(), sink).execute(
        SandboxJob(language="ruby", source="puts 1")
    )

    assert [event["event"] for event in sink.events] == [
        "sandbox_job_started",
        "sandbox_policy_violation",
        "sandbox_job_finished",
    ]


def test_every_started_job_has_exactly_one_terminal_event(tmp_path):
    sink = MemoryTraceSink()
    runtime = build_runtime(tmp_path, FakeCommandRunner(daemon_available=False), sink)
    with pytest.raises(SandboxUnavailable):
        runtime.execute(PYTHON_JOB)

    events = [event["event"] for event in sink.events]
    assert events.count("sandbox_job_started") == 1
    assert events.count("sandbox_job_finished") == 1


def test_trace_events_carry_the_required_correlation_fields(tmp_path):
    sink = MemoryTraceSink()
    result = build_runtime(tmp_path, FakeCommandRunner(), sink).execute(
        PYTHON_JOB, turn_id="turn-7"
    )

    for event in sink.events:
        assert event["run_id"] == "run-1"
        assert event["job_id"] == result.job_id
        assert event["turn_id"] == "turn-7"
        assert event["language"] == "python"
        assert event["policy_fingerprint"].startswith("sha256:")
    # The image id appears once preflight has resolved it.
    assert sink.events[1]["image_id"] == FAKE_IMAGE_ID


def test_traces_record_source_metadata_but_never_source_or_artifact_content(tmp_path):
    sink = MemoryTraceSink()
    secret_source = "print('SECRET-SOURCE-TEXT')"
    build_runtime(
        tmp_path,
        FakeCommandRunner(tar_files={"report.csv": b"SECRET-ARTIFACT-BYTES"}),
        sink,
    ).execute(
        SandboxJob(
            language=SandboxLanguage.PYTHON,
            source=secret_source,
            input_files={"data.csv": b"SECRET-INPUT-BYTES"},
        )
    )

    dumped = repr(sink.events)
    assert "SECRET-SOURCE-TEXT" not in dumped
    assert "SECRET-INPUT-BYTES" not in dumped
    assert "SECRET-ARTIFACT-BYTES" not in dumped
    assert str(tmp_path) not in dumped

    started = next(e for e in sink.events if e["event"] == "sandbox_container_started")
    assert started["source_bytes"] == len(secret_source.encode())
    assert started["source_sha256"].startswith("sha256:")
    assert started["input_file_count"] == 1
    assert started["input_total_bytes"] == len(b"SECRET-INPUT-BYTES")


def test_output_metadata_is_bounded_and_cleanup_status_is_visible(tmp_path):
    sink = MemoryTraceSink()
    runner = FakeCommandRunner(exec_result=completed_exec(stdout="x" * 5000))
    build_runtime(tmp_path, runner, sink).execute(PYTHON_JOB)

    execution = next(e for e in sink.events if e["event"] == "sandbox_execution_finished")
    assert execution["stdout_bytes"] == 5000
    assert len(execution["stdout_preview"]) <= 1100  # bounded preview, not the stream
    assert execution["stdout_sha256"]

    cleanup = next(e for e in sink.events if e["event"] == "sandbox_cleanup_finished")
    assert cleanup["cleanup_ok"] is True


def test_a_broken_trace_sink_never_breaks_a_job(tmp_path):
    class BrokenSink:
        def emit(self, event):
            raise RuntimeError("sink is down")

    result = build_runtime(tmp_path, FakeCommandRunner(), BrokenSink()).execute(PYTHON_JOB)
    assert result.status is SandboxStatus.COMPLETED
