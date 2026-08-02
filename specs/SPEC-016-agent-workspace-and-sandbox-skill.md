# SPEC-016 — Agent Workspace & Sandbox Skill

**Status:** Proposed  
**Step:** 16  
**Depends on:** SPEC-010, SPEC-011, SPEC-012, SPEC-015  
**Target repository:** `egorgut/lLLM`

---

## 1. Summary

SPEC-015 introduced an isolated Docker-based runtime capable of executing one untrusted Python or Bash script per disposable container. That runtime is deliberately host-only: it is not registered in `ToolRegistry`, is not visible to the model, is not available through a skill, and is not imported by the normal application path.

This step connects the existing runtime to the agent architecture through:

1. one model-facing tool, `sandbox_execute`;
2. one new skill, `code_workspace`;
3. a turn-scoped workspace and artifact publication layer bound to `run_id` and `turn_id`;
4. tracing that correlates agent tool calls with sandbox `job_id` without recording code or file contents;
5. integration with the existing tool-call budget, repeated-call detection, whole-turn deadline, typed termination outcomes, rollback, and semantic-only conversation persistence.

The design must reuse the runtime from SPEC-015 as the sole code-execution mechanism. The model must never receive control over Docker commands, image references, mounts, host paths, environment variables, network configuration, resource limits, container lifecycle, or sandbox cleanup.

---

## 2. Motivation

The project already has:

- a global `ToolRegistry`;
- a `ToolExecutor`;
- a bounded agent loop with at most four tool calls per turn;
- skill-scoped tool allowlists;
- repeated-call detection;
- per-request and whole-turn deadlines;
- typed termination outcomes;
- JSONL tracing;
- rollback of unsuccessful turns;
- semantic-only conversation persistence;
- an isolated Python/Bash runtime from SPEC-015.

What is still missing is the agent-facing workspace abstraction.

Without this step, the runtime can be exercised only by tests and host scripts. The agent cannot use code to transform input files, perform calculations that exceed `python_calculate`, create structured output files, inspect execution errors, correct its script, or return produced artifacts to the user.

The required integration must remain narrow. This is not a general terminal, persistent filesystem, package manager, Docker controller, or long-running compute service. It is a bounded skill that allows the model to submit one complete script and optional input files to the already-defined isolated runtime.

---

## 3. Goals

### 3.1 Functional goals

The agent must be able to:

- decide whether a task genuinely requires code;
- choose Python or Bash;
- submit one complete source file and optional input files in one tool call;
- receive structured execution status, exit code, stdout, stderr, and artifact metadata;
- inspect a correctable error and retry with corrected code;
- create user-facing artifacts such as CSV, JSON, Markdown, or plain text;
- return a final answer that includes the result, limitations, and paths or links to created artifacts;
- operate within the existing maximum of four tool calls and the whole-turn deadline.

### 3.2 Architectural goals

The implementation must:

- reuse `sandbox_runtime` from SPEC-015 unchanged as the execution boundary;
- expose one tool, `sandbox_execute`;
- add one skill, `code_workspace`;
- keep source creation and execution inside one tool call;
- bind all sandbox work to the current `run_id` and `turn_id`;
- isolate files between turns;
- publish only successful artifacts;
- keep transient files and sandbox protocol details out of semantic history;
- preserve rollback semantics for unsuccessful turns;
- preserve all existing agent reliability and observability mechanisms.

### 3.3 Security goals

The model must not be able to control or infer operational access to:

- Docker CLI arguments;
- Docker socket;
- container image or image tag;
- bind mounts;
- host filesystem paths;
- environment variables;
- secrets;
- network mode;
- resource ceilings;
- container user;
- capabilities;
- cleanup behavior;
- background execution.

All such policy remains host-owned in SPEC-015.

---

## 4. Non-goals

The following are explicitly outside scope:

- persistent user file manager;
- persistent workspace across turns;
- interactive terminal;
- interactive REPL;
- streaming terminal session;
- package installation during execution;
- internet access;
- access to Ollama from the sandbox;
- access to Docker socket;
- access to host secrets or `.env`;
- arbitrary Docker commands;
- background jobs;
- jobs that outlive the current turn;
- parallel sandbox jobs;
- languages other than Python and Bash;
- direct exposure of `sandbox_runtime` internals to the model;
- automatic SQL-to-sandbox analytical workflow;
- a combined `sql_query` + `sandbox_execute` skill;
- artifact preview UI;
- artifact versioning;
- artifact sharing between conversations;
- multi-user hardening beyond the local laboratory threat model documented in SPEC-015.

A later skill or PATCH may combine database extraction and sandbox processing after this basic workspace is stable.

---

## 5. Existing constraints that must remain unchanged

The implementation must preserve:

- `ToolRegistry`;
- `ToolExecutor`;
- one global agent loop;
- maximum four tool calls per turn;
- repeated-call detection;
- whole-turn deadline;
- model-request deadline;
- tool-execution deadline;
- typed `AgentTurnOutcome`;
- JSONL tracing;
- skill allowlists;
- rollback of unsuccessful turns;
- semantic-only conversation persistence;
- deterministic tests without requiring live Ollama;
- normal application startup behavior when optional dependencies are unavailable, subject to the startup policy defined below.

No second agent loop, second tool registry, or sandbox-specific orchestration engine may be introduced.

---

## 6. User-visible capability

A user may ask, for example:

- “Read these CSV files and produce a Markdown summary.”
- “Generate a JSON file containing these calculated values.”
- “Use Python to transform this input into a new CSV.”
- “Write a Bash script that lists and summarizes the supplied text files.”
- “Create a report file from this data.”

The router may select `code_workspace`.

The active skill then exposes only `sandbox_execute`.

The agent writes a minimal script, invokes the tool, evaluates the structured result, optionally corrects the script, and returns a semantic answer plus artifact references.

A simple conceptual flow:

```text
User request
→ skill router selects code_workspace
→ agent decides Python or Bash
→ sandbox_execute(language, source, input_files)
→ SPEC-015 runtime.execute(job, turn_id=...)
→ status / exit_code / stdout / stderr / artifacts
→ agent corrects or finishes
→ successful turn commits semantic answer and artifact references
```

---

## 7. Tool contract

## 7.1 Tool name

```text
sandbox_execute
```

The tool must be registered as a normal local tool in `ToolRegistry`.

It must not be an MCP tool.

## 7.2 Model-facing input

Preferred logical contract:

```text
sandbox_execute(
    language,
    source,
    input_files
)
```

Recommended JSON Schema:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["language", "source"],
  "properties": {
    "language": {
      "type": "string",
      "enum": ["python", "bash"]
    },
    "source": {
      "type": "string",
      "minLength": 1
    },
    "input_files": {
      "type": "array",
      "default": [],
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name", "content"],
        "properties": {
          "name": {
            "type": "string",
            "minLength": 1
          },
          "content": {
            "type": "string"
          },
          "encoding": {
            "type": "string",
            "enum": ["utf-8", "base64"],
            "default": "utf-8"
          }
        }
      }
    }
  }
}
```

The exact transport representation may differ if the current tool schema layer cannot express defaults, but the semantic contract must remain the same.

### Input restrictions

The adapter must reject before runtime execution:

- unsupported languages;
- empty source;
- malformed input file entries;
- duplicate input file names;
- absolute file paths;
- path traversal;
- reserved internal names;
- directory entries;
- names that normalize outside the input root;
- invalid base64;
- values exceeding SPEC-015 source or input limits.

The adapter must not introduce larger limits than SPEC-015.

## 7.3 Why source creation and execution are one call

One tool call must both create the source file and execute it.

Do not split this into:

- `write_file`;
- `run_file`;
- `read_file`;
- `list_files`.

The existing agent loop permits at most four tool calls. A syntax-error correction already requires at least two executions. Splitting write and run would waste the budget, increase failure states, and create unnecessary persistent file semantics.

The model submits a complete new source value on every retry.

## 7.4 Model-facing result

Recommended logical result:

```text
status
exit_code
stdout
stderr
artifacts
```

Recommended JSON shape:

```json
{
  "ok": true,
  "status": "succeeded",
  "exit_code": 0,
  "stdout": "processed 12 rows\n",
  "stderr": "",
  "artifacts": [
    {
      "name": "summary.csv",
      "media_type": "text/csv",
      "size_bytes": 418,
      "path": "artifacts/<run_id>/<turn_id>/summary.csv"
    }
  ]
}
```

For a failed job:

```json
{
  "ok": false,
  "status": "non_zero_exit",
  "exit_code": 1,
  "stdout": "",
  "stderr": "Traceback ...",
  "artifacts": []
}
```

The result must be a bounded, JSON-serializable envelope.

## 7.5 Status values

The model-facing adapter should normalize SPEC-015 runtime outcomes into a small stable enum.

Required values:

```text
succeeded
non_zero_exit
timed_out
stdout_limit_exceeded
stderr_limit_exceeded
artifact_limit_exceeded
invalid_request
runtime_unavailable
runtime_error
```

If SPEC-015 already exposes more precise typed statuses, the adapter may retain them, but the public tool contract must remain documented and deterministic.

`ok` is true only when:

- the runtime completed successfully;
- exit code is zero;
- artifact collection and publication completed successfully.

## 7.6 Exit code

- Integer on normal process completion.
- `null` when no meaningful process exit code exists, such as validation failure, host runtime failure, or timeout.
- A non-zero exit must never be converted into a successful tool result.

## 7.7 stdout and stderr

- Must reuse SPEC-015 bounds.
- Must be returned as bounded text.
- Must not be copied into the JSONL trace.
- May be shown to the model because error correction requires it.
- Must not be persisted into semantic chat history as protocol messages.
- The final assistant answer may summarize relevant output, but should not dump excessive logs.

## 7.8 Artifacts

Artifacts are output files produced in the runtime output directory and accepted by SPEC-015.

Only artifacts from a successful zero-exit execution may be published.

Each artifact entry must contain bounded metadata only:

```text
name
relative path or user-resolvable reference
size_bytes
media_type when safely inferable
```

Do not return:

- host absolute path;
- Docker path;
- container ID;
- mount path;
- temporary job directory;
- file content in the tool envelope;
- hashes unless already cheaply available and useful;
- internal runtime implementation details.

---

## 8. Runtime adapter

Add a thin adapter between `ToolExecutor` and SPEC-015.

Suggested module:

```text
sandbox_tool/
├── __init__.py
├── handler.py
├── schema.py
├── workspace.py
└── artifacts.py
```

The exact layout may follow project conventions.

The adapter is responsible for:

1. validating model-facing arguments;
2. converting them into the existing SPEC-015 `SandboxJob`;
3. passing the current `turn_id` to `runtime.execute`;
4. receiving the typed runtime result;
5. publishing successful artifacts into the turn artifact directory;
6. normalizing the result into the model-facing JSON envelope;
7. emitting safe trace metadata;
8. guaranteeing cleanup through the SPEC-015 runtime and workspace layer.

The adapter must not reimplement:

- Docker invocation;
- image resolution;
- container limits;
- timeout killing;
- output streaming;
- artifact extraction security;
- container cleanup.

Those remain owned by SPEC-015.

---

## 9. Turn-scoped workspace

## 9.1 Identity

Every execution belongs to:

```text
run_id
turn_id
job_id
```

Semantics:

- `run_id` identifies the application session and trace file;
- `turn_id` identifies one user turn across routing and agent execution;
- `job_id` identifies one sandbox execution attempt.

The same `turn_id` is reused for retries within one user request.

Each `sandbox_execute` call receives a new `job_id`.

## 9.2 Isolation boundary

Files from one turn must never be visible to another turn.

Conceptual paths:

```text
data/workspaces/<run_id>/<turn_id>/...
data/artifacts/<run_id>/<turn_id>/...
```

The implementation may use different names, but must preserve the separation.

The model must never provide or choose `run_id`, `turn_id`, `job_id`, workspace roots, or artifact roots.

## 9.3 Temporary workspace

Temporary workspace may contain:

- generated source file;
- normalized input files;
- runtime staging files;
- collected output before publication;
- internal job metadata required by the host.

It must be:

- host-created;
- private;
- turn-scoped;
- excluded from git;
- inaccessible by another turn;
- removed after completion or rollback;
- absent from semantic chat history.

SPEC-015 job-level cleanup remains mandatory. STEP 16 adds turn-level ownership and publication semantics around it.

## 9.4 Artifact directory

Successful artifacts may be copied to a separate turn-scoped directory.

Recommended behavior:

- artifact directory is created lazily;
- only regular files accepted by SPEC-015 are published;
- file names remain relative and validated;
- overwrite policy is deterministic;
- artifacts from multiple successful calls in the same turn may coexist;
- duplicate artifact names must not silently overwrite unrelated prior artifacts.

Recommended collision policy:

```text
first successful artifact keeps its name;
later collision receives deterministic suffix:
report.csv
report-2.csv
report-3.csv
```

Alternatively, publication may use a per-job subdirectory:

```text
artifacts/<run_id>/<turn_id>/<job_id>/report.csv
```

The chosen policy must be documented and tested.

## 9.5 Rollback behavior

An unsuccessful agent turn must not leave user-visible committed artifacts.

Required semantics:

- artifacts are staged during the turn;
- the final successful `AgentTurnOutcome` commits the turn artifact set;
- a failed or stopped turn removes staged artifacts;
- rollback includes exceptions after a successful sandbox execution but before semantic turn commit;
- transient runtime files are always removed independently of conversation rollback.

This preserves the existing “unsuccessful turn does not commit” rule.

## 9.6 End-of-run cleanup

Turn artifacts are allowed to survive the turn so that the user can access them.

They may survive application restart if stored under `data/artifacts`, but this does not make them a persistent user file manager. No browsing, mutation, or cross-turn reuse API is introduced.

A future retention policy may clean old artifact directories.

---

## 10. Passing input files

## 10.1 Initial scope

`input_files` are inline files included in the tool arguments.

Supported encodings:

- UTF-8 text;
- base64 for bounded binary input.

This keeps the tool contract self-contained.

## 10.2 File naming

Input file names must be relative, normalized, and limited to a safe leaf or relative path policy supported by SPEC-015.

Reject:

```text
/etc/passwd
../secret
a/../../secret
C:\Windows\...
\\server\share
.
..
empty name
NUL or platform-reserved names where relevant
```

## 10.3 Access inside the script

The skill documentation must tell the model the stable sandbox-visible layout.

Recommended convention:

```text
/work/input/<name>
/work/output/
```

or the existing SPEC-015 equivalents.

The model must not see host-side locations.

The script writes artifacts only to the documented output directory.

## 10.4 Existing external user attachments

Direct ingestion of arbitrary chat attachments is outside scope unless the current application already exposes their bytes to the turn orchestrator.

If attachment plumbing already exists, the host may translate approved attachment content into `input_files`.

Do not add a general filesystem browser in this step.

---

## 11. Tool registration and application startup

## 11.1 Registration

At startup:

1. construct the SPEC-015 runtime through a host-owned factory;
2. construct the `sandbox_execute` handler;
3. register its `ToolSpec` and handler in the existing registry/executor;
4. load and validate `code_workspace`;
5. make the skill visible to the router.

The model sees `sandbox_execute` only when `code_workspace` is active because the restricted tool policy filters declarations and execution.

## 11.2 Docker availability policy

The application should preserve a usable normal chat when Docker is unavailable.

Recommended policy:

- sandbox capability is optional at startup;
- run a bounded host-side availability check only when enabling the tool;
- if the sandbox runtime or pinned image is unavailable, omit `sandbox_execute` and omit `code_workspace` deterministically;
- print one concise startup diagnostic;
- keep all other tools and skills operational.

Example:

```text
[sandbox] unavailable: Docker daemon is not reachable
[skills] code_workspace: omitted
```

This matches the optional Tracker skill pattern more closely than failing the whole application.

However, invalid committed configuration or invalid skill package structure should remain fail-fast.

A simple configuration flag may be added:

```text
SANDBOX_TOOL_ENABLED = true
```

This flag is host-owned and not model-visible.

If disabled:

```text
[sandbox] disabled
```

If enabled but unavailable, omit the capability rather than exposing a tool that always fails.

## 11.3 Image build

The application must not build the image automatically.

The operator uses the existing SPEC-015 build script.

The runtime continues resolving the configured image reference to the immutable image ID as defined by SPEC-015.

---

## 12. Skill package: `code_workspace`

Suggested structure:

```text
skills/
└── code_workspace/
    ├── SKILL.md
    ├── input.schema.json
    ├── examples/
    │   ├── python_csv.md
    │   ├── bash_text.md
    │   └── error_recovery.md
    └── evals/
        └── cases.json
```

## 12.1 Front matter

Conceptual front matter:

```yaml
name: code_workspace
description: Use isolated Python or Bash to process supplied data, perform bounded computation, and create downloadable text, CSV, JSON, or Markdown artifacts.
allowed_tools:
  - sandbox_execute
```

Use the exact field names required by the current skill loader.

The allowlist must contain exactly:

```text
sandbox_execute
```

It must not include:

- `python_calculate`;
- `sql_query`;
- MCP tools;
- Tracker tools;
- any future filesystem tool.

## 12.2 Skill routing guidance

The router should select `code_workspace` when the task materially benefits from:

- multi-step computation;
- file transformation;
- structured artifact generation;
- processing inline input files;
- deterministic programmatic transformation;
- logic that is too broad for `python_calculate`.

The router should not select it for:

- ordinary explanation;
- simple arithmetic supported by `python_calculate`;
- SQL questions that can be answered with `sales_analysis`;
- general writing without file generation;
- commands that require internet or package installation;
- requests to inspect the host machine;
- requests to manage Docker;
- long-running or background computation.

## 12.3 Required procedure in `SKILL.md`

The active skill instruction must require the agent to:

1. determine whether code is actually needed;
2. prefer a direct answer when code is unnecessary;
3. choose Python or Bash;
4. write the smallest complete script that solves the task;
5. use only standard-library or image-preinstalled capabilities;
6. read inputs only from the documented input directory;
7. write user artifacts only to the documented output directory;
8. call `sandbox_execute`;
9. inspect `status`, `exit_code`, `stdout`, `stderr`, and `artifacts`;
10. retry only when the error is plausibly correctable;
11. submit the full corrected source on retry;
12. avoid repeating an identical failed call;
13. respect the remaining tool-call budget;
14. respect the whole-turn deadline;
15. stop rather than start an execution that cannot reasonably finish;
16. return a concise final answer with:
    - result;
    - relevant limitations;
    - list of created artifacts;
    - artifact paths or links.

## 12.4 Language choice

Guidance:

Use Python by default for:

- CSV;
- JSON;
- calculations;
- text parsing;
- report generation;
- structured data transformation.

Use Bash for:

- simple file enumeration;
- line-oriented text processing;
- shell-native transformations;
- cases where Bash is materially simpler.

Do not use Bash merely to invoke Python.

Do not use Python merely to shell out to unavailable host commands.

## 12.5 Retry policy

A retry is appropriate for:

- syntax error;
- wrong input file name;
- incorrect output path;
- missing import from standard library typo;
- deterministic logic error visible in stderr;
- output format mistake.

A retry is not appropriate for:

- timeout caused by inherently excessive work;
- network access requirement;
- missing package installation;
- host filesystem requirement;
- Docker or runtime unavailable;
- policy violation;
- remaining tool budget insufficient;
- whole-turn deadline nearly exhausted.

The skill should aim for no more than one correction retry in common cases, leaving budget for exceptional handling and final model generation.

The hard global maximum remains four tool calls.

---

## 13. Agent-loop integration

## 13.1 Tool-call budget

`sandbox_execute` is an ordinary tool call.

Each invocation consumes one of:

```text
MAX_TOOL_CALLS_PER_TURN = 4
```

The skill cannot raise or reset this budget.

## 13.2 Repeated-call detection

Existing repeated-call detection remains active.

An identical consecutive `sandbox_execute` call must trigger the existing repeated-call termination behavior.

A corrected source changes the normalized arguments and is therefore not identical.

Care must be taken not to include host-generated fields such as `job_id` in repeated-call comparison. Comparison must use model-supplied normalized arguments only.

## 13.3 Whole-turn deadline

The sandbox runtime deadline must remain below the outer tool deadline, which remains below or compatible with the whole-turn deadline.

The implementation must pass the shared `TurnContext` through routing and agent execution exactly as today.

Before invoking the runtime, the tool handler should check remaining turn time.

If insufficient time remains to execute and clean up safely, return a typed tool error without starting a container.

Recommended host-side condition:

```text
remaining_turn_time >
sandbox_execution_timeout
+ sandbox_cleanup_timeout
+ small_host_margin
```

Do not let the outer caller-side deadline abandon a live container before the SPEC-015 runtime can kill and remove it.

## 13.4 Typed termination outcomes

Existing typed outcomes must remain the authority for turn completion.

Examples:

- tool-call budget exhausted;
- repeated tool call;
- whole-turn deadline exceeded;
- skill policy violation;
- model failure;
- tool failure followed by model final answer;
- successful answer.

A sandbox job failure is normally a structured tool result, not automatically a failed turn. The agent may correct it or explain the limitation.

A host-level sandbox adapter exception that prevents a valid tool result must map through the existing tool failure path.

## 13.5 Rollback

No user or assistant semantic messages from a failed/stopped turn are committed.

Sandbox protocol messages are never semantic messages.

Staged artifacts are committed only with a successful turn and rolled back otherwise.

---

## 14. Conversation history

## 14.1 Semantic-only persistence

The persisted conversation must contain only semantic conversation messages.

Do not persist:

- tool declarations;
- tool call protocol objects;
- tool result protocol objects;
- source code submitted to `sandbox_execute`;
- input file contents;
- stdout;
- stderr;
- artifact contents;
- Docker diagnostics;
- workspace paths;
- `job_id`;
- container IDs;
- internal sandbox statuses.

The assistant’s final semantic answer may include:

- a concise summary of the execution result;
- artifact names;
- user-resolvable artifact paths or links;
- relevant limitations.

## 14.2 Artifact references in history

Artifact references in the final answer may be persisted as ordinary text.

They must use stable user-facing relative paths or application links, not host absolute paths.

No artifact bytes are embedded in chat history.

## 14.3 Subsequent turns

This SPEC does not grant automatic access to prior-turn artifacts.

A later turn cannot read an earlier turn’s workspace.

If the user manually supplies a prior artifact again through a future attachment mechanism, it becomes a new input.

---

## 15. Tracing and observability

## 15.1 Correlation

Trace events must correlate:

```text
run_id
turn_id
tool_call_index
tool_name = sandbox_execute
sandbox_job_id
```

The runtime trace and agent trace must be joinable through `sandbox_job_id`.

## 15.2 Safe metadata

Allowed trace fields include:

- event name;
- timestamp;
- run ID;
- turn ID;
- skill name;
- tool name;
- tool call index;
- sandbox job ID;
- language;
- source byte length;
- input file count;
- total input bytes;
- normalized status;
- exit code;
- stdout byte count;
- stderr byte count;
- artifact count;
- total artifact bytes;
- duration;
- timeout flag;
- cleanup result;
- rollback or commit state.

## 15.3 Forbidden trace content

Trace must not store:

- full source code;
- source excerpts;
- input file names when sensitive naming is possible, unless names are explicitly classified as safe;
- input file contents;
- stdout content;
- stderr content;
- artifact content;
- host absolute paths;
- environment variables;
- secrets;
- Docker command line;
- container inspect payload;
- mounts;
- container logs.

A source hash is not required. If added, it must be documented as sensitive metadata and must not weaken privacy expectations. Prefer byte length only.

## 15.4 Suggested events

Conceptual events:

```text
sandbox_tool_requested
sandbox_job_started
sandbox_job_finished
sandbox_artifacts_staged
sandbox_artifacts_committed
sandbox_artifacts_rolled_back
sandbox_tool_result_returned
```

Existing generic tool events should remain. Sandbox events supplement rather than replace them.

---

## 16. Error handling

## 16.1 Validation errors

Invalid model arguments return:

```json
{
  "ok": false,
  "status": "invalid_request",
  "exit_code": null,
  "stdout": "",
  "stderr": "<bounded safe explanation>",
  "artifacts": []
}
```

The message should help the model correct the call without revealing host internals.

## 16.2 Non-zero exit

Return bounded stdout and stderr with:

```text
status = non_zero_exit
artifacts = []
```

Even if output files exist, they are not published.

## 16.3 Timeout

Return:

```text
status = timed_out
exit_code = null
artifacts = []
```

The container and child processes must already be killed and removed by SPEC-015 before the tool returns.

## 16.4 Output limit

When stdout or stderr exceeds the configured limit:

- SPEC-015 terminates the container;
- result status identifies the exceeded stream;
- returned text remains bounded;
- no artifacts are published.

## 16.5 Artifact limit

When output artifacts violate count, per-file, total-size, path, type, or extraction constraints:

- result is unsuccessful;
- no artifacts are published;
- staged partial output is removed;
- safe structured error is returned.

## 16.6 Runtime unavailable

If startup omission is implemented as required, the model should never see the tool when unavailable.

If availability changes after startup, the handler returns:

```text
runtime_unavailable
```

without exposing Docker internals.

## 16.7 Network access attempt

Network is disabled by SPEC-015.

The script may fail with a normal runtime error or timeout depending on the attempted operation.

The tool returns the bounded result. The skill must not retry by changing sandbox policy.

## 16.8 Host filesystem access attempt

The runtime’s read-only isolated filesystem and mount policy remain authoritative.

The script must not gain access to arbitrary host files.

An attempted access returns a normal bounded failure.

The tool must not translate model-provided paths into host paths.

---

## 17. Artifact media types

Media type inference should be conservative and extension-based.

Minimum mappings:

```text
.csv       text/csv
.json      application/json
.md        text/markdown
.txt       text/plain
```

Unknown types:

```text
application/octet-stream
```

Do not parse untrusted artifacts merely to infer media type.

Optional light validation is permitted for JSON only if it is bounded and cannot turn a successful runtime job into an unsafe host workload. It is not required.

---

## 18. Configuration

Add only configuration necessary for the integration.

Conceptual additions:

```python
SANDBOX_TOOL_ENABLED = True
SANDBOX_ARTIFACT_ROOT = DATA_DIR / "artifacts"
SANDBOX_WORKSPACE_ROOT = DATA_DIR / "workspaces"
SANDBOX_TURN_TIME_MARGIN_SECONDS = 2
```

Do not duplicate SPEC-015 limits in the tool layer.

The adapter must import or receive the existing runtime policy rather than maintaining a second set of source, input, output, timeout, memory, CPU, PID, or artifact limits.

All paths must be host-owned `Path` values.

Add generated workspace and artifact directories to `.gitignore`.

Artifact retention is not configured in this step.

---

## 19. Suggested implementation changes

The exact file layout may differ, but the implementation should touch the following areas.

### 19.1 `app.py`

- construct optional sandbox capability;
- register `sandbox_execute`;
- omit `code_workspace` when unavailable or disabled;
- pass `run_id`, `turn_id`, and trace sink into the handler/workspace coordinator;
- commit or roll back staged artifacts with the existing turn outcome.

### 19.2 `config.py`

- add integration enablement and workspace/artifact roots;
- reuse all SPEC-015 runtime policy values;
- preserve timing invariants.

### 19.3 tool registration

- add one `ToolSpec`;
- add one handler;
- no special model transport path.

### 19.4 `skill_runtime`

Prefer no architectural rewrite.

Only add what is required to:

- load `code_workspace`;
- omit it deterministically if its only tool is unavailable;
- preserve strict validation;
- preserve restricted declarations and execution.

If the current omission mechanism already supports this, reuse it unchanged.

### 19.5 `agent.py` / turn orchestration

- preserve existing loop;
- expose current `TurnContext` to the tool handler;
- add artifact commit/rollback hook at the existing turn transaction boundary;
- do not add sandbox-specific loop logic.

### 19.6 `reliability.py`

No new termination reasons are required unless the current taxonomy cannot represent:

- insufficient remaining deadline before tool start;
- artifact publication failure.

Prefer mapping these through existing tool failure / deadline outcomes.

### 19.7 tracing

- add safe sandbox correlation metadata;
- redact or omit source and file content;
- add artifact staged/committed/rolled-back events.

### 19.8 README

Document:

- how to build the SPEC-015 image;
- how the capability is enabled;
- startup output;
- supported languages;
- no network;
- no package installation;
- workspace isolation;
- artifact location;
- example CLI flow;
- limitations and threat model.

---

## 20. Example tool interaction

### 20.1 Successful Python artifact

```text
You: Create a CSV with squares of numbers 1 through 5.

[skill] code_workspace

[tool 1/4] sandbox_execute
[args] {"language":"python","source":"...","input_files":[]}
[result] {
  "ok": true,
  "status": "succeeded",
  "exit_code": 0,
  "stdout": "wrote squares.csv\n",
  "stderr": "",
  "artifacts": [
    {
      "name": "squares.csv",
      "media_type": "text/csv",
      "size_bytes": 38,
      "path": "artifacts/<run_id>/<turn_id>/squares.csv"
    }
  ]
}

Qwen: I created the CSV file with numbers 1–5 and their squares:
- artifacts/<run_id>/<turn_id>/squares.csv
```

### 20.2 Syntax error and correction

```text
[tool 1/4] sandbox_execute
[result] {
  "ok": false,
  "status": "non_zero_exit",
  "exit_code": 1,
  "stdout": "",
  "stderr": "SyntaxError: ...",
  "artifacts": []
}

[tool 2/4] sandbox_execute
[result] {
  "ok": true,
  "status": "succeeded",
  "exit_code": 0,
  "stdout": "done\n",
  "stderr": "",
  "artifacts": [...]
}
```

The second call contains the full corrected source.

The first failed job publishes no artifacts.

---

## 21. Testing strategy

Tests must be committed.

The default deterministic suite must not require:

- live Ollama;
- live MCP;
- live Tracker;
- live Docker.

Reuse the fake Docker CLI and runtime seams introduced by SPEC-015.

Add opt-in live Docker tests where needed.

## 21.1 Unit tests

### Tool schema and validation

Test:

- Python accepted;
- Bash accepted;
- unsupported language rejected;
- empty source rejected;
- duplicate input names rejected;
- absolute path rejected;
- traversal rejected;
- invalid base64 rejected;
- source and input limits delegated consistently to SPEC-015;
- model cannot pass Docker/image/env/mount/network/resource fields because schema rejects additional properties.

### Runtime conversion

Test:

- arguments convert to correct `SandboxJob`;
- current `turn_id` passed unchanged;
- new `job_id` created per call;
- runtime result maps to stable tool envelope;
- host paths and container IDs are absent.

### Artifact publication

Test:

- only successful zero-exit artifacts are staged;
- non-zero exit publishes none;
- timeout publishes none;
- output-limit failure publishes none;
- invalid artifact publishes none;
- collisions handled deterministically;
- failed turn rolls back staged artifacts;
- successful turn commits them;
- publication failure produces safe failure and cleans partial state.

### Trace redaction

Test trace contains:

- run ID;
- turn ID;
- job ID;
- counts and sizes;
- status.

Test trace does not contain:

- source code;
- distinctive input content;
- stdout content;
- stderr content;
- artifact content;
- host path;
- Docker command.

### Skill policy

Test:

- `code_workspace` exposes exactly `sandbox_execute`;
- direct attempt to call it under another skill is rejected;
- tool declaration absent outside active skill;
- disabled/unavailable sandbox omits the skill;
- invalid skill remains fail-fast.

### Agent loop

Test:

- successful one-call execution;
- syntax error followed by corrected successful call;
- identical repeated sandbox call triggers repeated-call detection;
- corrected call is not considered identical;
- four-call limit remains enforced;
- remaining deadline check prevents unsafe start;
- whole-turn deadline remains authoritative;
- model request count includes routing and agent calls as before.

### Conversation persistence

Test:

- final semantic answer persists;
- tool protocol messages do not persist;
- source does not persist;
- input contents do not persist;
- stdout/stderr do not persist;
- artifact contents do not persist;
- failed turn persists nothing;
- artifact reference may persist as final answer text.

### Turn isolation

Test:

- turn A input is invisible to turn B;
- turn A temporary source is absent after completion;
- turn B cannot request turn A workspace by path;
- run/turn IDs are host-generated;
- artifacts are separated by turn;
- rollback removes only the affected turn’s staged artifacts.

---

## 22. Mandatory acceptance scenarios

All scenarios below are required.

### 22.1 Successful Python script

Given `code_workspace` is active, the agent executes a Python script and receives:

- `status = succeeded`;
- `exit_code = 0`;
- bounded stdout/stderr;
- correct artifact metadata when present.

### 22.2 Successful Bash script

Same for Bash.

### 22.3 CSV or JSON artifact

A script creates a CSV or JSON file in the documented output directory.

The artifact is staged, committed with the successful turn, and returned to the user by relative path or link.

### 22.4 Syntax error → correction → success

First call returns a syntax error.

The model changes the source.

Second call succeeds.

Both calls count toward the four-call budget.

The first call publishes no artifacts.

### 22.5 Non-zero exit

A script exits non-zero.

Result is structured and bounded.

Artifacts are empty.

The application remains healthy.

### 22.6 Timeout

A script runs forever.

SPEC-015 kills and removes the container.

Tool returns `timed_out`.

No files are published.

### 22.7 stdout limit exceeded

A script emits excessive stdout.

SPEC-015 terminates it.

Tool returns `stdout_limit_exceeded`.

Returned text remains bounded.

### 22.8 Network access attempt

A script attempts external network access.

It cannot connect.

No policy field allows the model to enable networking.

### 22.9 Host filesystem access attempt

A script attempts to read an arbitrary host path.

It cannot access host files.

No model-provided host path is mounted.

### 22.10 Skill allowlist rejection

The model attempts `sandbox_execute` outside `code_workspace`.

The restricted tool policy rejects it through the existing skill-policy mechanism.

### 22.11 Turn isolation

A file created or supplied in turn A is not visible in turn B.

Temporary workspace is removed.

Only committed artifact references remain user-visible.

### 22.12 Tool-call and deadline limits

Sandbox retries cannot exceed four total tool calls.

The whole-turn deadline includes routing, model calls, sandbox execution, cleanup, and final generation.

### 22.13 Clean semantic history

Persisted history contains no:

- sandbox protocol messages;
- code;
- input contents;
- stdout;
- stderr;
- artifact contents;
- job IDs;
- Docker details.

---

## 23. Optional live integration tests

Add an opt-in test marker or environment flag, following SPEC-015 conventions.

Examples:

```bash
LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_tool_integration.py -q
```

Live tests should verify:

- real Python execution;
- real Bash execution;
- real artifact publication;
- real timeout cleanup;
- real network isolation;
- real host-path isolation;
- no leaked containers;
- no leaked temporary job directories.

They must use the pinned SPEC-015 image.

---

## 24. Evals

Add scripted eval cases for router and agent behavior.

Required categories:

- code clearly needed → selects `code_workspace`;
- code unnecessary → no skill or another appropriate skill;
- simple arithmetic → prefers existing calculator path;
- Python transformation;
- Bash transformation;
- artifact creation;
- syntax error recovery;
- non-correctable network request;
- package installation request;
- host filesystem request;
- tool budget awareness;
- final answer includes artifact references.

Live evals remain optional.

---

## 25. Documentation requirements

README must explain in plain language:

- SPEC-015 provides the isolation boundary;
- SPEC-016 exposes it through `sandbox_execute`;
- the model can choose only language, source, and input files;
- Python and Bash only;
- no internet;
- no package installation;
- no host filesystem;
- no Docker control;
- maximum four tool calls;
- workspace isolated per turn;
- failed turns roll back artifacts;
- artifact location and example;
- how to build the sandbox image;
- how startup behaves when Docker/image is unavailable;
- local-laboratory threat model.

Add an implementation journal entry after completion, following existing project conventions.

The journal should include:

- branch and commits;
- implementation summary;
- tests run;
- live verification status;
- deviations;
- security findings;
- follow-ups;
- merge commit SHA.

---

## 26. Security review checklist

Before merge, verify:

- [ ] `sandbox_execute` has no Docker configuration arguments.
- [ ] Tool schema has `additionalProperties: false`.
- [ ] Only Python and Bash are accepted.
- [ ] Runtime is exclusively SPEC-015.
- [ ] Model cannot choose image or host paths.
- [ ] Model cannot pass environment variables.
- [ ] Network remains `none`.
- [ ] Docker socket is never mounted.
- [ ] Inputs are path-normalized and bounded.
- [ ] Artifacts are accepted only through SPEC-015 validation.
- [ ] Non-zero jobs publish no artifacts.
- [ ] Failed turns roll back staged artifacts.
- [ ] Workspaces are isolated by host-owned run/turn identity.
- [ ] Trace excludes code and file contents.
- [ ] History excludes tool protocol and contents.
- [ ] Repeated-call detection uses normalized model arguments.
- [ ] Sandbox execution fits safely inside the outer tool and turn deadlines.
- [ ] Cleanup occurs on success, failure, timeout, cancellation, and rollback.
- [ ] Other skills cannot invoke `sandbox_execute`.
- [ ] Normal chat remains usable when sandbox is disabled or unavailable.

---

## 27. Definition of Done

SPEC-016 is complete when:

1. `sandbox_execute` is registered through the existing `ToolRegistry` and `ToolExecutor`.
2. It accepts only language, source, and optional input files.
3. It invokes only the SPEC-015 runtime.
4. It returns typed bounded status, exit code, stdout, stderr, and artifact metadata.
5. `code_workspace` exists and allows only `sandbox_execute`.
6. The router can select the skill for appropriate tasks.
7. The agent can correct a syntax error and rerun successfully.
8. Workspaces are isolated by `run_id` and `turn_id`.
9. Every execution has a trace-correlated `job_id`.
10. Successful artifacts are staged and committed only with a successful turn.
11. Failed turns roll back staged artifacts.
12. Temporary files are cleaned.
13. Four-call budget, repeated-call detection, and whole-turn deadline remain enforced.
14. Typed termination outcomes remain intact.
15. Semantic-only history contains no sandbox protocol or file contents.
16. All mandatory deterministic tests pass.
17. Opt-in live Docker tests or smoke verification pass and are documented.
18. README and implementation journal are updated.
19. No regression occurs in existing tools, skills, MCP integrations, evals, or normal chat startup.
20. The implementation is merged through the normal SPEC cycle.

---

## 28. Follow-up candidates

Not part of this SPEC:

- skill combining `sql_query` with `sandbox_execute`;
- attachment ingestion;
- artifact previews;
- retention and cleanup policy;
- explicit artifact download endpoint;
- persistent project workspaces;
- additional preinstalled libraries;
- package allowlist;
- more languages;
- parallel jobs;
- background jobs;
- remote execution backend;
- stronger multi-user isolation.

These should be separate SPECs or focused PATCHes after the base workspace is proven stable.
