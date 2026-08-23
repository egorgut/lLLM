# PATCH-018-01 — Complete Mid-Turn Skill Activation Verification

## Parent spec

`specs/SPEC-018-Mid-Turn-Skill-Activation.md`

Parent journal:

`docs/journal/SPEC-018-mid-turn-skill-activation.md`

## Problem

SPEC-018 implemented mid-turn skill activation through the host-owned `activate_skill` control tool, but the step was deliberately merged with reduced verification.

The implementation was regression-tested against the existing suite, but the verification programme defined in SPEC-018 §7 was not completed.

The parent journal explicitly records the following outstanding verification debt:

- the deterministic tests required by §7.1 were not committed;
- the four scripted eval cases required by §7.1 were not added;
- the entire §7.2 live programme was not run;
- the current live eval path drives a raw `AgentRunner` and does not pass turns through `SkillTurnOrchestrator`, so the skill-coverage experiment required by §7.2 cannot currently be executed;
- the prefix-reprefill cost caused by replacing the active skill system block was not measured.

As a result, the mechanism exists, but the project still cannot answer with reproducible evidence:

1. whether the implementation's core invariants remain protected against regression;
2. whether real models actually use `activate_skill`;
3. whether skill coverage is preserved;
4. what runtime cost a mid-turn skill replacement introduces.

This verification debt should be closed before using a new model generation as a new project baseline.

## Expected change

Complete the verification programme already defined by SPEC-018.

This PATCH must not redesign or extend mid-turn skill activation.

The implementation work consists only of:

1. adding the missing deterministic regression tests from SPEC-018 §7.1;
2. adding the missing scripted eval cases;
3. adding the smallest live-evaluation path necessary to execute skill-aware turns through the real `SkillTurnOrchestrator`;
4. running the live verification programme defined by SPEC-018 §7.2 on the existing `fast`, `mid`, and `deep` profiles;
5. recording the resulting evidence in the parent journal.

If verification exposes an actual functional defect in SPEC-018, do not silently fix it inside this PATCH unless the correction is trivial and inseparable from making the verification executable.

Record the defect and classify the corrective work separately according to the repository PATCH/SPEC workflow.

## Required deterministic tests

Add committed tests covering the behaviours required by SPEC-018 §7.1.

At minimum:

1. a control-tool call bypasses the ordinary `ToolExecutor` and appends its model-facing tool result to the active turn transcript;
2. all existing pre-dispatch policies still run before a control tool:
   - parallel-call rejection;
   - repeated identical call detection;
   - tool-call budget;
3. activation when no skill is currently active replaces:
   - model-facing tool declarations;
   - active executor;
   - system-level active-skill block;
4. replacing one skill with another removes the previous wrapper and leaves exactly one `<active_skill>` block;
5. replacement builds the new `RestrictedToolExecutor` over the original global executor, not over the previous restricted executor;
6. a tool forbidden by the newly active skill is rejected before its underlying handler runs;
7. an unknown skill name produces a recoverable tool result and the turn can still reach `completed`;
8. exhausting `MAX_SKILL_ACTIVATIONS_PER_TURN` produces a recoverable result and leaves the previously active skill intact;
9. every activation increments `tool_calls_executed`;
10. a turn with no mid-turn activation preserves the SPEC-012 model-facing context semantics;
11. an empty skill registry produces no `activate_skill` declaration;
12. collision with the reserved `activate_skill` name fails at startup;
13. tracing verifies:
    - `skill_activated`;
    - `initial_skill`;
    - final `selected_skill`;
    - `skill_activations`;
14. the existing SPEC-011 / SPEC-012 / SPEC-016 / SPEC-018 regression suite continues to pass.

Prefer deterministic doubles and injected responses. Do not use a live Ollama model in unit tests.

## Required scripted eval cases

Extend the committed scripted eval suite with explicit cases for:

- activation from no active skill;
- replacement of one active skill with another;
- recovery from an unknown skill name;
- recovery after activation-cap exhaustion.

The cases must assert outcome and relevant skill state, not merely absence of exceptions.

At minimum verify:

```text
initial_skill
final_skill
skill_activations
turn outcome
```

Run:

```bash
python -m evals.runner --suite scripted
```

All committed scripted cases must pass.

## Live skill-aware evaluation path

The current live suite is insufficient for SPEC-018 §7.2 because it drives a raw `AgentRunner` and does not exercise skill routing or `SkillTurnOrchestrator`.

Add the smallest reusable live path that executes a case through the same skill-aware orchestration used by `app.py`.

The live path must reuse production components rather than reimplementing routing or activation semantics inside the eval runner.

It must be able to capture at least:

```text
profile
initial_skill
final_skill
skill_activations
tool calls
termination status
turn duration
```

Do not create a second orchestration implementation for evaluation.

Do not introduce a general benchmark framework unless it is strictly necessary.

## Live verification programme

Run the live programme using the three existing profiles:

```text
fast → qwen3:8b
mid  → qwen3:14b
deep → qwen3:32b
```

Do not introduce Qwen3.8 or another model in this PATCH.

### A. Skill coverage

Measure the share of cases in which the correct skill is active for the work being performed.

The comparison must preserve the intent of SPEC-018 §7.2:

- establish the router-only / no-mid-turn-activation baseline where reproducibly possible;
- measure the skill-aware path with SPEC-018 active;
- use the same committed cases, skills, MCP availability, and model profile for both sides of a comparison.

Coverage must not decrease as a result of the SPEC-018 mechanism.

If the exact historical pre-SPEC-018 baseline cannot be reproduced without creating artificial infrastructure, document the limitation transparently rather than fabricating a comparison.

The goal is evidence, not a forced positive result.

### B. Does the model actually use `activate_skill`?

Create or use a live case that cannot be completed correctly under one skill alone.

Target shape:

```text
tracker_read
    ↓
read real issue / evidence
    ↓
task becomes code-oriented
    ↓
activate_skill("code_workspace")
    ↓
code_workspace
    ↓
complete turn
```

Run the case on `fast`, `mid`, and `deep`.

For each profile record:

- initial router-selected skill;
- whether `activate_skill` was called;
- which activation index / tool-call position it occupied;
- replacement target;
- whether the turn completed successfully;
- final active skill;
- total number of tool calls;
- total turn duration.

A profile that never calls `activate_skill` is a valid experimental result, not a PATCH failure.

Do not modify prompts merely to force the expected activation unless SPEC-018's existing prompt contract is itself proven defective.

### C. Prefix-reprefill cost

SPEC-018 changes the system-level active-skill block after activation, invalidating the model's cached prefix.

Measure the practical cost.

Compare:

```text
comparable turn without activation
vs
comparable turn with one activation
```

for each practical profile where the comparison can be made reproducibly.

Record at minimum:

- profile;
- total turn duration;
- number of model requests;
- number of tool calls;
- activation count;
- observed latency difference.

Do not claim that the whole difference is caused exclusively by KV-cache/prefix rebuilding unless the measurement isolates that variable.

Describe it as observed activation / reprefill overhead.

## Constraints

- Preserve the architecture and semantics of SPEC-018.
- `agent.py` must remain skill-agnostic.
- Do not change `SkillRouter` behaviour.
- Do not change `activate_skill` semantics.
- Do not change activation or tool-call limits.
- Do not change existing skill packages merely to make the live test pass.
- Do not change model prompts solely to improve the measured result.
- Do not add Qwen3.8.
- Do not change model profiles.
- Do not change sampling parameters.
- Do not change model deadlines unless verification proves the existing value is invalid; if so, record the finding and handle it separately.
- Do not introduce new third-party dependencies.
- Keep the evaluation path framework-free and reuse production orchestration.
- Do not fold unrelated reliability, CLI, tool, MCP, or skill improvements into this PATCH.
- Negative model results are evidence and must be recorded honestly.

## Acceptance criteria

- [ ] All deterministic tests required by SPEC-018 §7.1 are committed.
- [ ] The four missing scripted eval scenarios are committed.
- [ ] The live eval infrastructure can execute a real turn through `SkillTurnOrchestrator`.
- [ ] The full existing test suite passes.
- [ ] The full scripted eval suite passes.
- [ ] Live skill-aware evidence is recorded for `fast`, `mid`, and `deep`.
- [ ] The journal records whether each profile actually invoked `activate_skill`.
- [ ] The journal records initial skill, final skill, activation count, tool sequence, completion state, and duration for the cross-skill case.
- [ ] Skill coverage is measured as far as the repository can reproduce it without inventing an artificial historical baseline.
- [ ] Activation / prefix-reprefill latency is measured and documented.
- [ ] Model provenance is recorded for every live profile exercised.
- [ ] Any negative or inconclusive result is retained rather than tuned away.
- [ ] No production behaviour changes unless required to correct a defect discovered by the verification.
- [ ] No new third-party dependency is added.

## Files likely affected

Advisory, not restrictive:

- `tests/test_agent.py`
- `tests/test_skill_turn.py`
- tests around `skill_runtime/activation.py`
- trace/reliability tests where appropriate
- `evals/runner.py`
- scripted eval case definitions
- live eval case definitions
- `docs/journal/SPEC-018-mid-turn-skill-activation.md`
- `README.md` only if the documented verification command or eval workflow materially changes

Avoid modifying production runtime modules unless verification exposes a real defect.

## Verification commands

At minimum:

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

Then execute the new skill-aware live suite for:

```text
fast
mid
deep
```

using the repository's normal profile-selection mechanism.

Capture exact commands in the journal.

## Journal strategy

Append a `PATCH-018-01` subsection to:

`docs/journal/SPEC-018-mid-turn-skill-activation.md`

Do not create a separate model-behaviour journal: this PATCH exists specifically to complete the parent SPEC's missing evidence.

The PATCH journal section must record:

- why the verification debt existed;
- tests added;
- scripted cases added;
- live-eval infrastructure added;
- exact test/eval commands;
- test counts;
- model provenance:
  - Ollama version;
  - model name;
  - digest;
  - parameter count;
  - quantization;
  - context length;
- coverage result;
- per-profile activation result;
- representative transcripts;
- activation / reprefill timing;
- deviations and limitations;
- implementation commit SHA;
- `--no-ff` merge commit SHA.

The previous journal statements saying the evidence was not produced must remain as historical record. Append the PATCH evidence; do not rewrite history as though the evidence existed when SPEC-018 was originally merged.

## Out of scope

This PATCH must not include:

- Qwen3.8 installation or support;
- a Qwen3.8 model profile;
- router/agent model separation;
- reasoning-state preservation;
- retirement of `SkillRouter`;
- autonomous skill selection without the router;
- changes to skill package format;
- additional activation capabilities;
- changes to the active-skill precedence model;
- dynamic model escalation;
- general-purpose benchmarking infrastructure;
- optimization of prefix caching;
- changes made only to improve benchmark numbers.

The question of whether the router can eventually be retired must remain closed until this PATCH produces the evidence SPEC-018 originally required.

## Suggested branch and commit conventions

```text
branch:
patch/PATCH-018-01-complete-skill-activation-verification

patch file:
patches/SPEC-018/PATCH-018-01-Complete-Mid-Turn-Skill-Activation-Verification.md

implementation commit:
Complete mid-turn skill activation verification (PATCH-018-01)

merge:
Merge PATCH-018-01: complete skill activation verification
```