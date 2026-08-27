# PATCH-012-02 — Compose baseline tools with active skills

- **Patch:** [PATCH-012-02](../../../patches/SPEC-012/PATCH-012-02-Compose-Baseline-Tools-with-Active-Skills.md)
- **Parent spec:** [SPEC-012](../../../specs/SPEC-012-Skills.md)
- **Date:** 2026-08-27
- **Branch:** patch/PATCH-012-02-compose-baseline-tools-with-active-skills
- **Implementation commit:** `<pending>`
- **Merge commit:** `<pending>`

## Hypothesis / intent

SPEC-012 made an active skill's `allowed_tools` the *complete* model-facing tool
view for the turn. The security property that buys is real — a skill can never
widen access — but the rule was applied one level too broadly: selecting a domain
skill also erased safe, general-purpose host capabilities the domain task may
genuinely need.

The reproducer was an ordinary Tracker conversation on:

```text
agent profile:  next-mlx
agent model:    qwen3.8:27b-mlx
router profile: fast
reasoning:      medium
```

The application started with both MCP servers connected:

```text
[mcp] connected: time (1 tool)
[mcp] connected: tracker (4 admitted, 35 filtered)
```

so `mcp_time__get_current_time` was in the global registry. But the moment the
router selected `tracker_read`, the model-facing set became the four Tracker read
tools plus `activate_skill`, and Time was gone:

```text
no active skill        -> Time exists
tracker_read active    -> Time disappears
next turn, no skill    -> Time exists again
```

Asked "does Sergey have overdue tasks?", the model could read deadlines but had
no way to obtain the current date. Across adjacent turns it exhibited every
failure this permits: asking the user for a date the host already knows how to
fetch, answering conditionally, hallucinating the date, and finally asserting
that no Time tool existed at all. The `tracker_read` instruction actively
reinforced the last one — it explicitly said `mcp_time__get_current_time` does
not belong to the skill.

The intent of this patch is a one-line change of rule:

```text
before:  effective_tools = skill.allowed_tools
after:   effective_tools = skill.allowed_tools + host baseline + activate_skill
```

A skill should narrow *domain* capability. It should not be able to remove a
host capability — just as it has never been able to add one.

## What changed

**One host-owned baseline set.** `config.BASELINE_TOOL_NAMES` is a tuple
containing exactly `mcp_time__get_current_time` and nothing else. It is fixed in
the repository, deterministic in order, not reachable from chat, the model, or a
skill package, and baseline status is never inferred from a tool's name, server,
description, or read-only behavior. Widening it is a repository change under
review — `tests/test_skill_policy.py` asserts the shipped contents so it breaks
first.

**Validated against the final registry.** MCP tools do not exist at import time,
so the names cannot be checked where they are declared.
`skill_runtime.config_validation.validate_baseline_tools(names, registry)` runs
where skills are already validated — after MCP registration — and rejects an
unknown or duplicated name with a plain `ValueError`. Silently dropping one would
quietly remove a capability every active skill is supposed to keep.

**One composition helper, two call sites.** `skill_runtime/policy.py` gained
`compose_skill_toolset(...) -> SkillToolset`. It returns the model declarations
and the executor allowlist *together*, both derived from one ordered name list:

```python
@dataclass(frozen=True)
class SkillToolset:
    declarations: tuple[dict[str, Any], ...]
    allowed_tools: frozenset[str]
    names: tuple[str, ...]
    skill_tools: tuple[str, ...]
    baseline_tools: tuple[str, ...]
```

Order is skill tools in declared order, then baseline names not already present,
then the control declaration last; duplicates collapse to their first occurrence,
so a baseline tool a skill also declares appears once, in its skill position. An
unknown name still raises rather than being dropped. The control declaration is
*passed in* rather than imported, because `activate_skill` is host-generated and
not a registry tool — and because `activation.py` already imports `policy.py`.

Keeping declarations and policy in one return value is the point: the previous
code built them from two separate expressions (`declarations_for_names(...)` and
`frozenset(spec.allowed_tools) | {ACTIVATE_SKILL_TOOL_NAME}`) in two different
modules, which is exactly how they could have drifted.

**Both composition points fixed.** `SkillTurnOrchestrator._run_with_skill()` and
`SkillActivationHandler.handle()` now call the same helper. Fixing only the first
would have left the identical bug behind `activate_skill`. Both take
`baseline_tools` as an injected argument defaulting to `()` — the host owns the
baseline, so `skill_runtime` composes what it is given rather than reaching into
config, and an empty default reproduces SPEC-012's view exactly.

**The activation receipt stopped lying.** `_receipt()` used to rebuild
`available_tools` as `[*spec.allowed_tools, ACTIVATE_SKILL_TOOL_NAME]`,
independently of the declarations. It now reports the composed set, including on
the "already active" branch, which returns a receipt without recomposing
anything. Telling the model Time is gone while it is still declared would be the
same contradiction this patch removes.

**Traces distinguish the two sources.** `skill_toolset_resolved.available_tools`
still reports what the model actually got, and gained two additive fields:

```text
skill_tools     = ["mcp_tracker__issue_get", ...]
baseline_tools  = ["mcp_time__get_current_time"]
available_tools = skill_tools + baseline_tools + ["activate_skill"]
```

**Prose that contradicted the host.** The host-generated `<active_skill_policy>`
now states that the supplied tools are the skill's own *together with* the host's
general utilities, and that the supplied set is authoritative even where the
skill text lists fewer. `skills/tracker_read/SKILL.md` no longer says
`mcp_time__get_current_time` can never belong to the skill;
`skills/sales_analysis/SKILL.md` no longer treats its front-matter list as the
whole host-supplied view. Neither skill's `## Use when` / `## Do not use when`
changed — a pure current-time question must still not route to either skill just
because Time is now visible inside them.

**Not changed:** the `SKILL.md` schema (no `baseline_tools` / `inherits` field —
a skill must never be able to grant itself authority), `allowed_tools` as the
skill's domain declaration, `agent.py` (still skill-agnostic), `activate_skill`
semantics beyond preserving the baseline across its existing replacement, and
every budget, deadline, routing rule, reasoning mode, and model profile.

### Effective tool view under `tracker_read`, before and after

```text
before                              after
------------------------------      ------------------------------
mcp_tracker__issue_get              mcp_tracker__issue_get
mcp_tracker__issues_find            mcp_tracker__issues_find
mcp_tracker__queue_get_metadata     mcp_tracker__queue_get_metadata
mcp_tracker__issue_get_comments     mcp_tracker__issue_get_comments
activate_skill                      mcp_time__get_current_time
                                    activate_skill
```

`sql_query`, `python_calculate`, `sandbox_execute`, and every Tracker mutation
tool remain inaccessible in both columns.

## Model & parameters (provenance)

- Ollama: 0.32.15
- Agent model (live): `qwen3.8:27b-mlx` (digest `5642e97495e1`, nvfp4)
- Router model (live): `qwen3:8b` (digest `500a1f067a9f`, Q4_K_M, 8.2B)
- Reasoning: `medium`
- Sampling: model defaults, unchanged — `llm.py` still sets no `options`.
- Deterministic verification below involves no live model at all.

## Verification

### Deterministic

```bash
python -m pytest -q                        # 838 passed, 29 skipped
python -m evals.runner --suite scripted    # 41/41 passed
```

New coverage, by the patch note's numbered list:

- `tests/test_skill_policy.py` — `TestComposeSkillToolset`: skill-first
  deterministic order, baseline appended, control tool last; a baseline tool the
  skill also declares deduplicated into its skill position; declarations and
  `allowed_tools` proven to be the same set; an unknown baseline name rejected;
  an empty baseline reproducing the SPEC-012 view. `TestValidateBaselineTools`:
  unknown and duplicate names fail startup, and the shipped baseline is pinned to
  exactly the Time tool.
- `tests/test_skill_turn.py` — under `tracker_read` the declarations are the
  skill tool + Time + `activate_skill`; the restricted executor really dispatches
  Tracker *and* Time in one turn with `activations == 0`; `sql_query` is still
  stopped with `skill_policy_violation` before its handler; the trace separates
  `skill_tools` from `baseline_tools`; the no-skill path still shows the whole
  global set.
- `tests/test_skill_activation.py` — `TestBaselineToolsSurviveActivation`: a
  replacement keeps Time while dropping the previous skill's domain tools; Time
  executes after the replacement; repeated activation duplicates neither Time nor
  `activate_skill`; the replaced skill's tools are still rejected; the trace and
  both receipt branches report the composed view.

### Two existing assertions had to be retargeted

Both used Time under `sales_analysis` to stand for "a tool this skill may not
call" — precisely the behavior being removed. Neither was weakened; each now uses
a genuine foreign *domain* tool:

- `tests/test_skill_turn.py::test_disallowed_tool_stops_turn_with_policy_violation`
  → `mcp_tracker__issue_get`;
- `evals/cases.json::skill-policy-violation-001` → `mcp_tracker__issue_get`.

The per-skill fixtures `skills/tracker_read/evals/cases.json` and
`skills/sales_analysis/evals/cases.json` (committed documentation, not executed
by the runner) dropped Time from `forbidden_tools` for the same reason.

### Scripted regression case

`skill-baseline-time-001` (category `skill_baseline_time`) is the defect shape,
committed:

```text
prompt : "Check the Tracker issue DATA-142 deadline and tell me whether it is
          overdue as of now."
route  : tracker_read
script : mcp_tracker__issue_get -> mcp_time__get_current_time -> answer
asserts: expected_selection=tracker_read, expected_final_skill=tracker_read,
         expected_activations=0, status=completed, reason=final_answer,
         required_tools=[mcp_tracker__issue_get, mcp_time__get_current_time],
         forbidden_tools=[sql_query, python_calculate, sandbox_execute]
```

It proves one active skill can use one domain capability and one host baseline
capability in the same turn without `activate_skill`. Result: `[PASS]
skill-baseline-time-001 (completed/final_answer)`.

### Real-registry startup

The validator was exercised against the actual MCP-registered tool set, not a
fixture:

```text
$ python app.py --profile fast
[mcp] connected: time (1 tool)
[mcp] connected: tracker (4 admitted, 35 filtered)
[skills] 2 loaded: sales_analysis, tracker_read
Local AI chat
```

Startup completed, so `validate_baseline_tools(BASELINE_TOOL_NAMES, registry)`
resolved `mcp_time__get_current_time` in the final registry after MCP
registration.

### Live

Passed — see "Live verification" below.

## Live verification

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium
```

```text
[model]  agent next-mlx: qwen3.8:27b-mlx (request 250s, turn 500s)
[router] fast: qwen3:8b (request 120s, routing 30s)
[reasoning] medium (transient preservation on)
[mcp] connected: time (1 tool)
[mcp] connected: tracker (4 admitted, 35 filtered)
[skills] 3 loaded: code_workspace, sales_analysis, tracker_read
```

Run `17b8cd29-954f-4550-8819-b9d053685019`, turn `9808c209`, 2026-08-27T20:40Z,
against a real Yandex Tracker instance. The turn asked for open issues in a queue
and whether they are overdue — the original reproducer's shape.

Composed view, from `skill_toolset_resolved`:

```text
skill_tools     ["mcp_tracker__issue_get", "mcp_tracker__issues_find",
                 "mcp_tracker__queue_get_metadata", "mcp_tracker__issue_get_comments"]
baseline_tools  ["mcp_time__get_current_time"]
available_tools skill_tools + baseline_tools + ["activate_skill"]
```

`turn_started.available_tools` carried the same six names, so the declarations the
model actually received match the composed view exactly.

Tool sequence, from `tool_execution_started`:

```text
[tool] mcp_tracker__issues_find
[tool] mcp_time__get_current_time
```

Terminal event:

```json
{"status": "completed", "reason": "final_answer", "tool_calls_executed": 2,
 "selected_skill": "tracker_read", "initial_skill": "tracker_read",
 "skill_activations": 0, "model_requests": 4, "duration_ms": 71293}
```

Every live criterion is met:

- the initial skill stayed `tracker_read`, and remained the final skill;
- Time was available without leaving or replacing the skill (`skill_activations`
  is 0 — no `activate_skill` was involved);
- Time **executed** through the `RestrictedToolExecutor`, in the same turn as a
  Tracker read;
- the answer no longer claims the Time capability does not exist;
- no unrelated domain tool appeared: `sql_query`, `python_calculate`, and
  `sandbox_execute` are absent from both the declarations and the allowlist.

The order (Tracker first, then Time) is model-owned and was not constrained.

### Negative model-behavior evidence from the same session

A later turn in an adjacent run (`eb3894f9`, same profile combination) asked for a
CSV report of Tracker issues. The composed view was correct — trace confirms the
same six tools, `skill_activations: 0` — and the model answered:

> Я **не могу записать файл на диск** — в этом сеансе нет инструмента записи (ни
> `sandbox_execute`, ни `file_write`). Мой арсенал — только чтение Tracker,
> чтение времени, один SQL-запрос к Chinook и одна арифметическая формула.

Two things are worth recording, neither of them a defect in this patch:

1. **The patch is visible in the model's own self-description.** Under
   `tracker_read` it now lists "чтение времени" as part of its arsenal. That is
   the exact claim the original reproducer got wrong in the opposite direction.
2. **The model confabulates the rest of its capability list.** It has never had
   `sql_query` or `python_calculate` under `tracker_read`, before or after this
   patch; the trace proves neither was declared. It is describing skills it can
   see in the `activate_skill` enum as if they were its own tools, while
   simultaneously failing to notice that the same enum offers `code_workspace`,
   whose catalog description explicitly advertises creating CSV files.

The second point is a real gap, but it belongs to SPEC-018's handoff, not to
baseline composition: `sandbox_execute` is deliberately *not* baseline, so a
Tracker-to-CSV turn requires a mid-turn `activate_skill("code_workspace")`. That
path is mechanically sound — a scripted probe drives
`tracker_read -> activate_skill(code_workspace) -> sandbox_execute` to
`completed/final_answer` with `activations: 1` — and the router correctly picks
`tracker_read` as the entry point for such prompts. What is missing is any
instruction that switching is a legitimate outcome: `tracker_read`'s procedure
says "Stop once the requested read task is answered", frames every non-read
request as a read-only refusal, and lists only read outcomes under its completion
criteria. Recorded as a follow-up; not widened into this patch.

## Outcome

Met. Live on `next-mlx` + `fast` + `reasoning medium`, one turn read Tracker and
the current time under a single active skill, with no skill change — the exact
combination that was impossible before. The capability model is now coherent: one host-owned baseline set, one composition rule, one type carrying
declarations and executor policy together, and both entry points — the router's
selection and mid-turn `activate_skill` — going through it.

The invariant a skill lives under is unchanged in substance and sharper in
statement. A skill can still only reduce access to *domain* capabilities. It
cannot add a baseline tool, remove a baseline tool, register a tool, alter a tool
contract, or reach another skill's domain tools. The host alone decides the
baseline.

This is a corrective evolution of SPEC-012's restricted-tool semantics, not a
retelling of them: SPEC-012 shipped with replacement, and replacement was wrong
for host utilities.

## Follow-ups

- The same transcript exposed a second, separate defect: completed tool actions
  are not persisted into cross-turn semantic history, so the model can later deny
  having called a tool. `Conversation` stores only user/assistant messages, so a
  turn's tool results die with the turn. Explicitly out of scope here; it needs
  its own PATCH or SPEC decision.
- A skill does not know it may hand off. `tracker_read` tells the model to stop
  once the read is answered and never mentions `activate_skill`, so a
  "read Tracker, then produce a file" request ends in a refusal even though
  `code_workspace` is loaded and offered in the activation enum. Candidate
  PATCH against SPEC-012/SPEC-018; see the live section above for the evidence.
- The baseline is deliberately one tool. Any addition should arrive with the
  argument for why it is a *host* utility rather than a domain capability.
- `skills/<name>/evals/cases.json` is still a committed fixture the runner never
  executes (noted in the SPEC-012 journal). It had to be corrected by hand here;
  executing those files would have caught the contradiction automatically.
