"""Host-owned policy validation (SPEC-015 "Configuration validation").

Every case here is a *deployment* defect rather than a job outcome, so the
expected behavior is a plain ``ValueError`` before anything reaches Docker —
matching how ``reliability.validate_reliability_config`` treats an incoherent
agent configuration.
"""

import pytest

from sandbox_runtime.models import SandboxLanguage
from sandbox_runtime.policy import (
    DEFAULT_ENVIRONMENT,
    OUTPUT_MOUNT,
    SOURCE_MOUNT,
    command_for,
    default_policy,
    policy_fingerprint,
    source_filename_for,
    validate_sandbox_policy,
)
from tests.support_sandbox import make_policy

TOOL_TIMEOUT = 30


def validate(policy) -> None:
    validate_sandbox_policy(policy, tool_execution_timeout_seconds=TOOL_TIMEOUT)


def test_the_default_policy_is_valid(tmp_path):
    # The committed config.py values must themselves satisfy every rule.
    validate(default_policy())


def test_a_realistic_test_policy_is_valid(tmp_path):
    validate(make_policy(tmp_path))


# -- image reference ---------------------------------------------------------


@pytest.mark.parametrize("image_ref", ["", "   ", "lllm-sandbox", "lllm-sandbox:latest"])
def test_mutable_or_missing_image_references_are_rejected(tmp_path, image_ref):
    # A `latest` tag is exactly the mutability the runtime exists to avoid.
    with pytest.raises(ValueError):
        validate(make_policy(tmp_path, image_ref=image_ref))


# -- timeouts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["execution_timeout_seconds", "docker_control_timeout_seconds", "cleanup_timeout_seconds"]
)
def test_non_positive_timeouts_are_rejected(tmp_path, field):
    with pytest.raises(ValueError):
        validate(make_policy(tmp_path, **{field: 0}))


def test_execution_plus_cleanup_must_fit_inside_the_outer_tool_timeout(tmp_path):
    # Otherwise SPEC-016's caller-side deadline fires first and abandons a live
    # container — the exact failure mode this runtime exists to prevent (§16).
    with pytest.raises(ValueError):
        validate(make_policy(tmp_path, execution_timeout_seconds=28, cleanup_timeout_seconds=5))

    validate(make_policy(tmp_path, execution_timeout_seconds=20, cleanup_timeout_seconds=5))


def test_committed_defaults_leave_cleanup_room(tmp_path):
    policy = default_policy()
    assert policy.execution_timeout_seconds + policy.cleanup_timeout_seconds < TOOL_TIMEOUT


# -- resource limits ---------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_bytes": 32 * 1024 * 1024},
        {"cpus": 0},
        {"cpus": -1.0},
        {"pids_limit": 4},
        {"nofile_limit": 8},
        {"tmp_bytes": 0},
        {"output_tmpfs_bytes": 0},
        {"max_source_bytes": 0},
        {"max_stdout_bytes": 0},
        {"max_stderr_bytes": 0},
        {"max_artifact_files": 0},
        {"max_artifact_path_chars": 8},
    ],
)
def test_incoherent_resource_limits_are_rejected(tmp_path, overrides):
    with pytest.raises(ValueError):
        validate(make_policy(tmp_path, **overrides))


def test_a_per_file_limit_above_its_total_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        validate(make_policy(tmp_path, max_input_file_bytes=3_000_000, max_input_total_bytes=2_000_000))
    with pytest.raises(ValueError):
        validate(
            make_policy(
                tmp_path,
                max_artifact_file_bytes=6_000_000,
                max_artifact_total_bytes=5_000_000,
                output_tmpfs_bytes=8 * 1024 * 1024,
            )
        )


def test_artifact_total_above_the_output_tmpfs_is_rejected(tmp_path):
    # A bound the container physically cannot reach is a bound that is never
    # actually enforced; advertising it would be misleading.
    with pytest.raises(ValueError):
        validate(
            make_policy(
                tmp_path,
                output_tmpfs_bytes=4 * 1024 * 1024,
                max_artifact_total_bytes=5_000_000,
            )
        )


# -- fixed environment -------------------------------------------------------


@pytest.mark.parametrize(
    "environment",
    [
        {"LD_PRELOAD": "/tmp/x.so"},
        {"PYTHONPATH": "/sandbox/input"},
        {"BASH_ENV": "/sandbox/input/rc"},
        {"HTTPS_PROXY": "http://proxy:3128"},
        {"TRACKER_TOKEN": "secret"},
        {"DOCKER_HOST": "unix:///var/run/docker.sock"},
        {"BAD KEY": "x"},
        {"HOME": "/sandbox/output\nLD_PRELOAD=/tmp/x.so"},
    ],
)
def test_unsafe_fixed_environment_entries_are_rejected(tmp_path, environment):
    with pytest.raises(ValueError):
        validate(make_policy(tmp_path, environment=environment))


def test_the_default_environment_forwards_nothing_from_the_host():
    # Built from scratch: no host path, no proxy, no credential, no Ollama or
    # Docker variable can appear because the set is a fixed literal (§18).
    assert set(DEFAULT_ENVIRONMENT) == {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
    }
    assert DEFAULT_ENVIRONMENT["HOME"] == OUTPUT_MOUNT


def test_policy_environment_is_read_only(tmp_path):
    policy = make_policy(tmp_path)
    with pytest.raises(TypeError):
        policy.environment["LD_PRELOAD"] = "/tmp/x.so"


# -- fixed commands ----------------------------------------------------------


def test_python_command_is_exact():
    assert command_for(SandboxLanguage.PYTHON) == (
        "python3",
        "-I",
        "-B",
        f"{SOURCE_MOUNT}/main.py",
    )


def test_bash_command_is_exact():
    assert command_for(SandboxLanguage.BASH) == (
        "/bin/bash",
        "--noprofile",
        "--norc",
        f"{SOURCE_MOUNT}/main.sh",
    )


def test_no_command_passes_source_as_an_inline_argument():
    # `-c` would make model text a command-line argument instead of a file the
    # host wrote and mounted read-only.
    for language in SandboxLanguage:
        assert "-c" not in command_for(language)


def test_unsupported_language_has_no_command_or_filename():
    with pytest.raises(ValueError):
        command_for("ruby")
    with pytest.raises(ValueError):
        source_filename_for("ruby")


# -- fingerprint -------------------------------------------------------------


def test_fingerprint_is_stable_and_reflects_effective_limits(tmp_path):
    policy = make_policy(tmp_path)
    assert policy_fingerprint(policy) == policy_fingerprint(make_policy(tmp_path))
    assert policy_fingerprint(policy) != policy_fingerprint(
        make_policy(tmp_path, memory_bytes=512 * 1024 * 1024)
    )


def test_fingerprint_ignores_host_paths(tmp_path):
    # Fingerprints are written to traces, and traces must not carry host paths.
    other_root = tmp_path / "elsewhere"
    assert policy_fingerprint(make_policy(tmp_path)) == policy_fingerprint(
        make_policy(other_root)
    )
