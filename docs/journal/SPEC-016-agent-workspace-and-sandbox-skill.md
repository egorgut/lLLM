# SPEC-016 — Agent Workspace & Sandbox Skill

- **Spec:** [SPEC-016](../../specs/SPEC-016-agent-workspace-and-sandbox-skill.md)
- **Date:** 2026-08-02
- **Branch:** feature/SPEC-016-agent-workspace-and-sandbox-skill
- **Merge commit:** `0f62c5a`

## Hypothesis / intent

SPEC-015 built an isolated Python/Bash execution boundary and deliberately left
it host-only: nothing in `app.py`, `agent.py`, or `tools/` imported it, and the
model had no way to reach it. This step connects it to the agent — and the whole
question is whether that can be done *without* the connection becoming the weak
point.

The expectation was that a narrow boundary is enough: one tool whose schema
offers only language, source, and input files; one skill that allows only that
tool; a turn-scoped artifact directory whose fate follows the turn's; and no
change at all to the four-call budget, repeated-call detection, whole-turn
deadline, typed outcomes, or semantic-only persistence.

## What changed

**New package `sandbox_tool/`** — the entire model-facing boundary:

- `schema.py` — `SANDBOX_EXECUTE_SPEC` (three properties, `additionalProperties:
  false`) and argument validation. Validates *shape* (types, encodings, duplicate
  names) and delegates every *limit* and the whole relative-path policy to
  SPEC-015 by calling its own `validate_input_path`, so the two layers cannot
  disagree about what a legal input is.
- `workspace.py` — `TurnWorkspace`: turn identity, remaining turn time, artifact
  publication, commit/rollback, and a generation counter that makes a result
  arriving after its turn ended unpublishable.
- `artifacts.py` — media-type inference (extension only), the collision policy,
  and the `O_EXCL | O_NOFOLLOW` write under a re-verified containment check.
- `handler.py` — the adapter: deadline guard → `SandboxJob` → `runtime.execute` →
  status mapping → publication → one uniform envelope. It reimplements no part of
  SPEC-015 (no Docker command, no image resolution, no timeout, no cleanup).
- `capability.py` — the startup probe and optional construction.

**New skill `skills/code_workspace/`** — `SKILL.md`, `input.schema.json`, three
examples, and an eval fixture. `allowed_tools` is exactly `sandbox_execute`.

**Wiring:**

- `config.py` — three additions only (`SANDBOX_TOOL_ENABLED`,
  `SANDBOX_ARTIFACT_ROOT`, `SANDBOX_TURN_TIME_MARGIN_SECONDS`). No SPEC-015 limit
  is duplicated.
- `skill_runtime/orchestrator.py` — one new hook, `on_turn_context`, called right
  after the `TurnContext` is minted and before routing. Same idiom as the
  existing `on_selection`. `turn_id` is minted inside the orchestrator, so
  without this hook `app.py` could not bind a per-turn resource to it.
- `sandbox_runtime/docker_backend.py` — one additive public method,
  `ensure_available()`, running the same preflight the first job would and
  memoising the image ID for it. No new Docker work.
- `agent.py` — `redacted_argument_tools`, a generic host-owned set of tool names
  whose arguments are never previewed or hashed into the trace (see Security
  findings).
- `app.py` — builds the capability, registers the tool, extends `omit_skills`,
  prints the diagnostic, and commits/rolls back artifacts at the *existing*
  transaction boundary beside the conversation rollback.
- `.gitignore` — `data/artifacts/`.

**Tests:** six new modules (159 new deterministic tests) plus `support_sandbox_tool.py`,
which builds a real handler over a real `DockerSandboxRuntime` driven by
SPEC-015's `FakeCommandRunner` — so the adapter is tested against the genuine
runtime path, not a mock of it. Nine opt-in live Docker tests. Eight new eval
categories.

## Model & parameters (provenance)

- Model: qwen3:8b (digest `500a1f067a9f`, Q4_K_M, ctx 40960, 8.2B params)
- Ollama: 0.31.1
- Sampling: defaults — `llm.py` sets no `options`
- Sandbox image: `lllm-sandbox:spec-015`, image ID
  `sha256:74a724e2933b3d8e76b128883b25aa75d53caa0b2390e57af30f074a32e2efe3`
- Docker: Desktop, server 29.6.1

## Decisions taken

Three points where the spec left a genuine choice:

1. **`insufficient_time` added as a tenth status** beyond §7.5's enum, for the
   §13.3 refusal to start a job the turn cannot outlive. Reusing
   `runtime_unavailable` would tell the model the sandbox is broken when it is
   merely late; `runtime_error` would hide a deliberate policy decision in a
   host-defect bucket.
2. **No `SANDBOX_WORKSPACE_ROOT`** (deviation from §18). SPEC-015 already
   creates, mounts, and removes `data/sandbox/tmp/<job_id>/` per job, and source,
   inputs, and artifacts all travel through the tool layer in memory. A second
   temp root would be dead code with a second cleanup path.
3. **Flat per-turn artifact directory with deterministic suffixes** (§9.4's first
   option): `report.csv`, `report-2.csv`. Shorter user-facing paths, and no
   `job_id` leaking into the answer the model quotes.

## Verification

### Deterministic

```
python -m pytest -q
552 passed, 29 skipped in 0.99s        (was 394 passed, 20 skipped)

evals scripted suite
{'total': 35, 'passed': 35, 'failed': 0}
```

### Live Docker (opt-in)

```
python scripts/build_sandbox_image.py
LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_integration.py \
                                     tests/test_sandbox_tool_integration.py -q
29 passed in 42.76s
```

The nine SPEC-016 live tests assert the claims the skill instruction makes to the
model: a real artifact on disk with the right bytes, a failing script publishing
nothing, a timeout publishing nothing, `BLOCKED` on a network connect, `False` on
a host-path existence check, `READ-ONLY` on a write outside `/sandbox/output`,
and no leaked container or job directory after any of them.

### Live model, end to end

Ollama running with qwen3:8b; scripted dialogue piped into `python app.py`.

```text
[sandbox] ready
[skills] 3 loaded: code_workspace, sales_analysis, tracker_read

You: Use the code_workspace skill to create a CSV file with the numbers 1
     through 5 and their squares.

[skill] code_workspace

[tool 1/4] sandbox_execute
[args] {"language": "python", "source": "import csv\n\n# Prepare data\nnumbers =
        list(range(1, 6))\n...", "input_files": []}
[result] {"ok": true, "status": "succeeded", "exit_code": 0,
          "stdout": "Wrote CSV file with numbers 1-5 and their squares\n",
          "stderr": "",
          "artifacts": [{"name": "numbers_squares.csv", "media_type": "text/csv",
                         "size_bytes": 42,
                         "path": "data/artifacts/aa6f9285-.../3d6c3588-.../numbers_squares.csv"}]}

Qwen: The CSV file has been successfully created ... The file is saved at:
      data/artifacts/aa6f9285-.../3d6c3588-.../numbers_squares.csv
```

The file was on disk with exactly the expected content:

```
Number,Square
1,1
2,4
3,9
4,16
5,25
```

**The second turn produced the most useful result of the whole step**, and it was
not scripted. Asked to read that CSV back, the model tried the artifact path it
had itself quoted a moment earlier:

```text
You: Now read that CSV back and tell me the sum of the squares column.

[tool 1/4] sandbox_execute   -> non_zero_exit, FileNotFoundError: 'numbers_squares.csv'
[tool 2/4] sandbox_execute   -> non_zero_exit, FileNotFoundError:
                                'data/artifacts/aa6f9285-.../numbers_squares.csv'

Qwen: The CSV file wasn't found because the sandbox environment doesn't have
      access to previously created artifacts. ...
```

That is §14.3 and §22.11 demonstrated live rather than merely asserted in a test:
a later turn cannot reach an earlier turn's workspace *even when the model knows
the path and asks for it directly*, and the failure is legible enough that the
model diagnosed it correctly and explained the limitation to the user instead of
inventing a result. Both failed calls published nothing; the turn's own artifact
directory was never created.

Trace correlation from the same run — agent and runtime events joined on
`job_id`, one `sandbox_job_finished` per `sandbox_job_started`:

```text
sandbox_tool_requested        turn_id=3d6c3588 call_index=1 language=python source_bytes=392
sandbox_job_started           turn_id=3d6c3588 job_id=12efebca
sandbox_execution_finished    job_id=12efebca exit_code=0
sandbox_artifact_collection_finished  job_id=12efebca artifact_count=1
sandbox_cleanup_finished      job_id=12efebca
sandbox_job_finished          job_id=12efebca status=completed artifact_count=1
sandbox_artifacts_staged      job_id=12efebca artifact_count=1
sandbox_tool_result_returned  sandbox_job_id=12efebca status=succeeded exit_code=0
sandbox_artifacts_committed   turn_id=3d6c3588 artifact_count=1
```

Persisted `data/chat_history.json` after the run: four semantic messages, and no
`sandbox_execute`, `job_id`, `/sandbox/`, Docker detail, stdout, or stderr. The
artifact path survives as ordinary answer text, which is what §14.2 allows.

### Degraded startup

With the Docker daemon stopped:

```text
[mcp] connected: time (1 tool)
[mcp] connected: tracker (4 admitted, 35 filtered)
[sandbox] unavailable: Docker sandbox is unavailable.
[skills] code_workspace: omitted
[skills] 2 loaded: sales_analysis, tracker_read
Local AI chat
```

Normal chat, both other skills, and every other tool were unaffected.

## Security findings

The §26 checklist was walked item by item; all twenty hold. Two findings came out
of actually reading a live trace rather than from the checklist:

**1. The generic tool event previewed the model's source code. Fixed.**

`AgentRunner` emits `tool_call_requested` with `arguments_preview` and
`arguments_sha256` — a bounded preview of every tool's arguments, which is the
right behavior for `sql_query`, where the argument is a parameter worth reading
back later. For `sandbox_execute` the arguments *are* the content: the user's
data expressed as code, plus the user's files verbatim. The first live trace
contained the whole script.

Fixed generically rather than by special-casing the sandbox in the loop: a
host-owned `redacted_argument_tools` set of tool names, wired from `app.py`. A
redacted call still records tool name, index, and repeat count — it is fully
traceable — but `arguments_preview` is empty, `arguments_sha256` is `None` (a
hash of a short script is not much of a secret), and `arguments_redacted: true`
says so explicitly. Verified against a fresh live trace and locked in by three
tests, including one asserting other tools keep their preview.

**2. `sandbox_execution_finished.stdout_preview` still carries script output.
Not fixed — out of scope, deliberately.**

SPEC-016 §15.3 says the trace must not store stdout content. SPEC-015 mandates
the opposite for its own event: "Any stdout/stderr preview must use the existing
bounded preview/hash approach." The two specs genuinely conflict on an event that
belongs to the parent spec, and SPEC-016 §5 requires existing mechanisms to stay
unchanged. Every event SPEC-016 itself adds carries byte counts only. Recorded as
a follow-up rather than silently rewriting SPEC-015's contract mid-step; the
preview is bounded and local, so this is a scope call, not an accepted leak of
unbounded data.

## Outcome

All twenty Definition-of-Done items are met and all thirteen mandatory acceptance
scenarios pass. Nothing about the existing agent had to change to accommodate the
sandbox: `sandbox_execute` is an ordinary tool call, and the budget, repeated-call
detection, deadline, and outcome taxonomy all applied to it unmodified. The only
new seam in the loop is a tracing redaction rule, and the only new seam in the
orchestrator is a turn-context callback — neither is sandbox-specific.

Two things worth carrying forward:

- **The narrow schema did its job.** Because the tool exposes three properties
  and refuses additional ones, "can the model reach Docker?" reduces to reading
  one schema. Every operational question stayed answerable in SPEC-015.
- **A legible failure is worth more than a prevented one.** The second live turn
  failed twice and still ended well, because `non_zero_exit` plus a real
  `FileNotFoundError` gave the model enough to diagnose the boundary itself. A
  vaguer error would have produced a confident wrong answer or a wasted retry.

Observed model behavior worth noting for reproducibility: qwen3:8b wrote correct
`/sandbox/output/` paths on the first attempt in both runs, chose Python without
prompting for CSV work, and used the retry budget on a genuinely non-correctable
error (cross-turn access) rather than on a syntax mistake — spending two of four
calls before explaining. The skill's retry guidance could be sharper about
recognising cross-turn access as non-correctable *before* the first call.

## Patches

- `PATCH-016-01` — cross-turn artifact guidance in `code_workspace`, addressing
  the wasted retry observed in the live run above. Partially successful: the
  recovery strategy changed in every run, the call count did not.
  See `docs/journal/patches/PATCH-016-01-cross-turn-artifact-guidance.md`.

## Follow-ups

- **`stdout_preview` in SPEC-015's trace** (finding 2 above) — a PATCH against
  SPEC-015 could bound it further or drop it, if the privacy expectation in
  SPEC-016 §15.3 is meant to govern the runtime's events too.
- **Skill guidance on cross-turn access** — teach `code_workspace` to recognise
  "read the file from my last message" as non-correctable before spending a call.
- **`sandbox_call_index` vs `tool_call_index`** (§15.1) — a handler receives only
  arguments, so the adapter emits its own 1-based per-turn counter. The real
  `tool_call_index` is on the agent's `tool_execution_started/finished` events for
  the same `turn_id`, so traces are joinable; unifying them would need a context
  argument in the `ToolHandler` signature, which is a larger change than this
  step warranted.
- Deferred by the spec itself (§28): a combined `sql_query` + `sandbox_execute`
  analytical skill, attachment ingestion as `input_files`, artifact retention
  policy, artifact previews.
