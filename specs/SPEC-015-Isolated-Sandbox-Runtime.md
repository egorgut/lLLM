# SPEC-015: Isolated Sandbox Runtime

> **Roadmap label:** STEP 15 — Isolated Sandbox Runtime  
> **Repository sequence:** SPEC-015, following SPEC-014  
> **Status:** Proposed

## Background

`lLLM` has evolved from a local CLI chat into a small, framework-free agent
harness with explicit execution boundaries.

The relevant sequence is:

- SPEC-006 introduced the shared `ToolSpec` / `ToolRegistry` contract.
- SPEC-007 added `python_calculate`, a deliberately restricted in-process
  arithmetic evaluator.
- SPEC-008 added a read-only SQLite boundary through `sql_query`.
- SPEC-009 added MCP child-process lifecycle and external tool discovery.
- SPEC-010 introduced the bounded model → tool → model agent loop.
- SPEC-011 added typed outcomes, deadlines, repeated-call detection, JSONL
  tracing, committed tests, and evaluations.
- SPEC-012 added host-controlled skills and per-turn tool allowlists.
- SPEC-013 proved a real external business integration through a least-privilege
  Yandex Tracker MCP allowlist.
- SPEC-014 added the parallel PATCH workflow for focused corrections.

The current tool executor is intentionally small:

```text
registered tool name
    │
    ▼
in-process Python handler
    │
    ▼
structured dictionary result
```

That model is appropriate for fixed, reviewed operations such as
`python_calculate` and `sql_query`. It is not an acceptable boundary for
arbitrary model-produced Python or Bash.

`python_calculate` is explicitly not a Python REPL. It accepts a small AST
allowlist, performs no imports, has no filesystem or network access, and executes
inside the main application process. Expanding it into `eval`, `exec`, or a
general script runner would destroy its security model.

SPEC-011 also documents an important limitation: the generic caller-side
deadline can return control to the host, but Python cannot forcibly terminate an
arbitrary running thread. That is acceptable as a documented safety net for the
current pure/read-only demo handlers. It is not acceptable for unknown code that
may loop forever, allocate memory, spawn processes, or continue after the user
turn has ended.

The next architectural boundary must therefore be an **isolated execution
runtime**.

This step creates that runtime before exposing it as a model-facing tool.

The sequence is deliberately split:

```text
SPEC-015
Isolated Sandbox Runtime
    │
    │  host-only execution boundary
    ▼
SPEC-016
Agent Workspace & Sandbox Skill
    │
    │  model-facing tool and artifact workflow
    ▼
agent writes, runs, repairs, and exports code safely
```

SPEC-015 is infrastructure. It proves that untrusted Python and Bash can execute
inside a bounded disposable environment without receiving access to the host,
the Docker daemon, project secrets, the network, or the ordinary `lLLM`
process.

---

## Goal

Introduce a local Docker-backed sandbox runtime that can execute one Python or
Bash script under a strict host-owned policy and return a bounded structured
result.

The runtime must:

1. execute untrusted source only inside a disposable Linux container;
2. support exactly two languages in this iteration: Python and Bash;
3. use a project-owned, reproducible sandbox image;
4. resolve the configured image tag to one immutable image ID before execution;
5. disable container networking;
6. use a read-only container root filesystem;
7. expose no Docker socket and no privileged host devices;
8. drop Linux capabilities and prohibit privilege escalation;
9. run the untrusted script as a fixed non-root user;
10. expose source and input files as read-only mounts;
11. keep every writable runtime directory in size-bounded container `tmpfs`;
12. avoid a writable host bind mount for generated output;
13. enforce CPU, memory, process-count, file-descriptor, wall-time, output, and
    artifact limits;
14. terminate the complete container when the script times out or exceeds a
    streaming-output limit;
15. remove the container on every normal, failed, timed-out, interrupted, and
    exceptional path;
16. collect only bounded regular files from a dedicated output directory;
17. reject unsafe input paths and unsafe output file types;
18. forward no host environment variables or `.env` values;
19. return stable dataclasses and stable error categories;
20. emit structured sandbox trace events through the existing `TraceSink`;
21. provide deterministic unit tests without requiring Docker;
22. provide opt-in integration tests against a real Docker daemon;
23. leave the normal `python app.py` chat behavior unchanged;
24. add no model-facing sandbox tool, skill, prompt instruction, or conversation
    state in this step.

Target architecture:

```text
trusted host caller
    │
    ▼
SandboxJob validation
    │
    ▼
DockerSandboxRuntime
    │
    ├── resolve pinned local image to immutable image ID
    ├── create disposable container
    ├── mount source/input read-only
    ├── allocate writable tmpfs only
    ├── execute fixed language command as non-root
    ├── stream and bound stdout/stderr
    ├── hard-kill on timeout/resource stop
    ├── copy bounded output while container is alive
    └── remove container
    │
    ▼
SandboxResult
```

The ordinary application remains:

```text
python app.py
    │
    └── existing tools / MCP / skills / agent loop
```

No sandbox code is invoked by the chat loop in SPEC-015.

---

## User- and developer-visible behavior

Although the sandbox is not model-facing yet, the step must provide a small
host-only smoke command so a developer can verify the boundary directly.

### 1. Build the project sandbox image

Representative command:

```bash
python scripts/build_sandbox_image.py
```

Expected output shape:

```text
Building lllm-sandbox:spec-015...
Sandbox image ready.
Tag: lllm-sandbox:spec-015
Image ID: sha256:...
```

Requirements:

- the Dockerfile is committed;
- the base image is pinned by digest, not `latest`;
- the build script invokes Docker with a fixed argument vector;
- the build command is never available to model-generated input;
- image build may use the network as an explicit developer setup action;
- runtime jobs never use the network.

### 2. Run the sandbox smoke suite

Representative command:

```bash
python scripts/sandbox_smoke.py
```

Expected output shape:

```text
[PASS] python-basic
[PASS] bash-basic
[PASS] python-artifact
[PASS] nonzero-exit
[PASS] timeout-kills-container
[PASS] network-disabled
[PASS] host-environment-not-forwarded
[PASS] host-file-not-mounted
[PASS] read-only-root
[PASS] cleanup

10/10 passed
```

The exact number may grow during implementation, but every mandatory scenario in
the verification section must be represented.

### 3. Successful Python job

Host-side example:

```python
job = SandboxJob(
    language=SandboxLanguage.PYTHON,
    source="print(sum(range(100)))",
)
result = runtime.execute(job)
```

Expected result semantics:

```python
result.status is SandboxStatus.COMPLETED
result.exit_code == 0
result.stdout == "4950\n"
result.stderr == ""
result.artifacts == ()
```

### 4. Successful Bash job with an artifact

Host-side example:

```python
job = SandboxJob(
    language=SandboxLanguage.BASH,
    source='printf "name,value\nalpha,42\n" > report.csv\nprintf "done\n"',
)
result = runtime.execute(job)
```

The script runs with `/sandbox/output` as its working directory.

Expected result semantics:

```python
result.status is SandboxStatus.COMPLETED
result.stdout == "done\n"
result.artifacts[0].path == "report.csv"
result.artifacts[0].size_bytes > 0
result.artifacts[0].sha256 != ""
result.artifacts[0].content == b"name,value\nalpha,42\n"
```

Artifacts are returned to the trusted host API as bounded bytes. They are not
persisted into chat history and are not copied into traces.

### 5. Script exits with an error

Example source:

```python
raise RuntimeError("example failure")
```

Expected behavior:

```python
result.status is SandboxStatus.FAILED
result.exit_code != 0
result.stdout == ""
"RuntimeError" in result.stderr
result.artifacts == ()
```

The script's own stderr is allowed as bounded job output.

The result must not contain a host Python traceback from
`DockerSandboxRuntime`.

### 6. Script exceeds its execution deadline

Example:

```python
while True:
    pass
```

Expected result:

```python
result.status is SandboxStatus.TIMED_OUT
result.exit_code is None
result.error_type == "execution_timeout"
```

The whole container is killed.

After the call returns:

- the container does not appear in `docker ps`;
- it does not appear in `docker ps -a`;
- no background job from that container remains;
- no output artifacts are returned.

This is **hard container termination**, not caller-side thread abandonment.

### 7. Script exceeds stdout or stderr limit

A script that continuously writes output must be stopped before the host buffers
unbounded data.

Expected result:

```python
result.status is SandboxStatus.STOPPED
result.error_type == "output_limit"
result.stdout_truncated is True
```

The complete container is killed and removed.

No artifacts are returned.

### 8. Invalid job input

Examples:

- unsupported language;
- empty source;
- source larger than the configured bound;
- too many input files;
- absolute input path;
- `../` path traversal;
- backslash-based path;
- NUL in a name;
- an input file that would overwrite the fixed source file;
- input bytes exceeding the total limit.

Expected result:

```python
result.status is SandboxStatus.REJECTED
result.error_type == "invalid_job"
```

No Docker command is executed.

### 9. Docker is unavailable

Examples:

- Docker CLI not installed;
- Docker daemon not running;
- sandbox image not built;
- configured image cannot be inspected;
- image platform cannot run locally.

Expected host-side exception:

```text
SandboxUnavailable: Docker sandbox is unavailable.
```

or:

```text
SandboxImageUnavailable: Build the sandbox image with
'python scripts/build_sandbox_image.py'.
```

The message is concise and stable. Raw Docker diagnostics may be written to a
bounded debug trace field, but must not be blindly printed as the public error.

Normal `python app.py` startup must not fail merely because Docker is missing,
because the sandbox is not wired into the application in SPEC-015.

### 10. Ordinary chat regression

After SPEC-015:

```bash
python app.py
```

must still start and behave exactly as before.

There is:

- no `sandbox_execute` declaration;
- no sandbox skill;
- no sandbox startup line;
- no Docker preflight;
- no new model prompt;
- no new chat-history field.

---

## Scope

This specification includes:

- a new `sandbox_runtime/` package;
- immutable job, policy, result, artifact, and status models;
- a `SandboxRuntime` protocol;
- a Docker CLI backend;
- an injectable command/process boundary for deterministic tests;
- a project-owned Docker image;
- a fixed container holder process used only to keep bounded `tmpfs` alive while
  the host executes and collects one job;
- Python execution;
- Bash execution;
- read-only source and input mounts;
- size-bounded writable `tmpfs`;
- disabled runtime networking;
- a non-root job user;
- dropped capabilities;
- `no-new-privileges`;
- CPU, memory, PID, file-descriptor, timeout, stream, and artifact limits;
- hard container termination;
- deterministic cleanup;
- bounded stdout and stderr capture;
- bounded artifact collection;
- input-path validation;
- output-file validation;
- trace events;
- developer build and smoke scripts;
- deterministic tests using a fake command runner;
- opt-in live Docker integration tests;
- README documentation;
- `.gitignore` updates;
- a SPEC-015 journal entry after implementation.

---

## Non-goals

This specification does not introduce:

- a model-facing `sandbox_execute` tool;
- any new `ToolSpec`;
- registration in `ToolRegistry`;
- binding in `ToolExecutor`;
- a `code_workspace` or `sandbox` skill;
- changes to `SkillRouter`;
- changes to the system prompt;
- model-generated Docker arguments;
- model-generated image names;
- model-generated container commands;
- model-generated mounts;
- model-generated environment variables;
- model-generated timeout, memory, CPU, PID, or output limits;
- direct access to the host shell;
- direct `subprocess` execution of untrusted Python or Bash on macOS;
- Docker socket mounting;
- privileged containers;
- host network mode;
- host PID mode;
- host IPC mode;
- host user namespace mode;
- arbitrary device access;
- GPU access;
- access to Ollama from the container;
- access to Yandex Tracker or any MCP server;
- internet access;
- DNS access;
- package installation during a job;
- `pip install`;
- `apt install`;
- dynamic image building during a job;
- arbitrary interpreters;
- JavaScript, TypeScript, R, Julia, PowerShell, SQL shells, or compiled languages;
- interactive terminal sessions;
- PTY allocation;
- stdin streaming from the user;
- background jobs;
- detached jobs;
- resumable jobs;
- job queues;
- scheduled jobs;
- parallel model tool calls;
- concurrent sandbox scheduling guarantees;
- cross-job persistent files;
- persistent workspaces;
- artifact download UX;
- artifact retention policy beyond the returned host object;
- binary artifact rendering in the CLI;
- content-addressed artifact storage;
- a web UI;
- remote Docker hosts;
- Kubernetes;
- Firecracker;
- gVisor;
- Kata Containers;
- production multi-tenant isolation;
- executing code from untrusted remote users;
- a generic secrets manager;
- seccomp profile authoring beyond Docker's default profile;
- macOS sandbox-exec;
- hidden chain-of-thought capture.

These may be considered later. SPEC-015 is a local single-user AI-laboratory
boundary, not a hostile public code-execution service.

---

## Terminology

### Sandbox job

One immutable request to execute one Python or Bash source file with optional
read-only input files.

### Sandbox policy

The complete host-owned set of permitted languages, image identity, resource
limits, filesystem rules, environment, and output bounds.

### Sandbox image

The committed project image used for runtime jobs. A human builds it before
running live sandbox tests.

### Configured image reference

A readable local tag such as:

```text
lllm-sandbox:spec-015
```

### Resolved image ID

The immutable local image identifier returned by `docker image inspect`, for
example:

```text
sha256:...
```

Every runtime container must use the resolved image ID, not the mutable tag.

### Holder container

A disposable container started with a fixed trusted idle process. It keeps the
container and its writable `tmpfs` mounts alive while the host runs one untrusted
command through `docker exec`, copies successful output, then kills/removes the
container.

The holder does not execute user source itself.

### Source mount

A temporary host directory containing only the fixed `main.py` or `main.sh`,
mounted read-only at:

```text
/sandbox/source
```

### Input mount

A temporary host directory containing validated optional input files, mounted
read-only at:

```text
/sandbox/input
```

### Output directory

A container-only, size-bounded writable `tmpfs` mounted at:

```text
/sandbox/output
```

It is the job working directory and the only location from which artifacts are
collected.

### Artifact

A bounded regular file created under `/sandbox/output` by a successful job and
returned to the trusted host as metadata plus bytes.

### Hard termination

Killing the complete holder container, thereby terminating the job and all of
its descendants. This is stronger than returning from a timed-out Python thread.

---

## Core architectural decisions

### 1. The sandbox is a new execution boundary, not an expansion of `python_calculate`

`python_calculate` must remain unchanged.

Do not add:

```python
eval(...)
exec(...)
subprocess.run(model_source)
```

to that handler.

The dependency direction is:

```text
sandbox_runtime
    │
    └── independent host infrastructure

tools
    │
    └── no dependency on sandbox_runtime in SPEC-015
```

SPEC-016 may later introduce a reviewed tool adapter that depends on
`sandbox_runtime`.

### 2. Untrusted source never executes in the main Python process

The `lLLM` process may:

- validate source length;
- write source bytes into a temporary host directory;
- construct fixed Docker arguments;
- stream Docker CLI output;
- inspect bounded artifact bytes;
- build a structured result.

It must never import, compile, evaluate, source, or directly execute the
untrusted job.

### 3. Use the Docker CLI, not a new Python Docker dependency

The current runtime dependency list is intentionally small.

SPEC-015 must not add `docker`, `docker-py`, or another container SDK to
`requirements.txt`.

Use `subprocess` only to invoke the trusted local Docker CLI with an argument
vector:

```python
subprocess.Popen(
    ["docker", "exec", ...],
    shell=False,
    ...
)
```

Never construct a shell command string.

Never use:

```python
subprocess.run("docker ...", shell=True)
```

The Docker invocation layer must be injectable so unit tests do not require a
real daemon.

### 4. The configured tag is resolved to an immutable image ID

Tags are mutable.

Before the first live job, resolve:

```bash
docker image inspect lllm-sandbox:spec-015
```

to an exact image ID.

The runtime must then create the container from:

```text
sha256:...
```

not from the tag.

The resolved image ID is:

- cached for the runtime instance;
- included in `SandboxResult`;
- included in trace metadata;
- never controlled by the job;
- refreshed only when a new runtime instance performs preflight.

### 5. The image is project-owned and reproducible

Expected files:

```text
sandbox/
└── image/
    ├── Dockerfile
    ├── hold.py
    └── README.md
```

Requirements:

- base image pinned by digest;
- no `latest`;
- Python 3.12 runtime;
- Bash available;
- ordinary small core utilities required for basic scripts;
- fixed non-root account, recommended UID/GID `65532:65532`;
- trusted holder script installed in the image;
- no project source code copied into the image;
- no `.env`;
- no SSH keys;
- no Docker CLI or socket;
- no application tokens;
- no runtime dependency on Ollama;
- image build documented for Apple Silicon and ordinary Linux Docker;
- runtime image reference kept in host configuration.

Where practical, omit unnecessary tools such as `curl`, `wget`, `ssh`, `git`,
and compilers. Their absence is defense in depth; network and privilege policy
remain authoritative.

### 6. One job uses one disposable container

Do not reuse a container between jobs.

Required lifecycle:

```text
validate job
    │
    ▼
prepare read-only host source/input directories
    │
    ▼
start disposable holder container
    │
    ▼
execute fixed interpreter command as non-root
    │
    ├── success
    │      │
    │      ▼
    │   copy bounded output while container is alive
    │
    └── failure / timeout / output stop / interrupt
           │
           └── no artifact collection
    │
    ▼
kill/remove container
    │
    ▼
delete host temporary directories
```

The lifecycle must be guarded by `try/finally`.

### 7. Use a holder container so writable output remains container-only

A direct writable bind mount would allow untrusted code to consume host disk
space before the host could inspect the result.

SPEC-015 must not mount a writable host output directory.

Instead:

1. start a container with a fixed trusted holder process;
2. mount source and input read-only;
3. allocate `/sandbox/output` as a size-bounded `tmpfs`;
4. execute the untrusted script through `docker exec`;
5. when and only when the job exits successfully, copy the bounded output to a
   host temporary collection directory while the container is still alive;
6. validate the copied tree;
7. read bounded artifact bytes;
8. kill/remove the container.

If the holder exits before collection, return a controlled runtime failure and
no artifacts.

### 8. Runtime networking is always disabled

Every container must use:

```text
--network none
```

The job cannot override it.

Do not expose:

- host networking;
- bridge networking;
- port publishing;
- DNS configuration;
- proxy environment variables;
- Ollama host addresses.

A network attempt should fail locally and quickly.

### 9. The root filesystem is read-only

Every container must use:

```text
--read-only
```

Writable locations are explicit `tmpfs` mounts only.

Recommended writable mounts:

```text
/tmp
/sandbox/output
```

No writable home directory is required.

### 10. Writable storage is size-bounded `tmpfs`

Recommended defaults:

```text
/tmp             16 MiB
/sandbox/output   8 MiB
```

Mount options should include, where supported:

```text
rw
nosuid
nodev
```

Use `noexec` for `/tmp`.

Do not use `noexec` for `/sandbox/output` as the security boundary; the fixed
host command, non-root user, read-only root, no-new-privileges, and dropped
capabilities remain authoritative. The implementation may use `noexec` there if
the live Python/Bash scenarios continue to work.

The output `tmpfs` owner/mode must permit only the fixed job user to write.

### 11. Source and input are read-only and separate from output

Mounts:

```text
host source dir  -> /sandbox/source  (read-only)
host input dir   -> /sandbox/input   (read-only)
tmpfs            -> /sandbox/output  (read-write, bounded)
```

The current working directory is:

```text
/sandbox/output
```

Python source path:

```text
/sandbox/source/main.py
```

Bash source path:

```text
/sandbox/source/main.sh
```

The job must not choose these paths.

### 12. Run the untrusted job as a fixed non-root user

The trusted holder may run as the image default process user, but the untrusted
command must execute through Docker with an explicit fixed identity:

```text
65532:65532
```

The job cannot select another user.

The job must not receive `sudo`, setuid helpers, or additional groups.

### 13. Drop capabilities and privilege escalation

Every container must include:

```text
--cap-drop ALL
--security-opt no-new-privileges
```

Do not use:

```text
--privileged
--device
--cap-add
```

Docker's default seccomp profile remains enabled.

### 14. Never expose the Docker daemon

The container must not receive:

```text
/var/run/docker.sock
```

It must not receive any equivalent Docker Desktop socket, TCP Docker endpoint,
or container-management credential.

The host process may invoke Docker because it is trusted operator software. The
untrusted job must never gain that capability.

### 15. Apply deterministic resource limits

Recommended initial policy:

```python
SANDBOX_EXECUTION_TIMEOUT_SECONDS = 10
SANDBOX_DOCKER_CONTROL_TIMEOUT_SECONDS = 10
SANDBOX_CLEANUP_TIMEOUT_SECONDS = 5

SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024
SANDBOX_CPUS = 1.0
SANDBOX_PIDS_LIMIT = 64
SANDBOX_NOFILE_LIMIT = 64

SANDBOX_TMP_BYTES = 16 * 1024 * 1024
SANDBOX_OUTPUT_TMPFS_BYTES = 8 * 1024 * 1024

SANDBOX_MAX_SOURCE_BYTES = 100_000
SANDBOX_MAX_INPUT_FILES = 20
SANDBOX_MAX_INPUT_FILE_BYTES = 1_000_000
SANDBOX_MAX_INPUT_TOTAL_BYTES = 2_000_000

SANDBOX_MAX_STDOUT_BYTES = 100_000
SANDBOX_MAX_STDERR_BYTES = 100_000

SANDBOX_MAX_ARTIFACT_FILES = 20
SANDBOX_MAX_ARTIFACT_FILE_BYTES = 2_000_000
SANDBOX_MAX_ARTIFACT_TOTAL_BYTES = 5_000_000
SANDBOX_MAX_ARTIFACT_PATH_CHARS = 240
```

The exact constant names may vary, but the values and semantics above are the
SPEC-015 defaults unless implementation evidence requires a small documented
adjustment.

Container limits must include equivalents of:

```text
--memory 256m
--memory-swap 256m
--cpus 1.0
--pids-limit 64
--ulimit nofile=64:64
```

All limits are host-owned.

The job input contains no policy override fields.

### 16. The sandbox timeout must finish before the outer tool timeout

SPEC-016 will likely call the runtime from one ordinary tool execution.

Reserve cleanup time now.

Configuration validation must require:

```text
SANDBOX_EXECUTION_TIMEOUT_SECONDS
+ SANDBOX_CLEANUP_TIMEOUT_SECONDS
< TOOL_EXECUTION_TIMEOUT_SECONDS
```

With the current `TOOL_EXECUTION_TIMEOUT_SECONDS = 30`, the recommended
`10 + 5 < 30` defaults are coherent.

SPEC-015 does not register the tool, but preserving this invariant prevents the
future caller-side timeout from firing before the sandbox can kill and clean up
its own container.

### 17. The language command is fixed by the host

Python command:

```text
python3 -I -B /sandbox/source/main.py
```

Required semantics:

- isolated Python mode;
- no bytecode writes;
- no user site-packages;
- source path fixed;
- no model-provided command-line arguments.

Bash command:

```text
/bin/bash --noprofile --norc /sandbox/source/main.sh
```

Required semantics:

- no user profile;
- no rc file;
- source path fixed;
- no `bash -c <model text>`;
- no model-provided command-line arguments.

The model-facing tool in the next step may provide source and files, but it must
still never provide an arbitrary command array.

### 18. Forward a minimal fixed environment only

Do not pass `os.environ` through to Docker.

Do not pass `.env`.

Do not pass:

- `TRACKER_TOKEN`;
- organisation IDs;
- proxy variables;
- Git credentials;
- home paths;
- Ollama variables;
- Docker variables;
- shell startup variables.

Recommended fixed environment:

```text
HOME=/sandbox/output
LANG=C.UTF-8
LC_ALL=C.UTF-8
PATH=/usr/local/bin:/usr/bin:/bin
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
```

The implementation may add another non-secret deterministic variable when
documented.

The job cannot add environment values in SPEC-015.

### 19. Input paths use a strict relative POSIX subset

Each input path must:

- be a non-empty string;
- use `/` as the separator;
- be relative;
- contain no NUL;
- contain no backslash;
- contain no empty component;
- contain no `.` component;
- contain no `..` component;
- remain below the configured character limit;
- not equal or overlap the host-owned source path;
- resolve under the temporary input root;
- be unique after normalization.

The host creates regular files only.

Do not preserve symlinks, device nodes, FIFOs, sockets, or executable metadata
from any caller.

### 20. Job source is one fixed UTF-8 file

`SandboxJob.source` is a string.

The runtime:

1. encodes it as UTF-8;
2. checks the byte limit;
3. writes it to `main.py` or `main.sh`;
4. uses a host-created regular file;
5. mounts the containing directory read-only.

An empty or whitespace-only source is rejected.

A UTF-8 encoding failure is rejected before Docker execution.

### 21. Stream output with bounded memory

The Docker exec process must be read incrementally.

Do not call an API that may buffer unlimited stdout and stderr before enforcing
limits.

The implementation must:

- drain stdout and stderr without deadlock;
- count bytes separately;
- retain at most the configured bytes;
- terminate the whole container when either stream crosses its limit;
- set the corresponding truncation flag;
- return bounded UTF-8 text using deterministic replacement for invalid bytes;
- avoid logging the complete stream.

Reader threads, selectors, or another small standard-library mechanism are
acceptable.

### 22. Script-level failure is data; host-runtime failure is an exception

Expected untrusted-job outcomes return `SandboxResult`:

```text
completed
failed
timed_out
stopped
rejected
```

Examples:

- Python exception → `failed`;
- Bash `exit 2` → `failed`;
- wall deadline → `timed_out`;
- stdout limit → `stopped`;
- invalid input path → `rejected`.

Host/runtime setup defects raise typed exceptions:

```text
SandboxUnavailable
SandboxImageUnavailable
SandboxRuntimeError
```

Examples:

- Docker CLI missing;
- Docker daemon unavailable;
- image not built;
- Docker protocol output cannot be parsed;
- output cannot be copied because the holder unexpectedly disappeared;
- cleanup command itself fails after best effort.

Public exception messages are stable and sanitised.

Raw bounded Docker stderr belongs in trace diagnostics, not in the ordinary
exception string.

### 23. Collect artifacts only after exit code zero

Artifacts are collected only when:

```text
exit_code == 0
```

A failed, timed-out, stopped, rejected, or interrupted job returns no artifacts.

This prevents partial output from being mistaken for a completed result.

### 24. Copy output while the holder is alive

The runtime must copy:

```text
/sandbox/output
```

to a host temporary collection directory before killing the holder.

The output `tmpfs` disappears with the container.

Use a fixed Docker copy source.

The job cannot control the host destination.

### 25. Accept only bounded regular-file artifacts

Artifact traversal must:

- not follow symlinks;
- reject symlinks;
- reject sockets;
- reject FIFOs;
- reject block/character devices;
- reject hard-link anomalies when detectable;
- count files;
- bound every file;
- bound total bytes;
- bound relative-path length;
- use relative POSIX paths;
- sort artifacts deterministically by path.

Directories themselves are not artifacts.

An empty directory is ignored.

If any unsafe output type or artifact bound is violated, the job result becomes:

```text
status = stopped
error_type = artifact_policy_violation
artifacts = ()
```

The complete host collection directory is deleted.

### 26. Artifact content is bounded host data, not trace data

Proposed model:

```python
@dataclass(frozen=True)
class SandboxArtifact:
    path: str
    size_bytes: int
    sha256: str
    content: bytes
```

Requirements:

- immutable;
- path relative to `/sandbox/output`;
- size agrees with `len(content)`;
- digest computed by the host;
- no host absolute path;
- no container ID;
- no mutable byte buffer.

The `content` field must be excluded or abbreviated in `repr` to prevent
accidental console/log dumping.

### 27. Temporary host directories are private and ephemeral

Use a dedicated ignored root, for example:

```text
data/sandbox/tmp/
```

Each job receives a unique directory generated from a host-owned job ID.

Requirements:

- no user text in directory names;
- best-effort restrictive permissions;
- source/input/collection directories deleted in `finally`;
- no retention after the result object has been constructed;
- no secret values written there;
- `.gitignore` covers the complete `data/sandbox/` runtime tree.

### 28. Container identity is host-generated

Container name shape:

```text
lllm-sandbox-<job_id>
```

Requirements:

- `job_id` generated by the host;
- safe fixed character set;
- never derived from source or filename;
- label the container with a project marker and job ID;
- cleanup targets the exact returned container ID, not a broad name pattern.

Recommended labels:

```text
lllm.sandbox=true
lllm.sandbox.job_id=<job_id>
lllm.sandbox.spec=015
```

### 29. Cleanup is mandatory and idempotent

Every path must attempt:

1. terminate the container;
2. remove the container;
3. remove host temporary directories.

Cleanup must tolerate:

- container already stopped;
- `--rm` already removed the container;
- Docker daemon disappearing during cleanup;
- temporary directory already absent.

Cleanup errors must not replace a more important primary outcome.

They must be traced.

If execution succeeded but mandatory cleanup cannot be confirmed, the runtime
must not report an unqualified successful result. It should raise a sanitised
`SandboxRuntimeError` or return a documented non-success status.

### 30. Keyboard interrupt performs cleanup before propagation

If the trusted host receives `KeyboardInterrupt` during execution:

1. kill/remove the container;
2. remove host temporary directories;
3. emit a terminal sandbox trace event;
4. re-raise `KeyboardInterrupt`.

Do not convert it into a normal script failure.

This preserves the existing application-level user-interrupt semantics when the
runtime is connected in SPEC-016.

### 31. Docker Desktop is a local-lab boundary, not a public multi-tenant guarantee

The security claim must be precise.

SPEC-015 provides:

- process isolation through Docker;
- network removal;
- no host writable mount;
- read-only root;
- explicit resource controls;
- a non-root job;
- capability removal;
- hard container termination;
- bounded data crossing.

It does not claim:

- formally verified isolation;
- a hardened public code-runner service;
- protection against an unknown Docker/kernel escape;
- safe execution for arbitrary internet users;
- tenant isolation.

The README and journal must describe it as a local single-user laboratory
sandbox.

---

## Data model

### `SandboxLanguage`

```python
from enum import StrEnum


class SandboxLanguage(StrEnum):
    PYTHON = "python"
    BASH = "bash"
```

Only these exact values are accepted.

### `SandboxStatus`

```python
class SandboxStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    REJECTED = "rejected"
```

### `SandboxJob`

```python
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class SandboxJob:
    language: SandboxLanguage
    source: str
    input_files: Mapping[str, bytes] = field(default_factory=dict)
```

Required semantics:

- immutable;
- defensive copy of `input_files`;
- unique paths;
- byte values only;
- no timeout/image/command/environment/mount/resource fields;
- no caller-supplied job ID.

A mapping proxy or equivalent deep immutability is required.

### `SandboxPolicy`

Representative shape:

```python
@dataclass(frozen=True)
class SandboxPolicy:
    image_ref: str
    execution_timeout_seconds: float
    docker_control_timeout_seconds: float
    cleanup_timeout_seconds: float
    memory_bytes: int
    cpus: float
    pids_limit: int
    nofile_limit: int
    tmp_bytes: int
    output_tmpfs_bytes: int
    max_source_bytes: int
    max_input_files: int
    max_input_file_bytes: int
    max_input_total_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_artifact_files: int
    max_artifact_file_bytes: int
    max_artifact_total_bytes: int
    max_artifact_path_chars: int
```

The implementation may group related limits into nested immutable dataclasses if
that makes validation clearer.

The policy must have a stable fingerprint for traces.

### `SandboxArtifact`

```python
@dataclass(frozen=True)
class SandboxArtifact:
    path: str
    size_bytes: int
    sha256: str
    content: bytes = field(repr=False)
```

### `SandboxResult`

Representative shape:

```python
@dataclass(frozen=True)
class SandboxResult:
    job_id: str
    status: SandboxStatus
    language: SandboxLanguage
    image_id: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: tuple[SandboxArtifact, ...]
    duration_ms: int
    error_type: str | None = None
    error_message: str | None = None
```

Required invariants:

```text
completed -> exit_code == 0, error_type is None
failed    -> exit_code is not None and exit_code != 0
timed_out -> exit_code is None, error_type == execution_timeout
rejected  -> image_id may be None and no Docker call occurred
non-completed -> artifacts == ()
```

### `SandboxRuntime`

```python
from typing import Protocol


class SandboxRuntime(Protocol):
    def execute(self, job: SandboxJob, *, turn_id: str | None = None) -> SandboxResult:
        ...
```

The concrete runtime receives host-owned `run_id`, `SandboxPolicy`, and
`TraceSink` during construction, matching the existing MCP manager convention.

---

## Proposed module structure

```text
sandbox_runtime/
├── __init__.py
├── models.py
├── policy.py
├── paths.py
├── artifacts.py
├── command_runner.py
├── docker_backend.py
├── errors.py
└── tracing.py
```

Responsibilities:

### `models.py`

- immutable enums and dataclasses;
- model invariants;
- no Docker calls;
- no filesystem I/O.

### `policy.py`

- default policy construction from `config.py`;
- startup/runtime validation;
- stable policy fingerprint;
- language-to-command mapping remains host-owned.

### `paths.py`

- input relative-path validation;
- safe host materialisation;
- temporary directory ownership;
- no Docker behavior.

### `artifacts.py`

- copied output traversal;
- regular-file validation;
- bounds;
- deterministic sorting;
- digest and bounded bytes.

### `command_runner.py`

- small injectable abstraction over trusted Docker CLI calls;
- bounded control-command execution;
- streaming exec output;
- no business policy.

### `docker_backend.py`

- image preflight;
- container argument construction;
- lifecycle orchestration;
- hard kill;
- artifact copy;
- cleanup;
- structured result mapping.

### `errors.py`

- typed, sanitised host/runtime exceptions.

### `tracing.py`

- sandbox-specific event helpers using the existing root `tracing.build_event`;
- no second trace sink implementation.

The split is advisory. Equivalent small modules are acceptable when dependency
direction and testability remain clear.

---

## Docker command contract

The exact command may vary slightly by Docker version, but the effective policy
must be equivalent to the following.

### Holder start

Conceptual argument vector:

```text
docker run
  --detach
  --rm
  --name lllm-sandbox-<job_id>
  --label lllm.sandbox=true
  --label lllm.sandbox.job_id=<job_id>
  --label lllm.sandbox.spec=015
  --network none
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --memory 256m
  --memory-swap 256m
  --cpus 1.0
  --pids-limit 64
  --ulimit nofile=64:64
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
  --tmpfs /sandbox/output:rw,nosuid,nodev,size=8m,uid=65532,gid=65532,mode=0700
  --mount type=bind,src=<host-source>,dst=/sandbox/source,readonly
  --mount type=bind,src=<host-input>,dst=/sandbox/input,readonly
  --env HOME=/sandbox/output
  --env LANG=C.UTF-8
  --env LC_ALL=C.UTF-8
  --env PATH=/usr/local/bin:/usr/bin:/bin
  --env PYTHONUNBUFFERED=1
  --env PYTHONDONTWRITEBYTECODE=1
  <resolved-image-id>
  /usr/local/bin/lllm-sandbox-hold
```

Do not use a shell to assemble it.

### Python execution

```text
docker exec
  --user 65532:65532
  --workdir /sandbox/output
  <container-id>
  python3
  -I
  -B
  /sandbox/source/main.py
```

### Bash execution

```text
docker exec
  --user 65532:65532
  --workdir /sandbox/output
  <container-id>
  /bin/bash
  --noprofile
  --norc
  /sandbox/source/main.sh
```

### Artifact collection

Conceptual fixed source:

```text
docker cp <container-id>:/sandbox/output/. <host-collection-dir>
```

Only after exit code zero.

### Termination

Conceptual cleanup:

```text
docker kill <container-id>
docker rm --force <container-id>
```

The implementation must handle the expected `--rm` already-removed case.

---

## Configuration

Add host-owned constants to `config.py`.

Representative configuration:

```python
SANDBOX_IMAGE_REF = "lllm-sandbox:spec-015"

SANDBOX_EXECUTION_TIMEOUT_SECONDS = 10
SANDBOX_DOCKER_CONTROL_TIMEOUT_SECONDS = 10
SANDBOX_CLEANUP_TIMEOUT_SECONDS = 5

SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024
SANDBOX_CPUS = 1.0
SANDBOX_PIDS_LIMIT = 64
SANDBOX_NOFILE_LIMIT = 64

SANDBOX_TMP_BYTES = 16 * 1024 * 1024
SANDBOX_OUTPUT_TMPFS_BYTES = 8 * 1024 * 1024

SANDBOX_MAX_SOURCE_BYTES = 100_000
SANDBOX_MAX_INPUT_FILES = 20
SANDBOX_MAX_INPUT_FILE_BYTES = 1_000_000
SANDBOX_MAX_INPUT_TOTAL_BYTES = 2_000_000

SANDBOX_MAX_STDOUT_BYTES = 100_000
SANDBOX_MAX_STDERR_BYTES = 100_000

SANDBOX_MAX_ARTIFACT_FILES = 20
SANDBOX_MAX_ARTIFACT_FILE_BYTES = 2_000_000
SANDBOX_MAX_ARTIFACT_TOTAL_BYTES = 5_000_000
SANDBOX_MAX_ARTIFACT_PATH_CHARS = 240

SANDBOX_TEMP_ROOT = PROJECT_ROOT / "data" / "sandbox" / "tmp"
```

Do not add:

```python
SANDBOX_ENABLED
```

to the application startup path in this spec.

The runtime is instantiated explicitly by tests and smoke scripts.

### Configuration validation

Reject at construction/preflight:

- empty image reference;
- `latest` tag;
- non-positive timeout;
- execution + cleanup timeout not below the existing tool timeout;
- memory below 64 MiB;
- CPU <= 0;
- PID limit < 8;
- file-descriptor limit < 16;
- non-positive tmpfs limits;
- any per-file limit above its total limit;
- artifact total above output tmpfs capacity;
- stdout/stderr limit <= 0;
- path limit too small for ordinary filenames;
- unsupported language configuration;
- unsafe fixed environment key/value.

No invalid policy may reach Docker.

---

## Tracing

Reuse:

```text
TraceSink
build_event
SafeTraceSink
MemoryTraceSink
JsonlTraceSink
```

Do not create another JSONL format.

Sandbox events are additive under trace schema version 1 unless implementation
changes an existing event's meaning.

Required events:

```text
sandbox_job_started
sandbox_container_started
sandbox_execution_finished
sandbox_artifact_collection_finished
sandbox_cleanup_finished
sandbox_job_finished
```

A rejected job may emit:

```text
sandbox_job_started
sandbox_policy_violation
sandbox_job_finished
```

A setup failure before a job starts may emit a host diagnostic event but does
not need to fabricate a successful `sandbox_job_started`.

### Required common fields

```text
run_id
job_id
turn_id             optional in SPEC-015, populated by SPEC-016
language
image_id            after successful preflight
policy_fingerprint
duration_ms          on finished events
```

### Safe source metadata

The trace may record:

```text
source_bytes
source_sha256
input_file_count
input_total_bytes
```

The trace must not record:

- complete source;
- complete input file content;
- artifact content;
- host temporary paths;
- host environment;
- Docker socket paths;
- complete stdout/stderr beyond the existing preview bound.

### Output trace metadata

Allowed:

```text
exit_code
stdout_bytes
stderr_bytes
stdout_truncated
stderr_truncated
artifact_count
artifact_total_bytes
status
error_type
cleanup_ok
```

Any stdout/stderr preview must use the existing bounded preview/hash approach.

### Terminal-event invariant

Every emitted `sandbox_job_started` must have exactly one
`sandbox_job_finished`, unless the complete host process is externally
terminated.

`SandboxRuntime.execute()` must enforce this with `finally`, following the
existing `AgentRunner` terminal-event pattern.

---

## Error taxonomy

### Expected job error categories

```text
invalid_job
unsupported_language
source_too_large
input_path_invalid
input_file_limit
input_size_limit
execution_timeout
output_limit
artifact_policy_violation
nonzero_exit
```

A stable public `error_message` accompanies each category.

### Host/runtime exception types

```python
class SandboxError(Exception): ...
class SandboxUnavailable(SandboxError): ...
class SandboxImageUnavailable(SandboxError): ...
class SandboxRuntimeError(SandboxError): ...
```

Host/runtime exceptions must not include:

- host absolute paths;
- source code;
- input bytes;
- artifact bytes;
- environment values;
- raw unbounded Docker stderr.

---

## Security invariants

The following are acceptance-level invariants, not recommendations.

### Host execution

- untrusted source never runs outside Docker;
- no `shell=True`;
- no model/caller command string reaches a shell;
- Docker arguments are a list of separate tokens.

### Filesystem

- root filesystem read-only;
- source read-only;
- input read-only;
- writable output is container `tmpfs`;
- no writable host bind mount;
- no project-root mount;
- no home-directory mount;
- no `.env` mount;
- no Docker socket mount;
- artifacts only from `/sandbox/output`;
- symlinks and non-regular output rejected.

### Identity and privilege

- job runs as fixed non-root UID/GID;
- all capabilities dropped;
- no-new-privileges enabled;
- no privileged mode;
- no devices;
- no additional groups.

### Network

- network mode `none`;
- no published ports;
- no host network;
- no proxy variables.

### Resources

- fixed memory;
- fixed CPU;
- fixed PIDs;
- fixed file descriptors;
- fixed wall time;
- bounded stdout;
- bounded stderr;
- bounded writable tmpfs;
- bounded artifacts.

### Data crossing

Into container:

```text
source
validated input files
fixed non-secret environment
```

Out of container:

```text
bounded stdout
bounded stderr
bounded regular-file artifacts after successful exit
exit code
timing and metadata
```

Nothing else crosses by design.

---

## Testing strategy

### 1. Deterministic unit tests — no Docker

Unit tests must use an injectable fake command runner and temporary directories.

Suggested files:

```text
tests/test_sandbox_models.py
tests/test_sandbox_policy.py
tests/test_sandbox_paths.py
tests/test_sandbox_artifacts.py
tests/test_docker_sandbox_runtime.py
tests/support_sandbox.py
```

Mandatory deterministic cases:

1. `SandboxJob` defensively freezes input mappings.
2. Unsupported language is rejected before Docker.
3. Empty source is rejected.
4. Source byte limit is enforced.
5. Absolute input path rejected.
6. `../` rejected.
7. backslash rejected.
8. duplicate normalized path rejected.
9. input count enforced.
10. per-file input limit enforced.
11. total input limit enforced.
12. fixed source file cannot be overwritten by input.
13. policy validation rejects `latest`.
14. policy validation rejects incoherent timeouts.
15. policy validation rejects artifact total above output tmpfs.
16. image tag is resolved to an exact image ID.
17. container creation uses the image ID, not the tag.
18. Docker command is an argument vector.
19. `shell=True` is never used.
20. required isolation flags are present.
21. forbidden flags/mounts are absent.
22. host environment is not forwarded.
23. Python command is exact.
24. Bash command is exact.
25. script executes as fixed non-root UID/GID.
26. stdout cap stops execution.
27. stderr cap stops execution.
28. timeout invokes container kill.
29. nonzero exit returns `failed`.
30. failed job does not collect artifacts.
31. successful job copies output before cleanup.
32. artifacts sorted by path.
33. artifact content and digests are correct.
34. artifact count limit enforced.
35. artifact per-file limit enforced.
36. artifact total limit enforced.
37. symlink output rejected.
38. FIFO/socket/device-like output rejected when represented by fixtures.
39. cleanup runs after success.
40. cleanup runs after nonzero exit.
41. cleanup runs after timeout.
42. cleanup runs after output limit.
43. cleanup runs after artifact-policy failure.
44. cleanup runs before `KeyboardInterrupt` propagates.
45. primary outcome is not replaced by a secondary cleanup warning.
46. trace includes one terminal job event.
47. trace never includes source or artifact bytes.
48. trace uses source hash and bounded metadata.
49. host temporary directories are removed.
50. two jobs receive distinct IDs and directories.

The full existing `pytest` suite must continue to pass.

### 2. Opt-in live Docker integration tests

Live tests must be skipped by default.

Recommended opt-in variable:

```text
LLLM_SANDBOX_LIVE=1
```

Recommended command:

```bash
LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_integration.py -q
```

The test may require:

```bash
python scripts/build_sandbox_image.py
```

first.

Mandatory live scenarios:

#### A. Python success

```python
print(sum(range(100)))
```

Expected `4950`.

#### B. Bash success

Writes and prints a small deterministic value.

#### C. Python artifact

Creates one CSV under `/sandbox/output`.

Host verifies content, size, and digest.

#### D. Read-only input

Input file is readable from `/sandbox/input`.

An attempt to modify it fails.

#### E. Read-only root

Attempt to write under `/etc` fails.

#### F. Host file not mounted

Create a random host secret file outside the source/input roots.

The container cannot read its absolute host path.

#### G. Host environment not forwarded

Set a random environment variable in the pytest process.

The job cannot read it.

#### H. `.env` not forwarded

A local `.env` value does not appear in the job environment.

#### I. Network disabled

A Python socket connection attempt fails without contacting an external service.

The test must not depend on an internet host being reachable.

#### J. Docker socket absent

`/var/run/docker.sock` does not exist in the job.

#### K. Non-root identity

The job reports a non-zero UID equal to the configured job UID.

#### L. Timeout hard-kills

Infinite loop returns within bounded host time.

Container is absent afterward.

#### M. Descendant process killed

A Bash/Python job starts a child process and then blocks.

Timeout removes both with the container.

#### N. PID limit

A controlled process-spawn script reaches a resource failure or is stopped
without affecting the host.

Do not use an uncontrolled classic fork bomb.

#### O. Memory limit

A controlled allocation attempt fails or is killed within the container without
destabilising the host test process.

#### P. Output tmpfs bound

A script attempts to create a file larger than the output tmpfs.

The host disk does not receive that large file.

The result is non-successful and returns no artifact.

#### Q. Stdout limit

Continuous output is stopped and container removed.

#### R. Nonzero exit

Returns `failed`, preserves bounded stderr, no artifacts.

#### S. Artifact symlink

A script creates a symlink in output.

Artifact collection rejects the output set.

#### T. Job isolation

Job B cannot see Job A's output.

#### U. Cleanup

After every scenario, no container with that exact job ID remains.

### 3. Smoke script

`scripts/sandbox_smoke.py` runs a concise safe subset of the live integration
cases and prints a human-readable summary.

It must:

- never accept arbitrary code from CLI arguments;
- use committed fixed smoke sources;
- create a fresh `run_id`;
- use `MemoryTraceSink` or an explicit local trace path;
- return exit code `0` only when all mandatory smoke checks pass;
- perform cleanup on interruption.

### 4. Live model regression

Because SPEC-015 does not alter model-facing behavior, no model sandbox
interaction exists to verify.

The journal must still record one ordinary live-model regression:

- start `python app.py`;
- perform one direct answer;
- perform one existing safe tool call;
- exit cleanly;
- confirm no sandbox declaration appeared.

This proves the new package did not accidentally change the current agent
surface.

---

## Script contracts

### `scripts/build_sandbox_image.py`

Responsibilities:

- verify Docker CLI;
- build from the committed Dockerfile;
- use the configured tag;
- print resolved image ID;
- fail with a concise message;
- no model input;
- no application startup integration.

It may accept only developer-safe flags such as:

```text
--no-cache
```

It must not accept an arbitrary Dockerfile path, image tag, build argument, or
remote context in SPEC-015.

### `scripts/sandbox_smoke.py`

Responsibilities:

- instantiate the default policy;
- preflight the image;
- run committed smoke jobs;
- assert isolation;
- print pass/fail summary;
- clean up.

It must not become a general local shell runner.

---

## Expected project structure

```text
sandbox/
└── image/
    ├── Dockerfile
    ├── hold.py
    └── README.md

sandbox_runtime/
├── __init__.py
├── models.py
├── policy.py
├── paths.py
├── artifacts.py
├── command_runner.py
├── docker_backend.py
├── errors.py
└── tracing.py

scripts/
├── build_sandbox_image.py
└── sandbox_smoke.py

tests/
├── support_sandbox.py
├── test_sandbox_models.py
├── test_sandbox_policy.py
├── test_sandbox_paths.py
├── test_sandbox_artifacts.py
├── test_docker_sandbox_runtime.py
└── test_sandbox_integration.py

specs/
└── SPEC-015-Isolated-Sandbox-Runtime.md

docs/journal/
└── SPEC-015-isolated-sandbox-runtime.md
```

Existing files likely affected:

```text
config.py
.gitignore
README.md
requirements-dev.txt        only if a pytest marker/config update is needed
```

Files that should remain behaviorally unchanged:

```text
app.py
agent.py
conversation.py
llm.py
prompts.py
storage.py
tools/executor.py
tools/python_calculate.py
tools/sql_query.py
skill_runtime/*
mcp_integration/*
skills/*
```

A small import-only change to a shared utility is allowed only when justified in
the journal. There should be no sandbox registration in `app.py`.

---

## README requirements

Update the root Russian README with a section similar to:

```text
### Изолированный sandbox runtime (SPEC-015)
```

It must explain:

- this is a host-only infrastructure layer;
- Docker Desktop/Engine is required only for sandbox smoke/integration tests;
- ordinary chat still works without Docker;
- how to build the image;
- how to run the smoke suite;
- Python and Bash are supported;
- runtime network is disabled;
- root filesystem is read-only;
- generated files live in bounded container tmpfs;
- scripts run as non-root;
- the container is killed on timeout;
- the model cannot use the sandbox yet;
- STEP 16 will add the agent workspace/tool boundary;
- this is a local lab sandbox, not a public multi-tenant execution service.

Update the project structure table.

---

## `.gitignore` requirements

Ignore:

```text
data/sandbox/
```

Do not ignore:

```text
sandbox/
sandbox_runtime/
tests/test_sandbox_*.py
scripts/build_sandbox_image.py
scripts/sandbox_smoke.py
```

No generated artifact, source copy, input copy, Docker ID file, or smoke output
belongs in git.

---

## Dependency requirements

Runtime Python dependencies remain:

```text
ollama==0.6.2
mcp>=1.27,<2
```

No new runtime package is required.

Docker is an external developer/runtime prerequisite for the sandbox only.

The Docker image may contain system packages installed at image-build time, but
those package versions and the pinned base digest must be documented in
`sandbox/image/README.md` and the implementation journal.

---

## Implementation sequence

### Phase 1 — Models and policy

1. add immutable models;
2. add default host configuration;
3. validate all limits;
4. add policy fingerprint;
5. add deterministic tests.

No Docker calls yet.

### Phase 2 — Safe path and artifact boundaries

1. validate input paths;
2. materialise source/input safely;
3. validate copied output;
4. create artifact objects;
5. test traversal, symlinks, file types, and limits.

### Phase 3 — Docker image

1. add pinned Dockerfile;
2. add fixed holder;
3. add image README;
4. add build script;
5. build and inspect locally.

### Phase 4 — Docker command boundary

1. add injectable command runner;
2. add bounded control calls;
3. add streaming exec capture;
4. test exact argument vectors and output caps.

### Phase 5 — Runtime lifecycle

1. image preflight;
2. holder start;
3. fixed non-root exec;
4. timeout/output stop;
5. success-only artifact copy;
6. idempotent cleanup;
7. terminal trace event.

### Phase 6 — Verification and documentation

1. full deterministic tests;
2. opt-in live Docker tests;
3. smoke script;
4. full project pytest;
5. live ordinary-chat regression;
6. README;
7. journal.

---

## Acceptance criteria

### Architecture

- [ ] `sandbox_runtime/` exists as an independent infrastructure package.
- [ ] It is not imported by `app.py`, `tools/`, `skill_runtime/`, or
      `prompts.py` in SPEC-015.
- [ ] No sandbox `ToolSpec` exists.
- [ ] No sandbox skill exists.
- [ ] `python_calculate` remains restricted and unchanged in security meaning.
- [ ] Untrusted code never executes in the main process.
- [ ] No new Python runtime dependency is added.

### Image

- [ ] Committed Dockerfile exists.
- [ ] Base image is pinned by digest.
- [ ] No `latest` reference exists in committed sandbox configuration.
- [ ] Image contains Python 3.12 and Bash.
- [ ] Fixed non-root job user exists.
- [ ] Fixed holder exists.
- [ ] No project secrets or source are baked into the image.
- [ ] Build script produces the configured local tag.
- [ ] Runtime resolves tag to immutable image ID.
- [ ] Container uses the image ID.

### Container isolation

- [ ] `--network none`.
- [ ] `--read-only`.
- [ ] `--cap-drop ALL`.
- [ ] `no-new-privileges`.
- [ ] fixed non-root exec user.
- [ ] no privileged mode.
- [ ] no devices.
- [ ] no host PID/IPC/network.
- [ ] no Docker socket.
- [ ] no project-root mount.
- [ ] no home mount.
- [ ] source/input are read-only.
- [ ] output is writable container `tmpfs`, not a writable host bind mount.
- [ ] fixed environment only.
- [ ] no host environment forwarding.

### Resource policy

- [ ] wall-time limit.
- [ ] hard container kill on timeout.
- [ ] memory limit.
- [ ] CPU limit.
- [ ] PID limit.
- [ ] file-descriptor limit.
- [ ] bounded `/tmp`.
- [ ] bounded output tmpfs.
- [ ] bounded stdout.
- [ ] bounded stderr.
- [ ] bounded source.
- [ ] bounded input files.
- [ ] bounded artifacts.
- [ ] sandbox timeout plus cleanup fits below outer tool timeout.

### Files and artifacts

- [ ] strict relative POSIX input paths.
- [ ] no path traversal.
- [ ] no symlink input.
- [ ] fixed source filename.
- [ ] artifacts collected only after exit code zero.
- [ ] artifacts copied while holder is alive.
- [ ] regular files only.
- [ ] symlinks rejected.
- [ ] special files rejected.
- [ ] deterministic artifact order.
- [ ] artifact digest and size supplied.
- [ ] artifact bytes bounded.
- [ ] no host path in public artifact metadata.
- [ ] temporary host directories deleted.

### Lifecycle and errors

- [ ] one job = one container.
- [ ] unique host-owned job ID.
- [ ] cleanup after success.
- [ ] cleanup after nonzero exit.
- [ ] cleanup after timeout.
- [ ] cleanup after output limit.
- [ ] cleanup after artifact rejection.
- [ ] cleanup before `KeyboardInterrupt` propagates.
- [ ] no expected path prints a raw host traceback.
- [ ] job failures return stable results.
- [ ] Docker/image/setup failures raise typed sanitised exceptions.
- [ ] secondary cleanup failure does not hide the primary failure.
- [ ] successful result is not returned when mandatory cleanup is unconfirmed.

### Tracing

- [ ] existing `TraceSink` reused.
- [ ] required sandbox events emitted.
- [ ] one terminal sandbox event per started job.
- [ ] source recorded only by length/hash.
- [ ] input content absent.
- [ ] artifact content absent.
- [ ] host environment absent.
- [ ] host temporary paths absent.
- [ ] output previews bounded.
- [ ] cleanup status visible.

### Tests

- [ ] deterministic tests require no Docker.
- [ ] mandatory deterministic cases pass.
- [ ] live Docker tests are opt-in.
- [ ] mandatory live scenarios pass on the development Mac.
- [ ] smoke script passes.
- [ ] full existing pytest suite passes.
- [ ] ordinary live chat regression passes.
- [ ] no sandbox tool appears to the model.

### Documentation and process

- [ ] root README updated in Russian.
- [ ] sandbox image README documents provenance and build.
- [ ] `.gitignore` covers runtime sandbox data.
- [ ] `docs/journal/SPEC-015-isolated-sandbox-runtime.md` records:
  - image base digest;
  - resolved image ID;
  - Docker version;
  - macOS / architecture;
  - Python and Bash versions inside the image;
  - effective resource limits;
  - deterministic test count;
  - live integration transcript;
  - cleanup evidence;
  - ordinary model regression;
  - deviations.
- [ ] step delivered through feature branch
      `feature/SPEC-015-isolated-sandbox-runtime`.
- [ ] merge uses `--no-ff`.

---

## Verification commands

Representative implementation verification:

```bash
python -m pytest -q
```

Build image:

```bash
python scripts/build_sandbox_image.py
```

Run opt-in integration tests:

```bash
LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_integration.py -q
```

Run human-readable smoke:

```bash
python scripts/sandbox_smoke.py
```

Inspect for leaked containers:

```bash
docker ps --filter label=lllm.sandbox=true
docker ps -a --filter label=lllm.sandbox=true
```

Both must be empty after tests.

Run ordinary chat regression:

```bash
python app.py
```

Confirm that the tool catalog still contains only the pre-existing tools enabled
by local/MCP configuration and that no sandbox tool is announced.

---

## Expected journal evidence

The journal should contain concise real output for at least:

### Python success

```text
status=completed exit_code=0 stdout="4950\n"
```

### Bash artifact

```text
status=completed artifact=report.csv size=... sha256=...
```

### Timeout

```text
status=timed_out error_type=execution_timeout duration_ms=...
container_present_after=false
```

### Network denial

```text
network_access=false
```

### Host isolation

```text
host_secret_env_visible=false
host_secret_file_visible=false
docker_socket_visible=false
```

### Read-only and resource enforcement

```text
root_write_allowed=false
output_tmpfs_limit_enforced=true
stdout_limit_enforced=true
```

### Cleanup

```text
remaining_labeled_containers=0
remaining_job_temp_directories=0
```

### Existing agent regression

A short real `qwen3:8b` transcript showing:

- normal answer;
- existing tool call;
- no sandbox declaration;
- clean exit.

---

## Risks and mitigations

### Risk: Docker CLI output or behavior differs slightly across versions

Mitigation:

- isolate Docker interaction in one module;
- parse minimal stable fields;
- keep public errors stable;
- record tested Docker version in the journal;
- assert effective security flags in integration tests.

### Risk: a writable bind mount permits host disk exhaustion

Mitigation:

- no writable output bind mount;
- output lives in bounded container tmpfs;
- copy only after successful exit.

### Risk: timeout returns but code keeps running

Mitigation:

- kill the complete container;
- verify disappearance from Docker state;
- no thread-only cancellation claim.

### Risk: output floods host memory

Mitigation:

- incremental dual-stream reading;
- byte counters;
- kill on limit;
- bounded retained preview.

### Risk: artifact traversal follows a symlink

Mitigation:

- copy to private temporary collection root;
- `lstat`;
- never follow symlinks;
- accept regular files only;
- verify relative paths.

### Risk: host secrets leak through environment

Mitigation:

- construct a fixed environment from scratch;
- integration test with random host-only secret;
- never forward `.env`.

### Risk: Docker socket grants host control

Mitigation:

- never mount it;
- test absence from the container;
- omit Docker CLI from the image.

### Risk: image tag changes between preflight and run

Mitigation:

- resolve to immutable image ID;
- run by ID.

### Risk: a container remains after host failure

Mitigation in scope:

- `try/finally`;
- exact container ID cleanup;
- `--rm`;
- integration checks after every scenario.

Abrupt host-process or machine termination may still leave a holder container.
An explicit stale-container cleanup command may be added later as a focused
PATCH if real usage demonstrates the need. SPEC-015 must not silently delete
all labeled containers at startup because another legitimate process may own
one.

### Risk: Docker is treated as perfect isolation

Mitigation:

- document the local-lab threat model;
- do not expose the service remotely;
- no multi-tenant claim;
- retain multiple defense layers inside Docker.

---

## Future compatibility with SPEC-016

SPEC-015 must leave a narrow seam for the next step:

```python
runtime.execute(job, turn_id=current_turn_id)
```

SPEC-016 may then add:

```text
sandbox_execute ToolSpec
    │
    ▼
argument validation
    │
    ▼
SandboxJob
    │
    ▼
DockerSandboxRuntime
    │
    ▼
tool result + artifact persistence
```

The future adapter must not need to:

- construct Docker flags;
- know host paths;
- choose image;
- choose mounts;
- choose command;
- implement timeout cleanup;
- implement artifact safety;
- access secrets.

Those responsibilities belong permanently to SPEC-015.

---

## Definition of Done

SPEC-015 is complete when:

1. the isolated runtime and project image exist;
2. Python and Bash jobs execute only inside disposable containers;
3. the effective isolation and resource policy is proven by live tests;
4. timeout and output overflow hard-kill the container;
5. no writable host output mount exists;
6. only bounded regular-file artifacts cross back after successful execution;
7. host environment, host files, network, and Docker socket are inaccessible;
8. cleanup is deterministic on all tested paths;
9. structured traces contain metadata but no source/input/artifact content;
10. all deterministic and opt-in live tests pass;
11. ordinary `lLLM` chat behavior remains unchanged;
12. the model has no sandbox capability yet;
13. README and journal accurately document both protections and limitations;
14. the step is merged through the established SPEC cycle.
