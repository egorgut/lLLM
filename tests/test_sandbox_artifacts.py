"""Artifact collection from a copied output tree (SPEC-015 §25-26).

The fixtures below stand in for what a job can actually leave in
``/sandbox/output``: ordinary files, nested directories, and the file types a
malicious or careless script might create. Every non-regular entry must be
rejected rather than resolved — a symlink to ``/etc/passwd`` in the collected
tree must never be readable back as an artifact.
"""

import hashlib
import os
import pathlib

import pytest

from sandbox_runtime.artifacts import ArtifactPolicyViolation, collect_artifacts
from tests.support_sandbox import make_policy


def write(root, relative: str, content: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# -- ordinary collection -----------------------------------------------------


def test_regular_files_are_collected_with_size_and_digest(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    write(root, "report.csv", b"name,value\nalpha,42\n")

    artifacts = collect_artifacts(root, make_policy(tmp_path))

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.path == "report.csv"
    assert artifact.content == b"name,value\nalpha,42\n"
    assert artifact.size_bytes == 20
    assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()


def test_artifacts_are_sorted_by_path_and_use_relative_posix_paths(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    for relative in ("zebra.txt", "alpha.txt", "nested/beta.txt", "nested/deep/gamma.txt"):
        write(root, relative, b"x")

    paths = [artifact.path for artifact in collect_artifacts(root, make_policy(tmp_path))]

    assert paths == ["alpha.txt", "nested/beta.txt", "nested/deep/gamma.txt", "zebra.txt"]
    assert all(not path.startswith("/") and str(tmp_path) not in path for path in paths)


def test_directories_are_not_artifacts_and_empty_ones_are_ignored(tmp_path):
    root = tmp_path / "collect"
    (root / "empty/deeper").mkdir(parents=True)
    write(root, "kept.txt", b"x")

    artifacts = collect_artifacts(root, make_policy(tmp_path))
    assert [artifact.path for artifact in artifacts] == ["kept.txt"]


def test_an_empty_output_directory_yields_no_artifacts(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    assert collect_artifacts(root, make_policy(tmp_path)) == ()


# -- unsafe file types -------------------------------------------------------


def test_a_symlink_is_rejected_and_never_followed(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_bytes(b"HOST SECRET")
    os.symlink(secret, root / "link.txt")

    with pytest.raises(ArtifactPolicyViolation) as error:
        collect_artifacts(root, make_policy(tmp_path))
    assert "symlink" in str(error.value)


def test_a_symlinked_directory_is_rejected_rather_than_descended(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"HOST SECRET")
    os.symlink(outside, root / "escape")

    with pytest.raises(ArtifactPolicyViolation):
        collect_artifacts(root, make_policy(tmp_path))


def test_a_fifo_is_rejected(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    os.mkfifo(root / "pipe")

    with pytest.raises(ArtifactPolicyViolation) as error:
        collect_artifacts(root, make_policy(tmp_path))
    assert "FIFO" in str(error.value)


def test_a_socket_is_rejected(tmp_path):
    import shutil
    import socket
    import tempfile

    # A short root: macOS caps an AF_UNIX path at ~104 characters, which
    # pytest's own tmp_path already exceeds.
    root = pathlib.Path(tempfile.mkdtemp(prefix="sbx", dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(root / "s"))
        with pytest.raises(ArtifactPolicyViolation) as error:
            collect_artifacts(root, make_policy(tmp_path))
        assert "socket" in str(error.value)
    finally:
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_a_hard_link_is_rejected(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    write(root, "original.txt", b"x")
    os.link(root / "original.txt", root / "clone.txt")

    with pytest.raises(ArtifactPolicyViolation) as error:
        collect_artifacts(root, make_policy(tmp_path))
    assert "hard link" in str(error.value)


# -- bounds ------------------------------------------------------------------


def test_artifact_count_limit_is_enforced(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    for index in range(5):
        write(root, f"file{index}.txt", b"x")

    with pytest.raises(ArtifactPolicyViolation):
        collect_artifacts(root, make_policy(tmp_path, max_artifact_files=3))


def test_per_file_artifact_limit_is_enforced(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    write(root, "big.bin", b"x" * 500)

    with pytest.raises(ArtifactPolicyViolation):
        collect_artifacts(
            root, make_policy(tmp_path, max_artifact_file_bytes=100, max_artifact_total_bytes=1000)
        )


def test_total_artifact_limit_is_enforced(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    for index in range(5):
        write(root, f"file{index}.bin", b"x" * 100)

    with pytest.raises(ArtifactPolicyViolation):
        collect_artifacts(
            root, make_policy(tmp_path, max_artifact_file_bytes=200, max_artifact_total_bytes=250)
        )


def test_artifact_path_length_is_bounded(tmp_path):
    root = tmp_path / "collect"
    root.mkdir()
    write(root, "a" * 120 + "/" + "b" * 120, b"x")

    with pytest.raises(ArtifactPolicyViolation):
        collect_artifacts(root, make_policy(tmp_path, max_artifact_path_chars=64))


def test_a_violation_returns_no_partial_set(tmp_path):
    # A bounded subset is indistinguishable from a complete result to a caller,
    # so a violation must yield nothing at all rather than the files seen so far.
    root = tmp_path / "collect"
    root.mkdir()
    write(root, "good.txt", b"x")
    os.symlink(tmp_path / "elsewhere", root / "zz-link")

    with pytest.raises(ArtifactPolicyViolation):
        collect_artifacts(root, make_policy(tmp_path))
