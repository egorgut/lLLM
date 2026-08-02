"""Immutability and invariants of the sandbox contracts (SPEC-015 "Data model").

Pure data tests: no Docker, no filesystem, no policy. The point of each one is
that a defect in a caller — or in the backend — surfaces as an immediate error
rather than as a plausible-looking wrong result.
"""

import dataclasses

import pytest

from sandbox_runtime.models import (
    SandboxArtifact,
    SandboxJob,
    SandboxLanguage,
    SandboxResult,
    SandboxStatus,
)


def make_result(**overrides) -> SandboxResult:
    defaults = dict(
        job_id="job-1",
        status=SandboxStatus.COMPLETED,
        language=SandboxLanguage.PYTHON,
        image_id="sha256:abc",
        exit_code=0,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        artifacts=(),
        duration_ms=5,
    )
    defaults.update(overrides)
    return SandboxResult(**defaults)


# -- SandboxJob --------------------------------------------------------------


def test_job_defensively_copies_and_freezes_input_files():
    supplied = {"data.csv": b"a,b\n"}
    job = SandboxJob(language=SandboxLanguage.PYTHON, source="print(1)", input_files=supplied)

    # Mutating the original dict after construction must not change the job.
    supplied["extra.txt"] = b"injected"
    assert dict(job.input_files) == {"data.csv": b"a,b\n"}

    with pytest.raises(TypeError):
        job.input_files["another.txt"] = b"x"


def test_job_itself_is_frozen():
    job = SandboxJob(language=SandboxLanguage.BASH, source="echo hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.source = "echo other"


def test_job_carries_no_policy_fields():
    # The model can never reach a limit, an image, a mount, or a command,
    # because no such field exists on the request object (§"SandboxJob").
    field_names = {field.name for field in dataclasses.fields(SandboxJob)}
    assert field_names == {"language", "source", "input_files"}


# -- SandboxArtifact ---------------------------------------------------------


def test_artifact_repr_excludes_content():
    artifact = SandboxArtifact(
        path="report.csv", size_bytes=5, sha256="deadbeef", content=b"hello"
    )
    assert "hello" not in repr(artifact)
    assert "report.csv" in repr(artifact)


def test_artifact_size_must_match_content():
    with pytest.raises(ValueError):
        SandboxArtifact(path="a.txt", size_bytes=99, sha256="x", content=b"hi")


# -- SandboxResult invariants ------------------------------------------------


def test_completed_result_requires_zero_exit_and_no_error():
    with pytest.raises(ValueError):
        make_result(status=SandboxStatus.COMPLETED, exit_code=1)
    with pytest.raises(ValueError):
        make_result(status=SandboxStatus.COMPLETED, error_type="nonzero_exit")


def test_only_a_completed_job_may_carry_artifacts():
    artifact = SandboxArtifact(path="a.txt", size_bytes=1, sha256="x", content=b"a")
    for status in (
        SandboxStatus.FAILED,
        SandboxStatus.TIMED_OUT,
        SandboxStatus.STOPPED,
        SandboxStatus.REJECTED,
    ):
        with pytest.raises(ValueError):
            make_result(
                status=status,
                exit_code=1 if status is SandboxStatus.FAILED else None,
                artifacts=(artifact,),
            )


def test_failed_result_requires_a_non_zero_exit_code():
    with pytest.raises(ValueError):
        make_result(status=SandboxStatus.FAILED, exit_code=0)


def test_timed_out_result_has_no_exit_code():
    # A process the host killed has no meaningful exit status to report.
    with pytest.raises(ValueError):
        make_result(status=SandboxStatus.TIMED_OUT, exit_code=137)
    assert (
        make_result(
            status=SandboxStatus.TIMED_OUT, exit_code=None, error_type="execution_timeout"
        ).exit_code
        is None
    )


def test_rejected_result_may_have_no_image_id():
    result = make_result(
        status=SandboxStatus.REJECTED,
        exit_code=None,
        image_id=None,
        error_type="invalid_job",
    )
    assert result.image_id is None
    assert result.artifacts == ()
