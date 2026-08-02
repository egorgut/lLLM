"""Live Docker tests for the sandbox tool (SPEC-016 §23).

Skipped by default. SPEC-015's live suite proves the *runtime* isolates a job;
this one proves the *tool* on top of it behaves as the model is told it does:
that a real script's real output file reaches a real path on disk, that a
failure publishes nothing, and that the isolation claims printed in the skill
instruction ("no network", "no host filesystem") survive contact with a real
container.

Run them explicitly, after building the image:

    python scripts/build_sandbox_image.py
    LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_tool_integration.py -q

Like the SPEC-015 suite, every test asserts that no container and no job
directory survives it.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from config import SANDBOX_TEMP_ROOT
from reliability import TurnContext, new_id
from sandbox_runtime.docker_backend import DockerSandboxRuntime
from sandbox_runtime.policy import default_policy
from sandbox_tool.handler import create_sandbox_execute_handler
from sandbox_tool.workspace import TurnWorkspace

pytestmark = pytest.mark.skipif(
    os.environ.get("LLLM_SANDBOX_LIVE") != "1",
    reason="Live Docker sandbox tests are opt-in: set LLLM_SANDBOX_LIVE=1.",
)


@dataclass
class LiveTool:
    """One wired handler plus the turn state a test needs to inspect."""

    call: Callable[[dict[str, Any]], dict[str, Any]]
    workspace: TurnWorkspace
    turn_dir: Path

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.call(arguments)


@pytest.fixture
def tool(tmp_path):
    """A real handler over a real runtime, publishing into a temp artifact root."""

    run_id = new_id()
    policy = default_policy()
    workspace = TurnWorkspace(
        run_id=run_id,
        artifact_root=tmp_path / "artifacts",
        project_root=tmp_path,
    )
    now = time.monotonic()
    workspace.begin_turn(TurnContext(run_id, "live-turn", now, now + 120))
    handler = create_sandbox_execute_handler(
        runtime=DockerSandboxRuntime(run_id=run_id, policy=policy),
        policy=policy,
        workspace=workspace,
        turn_time_margin_seconds=2,
        run_id=run_id,
    )
    return LiveTool(
        call=handler,
        workspace=workspace,
        turn_dir=tmp_path / "artifacts" / run_id / "live-turn",
    )


@pytest.fixture(autouse=True)
def _nothing_leaks():
    yield
    leaked = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", "label=lllm.sandbox=true"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert leaked == "", f"sandbox containers leaked: {leaked}"
    assert list(SANDBOX_TEMP_ROOT.iterdir()) == [], "job scratch directories leaked"


def test_python_produces_a_real_artifact_on_disk(tool):
    result = tool(
        {
            "language": "python",
            "source": (
                "import csv\n"
                "with open('/sandbox/output/squares.csv', 'w', newline='') as handle:\n"
                "    writer = csv.writer(handle)\n"
                "    writer.writerow(['n', 'square'])\n"
                "    for n in range(1, 6):\n"
                "        writer.writerow([n, n * n])\n"
                "print('wrote squares.csv')\n"
            ),
        }
    )

    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert result["exit_code"] == 0
    assert result["stdout"] == "wrote squares.csv\n"
    assert result["artifacts"][0]["name"] == "squares.csv"
    assert result["artifacts"][0]["media_type"] == "text/csv"
    published = tool.turn_dir / "squares.csv"
    assert published.read_text().splitlines()[0] == "n,square"
    assert published.read_text().splitlines()[-1] == "5,25"


def test_bash_reads_supplied_input_and_writes_output(tool):
    result = tool(
        {
            "language": "bash",
            "source": (
                "wc -l < /sandbox/input/access.log > /sandbox/output/count.txt\n"
                "cat /sandbox/output/count.txt\n"
            ),
            "input_files": [{"name": "access.log", "content": "a\nb\nc\n"}],
        }
    )

    assert result["ok"] is True
    assert result["stdout"].strip() == "3"
    assert (tool.turn_dir / "count.txt").read_text().strip() == "3"


def test_a_failing_script_publishes_nothing(tool):
    result = tool(
        {
            "language": "python",
            "source": (
                "open('/sandbox/output/partial.csv', 'w').write('junk')\n"
                "raise SystemExit(3)\n"
            ),
        }
    )

    assert result["ok"] is False
    assert result["status"] == "non_zero_exit"
    assert result["exit_code"] == 3
    assert result["artifacts"] == []
    assert not tool.turn_dir.exists()


def test_an_endless_script_times_out_and_publishes_nothing(tool):
    result = tool(
        {
            "language": "python",
            "source": (
                "open('/sandbox/output/partial.csv', 'w').write('junk')\n"
                "while True:\n"
                "    pass\n"
            ),
        }
    )

    assert result["status"] == "timed_out"
    assert result["exit_code"] is None
    assert result["artifacts"] == []
    assert not tool.turn_dir.exists()


def test_the_script_cannot_reach_the_network(tool):
    result = tool(
        {
            "language": "python",
            "source": (
                "import socket\n"
                "socket.setdefaulttimeout(3)\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 53))\n"
                "    print('CONNECTED')\n"
                "except OSError as error:\n"
                "    print('BLOCKED')\n"
            ),
        }
    )

    assert result["stdout"].strip() == "BLOCKED"


def test_the_script_cannot_read_the_host_filesystem(tool, tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("do not read me")

    result = tool(
        {
            "language": "python",
            "source": (
                "import os\n"
                f"print(os.path.exists({str(secret)!r}))\n"
                "print(sorted(os.listdir('/sandbox/input')))\n"
            ),
        }
    )

    assert result["stdout"].splitlines()[0] == "False"
    assert result["stdout"].splitlines()[1] == "[]"


def test_the_model_cannot_escape_the_output_directory(tool):
    """A script writing outside /sandbox/output produces no artifact."""

    result = tool(
        {
            "language": "python",
            "source": (
                "import os\n"
                "try:\n"
                "    open('/sandbox/source/injected.py', 'w').write('x')\n"
                "    print('WROTE-SOURCE')\n"
                "except OSError:\n"
                "    print('READ-ONLY')\n"
            ),
        }
    )

    assert result["stdout"].strip() == "READ-ONLY"
    assert result["artifacts"] == []


def test_two_calls_in_one_turn_do_not_overwrite_each_other(tool):
    source = (
        "with open('/sandbox/output/report.csv', 'w') as handle:\n"
        "    handle.write({value!r})\n"
    )

    first = tool({"language": "python", "source": source.format(value="first")})
    second = tool({"language": "python", "source": source.format(value="second")})

    assert first["artifacts"][0]["name"] == "report.csv"
    assert second["artifacts"][0]["name"] == "report-2.csv"
    assert (tool.turn_dir / "report.csv").read_text() == "first"
    assert (tool.turn_dir / "report-2.csv").read_text() == "second"


def test_a_rolled_back_turn_leaves_no_files(tool):
    tool({"language": "python", "source": "open('/sandbox/output/a.txt','w').write('x')\n"})
    assert tool.turn_dir.exists()

    tool.workspace.rollback()

    assert not tool.turn_dir.exists()
