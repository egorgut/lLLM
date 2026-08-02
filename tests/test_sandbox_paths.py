"""Input validation and host materialisation (SPEC-015 §19-20).

The rejection matrix below is the boundary between "a caller supplies bytes and
relative names" and "the host decides where those land". Every case must be a
rejection rather than a sanitisation: silently rewriting a suspicious path would
make the mounted tree differ from what the caller believes it supplied.
"""

import os

import pytest

from sandbox_runtime.models import SandboxJob, SandboxLanguage
from sandbox_runtime.paths import (
    JobRejected,
    materialise_job,
    new_job_dir,
    remove_job_dir,
    validate_input_path,
)
from tests.support_sandbox import make_policy


def materialise(tmp_path, job, **policy_overrides):
    policy = make_policy(tmp_path, **policy_overrides)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    return materialise_job(job, policy, job_dir), job_dir


# -- source ------------------------------------------------------------------


def test_source_is_written_to_the_fixed_filename(tmp_path):
    for language, filename in (
        (SandboxLanguage.PYTHON, "main.py"),
        (SandboxLanguage.BASH, "main.sh"),
    ):
        job_dir = tmp_path / f"job-{language}"
        job_dir.mkdir()
        result = materialise_job(
            SandboxJob(language=language, source="print(1)"),
            make_policy(tmp_path),
            job_dir,
        )
        assert (result.source_dir / filename).read_bytes() == b"print(1)"


@pytest.mark.parametrize("source", ["", "   ", "\n\t "])
def test_empty_source_is_rejected(tmp_path, source):
    with pytest.raises(JobRejected) as error:
        materialise(tmp_path, SandboxJob(language=SandboxLanguage.PYTHON, source=source))
    assert error.value.error_type == "invalid_job"


def test_source_byte_limit_is_enforced(tmp_path):
    with pytest.raises(JobRejected) as error:
        materialise(
            tmp_path,
            SandboxJob(language=SandboxLanguage.PYTHON, source="x" * 500),
            max_source_bytes=100,
        )
    assert error.value.error_type == "source_too_large"


def test_source_limit_counts_bytes_not_characters(tmp_path):
    # Multi-byte source must not slip past a byte budget by being short in
    # characters.
    with pytest.raises(JobRejected):
        materialise(
            tmp_path,
            SandboxJob(language=SandboxLanguage.PYTHON, source="'ы' * 1"  + "ы" * 60),
            max_source_bytes=100,
        )


def test_unsupported_language_is_rejected_before_anything_is_written(tmp_path):
    with pytest.raises(JobRejected) as error:
        materialise(tmp_path, SandboxJob(language="ruby", source="puts 1"))
    assert error.value.error_type == "unsupported_language"


# -- input paths -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/tmp/x",
        "../escape.txt",
        "nested/../../escape.txt",
        "..",
        "dir/../file.txt",
        "./file.txt",
        "a//b.txt",
        "windows\\path.txt",
        "nul\x00byte.txt",
        "",
    ],
)
def test_unsafe_input_paths_are_rejected(path):
    with pytest.raises(JobRejected) as error:
        validate_input_path(path, max_chars=240)
    assert error.value.error_type == "input_path_invalid"


def test_input_path_length_is_bounded():
    with pytest.raises(JobRejected):
        validate_input_path("a" * 300, max_chars=240)


@pytest.mark.parametrize("filename", ["main.py", "main.sh", "nested/main.py"])
def test_input_cannot_use_the_reserved_source_filename(filename):
    with pytest.raises(JobRejected):
        validate_input_path(filename, max_chars=240)


@pytest.mark.parametrize("path", ["data.csv", "nested/data.csv", "a/b/c/d.txt"])
def test_ordinary_relative_paths_are_accepted(path):
    assert validate_input_path(path, max_chars=240) == path


def test_duplicate_normalized_paths_are_rejected(tmp_path):
    # The host filesystem here is case-insensitive, so two paths differing only
    # in case would silently overwrite one another instead of producing two files.
    job = SandboxJob(
        language=SandboxLanguage.PYTHON,
        source="print(1)",
        input_files={"Data.csv": b"a", "data.csv": b"b"},
    )
    with pytest.raises(JobRejected) as error:
        materialise(tmp_path, job)
    assert error.value.error_type == "input_path_invalid"


# -- input bounds ------------------------------------------------------------


def test_input_file_count_is_enforced(tmp_path):
    job = SandboxJob(
        language=SandboxLanguage.PYTHON,
        source="print(1)",
        input_files={f"f{index}.txt": b"x" for index in range(5)},
    )
    with pytest.raises(JobRejected) as error:
        materialise(tmp_path, job, max_input_files=3)
    assert error.value.error_type == "input_file_limit"


def test_per_file_input_limit_is_enforced(tmp_path):
    job = SandboxJob(
        language=SandboxLanguage.PYTHON,
        source="print(1)",
        input_files={"big.bin": b"x" * 500},
    )
    with pytest.raises(JobRejected) as error:
        materialise(tmp_path, job, max_input_file_bytes=100, max_input_total_bytes=1000)
    assert error.value.error_type == "input_size_limit"


def test_total_input_limit_is_enforced(tmp_path):
    job = SandboxJob(
        language=SandboxLanguage.PYTHON,
        source="print(1)",
        input_files={f"f{index}.bin": b"x" * 100 for index in range(5)},
    )
    with pytest.raises(JobRejected) as error:
        materialise(tmp_path, job, max_input_file_bytes=200, max_input_total_bytes=250)
    assert error.value.error_type == "input_size_limit"


def test_non_bytes_input_is_rejected(tmp_path):
    job = SandboxJob(
        language=SandboxLanguage.PYTHON, source="print(1)", input_files={"a.txt": "text"}
    )
    with pytest.raises(JobRejected):
        materialise(tmp_path, job)


# -- materialisation ---------------------------------------------------------


def test_inputs_are_written_as_plain_regular_files(tmp_path):
    job = SandboxJob(
        language=SandboxLanguage.PYTHON,
        source="print(1)",
        input_files={"data.csv": b"a,b\n", "nested/deep.txt": b"deep"},
    )
    result, _ = materialise(tmp_path, job)

    for relative, expected in (("data.csv", b"a,b\n"), ("nested/deep.txt", b"deep")):
        path = result.input_dir / relative
        assert path.read_bytes() == expected
        assert path.is_file() and not path.is_symlink()
        # No executable bit is ever carried over from a caller.
        assert not os.stat(path).st_mode & 0o111

    assert result.input_file_count == 2
    assert result.input_total_bytes == len(b"a,b\n") + len(b"deep")


def test_a_rejected_job_leaves_no_mount_directories(tmp_path):
    # Validation completes before the first byte is written, so a rejection
    # cannot leave a half-built tree that a later step might mount.
    job = SandboxJob(
        language=SandboxLanguage.PYTHON,
        source="print(1)",
        input_files={"../escape": b"x"},
    )
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with pytest.raises(JobRejected):
        materialise_job(job, make_policy(tmp_path), job_dir)
    assert list(job_dir.iterdir()) == []


def test_job_directories_are_private_and_named_only_from_the_job_id(tmp_path):
    job_dir = new_job_dir(tmp_path, "0123-abcd")
    assert job_dir.name == "0123-abcd"
    assert os.stat(job_dir).st_mode & 0o777 == 0o700

    remove_job_dir(job_dir)
    assert not job_dir.exists()
    # Idempotent: cleanup runs on paths that may already be gone.
    remove_job_dir(job_dir)


def test_two_jobs_get_distinct_directories(tmp_path):
    first = new_job_dir(tmp_path, "job-a")
    second = new_job_dir(tmp_path, "job-b")
    assert first != second
    with pytest.raises(FileExistsError):
        new_job_dir(tmp_path, "job-a")
