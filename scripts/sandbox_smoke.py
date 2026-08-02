"""A human-readable isolation check for the sandbox runtime (SPEC-015).

Usage:

    python scripts/build_sandbox_image.py    # once, to build the image
    python scripts/sandbox_smoke.py

Prints one line per scenario and exits non-zero unless every mandatory check
passes. It is a developer's quick answer to "is the boundary actually doing what
it claims?", complementing the opt-in pytest suite in
`tests/test_sandbox_integration.py`.

The sources below are **committed and fixed**. This script deliberately accepts
no code, file, or flag from the command line: it must never become a convenient
way to run arbitrary text through the sandbox, and it is not reachable from the
model, the chat loop, or any tool.
"""

import os
import subprocess
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/sandbox_smoke.py) by making
# the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reliability import new_id  # noqa: E402
from sandbox_runtime import (  # noqa: E402
    DockerSandboxRuntime,
    SandboxError,
    SandboxJob,
    SandboxLanguage,
    SandboxStatus,
)
from sandbox_runtime.policy import JOB_UID  # noqa: E402
from tracing import MemoryTraceSink  # noqa: E402


# A host-only variable planted in this process before any job runs. If it ever
# shows up inside a container, the fixed-environment guarantee is broken.
# The remaining container variables (HOSTNAME, PYTHON_VERSION, PYTHON_SHA256,
# GPG_KEY) come from Docker and the base image itself, never from this host.
HOST_MARKER_VARIABLE = "LLLM_SANDBOX_SMOKE_HOST_ONLY"
os.environ[HOST_MARKER_VARIABLE] = "host-only-value"


def python_job(source: str, **kwargs) -> SandboxJob:
    return SandboxJob(language=SandboxLanguage.PYTHON, source=source, **kwargs)


def bash_job(source: str) -> SandboxJob:
    return SandboxJob(language=SandboxLanguage.BASH, source=source)


# Each check is (name, job, verdict) where verdict inspects the SandboxResult.
CHECKS = [
    (
        "python-basic",
        python_job("print(sum(range(100)))"),
        lambda r: r.status is SandboxStatus.COMPLETED and r.stdout == "4950\n",
    ),
    (
        "bash-basic",
        bash_job('printf "done\n"'),
        lambda r: r.status is SandboxStatus.COMPLETED and r.stdout == "done\n",
    ),
    (
        "python-artifact",
        python_job("open('report.csv', 'w').write('name,value\\nalpha,42\\n')"),
        lambda r: (
            r.status is SandboxStatus.COMPLETED
            and len(r.artifacts) == 1
            and r.artifacts[0].path == "report.csv"
            and r.artifacts[0].content == b"name,value\nalpha,42\n"
        ),
    ),
    (
        "read-only-input",
        python_job(
            "try:\n"
            "    open('/sandbox/input/data.csv', 'w').write('tampered')\n"
            "    print('writable')\n"
            "except OSError:\n"
            "    print('denied')\n",
            input_files={"data.csv": b"alpha,42\n"},
        ),
        lambda r: r.stdout.strip() == "denied",
    ),
    (
        "read-only-root",
        python_job(
            "try:\n"
            "    open('/etc/lllm-probe', 'w').write('x')\n"
            "    print('writable')\n"
            "except OSError:\n"
            "    print('denied')\n"
        ),
        lambda r: r.stdout.strip() == "denied",
    ),
    (
        "network-disabled",
        python_job(
            "import socket\n"
            "socket.setdefaulttimeout(2)\n"
            "try:\n"
            "    socket.create_connection(('10.255.255.1', 80))\n"
            "    print('reachable')\n"
            "except OSError:\n"
            "    print('denied')\n"
        ),
        lambda r: r.stdout.strip() == "denied",
    ),
    (
        "host-environment-not-forwarded",
        python_job(
            "import os\n"
            "fixed = ('HOME', 'LANG', 'LC_ALL', 'PATH', 'PYTHONUNBUFFERED', "
            "'PYTHONDONTWRITEBYTECODE')\n"
            "print('fixed=' + ','.join(f'{k}={os.environ.get(k)}' for k in fixed))\n"
            f"print('host_marker_visible=' + str({HOST_MARKER_VARIABLE!r} in os.environ))\n"
            "print('secretish=' + ','.join(k for k in os.environ "
            "if 'TOKEN' in k or 'PROXY' in k.upper() or 'TRACKER' in k))\n"
        ),
        lambda r: (
            "host_marker_visible=False" in r.stdout
            and "secretish=\n" in r.stdout + "\n"
            and "HOME=/sandbox/output" in r.stdout
            and "PATH=/usr/local/bin:/usr/bin:/bin" in r.stdout
        ),
    ),
    (
        "docker-socket-absent",
        python_job(
            "import os\n"
            "print(os.path.exists('/var/run/docker.sock'))\n"
        ),
        lambda r: r.stdout.strip() == "False",
    ),
    (
        "non-root-identity",
        python_job("import os\nprint(os.getuid())\n"),
        lambda r: r.stdout.strip() == str(JOB_UID),
    ),
    (
        "nonzero-exit",
        python_job('raise RuntimeError("example failure")'),
        lambda r: (
            r.status is SandboxStatus.FAILED
            and r.exit_code != 0
            and "RuntimeError" in r.stderr
            and r.artifacts == ()
        ),
    ),
    (
        "artifact-symlink-rejected",
        python_job("import os\nos.symlink('/etc/passwd', 'escape.txt')\n"),
        lambda r: (
            r.status is SandboxStatus.STOPPED
            and r.error_type == "artifact_policy_violation"
            and r.artifacts == ()
        ),
    ),
    (
        "output-limit",
        python_job("import sys\nwhile True:\n    sys.stdout.write('x' * 4096)\n"),
        lambda r: (
            r.status is SandboxStatus.STOPPED
            and r.error_type == "output_limit"
            and r.stdout_truncated
        ),
    ),
    (
        "timeout-kills-container",
        python_job("while True:\n    pass\n"),
        lambda r: (
            r.status is SandboxStatus.TIMED_OUT
            and r.exit_code is None
            and r.error_type == "execution_timeout"
        ),
    ),
]


def labelled_containers() -> str:
    result = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", "label=lllm.sandbox=true"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def main() -> int:
    runtime = DockerSandboxRuntime(run_id=new_id(), trace_sink=MemoryTraceSink())
    passed = 0
    interrupted = False

    try:
        for name, job, verdict in CHECKS:
            try:
                result = runtime.execute(job)
                ok = bool(verdict(result))
                detail = "" if ok else f"  (status={result.status} error={result.error_type})"
            except SandboxError as error:
                # A host/setup failure is reported as a failed check rather than
                # a traceback: the point of this script is a readable verdict.
                ok, detail = False, f"  ({error})"
            print(f"[{'PASS' if ok else 'FAIL'}] {name}{detail}")
            passed += ok
    except KeyboardInterrupt:
        # The runtime already cleaned up the container it was running; the
        # cleanup verdict below still runs so an interrupted developer sees
        # whether anything was left behind.
        interrupted = True
        print("\nInterrupted.")

    leaked = labelled_containers()
    cleanup_ok = leaked == ""
    print(f"[{'PASS' if cleanup_ok else 'FAIL'}] cleanup" + ("" if cleanup_ok else f"  (leaked: {leaked})"))
    passed += cleanup_ok

    total = len(CHECKS) + 1
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total and not interrupted else 1


if __name__ == "__main__":
    raise SystemExit(main())
