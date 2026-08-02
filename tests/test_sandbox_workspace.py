"""Turn-scoped workspace and artifact publication (SPEC-016 §9, §17, §21.1).

The rule this file exists to prove: a turn the user never got an answer from
leaves nothing behind, and a turn that succeeded leaves exactly what it said it
did — under a path no other turn can reach.

The collision policy is tested rather than assumed because §9.4 permits two
designs and this one was chosen: a flat per-turn directory where the first
artifact keeps its name and later ones take `-2`, `-3`. That is only safe if a
second call can never silently overwrite the first call's file, so both the
naming and the `O_EXCL` write are covered.
"""

import pytest

from reliability import TurnContext
from sandbox_runtime.models import SandboxArtifact
from sandbox_tool.artifacts import (
    ArtifactPublicationError,
    media_type_for,
    unique_relative_path,
)
from sandbox_tool.workspace import TurnWorkspace
from support import FakeClock
from support_sandbox_tool import RUN_ID, make_harness, python_call
from support_sandbox import completed_exec
from tracing import MemoryTraceSink


def artifact(path: str, content: bytes = b"data") -> SandboxArtifact:
    return SandboxArtifact(
        path=path, size_bytes=len(content), sha256="0" * 64, content=content
    )


@pytest.fixture
def workspace(tmp_path):
    return TurnWorkspace(
        run_id=RUN_ID,
        artifact_root=tmp_path / "artifacts",
        project_root=tmp_path,
        trace_sink=MemoryTraceSink(),
        clock=FakeClock(),
    )


def open_turn(workspace, turn_id="turn-1", *, remaining=120.0):
    context = TurnContext(RUN_ID, turn_id, 0.0, remaining)
    workspace.begin_turn(context)
    return context


class TestNaming:
    def test_first_artifact_keeps_its_name(self):
        assert unique_relative_path("report.csv", set()) == "report.csv"

    def test_collisions_take_a_deterministic_suffix(self):
        taken = {"report.csv"}
        assert unique_relative_path("report.csv", taken) == "report-2.csv"
        taken.add("report-2.csv")
        assert unique_relative_path("report.csv", taken) == "report-3.csv"

    def test_a_name_without_an_extension_still_gets_a_suffix(self):
        assert unique_relative_path("report", {"report"}) == "report-2"

    def test_a_nested_path_keeps_its_directory(self):
        assert (
            unique_relative_path("sub/dir/report.csv", {"sub/dir/report.csv"})
            == "sub/dir/report-2.csv"
        )

    def test_a_dotfile_is_treated_as_a_name_not_an_extension(self):
        assert unique_relative_path(".gitignore", {".gitignore"}) == ".gitignore-2"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("a.csv", "text/csv"),
            ("a.CSV", "text/csv"),
            ("a.json", "application/json"),
            ("a.md", "text/markdown"),
            ("a.txt", "text/plain"),
            ("a.bin", "application/octet-stream"),
            ("a", "application/octet-stream"),
            ("a.csv.gz", "application/octet-stream"),
        ],
    )
    def test_media_types_are_inferred_by_extension_only(self, name, expected):
        assert media_type_for(name) == expected


class TestPublication:
    def test_publishing_writes_the_files_and_returns_metadata(self, workspace, tmp_path):
        open_turn(workspace)

        entries = workspace.publish(
            (artifact("out.csv", b"a,b\n"),), generation=workspace.generation, job_id="j1"
        )

        assert entries == [
            {
                "name": "out.csv",
                "media_type": "text/csv",
                "size_bytes": 4,
                "path": f"artifacts/{RUN_ID}/turn-1/out.csv",
            }
        ]
        assert (tmp_path / "artifacts" / RUN_ID / "turn-1" / "out.csv").read_bytes() == b"a,b\n"

    def test_the_directory_is_created_lazily(self, workspace, tmp_path):
        open_turn(workspace)
        assert not (tmp_path / "artifacts").exists()

        workspace.publish((), generation=workspace.generation, job_id="j1")
        assert not (tmp_path / "artifacts").exists()

        workspace.publish(
            (artifact("a.txt"),), generation=workspace.generation, job_id="j2"
        )
        assert (tmp_path / "artifacts" / RUN_ID / "turn-1").is_dir()

    def test_two_calls_in_one_turn_coexist_without_overwriting(
        self, workspace, tmp_path
    ):
        open_turn(workspace)
        generation = workspace.generation

        first = workspace.publish(
            (artifact("report.csv", b"first"),), generation=generation, job_id="j1"
        )
        second = workspace.publish(
            (artifact("report.csv", b"second"),), generation=generation, job_id="j2"
        )

        assert first[0]["name"] == "report.csv"
        assert second[0]["name"] == "report-2.csv"
        turn_dir = tmp_path / "artifacts" / RUN_ID / "turn-1"
        assert (turn_dir / "report.csv").read_bytes() == b"first"
        assert (turn_dir / "report-2.csv").read_bytes() == b"second"

    def test_nested_artifact_paths_are_preserved(self, workspace, tmp_path):
        open_turn(workspace)

        entries = workspace.publish(
            (artifact("sub/dir/out.txt", b"x"),),
            generation=workspace.generation,
            job_id="j1",
        )

        assert entries[0]["name"] == "sub/dir/out.txt"
        assert (
            tmp_path / "artifacts" / RUN_ID / "turn-1" / "sub" / "dir" / "out.txt"
        ).exists()

    def test_a_path_escaping_the_turn_directory_is_refused(self, workspace):
        """Defence in depth: SPEC-015 already rejects such a path upstream."""

        open_turn(workspace)

        with pytest.raises(ArtifactPublicationError):
            workspace.publish(
                (artifact("../escaped.txt"),),
                generation=workspace.generation,
                job_id="j1",
            )

    def test_a_failed_write_leaves_no_partial_output(self, workspace, tmp_path):
        open_turn(workspace)

        with pytest.raises(ArtifactPublicationError):
            workspace.publish(
                (artifact("good.txt", b"kept?"), artifact("../bad.txt")),
                generation=workspace.generation,
                job_id="j1",
            )

        turn_dir = tmp_path / "artifacts" / RUN_ID / "turn-1"
        assert list(turn_dir.iterdir()) == []

    def test_a_result_from_an_ended_turn_is_never_written(self, workspace, tmp_path):
        """An abandoned worker thread cannot write into a turn that is over."""

        open_turn(workspace, "turn-1")
        stale_generation = workspace.generation
        open_turn(workspace, "turn-2")

        with pytest.raises(ArtifactPublicationError):
            workspace.publish(
                (artifact("late.txt"),), generation=stale_generation, job_id="j1"
            )
        assert not (tmp_path / "artifacts" / RUN_ID / "turn-1").exists()
        assert not (tmp_path / "artifacts" / RUN_ID / "turn-2").exists()

    def test_publishing_without_an_active_turn_is_refused(self, workspace):
        with pytest.raises(ArtifactPublicationError):
            workspace.publish((artifact("a.txt"),), generation=0, job_id="j1")


class TestCommitAndRollback:
    def test_commit_keeps_the_files(self, workspace, tmp_path):
        open_turn(workspace)
        workspace.publish(
            (artifact("kept.csv"),), generation=workspace.generation, job_id="j1"
        )

        workspace.commit()

        assert (tmp_path / "artifacts" / RUN_ID / "turn-1" / "kept.csv").exists()

    def test_rollback_removes_the_whole_turn_directory(self, workspace, tmp_path):
        open_turn(workspace)
        workspace.publish(
            (artifact("gone.csv"),), generation=workspace.generation, job_id="j1"
        )

        workspace.rollback()

        assert not (tmp_path / "artifacts" / RUN_ID / "turn-1").exists()
        # The now-empty run directory is tidied up too.
        assert not (tmp_path / "artifacts" / RUN_ID).exists()

    def test_rollback_only_touches_its_own_turn(self, workspace, tmp_path):
        open_turn(workspace, "turn-committed")
        workspace.publish(
            (artifact("keep.csv"),), generation=workspace.generation, job_id="j1"
        )
        workspace.commit()

        open_turn(workspace, "turn-failed")
        workspace.publish(
            (artifact("drop.csv"),), generation=workspace.generation, job_id="j2"
        )
        workspace.rollback()

        root = tmp_path / "artifacts" / RUN_ID
        assert (root / "turn-committed" / "keep.csv").exists()
        assert not (root / "turn-failed").exists()

    def test_rollback_without_an_active_turn_is_a_no_op(self, workspace):
        workspace.rollback()
        workspace.rollback()

    def test_beginning_a_turn_discards_an_undecided_previous_turn(
        self, workspace, tmp_path
    ):
        open_turn(workspace, "turn-abandoned")
        workspace.publish(
            (artifact("orphan.csv"),), generation=workspace.generation, job_id="j1"
        )

        open_turn(workspace, "turn-next")

        assert not (tmp_path / "artifacts" / RUN_ID / "turn-abandoned").exists()

    def test_commit_and_rollback_are_traced(self, tmp_path):
        sink = MemoryTraceSink()
        workspace = TurnWorkspace(
            run_id=RUN_ID,
            artifact_root=tmp_path / "artifacts",
            project_root=tmp_path,
            trace_sink=sink,
        )
        open_turn(workspace, "t1")
        workspace.publish(
            (artifact("a.csv", b"12345"),), generation=workspace.generation, job_id="j1"
        )
        workspace.commit()
        open_turn(workspace, "t2")
        workspace.publish(
            (artifact("b.csv"),), generation=workspace.generation, job_id="j2"
        )
        workspace.rollback()

        names = [event["event"] for event in sink.events]
        assert names == [
            "sandbox_artifacts_staged",
            "sandbox_artifacts_committed",
            "sandbox_artifacts_staged",
            "sandbox_artifacts_rolled_back",
        ]
        staged = sink.events[0]
        assert staged["job_id"] == "j1"
        assert staged["artifact_count"] == 1
        assert staged["artifact_total_bytes"] == 5
        assert sink.events[1]["turn_id"] == "t1"
        assert sink.events[3]["turn_id"] == "t2"

    def test_a_turn_with_no_artifacts_emits_nothing(self, tmp_path):
        sink = MemoryTraceSink()
        workspace = TurnWorkspace(
            run_id=RUN_ID,
            artifact_root=tmp_path / "artifacts",
            project_root=tmp_path,
            trace_sink=sink,
        )
        open_turn(workspace)
        workspace.commit()

        assert sink.events == []


class TestTurnIsolation:
    def test_turns_write_into_separate_directories(self, tmp_path):
        harness = make_harness(
            tmp_path,
            exec_result=completed_exec(),
            tar_files={"shared-name.csv": b"turn data"},
        )

        harness.open_turn("turn-a")
        first = harness.handler(python_call())
        harness.workspace.commit()

        harness.open_turn("turn-b")
        second = harness.handler(python_call())
        harness.workspace.commit()

        assert first["artifacts"][0]["path"] != second["artifacts"][0]["path"]
        assert harness.turn_dir("turn-a").is_dir()
        assert harness.turn_dir("turn-b").is_dir()

    def test_a_later_turn_cannot_name_an_earlier_turns_directory(self, tmp_path):
        """The model supplies no identifier that reaches a workspace path."""

        harness = make_harness(tmp_path, tar_files={"out.csv": b"x"})
        harness.open_turn("turn-a")
        harness.handler(python_call())
        harness.workspace.commit()

        harness.open_turn("turn-b")
        result = harness.handler(
            python_call(
                input_files=[
                    {"name": "steal.txt", "content": f"artifacts/{RUN_ID}/turn-a"}
                ]
            )
        )

        # The path text is inert data inside an input file; it selects nothing.
        # Turn B's own output lands under turn B, and turn A is untouched.
        assert result["ok"] is True
        assert result["artifacts"][0]["path"].endswith(f"{RUN_ID}/turn-b/out.csv")
        assert (harness.turn_dir("turn-a") / "out.csv").read_bytes() == b"x"
        assert sorted(
            path.name for path in harness.turn_dir("turn-b").iterdir()
        ) == ["out.csv"]

    def test_the_job_scratch_directory_is_gone_after_the_call(self, tmp_path):
        harness = make_harness(tmp_path, tar_files={"out.csv": b"x"})
        harness.open_turn()

        harness.handler(python_call("print('x')"))

        scratch = tmp_path / "sandbox-tmp"
        assert list(scratch.iterdir()) == [], "SPEC-015 removes every job directory"
