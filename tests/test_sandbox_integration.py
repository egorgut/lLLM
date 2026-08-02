"""Live Docker integration tests for the sandbox runtime (SPEC-015 "Testing").

Skipped by default. These are the only tests that prove the isolation claims are
real rather than merely present in an argument vector — a flag can be passed and
still not do what it says, so each scenario below asks the *container* what it
can actually see and do.

Run them explicitly, after building the image:

    python scripts/build_sandbox_image.py
    LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_integration.py -q

Every test asserts that no container survives it. If one ever does, the whole
premise of hard termination is wrong and that must fail loudly.
"""

import os
import subprocess
import uuid

import pytest

from reliability import new_id
from sandbox_runtime import DockerSandboxRuntime, SandboxJob, SandboxLanguage, SandboxStatus
from sandbox_runtime.policy import JOB_UID

pytestmark = pytest.mark.skipif(
    os.environ.get("LLLM_SANDBOX_LIVE") != "1",
    reason="Live Docker sandbox tests are opt-in: set LLLM_SANDBOX_LIVE=1.",
)


@pytest.fixture
def runtime():
    return DockerSandboxRuntime(run_id=new_id())


def python(source: str, **kwargs) -> SandboxJob:
    return SandboxJob(language=SandboxLanguage.PYTHON, source=source, **kwargs)


def bash(source: str, **kwargs) -> SandboxJob:
    return SandboxJob(language=SandboxLanguage.BASH, source=source, **kwargs)


def containers_for(job_id: str) -> str:
    """Any container still carrying this job's label, running or not."""

    return subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=lllm.sandbox.job_id={job_id}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _no_container_survives(request):
    """Scenario U, applied after every scenario rather than as one test."""

    yield
    leaked = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", "label=lllm.sandbox=true"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert leaked == "", f"sandbox containers leaked: {leaked}"


# -- A/B/C: the jobs actually run --------------------------------------------


def test_python_job_succeeds(runtime):
    result = runtime.execute(python("print(sum(range(100)))"))

    assert result.status is SandboxStatus.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == "4950\n"
    assert result.stderr == ""
    assert result.artifacts == ()
    assert result.image_id.startswith("sha256:")
    assert containers_for(result.job_id) == ""


def test_bash_job_succeeds(runtime):
    result = runtime.execute(bash('printf "done\n"'))

    assert result.status is SandboxStatus.COMPLETED
    assert result.stdout == "done\n"


def test_a_job_returns_a_bounded_artifact(runtime):
    import hashlib

    result = runtime.execute(
        bash('printf "name,value\nalpha,42\n" > report.csv\nprintf "done\n"')
    )

    assert result.status is SandboxStatus.COMPLETED
    assert result.stdout == "done\n"
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.path == "report.csv"
    assert artifact.content == b"name,value\nalpha,42\n"
    assert artifact.size_bytes == len(artifact.content)
    assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()


# -- D/E: filesystem boundaries ----------------------------------------------


def test_input_files_are_readable_but_not_writable(runtime):
    result = runtime.execute(
        python(
            "print(open('/sandbox/input/data.csv').read().strip())\n"
            "try:\n"
            "    open('/sandbox/input/data.csv', 'w').write('tampered')\n"
            "    print('WRITE-SUCCEEDED')\n"
            "except OSError as error:\n"
            "    print('write-denied')\n",
            input_files={"data.csv": b"alpha,42\n"},
        )
    )

    assert result.status is SandboxStatus.COMPLETED
    assert "alpha,42" in result.stdout
    assert "write-denied" in result.stdout
    assert "WRITE-SUCCEEDED" not in result.stdout


def test_the_root_filesystem_is_read_only(runtime):
    result = runtime.execute(
        python(
            "try:\n"
            "    open('/etc/lllm-probe', 'w').write('x')\n"
            "    print('root_write_allowed=true')\n"
            "except OSError:\n"
            "    print('root_write_allowed=false')\n"
        )
    )

    assert result.stdout.strip() == "root_write_allowed=false"


# -- F/G/H/J: host isolation --------------------------------------------------


def test_a_host_file_outside_the_mounts_is_invisible(runtime, tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-SECRET-CONTENT")

    result = runtime.execute(
        python(
            "import os\n"
            f"print('host_secret_file_visible=' + str(os.path.exists({str(secret)!r})))\n"
        )
    )

    assert result.stdout.strip() == "host_secret_file_visible=False"


def test_host_environment_is_not_forwarded(runtime, monkeypatch):
    marker = f"HOST-ONLY-{uuid.uuid4().hex}"
    monkeypatch.setenv("LLLM_HOST_SECRET", marker)

    result = runtime.execute(
        python(
            "import os\n"
            "print(sorted(os.environ))\n"
            "print('host_secret_env_visible=' + str('LLLM_HOST_SECRET' in os.environ))\n"
        )
    )

    assert "host_secret_env_visible=False" in result.stdout
    assert marker not in result.stdout
    # Only the fixed set crosses in.
    assert "'HOME'" in result.stdout and "'PATH'" in result.stdout


def test_dotenv_values_are_not_forwarded(runtime):
    # The project's own .env is read solely by mcp_integration.config; nothing
    # from it is ever handed to a container.
    result = runtime.execute(
        python(
            "import os\n"
            "leaked = [k for k in os.environ if 'TRACKER' in k or 'TOKEN' in k]\n"
            "print('leaked=' + str(leaked))\n"
            "print('dotenv_visible=' + str(os.path.exists('/sandbox/.env')))\n"
        )
    )

    assert "leaked=[]" in result.stdout
    assert "dotenv_visible=False" in result.stdout


def test_the_docker_socket_is_absent(runtime):
    result = runtime.execute(
        python(
            "import os\n"
            "paths = ['/var/run/docker.sock', '/run/docker.sock']\n"
            "print('docker_socket_visible=' + str(any(os.path.exists(p) for p in paths)))\n"
        )
    )

    assert result.stdout.strip() == "docker_socket_visible=False"


# -- I: no network -----------------------------------------------------------


def test_the_network_is_disabled(runtime):
    # Deliberately dialled at a private address: the assertion must not depend
    # on any internet host being reachable.
    result = runtime.execute(
        python(
            "import socket\n"
            "socket.setdefaulttimeout(2)\n"
            "try:\n"
            "    socket.create_connection(('10.255.255.1', 80))\n"
            "    print('network_access=true')\n"
            "except OSError:\n"
            "    print('network_access=false')\n"
        )
    )

    assert result.stdout.strip() == "network_access=false"


# -- K: identity -------------------------------------------------------------


def test_the_job_runs_as_the_fixed_non_root_user(runtime):
    result = runtime.execute(python("import os\nprint(os.getuid(), os.getgid())\n"))

    assert result.stdout.strip() == f"{JOB_UID} {JOB_UID}"
    assert not result.stdout.startswith("0 ")


# -- L/M: hard termination ---------------------------------------------------


def test_a_timeout_kills_the_container(runtime):
    result = runtime.execute(python("while True:\n    pass\n"))

    assert result.status is SandboxStatus.TIMED_OUT
    assert result.exit_code is None
    assert result.error_type == "execution_timeout"
    assert result.artifacts == ()
    assert containers_for(result.job_id) == ""


def test_a_timeout_takes_descendant_processes_with_it(runtime):
    # A caller-side deadline could only abandon the parent; killing the whole
    # container is what removes the child too (§"Hard termination").
    result = runtime.execute(
        bash("sleep 300 &\nCHILD=$!\necho started $CHILD\nwait $CHILD\n")
    )

    assert result.status is SandboxStatus.TIMED_OUT
    assert containers_for(result.job_id) == ""


# -- N/O/P: resource ceilings ------------------------------------------------


def test_the_process_limit_is_enforced(runtime):
    # A controlled, bounded spawn — never an uncontrolled fork bomb.
    result = runtime.execute(
        python(
            "import subprocess\n"
            "children = []\n"
            "try:\n"
            "    for _ in range(200):\n"
            "        children.append(subprocess.Popen(['sleep', '5']))\n"
            "    print('spawned=' + str(len(children)))\n"
            "except OSError as error:\n"
            "    print('pid_limit_enforced=true')\n"
            "finally:\n"
            "    for child in children:\n"
            "        child.kill()\n"
        )
    )

    assert result.status in (SandboxStatus.COMPLETED, SandboxStatus.FAILED, SandboxStatus.TIMED_OUT)
    assert "spawned=200" not in result.stdout


def test_the_memory_limit_is_enforced(runtime):
    result = runtime.execute(
        python(
            "blocks = []\n"
            "try:\n"
            "    for _ in range(400):\n"
            "        blocks.append(bytearray(4 * 1024 * 1024))\n"
            "    print('allocated_1600MiB')\n"
            "except MemoryError:\n"
            "    print('memory_limit_enforced=true')\n"
        )
    )

    # Either Python raised MemoryError or the kernel killed the container; what
    # must never happen is the allocation succeeding.
    assert "allocated_1600MiB" not in result.stdout
    assert result.status is not SandboxStatus.COMPLETED or "memory_limit_enforced=true" in result.stdout


def test_the_output_tmpfs_bound_protects_the_host_disk(runtime):
    result = runtime.execute(
        python(
            "try:\n"
            "    with open('big.bin', 'wb') as handle:\n"
            "        for _ in range(64):\n"
            "            handle.write(b'x' * 1024 * 1024)\n"
            "    print('wrote_64MiB')\n"
            "except OSError:\n"
            "    print('output_tmpfs_limit_enforced=true')\n"
        )
    )

    assert "wrote_64MiB" not in result.stdout
    assert result.artifacts == ()


# -- Q/R/S: bounded and rejected output --------------------------------------


def test_a_flooding_job_is_stopped_and_its_container_removed(runtime):
    result = runtime.execute(
        python("import sys\nwhile True:\n    sys.stdout.write('x' * 4096)\n")
    )

    assert result.status is SandboxStatus.STOPPED
    assert result.error_type == "output_limit"
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 100_000
    assert result.artifacts == ()
    assert containers_for(result.job_id) == ""


def test_a_non_zero_exit_preserves_bounded_stderr(runtime):
    result = runtime.execute(python('raise RuntimeError("example failure")'))

    assert result.status is SandboxStatus.FAILED
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "RuntimeError" in result.stderr
    assert result.artifacts == ()
    # A job's own failure must never surface as a host traceback.
    assert "docker_backend.py" not in result.stderr


def test_a_symlink_in_the_output_rejects_the_whole_set(runtime):
    result = runtime.execute(
        python(
            "import os\n"
            "open('good.csv', 'w').write('a,b\\n')\n"
            "os.symlink('/etc/passwd', 'escape.txt')\n"
        )
    )

    assert result.status is SandboxStatus.STOPPED
    assert result.error_type == "artifact_policy_violation"
    assert result.artifacts == ()


# -- T: job isolation --------------------------------------------------------


def test_one_job_cannot_see_another_jobs_output(runtime):
    marker = uuid.uuid4().hex
    first = runtime.execute(python(f"open('leftover.txt','w').write({marker!r})\n"))
    assert first.status is SandboxStatus.COMPLETED

    second = runtime.execute(
        python("import os\nprint(sorted(os.listdir('.')))\n")
    )

    assert second.status is SandboxStatus.COMPLETED
    assert "leftover.txt" not in second.stdout
    assert marker not in second.stdout
