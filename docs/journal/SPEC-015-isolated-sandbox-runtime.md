# SPEC-015 — Isolated Sandbox Runtime

- **Spec:** [SPEC-015](../../specs/SPEC-015-Isolated-Sandbox-Runtime.md)
- **Date:** 2026-08-02
- **Branch:** feature/SPEC-015-isolated-sandbox-runtime
- **Merge commit:** 2221565

## Hypothesis / intent

Every execution boundary in the lab so far is built for *fixed, reviewed*
operations: `python_calculate` is an AST allowlist evaluated in-process,
`sql_query` is read-only SQLite, MCP servers are trusted child processes. None
of them can host arbitrary model-produced code.

Two documented limits force a new layer rather than an extension of an old one:

- `python_calculate` is deliberately not a REPL. Adding `eval`/`exec` would
  destroy the property that makes it safe.
- SPEC-011's `run_with_deadline` is a **caller-side** deadline — its own
  docstring says the worker thread is abandoned, not terminated. That is an
  acceptable safety net for pure handlers and useless against code that loops
  forever, allocates without bound, or spawns children.

The expectation was that a Docker-backed runtime could give real termination
(kill the container, not the thread) and a real privilege boundary, while
leaving the chat application completely untouched. The step is deliberately
host-only: no model-facing surface until SPEC-016.

## What changed

- **`sandbox_runtime/`** — new independent package, imported by nothing else in
  the project:
  - `models.py` — frozen `SandboxJob` / `SandboxResult` / `SandboxArtifact`,
    `SandboxLanguage` / `SandboxStatus`, and the `SandboxRuntime` protocol.
    Result invariants are enforced in `__post_init__`, so a non-completed job
    can never carry artifacts.
  - `policy.py` — the host-owned `SandboxPolicy`, its validation, its
    fingerprint, and the fixed per-language command vectors.
  - `paths.py` — strict relative-POSIX input validation and safe host
    materialisation of source/input.
  - `artifacts.py` — `lstat` walk of the collected output; regular files only,
    fully bounded, deterministically sorted.
  - `command_runner.py` — the injectable Docker CLI boundary, with concurrent
    bounded pipe draining.
  - `docker_backend.py` — `DockerSandboxRuntime`: preflight, holder start, the
    one untrusted exec, output collection, hard kill, cleanup.
  - `tracing.py` — sandbox events on the existing `TraceSink`/`build_event`.
  - `errors.py` — `SandboxUnavailable` / `SandboxImageUnavailable` /
    `SandboxRuntimeError`.
- **`sandbox/image/`** — committed `Dockerfile` (base pinned by digest),
  `hold.py` (the trusted idle holder), and a provenance `README.md`.
- **`scripts/build_sandbox_image.py`**, **`scripts/sandbox_smoke.py`** —
  developer build and a readable isolation check. Neither accepts code or paths
  from the command line.
- **`config.py`** — the SPEC-015 constants block. No `SANDBOX_ENABLED`, nothing
  wired into startup.
- **Tests** — `tests/support_sandbox.py` plus five deterministic modules
  (152 tests, no Docker) and `tests/test_sandbox_integration.py` (20 live tests,
  opt-in).
- **Docs** — README section + structure table, `.gitignore` for
  `data/sandbox/`.

Unchanged, as required: `app.py`, `agent.py`, `conversation.py`, `prompts.py`,
`tools/`, `skill_runtime/`, `mcp_integration/`, `skills/`. No `ToolSpec`, no
registry entry, no skill, no prompt text, no chat-history field.

## Environment & image provenance

- macOS 26.5.2, arm64 (Apple Silicon)
- Docker 29.6.1 (client and server), Docker Desktop, `desktop-linux` context
- Base image: `python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b`
- Built image: `lllm-sandbox:spec-015`
- Resolved image ID: `sha256:23106e07ed87075c7194a3ba92c7e5efcd0ddc84a78213df52cda2491a85716b`
  (linux/arm64, 45 MB)
- Inside the image: Debian 12.15, Python 3.12.13, GNU bash 5.2.15(1), GNU tar 1.34
- Job user: `nonroot`, uid/gid `65532:65532`
- Absent from the image: curl, wget, git, ssh, gcc/cc, make, sudo, docker
  (verified by probing the base) — plus no project source, no `.env`, no tokens

Effective limits (`config.py`): 10 s execution, 5 s cleanup, 10 s Docker control;
256 MiB memory with swap pinned equal; 1.0 CPU; 64 PIDs; `nofile=64:64`;
16 MiB `/tmp`; 8 MiB `/sandbox/output`; 100 000 bytes each of stdout and stderr;
20 artifacts / 2 MB per file / 5 MB total.

## Model & parameters (provenance)

- Model: `qwen3:8b` (digest `500a1f067a9f`, Q4_K_M, 8.2B)
- Ollama: local at `http://localhost:11434`
- Sampling: defaults — no options set in `llm.py`

The model is recorded for the regression check only. SPEC-015 adds no
model-facing behavior, so there is no sandbox model interaction to replay.

## Verification

### Deterministic tests

```text
152 passed   # the five tests/test_sandbox_*.py modules, no Docker involved
392 passed, 20 skipped   # whole suite (240 pre-existing + 152 new)
```

The 20 skips are the live Docker tests, which are opt-in by design.

### Live sandbox behavior

```text
A status=completed exit_code=0 stdout='4950\n' duration_ms=300
B status=completed artifact=report.csv size=20 sha256=57a3c6f5f15619637bc1eae674e6acda...
C status=timed_out error_type=execution_timeout duration_ms=10146 container_present_after=False
D status=stopped error_type=output_limit stdout_truncated=True retained_bytes=100000
E output_tmpfs_limit_enforced=true status=stopped artifacts=0
F remaining_labeled_containers=0 remaining_job_temp_directories=0
```

Host isolation, from the smoke suite and the integration tests:

```text
network_access=false
host_secret_env_visible=false
host_secret_file_visible=false
docker_socket_visible=false
dotenv_visible=false
root_write_allowed=false
output_tmpfs_limit_enforced=true
stdout_limit_enforced=true
job_uid=65532            (non-root)
```

```bash
LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_integration.py -q
# 20 passed in 29.32s
```

```text
$ python scripts/sandbox_smoke.py
[PASS] python-basic
[PASS] bash-basic
[PASS] python-artifact
[PASS] read-only-input
[PASS] read-only-root
[PASS] network-disabled
[PASS] host-environment-not-forwarded
[PASS] docker-socket-absent
[PASS] non-root-identity
[PASS] nonzero-exit
[PASS] artifact-symlink-rejected
[PASS] output-limit
[PASS] timeout-kills-container
[PASS] cleanup

14/14 passed
```

Cleanup evidence after every run:

```text
$ docker ps    --filter label=lllm.sandbox=true    # empty
$ docker ps -a --filter label=lllm.sandbox=true    # empty
remaining_job_temp_directories=0
```

### Ordinary chat regression (live model)

```text
$ printf 'Say hi in one short sentence.\nWhat is 17 * 23? Use your calculator tool.\n/exit\n' \
    | TRACKER_MCP_ENABLED=false python app.py

[mcp] connected: time (1 tool)
[mcp] tracker: disabled
[skills] 1 loaded: sales_analysis
Local AI chat

You:
Qwen: Hello! How can I assist you today? 😊

You:
[tool 1/4] python_calculate
[args] {"expression": "17 * 23"}
[result] {"ok": true, "result": 391}

Qwen: 17 multiplied by 23 equals **391**.

You:
Qwen: Goodbye! Feel free to reach out if you need anything else. 😊
```

One direct answer, one existing tool call, clean exit, **no sandbox tool
announced anywhere** — the new package did not change the agent surface.

## Outcome

All acceptance criteria are met. The interesting findings were three.

**1. `docker cp` cannot read a tmpfs mount.** SPEC-015 §24 prescribes
`docker cp <cid>:/sandbox/output/. <host-dir>`. On Docker 29.6.1 that returns an
empty copy, because the daemon resolves container paths against the host-side
rootfs and a tmpfs exists only inside the container's mount namespace. Verified
both ways — `--tmpfs` and `--mount type=tmpfs` — with the files plainly visible
via `docker exec ls` at the same moment.

Rather than weaken the boundary (a writable bind mount or an unbounded volume
would have made `docker cp` work), collection now streams the output as a
bounded `tar -cf - .` over `docker exec`, extracts it under `tarfile`'s
`filter="data"` rules, and then validates the result again with the `lstat`
walk. The security properties are unchanged and arguably better: two independent
layers now reject unsafe entries, and the tar bytes are capped before anything
touches the host filesystem. This is the one deliberate deviation from the spec
text; §24's intent — copy bounded output while the holder is alive, into a
private host directory, only after a zero exit — is preserved exactly.

**2. Docker Desktop caches negative path lookups.** A job failed with
`bind source path does not exist` for a directory that demonstrably existed on
the host. The trigger was a `git stash -u` that removed and recreated
`data/sandbox/`; Docker Desktop's file-sharing layer kept the stale negative
entry for roughly half a minute. The runtime now creates its scratch root once
at construction instead of letting the first job create the whole ancestor chain
microseconds before mounting it. No retry loop was added — that would hide real
failures — but `sandbox_container_start_failed` now carries Docker's bounded
stderr, which is what made the cause visible in the first place.

**3. Preflight needed a finer distinction.** One transient `docker image
inspect` failure produced "Build the sandbox image with …" for an image that was
already built. Only Docker's actual `No such image` response now yields
`SandboxImageUnavailable`; anything else is `SandboxUnavailable`, and both paths
emit `sandbox_preflight_failed` with a bounded stderr preview.

Worth stating plainly: the container's environment also contains `HOSTNAME`,
`PYTHON_VERSION`, `PYTHON_SHA256`, and `GPG_KEY`. Those come from Docker and the
base image's own `ENV` directives, not from the host — the six fixed variables
are the only ones this project sets, and no host variable or `.env` value
crosses in, which the smoke check now proves with a planted host-only marker.

The claim this step makes is narrow and should stay narrow: process isolation,
no network, no writable host mount, read-only root, dropped capabilities, a
non-root job, hard container termination, bounded data crossing. It is a local
single-user laboratory sandbox, not a hardened multi-tenant code-execution
service, and it does not claim protection against an unknown Docker or kernel
escape.

## Follow-ups

- **SPEC-016** adds the model-facing boundary on top of `runtime.execute(job,
  turn_id=...)`: a `sandbox_execute` tool, the workspace/artifact workflow, and
  the skill. The seam is already in place, and the future adapter must never
  construct Docker flags, choose an image or mounts, or implement timeout
  cleanup — those stay here permanently.
- The Yandex Tracker MCP server (SPEC-013) currently fails to start in this
  environment: `yandex-tracker-mcp==0.7.2` imports `FastMCP` from `mcp.server`,
  which the version uvx resolves under Python 3.14 no longer exports. Confirmed
  **pre-existing** by reproducing it on a clean `main` (`git stash -u`), so it is
  unrelated to this step — the chat regression above was run with
  `TRACKER_MCP_ENABLED=false`. It deserves its own PATCH against SPEC-013.
- An explicit stale-container cleanup command was deliberately not added. An
  abrupt host kill can still leave a holder behind, but SPEC-015 must not
  delete all labelled containers at startup, since another legitimate process
  may own one. If real usage shows the need, that is a focused PATCH.
- `nofile=64:64` and `pids-limit=64` proved sufficient for CPython plus bash in
  every live scenario, including the controlled process-spawn test. Worth
  revisiting only if SPEC-016 workloads hit them.
