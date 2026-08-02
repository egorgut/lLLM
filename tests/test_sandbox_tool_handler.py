"""The `sandbox_execute` handler against a real runtime (SPEC-016 §7, §13, §15-16).

Every test here drives the genuine `DockerSandboxRuntime` over a fake Docker
CLI, so what is under test is the actual translation the application performs:
the same job materialisation, the same result invariants, the same error
taxonomy — only the daemon is missing.

The three properties that matter:

* a non-zero exit is never dressed up as a success, and neither is a timeout, an
  output flood, or an artifact the host refused;
* nothing that reaches the model or the trace carries a host path, a container
  id, or the bytes of anything the model or the script produced;
* a job is never started when the turn cannot outlive it.
"""

import json

import pytest

from sandbox_tool.handler import classify
from sandbox_runtime.models import (
    SandboxLanguage,
    SandboxResult,
    SandboxStatus,
)
from support_sandbox import (
    FAKE_CONTAINER_ID,
    FAKE_IMAGE_ID,
    completed_exec,
    flooded_exec,
    timed_out_exec,
)
from support_sandbox_tool import (
    DEFAULT_REMAINING_SECONDS,
    RUN_ID,
    TURN_ID,
    make_harness,
    python_call,
)


class TestSuccess:
    def test_python_job_succeeds_with_bounded_output(self, tmp_path):
        harness = make_harness(
            tmp_path, exec_result=completed_exec(stdout="processed 12 rows\n")
        )
        harness.open_turn()

        result = harness.handler(python_call("print('processed 12 rows')"))

        assert result == {
            "ok": True,
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "processed 12 rows\n",
            "stderr": "",
            "artifacts": [],
        }

    def test_bash_job_succeeds(self, tmp_path):
        harness = make_harness(tmp_path, exec_result=completed_exec(stdout="done\n"))
        harness.open_turn()

        result = harness.handler({"language": "bash", "source": "echo done"})

        assert result["ok"] is True
        assert result["status"] == "succeeded"

    def test_artifacts_are_published_with_bounded_metadata(self, tmp_path):
        harness = make_harness(
            tmp_path,
            exec_result=completed_exec(stdout="wrote squares.csv\n"),
            tar_files={"squares.csv": b"n,square\n1,1\n2,4\n"},
        )
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["artifacts"] == [
            {
                "name": "squares.csv",
                "media_type": "text/csv",
                "size_bytes": 17,
                "path": f"artifacts/{RUN_ID}/{TURN_ID}/squares.csv",
            }
        ]
        published = harness.turn_dir() / "squares.csv"
        assert published.read_bytes() == b"n,square\n1,1\n2,4\n"

    def test_input_files_reach_the_container(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        result = harness.handler(
            python_call(
                "print(1)",
                input_files=[{"name": "sales.csv", "content": "a,b\n1,2\n"}],
            )
        )

        assert result["ok"] is True
        requested = harness.events("sandbox_tool_requested")[0]
        assert requested["input_file_count"] == 1
        assert requested["input_total_bytes"] == 8

    def test_the_turn_id_is_passed_to_the_runtime(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn("turn-xyz")

        harness.handler(python_call())

        job_events = harness.events("sandbox_job_started")
        assert [event["turn_id"] for event in job_events] == ["turn-xyz"]

    def test_each_call_gets_a_fresh_job_id(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        harness.handler(python_call("print(1)"))
        harness.handler(python_call("print(2)"))

        job_ids = [event["job_id"] for event in harness.events("sandbox_job_started")]
        assert len(job_ids) == 2 and len(set(job_ids)) == 2


class TestFailureMapping:
    def test_non_zero_exit_is_never_a_success(self, tmp_path):
        harness = make_harness(
            tmp_path,
            exec_result=completed_exec(
                stderr="Traceback ...\nSyntaxError: invalid syntax\n", exit_code=1
            ),
        )
        harness.open_turn()

        result = harness.handler(python_call("prin('oops')"))

        assert result["ok"] is False
        assert result["status"] == "non_zero_exit"
        assert result["exit_code"] == 1
        assert "SyntaxError" in result["stderr"]
        assert result["artifacts"] == []

    def test_non_zero_exit_publishes_nothing_even_with_output_files(self, tmp_path):
        harness = make_harness(
            tmp_path,
            exec_result=completed_exec(exit_code=2),
            tar_files={"partial.csv": b"junk"},
        )
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "non_zero_exit"
        assert result["artifacts"] == []
        assert not harness.turn_dir().exists()

    def test_timeout_reports_no_exit_code_and_no_artifacts(self, tmp_path):
        harness = make_harness(tmp_path, exec_result=timed_out_exec())
        harness.open_turn()

        result = harness.handler(python_call("while True: pass"))

        assert result["status"] == "timed_out"
        assert result["exit_code"] is None
        assert result["artifacts"] == []
        assert result["ok"] is False

    def test_stdout_flood_identifies_the_stream(self, tmp_path):
        harness = make_harness(
            tmp_path, exec_result=flooded_exec(stream="stdout"), stop_on_stream="stdout"
        )
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "stdout_limit_exceeded"
        assert result["exit_code"] is None
        assert len(result["stdout"]) <= 100

    def test_stderr_flood_identifies_the_other_stream(self, tmp_path):
        harness = make_harness(
            tmp_path, exec_result=flooded_exec(stream="stderr"), stop_on_stream="stderr"
        )
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "stderr_limit_exceeded"

    def test_rejected_job_is_an_invalid_request(self, tmp_path):
        """An oversized source is rejected by SPEC-015, not by a second limit."""

        harness = make_harness(tmp_path)
        harness.open_turn()

        result = harness.handler(python_call("#" * 200_000))

        assert result["status"] == "invalid_request"
        assert result["exit_code"] is None
        # No container was ever created for it.
        assert harness.runner.argv_starting("docker", "run") == []

    def test_too_many_input_files_is_an_invalid_request(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        result = harness.handler(
            python_call(
                input_files=[
                    {"name": f"f{index}.txt", "content": "x"} for index in range(21)
                ]
            )
        )

        assert result["status"] == "invalid_request"
        assert harness.runner.argv_starting("docker", "run") == []

    def test_validation_error_never_reaches_docker(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        result = harness.handler({"language": "ruby", "source": "puts 1"})

        assert result == {
            "ok": False,
            "status": "invalid_request",
            "exit_code": None,
            "stdout": "",
            "stderr": result["stderr"],
            "artifacts": [],
        }
        assert "python" in result["stderr"]
        assert harness.runner.calls == []

    def test_unavailable_daemon_is_reported_without_docker_detail(self, tmp_path):
        harness = make_harness(tmp_path, daemon_available=False)
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "runtime_unavailable"
        assert "docker" not in result["stderr"].lower()

    def test_missing_image_is_reported_as_unavailable(self, tmp_path):
        harness = make_harness(tmp_path, image_available=False)
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "runtime_unavailable"

    def test_container_start_failure_is_a_runtime_error(self, tmp_path):
        harness = make_harness(tmp_path, run_succeeds=False)
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "runtime_error"
        assert result["ok"] is False

    def test_unconfirmed_cleanup_is_a_runtime_error(self, tmp_path):
        """A container that may still exist is not a success, however it exited."""

        harness = make_harness(
            tmp_path,
            cleanup_confirmed=False,
            tar_files={"out.txt": b"data"},
        )
        harness.open_turn()

        result = harness.handler(python_call())

        assert result["status"] == "runtime_error"
        assert result["artifacts"] == []
        assert not harness.turn_dir().exists()

    def test_an_unexpected_host_exception_is_a_runtime_error(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        class Exploding:
            def execute(self, job, *, turn_id=None):
                raise RuntimeError("host defect with /Users/secret/path")

        from sandbox_tool.handler import create_sandbox_execute_handler
        from support_sandbox import make_policy

        handler = create_sandbox_execute_handler(
            runtime=Exploding(),
            policy=make_policy(tmp_path / "p"),
            workspace=harness.workspace,
            turn_time_margin_seconds=2,
            trace_sink=harness.trace,
            run_id=RUN_ID,
        )

        result = handler(python_call())

        assert result["status"] == "runtime_error"
        assert "/Users" not in result["stderr"]


class TestClassify:
    """The status table itself, over results the fake CLI cannot easily produce."""

    def _result(self, status, **kwargs):
        defaults = dict(
            job_id="job-1",
            status=status,
            language=SandboxLanguage.PYTHON,
            image_id=FAKE_IMAGE_ID,
            exit_code=None,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            artifacts=(),
            duration_ms=1,
        )
        defaults.update(kwargs)
        return SandboxResult(**defaults)

    @pytest.mark.parametrize(
        "error_type,expected",
        [
            ("output_limit", "stderr_limit_exceeded"),
            ("artifact_policy_violation", "artifact_limit_exceeded"),
            ("cleanup_unconfirmed", "runtime_error"),
            ("something_new_from_a_later_spec", "runtime_error"),
        ],
    )
    def test_stopped_reasons(self, error_type, expected):
        assert (
            classify(self._result(SandboxStatus.STOPPED, error_type=error_type))
            == expected
        )

    def test_stopped_on_stdout_flood_picks_stdout(self):
        result = self._result(
            SandboxStatus.STOPPED, error_type="output_limit", stdout_truncated=True
        )
        assert classify(result) == "stdout_limit_exceeded"

    @pytest.mark.parametrize(
        "error_type",
        [
            "invalid_job",
            "unsupported_language",
            "source_too_large",
            "input_path_invalid",
            "input_file_limit",
            "input_size_limit",
        ],
    )
    def test_every_rejection_reason_is_an_invalid_request(self, error_type):
        result = self._result(SandboxStatus.REJECTED, error_type=error_type)
        assert classify(result) == "invalid_request"


class TestDeadlineGuard:
    def test_a_job_is_refused_when_the_turn_cannot_outlive_it(self, tmp_path):
        harness = make_harness(tmp_path)
        # 10 (execution) + 5 (cleanup) + 2 (margin) = 17 needed.
        harness.open_turn(remaining=16.0)

        result = harness.handler(python_call())

        assert result["status"] == "insufficient_time"
        assert result["exit_code"] is None
        assert harness.runner.calls == [], "no container may be created"

    def test_a_job_starts_when_enough_time_remains(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn(remaining=18.0)

        result = harness.handler(python_call())

        assert result["status"] == "succeeded"

    def test_time_spent_earlier_in_the_turn_counts(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn(remaining=DEFAULT_REMAINING_SECONDS)
        harness.clock.advance(DEFAULT_REMAINING_SECONDS - 10)

        result = harness.handler(python_call())

        assert result["status"] == "insufficient_time"

    def test_a_call_outside_any_turn_is_a_runtime_error(self, tmp_path):
        harness = make_harness(tmp_path)

        result = harness.handler(python_call())

        assert result["status"] == "runtime_error"
        assert harness.runner.calls == []


class TestEnvelopeSafety:
    def test_the_envelope_is_json_serialisable_and_fixed_shape(self, tmp_path):
        harness = make_harness(tmp_path, tar_files={"a.json": b"{}"})
        harness.open_turn()

        result = harness.handler(python_call())

        assert set(result) == {
            "ok",
            "status",
            "exit_code",
            "stdout",
            "stderr",
            "artifacts",
        }
        json.dumps(result)

    def test_no_host_path_container_id_or_image_id_reaches_the_model(self, tmp_path):
        harness = make_harness(tmp_path, tar_files={"out.csv": b"x"})
        harness.open_turn()

        rendered = json.dumps(harness.handler(python_call()))

        assert FAKE_CONTAINER_ID not in rendered
        assert FAKE_IMAGE_ID not in rendered
        assert str(tmp_path) not in rendered
        assert "/sandbox/" not in rendered
        assert "sha256" not in rendered

    def test_artifact_media_types_are_inferred_by_extension_only(self, tmp_path):
        harness = make_harness(
            tmp_path,
            tar_files={
                "a.csv": b"1",
                "b.json": b"2",
                "c.md": b"3",
                "d.txt": b"4",
                "e.bin": b"5",
                "f": b"6",
            },
        )
        harness.open_turn()

        artifacts = harness.handler(python_call())["artifacts"]

        assert {entry["name"]: entry["media_type"] for entry in artifacts} == {
            "a.csv": "text/csv",
            "b.json": "application/json",
            "c.md": "text/markdown",
            "d.txt": "text/plain",
            "e.bin": "application/octet-stream",
            "f": "application/octet-stream",
        }


class TestTracing:
    def test_the_trace_correlates_the_tool_call_with_the_sandbox_job(self, tmp_path):
        harness = make_harness(tmp_path, tar_files={"out.csv": b"12345"})
        harness.open_turn("turn-corr")

        harness.handler(python_call())

        requested = harness.events("sandbox_tool_requested")[0]
        returned = harness.events("sandbox_tool_result_returned")[0]
        job_finished = harness.events("sandbox_job_finished")[0]

        assert requested["run_id"] == returned["run_id"] == RUN_ID
        assert requested["turn_id"] == returned["turn_id"] == "turn-corr"
        assert requested["sandbox_call_index"] == 1
        # The join between the agent trace and the runtime trace.
        assert returned["sandbox_job_id"] == job_finished["job_id"]

    def test_the_trace_records_sizes_and_status_but_not_content(self, tmp_path):
        harness = make_harness(
            tmp_path,
            exec_result=completed_exec(stdout="SECRET-STDOUT\n"),
            tar_files={"out.csv": b"SECRET-ARTIFACT"},
        )
        harness.open_turn()

        harness.handler(
            python_call(
                "SECRET-SOURCE-MARKER = 1",
                input_files=[{"name": "in.txt", "content": "SECRET-INPUT"}],
            )
        )

        requested = harness.events("sandbox_tool_requested")[0]
        returned = harness.events("sandbox_tool_result_returned")[0]
        assert requested["language"] == "python"
        assert requested["source_bytes"] == 24
        assert returned["status"] == "succeeded"
        assert returned["exit_code"] == 0
        assert returned["artifact_count"] == 1
        assert returned["artifact_total_bytes"] == 15

        text = harness.trace_text()
        for secret in (
            "SECRET-SOURCE-MARKER",
            "SECRET-INPUT",
            "SECRET-ARTIFACT",
        ):
            assert secret not in text
        assert str(tmp_path) not in text

    def test_the_trace_carries_no_docker_command_line(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        harness.handler(python_call())

        text = harness.trace_text()
        assert "--network" not in text
        assert FAKE_CONTAINER_ID not in text

    def test_a_rejected_call_still_emits_a_result_event(self, tmp_path):
        harness = make_harness(tmp_path)
        harness.open_turn()

        harness.handler({"language": "ruby", "source": "x"})

        returned = harness.events("sandbox_tool_result_returned")[0]
        assert returned["status"] == "invalid_request"
        assert returned["sandbox_job_id"] is None

    def test_call_index_increments_within_a_turn_and_resets_between_turns(
        self, tmp_path
    ):
        harness = make_harness(tmp_path)
        harness.open_turn("turn-1")
        harness.handler(python_call("print(1)"))
        harness.handler(python_call("print(2)"))
        harness.open_turn("turn-2")
        harness.handler(python_call("print(3)"))

        indexes = [
            (event["turn_id"], event["sandbox_call_index"])
            for event in harness.events("sandbox_tool_requested")
        ]
        assert indexes == [("turn-1", 1), ("turn-1", 2), ("turn-2", 1)]
