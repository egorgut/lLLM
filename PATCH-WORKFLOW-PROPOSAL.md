# Proposal: Introduce PATCH Workflow Alongside SPEC Cycle

## Purpose

The project currently uses a strict spec-driven development cycle:

```text
SPEC
→ branch
→ implementation
→ verification
→ journal
→ commit
→ merge
```

This cycle should remain the primary mechanism for project evolution.

However, not every meaningful change deserves a new architectural step and a new sequential `SPEC-NNN`. Small bug fixes, behavioural corrections, diagnostics improvements, additional safeguards, and narrow refinements to an already implemented step need a lighter but still reproducible workflow.

The goal of this change is to introduce a formal **PATCH workflow** without weakening the existing SPEC-driven history.

The resulting model should be:

```text
SPEC  = a new stage of project evolution
PATCH = a local correction or refinement of an existing SPEC
TYPO / DOCS FIX = a truly trivial direct change
```

The PATCH workflow must preserve:

- chronological reproducibility;
- explicit intent;
- readable Git history;
- traceability to the original SPEC;
- live-model verification when model behaviour is affected;
- the framework-free and simplicity-first ethos of the project.

---

# 1. Core Concepts

## 1.1 SPEC

A SPEC represents a new project capability, architectural stage, or independently meaningful behavioural contract.

A SPEC answers:

> What new stage of project evolution are we implementing?

Examples:

- adding a Python tool;
- adding an SQL tool;
- introducing MCP support;
- implementing the agent loop;
- adding the skills layer;
- introducing persistence;
- changing a public tool or agent contract;
- redesigning orchestration.

A SPEC continues to use the existing full workflow:

```text
spec
→ feature branch
→ implementation
→ live verification
→ full journal
→ commit
→ --no-ff merge
```

## 1.2 PATCH

A PATCH is a focused correction, safeguard, bug fix, or refinement of behaviour introduced by an existing SPEC.

A PATCH answers:

> What exactly must be corrected or improved in an already implemented step?

Examples:

- prevent repeated identical tool calls;
- improve a termination diagnostic;
- fix preservation of assistant tool-call messages;
- add a timeout guard to an existing execution path;
- correct an edge case in an already defined contract;
- improve logging without introducing a new observability subsystem;
- add a missing regression test;
- fix an implementation that does not fully satisfy an existing acceptance criterion.

A PATCH must always have a parent SPEC.

Example:

```text
SPEC-010 Agent Loop
├── PATCH-010-01 Prevent repeated identical tool calls
├── PATCH-010-02 Improve max-iterations diagnostics
└── PATCH-010-03 Preserve assistant tool-call messages
```

## 1.3 Trivial direct fix

A truly trivial change may be committed directly without a PATCH document.

Examples:

- typo correction;
- broken Markdown link;
- formatting-only documentation cleanup;
- comment spelling fix;
- non-behavioural renaming inside documentation.

A direct fix must not:

- change runtime behaviour;
- alter a public contract;
- change model-facing instructions;
- affect tests or acceptance criteria;
- introduce a new decision or constraint.

When uncertain, create a PATCH.

---

# 2. PATCH Numbering

PATCH identifiers must be tied to the parent SPEC.

Use:

```text
PATCH-<SPEC number>-<sequential patch number>
```

Examples:

```text
PATCH-010-01
PATCH-010-02
PATCH-011-01
```

Do not use one global sequence such as:

```text
PATCH-001
PATCH-002
```

The parent-linked identifier makes the origin and purpose of the change immediately visible.

The patch sequence is local to the parent SPEC:

```text
SPEC-009
├── PATCH-009-01
└── PATCH-009-02

SPEC-010
├── PATCH-010-01
├── PATCH-010-02
└── PATCH-010-03
```

---

# 3. Repository Structure

Add a new top-level `patches/` directory.

Recommended structure:

```text
specs/
├── SPEC-009-MCP.md
├── SPEC-010-Agent.md
└── SPEC-011-Agent-Reliability-and-Observability.md

patches/
├── SPEC-009/
│   └── PATCH-009-01-Improve-MCP-Error-Normalization.md
├── SPEC-010/
│   ├── PATCH-010-01-Repeated-Tool-Call-Detection.md
│   └── PATCH-010-02-Agent-Termination-Diagnostics.md
└── README.md

docs/
└── journal/
    ├── SPEC-009-mcp.md
    ├── SPEC-010-agent.md
    ├── SPEC-011-agent-reliability-and-observability.md
    └── patches/
        └── PATCH-010-01-repeated-tool-call-detection.md
```

## Required additions

Create:

```text
patches/README.md
```

This file must explain:

- what a PATCH is;
- when to use PATCH instead of SPEC;
- numbering rules;
- filename conventions;
- required document sections;
- branch, commit, and merge conventions;
- journal rules;
- escalation rules from PATCH to SPEC.

Optionally add a reusable template:

```text
patches/PATCH-TEMPLATE.md
```

---

# 4. PATCH Document Format

A PATCH document should be shorter than a full SPEC but must still be an implementation contract.

Recommended template:

```markdown
# PATCH-010-01 — Prevent Repeated Identical Tool Calls

## Parent spec

`specs/SPEC-010-Agent.md`

## Problem

Describe the observed defect, missing safeguard, ambiguity, or incomplete
implementation.

State the current behaviour and why it is undesirable.

## Expected change

Describe the smallest behaviour change that resolves the problem.

## Constraints

- Preserve the parent SPEC's architecture and intent.
- Do not introduce unrelated abstractions.
- Keep existing public contracts unchanged unless explicitly stated.
- Follow the project's framework-free and simplicity-first principles.

## Acceptance criteria

- Observable criterion 1.
- Observable criterion 2.
- Existing successful flows continue to work.
- Relevant regression tests pass.
- Live-model verification is performed when model behaviour is affected.

## Files likely affected

- `path/to/file.py`
- `tests/test_file.py`

This list is advisory rather than restrictive.

## Verification

Describe:

- automated checks;
- regression tests;
- end-to-end scenario;
- whether a live Ollama model is required;
- expected termination state or transcript fragment.

## Journal strategy

Choose one:

- append a PATCH section to the parent SPEC journal;
- create a standalone PATCH journal.

## Out of scope

Explicitly list nearby changes that must not be included.
```

## PATCH size guideline

A PATCH should usually fit on approximately one page.

If the document grows into a broad design proposal with substantial background,
multiple new contracts, or changes across several architectural layers, stop and
create a new SPEC instead.

---

# 5. Decision Rule: SPEC or PATCH

The AI developer must classify every non-trivial change before implementation.

## Use PATCH when all or most of the following are true

- the parent SPEC's original goal remains unchanged;
- the change fixes or strengthens existing behaviour;
- no independent new capability is introduced;
- the public contract remains substantially the same;
- the change can be described as:
  - fix;
  - correct;
  - clarify;
  - guard;
  - harden;
  - improve;
  - prevent;
  - complete;
- the parent SPEC's acceptance criteria become more reliable rather than fundamentally broader;
- the implementation is narrow and can be verified with focused regression checks.

## Use a new SPEC when any of the following are true

- a new user-visible capability appears;
- a new architectural boundary is introduced;
- a public contract changes materially;
- a new class of behaviour is added;
- several layers must change for a shared new purpose;
- new configuration or lifecycle semantics are introduced;
- the change needs substantial design background;
- the PATCH document is becoming large or ambiguous;
- the work no longer feels subordinate to one existing SPEC.

## Escalation rule

If work starts as a PATCH but implementation reveals a broader architectural
change, stop the PATCH, document the discovery, and create a new sequential SPEC.

Do not use PATCH as a way to bypass the full SPEC process.

---

# 6. PATCH Development Cycle

Introduce the following focused cycle:

```text
classify
→ patch note
→ patch branch
→ focused implementation
→ regression verification
→ journal update
→ commit
→ --no-ff merge
```

## 6.1 Classify

Before editing code, identify:

- the parent SPEC;
- why the change is a PATCH rather than a new SPEC;
- whether model behaviour is affected;
- which journal strategy is required.

## 6.2 Create the PATCH document

Create:

```text
patches/SPEC-NNN/PATCH-NNN-XX-Title-Case.md
```

The PATCH document must exist before implementation begins.

## 6.3 Create the branch

Branch from an up-to-date `main`:

```bash
git switch main
git pull
git switch -c patch/PATCH-NNN-XX-slug
```

Example:

```bash
git switch -c patch/PATCH-010-01-repeated-tool-call
```

## 6.4 Implement

Implementation rules:

- make the smallest change that satisfies the PATCH;
- preserve the parent SPEC's architecture;
- do not combine unrelated cleanups;
- avoid premature abstractions;
- remain framework-free;
- add or update regression tests;
- update README or user-facing documentation when behaviour visible to users changes.

If unrelated defects are discovered:

- note them separately;
- do not silently include them;
- create another PATCH or a new SPEC as appropriate.

## 6.5 Verify

Verification must be proportional to the change but must prove both:

1. the PATCH behaviour works;
2. existing successful behaviour has not regressed.

Minimum verification:

- relevant automated tests;
- focused regression scenario;
- project-level test suite where practical.

Live-model verification is required when the PATCH affects:

- agent decisions;
- model-facing prompts;
- tool selection;
- conversation history sent to the model;
- loop termination caused by model actions;
- model-visible tool results;
- behaviour that depends on an actual Ollama response.

When live verification is required:

- run against the configured Ollama model;
- capture the model identity and provenance;
- record the end-to-end transcript;
- record the observed termination state;
- compare the result with PATCH acceptance criteria.

## 6.6 Journal

Use the journal policy defined in Section 7.

## 6.7 Commit

Recommended commit format:

```text
<imperative summary> (PATCH-NNN-XX)
```

Example:

```text
Prevent repeated identical tool calls (PATCH-010-01)
```

Commit body should briefly explain why the correction was necessary.

Preserve the existing co-author convention used by the project.

Only commit when the user explicitly asks.

## 6.8 Merge

Merge with `--no-ff` so the PATCH remains a visible chronological boundary:

```bash
git switch main
git merge --no-ff patch/PATCH-010-01-repeated-tool-call   -m "Merge PATCH-010-01: prevent repeated tool calls"
git push
```

The resulting first-parent history should remain readable:

```text
Merge SPEC-009: MCP
Merge SPEC-010: Agent loop
Merge PATCH-010-01: prevent repeated tool calls
Merge PATCH-010-02: improve iteration diagnostics
Merge SPEC-011: Agent reliability and observability
```

Delete the patch branch after merge if desired.

---

# 7. PATCH Journal Policy

The existing project principle remains:

```text
Git preserves code.
SPEC or PATCH preserves intent.
Journal preserves observed model behaviour and reproducibility.
```

Use a hybrid journal strategy.

## 7.1 Append to the parent SPEC journal

Append a PATCH section to the parent journal when the change:

- affects deterministic Python code only;
- does not change model-facing context;
- does not change model decisions;
- does not require a distinct live-model transcript;
- is small enough to be understood as an addendum to the original step.

Example:

```markdown
## Patches

### PATCH-010-02 — Improve Agent Termination Diagnostics

#### Reason

The max-iteration path returned a generic error that did not expose the
termination reason.

#### Change

Added an explicit `max_iterations` termination state and surfaced it in the CLI.

#### Verification

- Unit tests passed.
- Existing successful tool-call flow passed.
- No model-facing messages were changed.

#### Outcome

Acceptance criteria met.
```

## 7.2 Create a standalone PATCH journal

Create:

```text
docs/journal/patches/PATCH-NNN-XX-slug.md
```

when the change affects observable LLM or agent behaviour.

Examples:

- model prompt changes;
- history construction changes;
- agent decision flow changes;
- tool-call loop semantics;
- repeated-call detection triggered by model output;
- termination conditions based on model actions;
- model-visible errors or tool responses.

The standalone PATCH journal must include:

- parent SPEC;
- PATCH document;
- reason for the correction;
- implementation summary;
- model name;
- model digest;
- quantization;
- context length;
- sampling parameters;
- exact verification command or procedure;
- captured transcript;
- observed result;
- acceptance-criteria assessment;
- known limitations;
- commit and merge references after completion.

## 7.3 Parent journal index

When a standalone PATCH journal is created, add a short link or index entry to
the parent SPEC journal:

```markdown
## Patches

- `PATCH-010-01` — repeated tool-call detection.
  See `docs/journal/patches/PATCH-010-01-repeated-tool-call-detection.md`.
```

This preserves discoverability from the original project step.

---

# 8. Required Changes to `spec-cycle` Skill

Update the existing `spec-cycle` skill rather than immediately creating a
separate competing skill.

The updated skill should become the single entry point for both SPEC and PATCH
work.

## 8.1 Update description and triggers

The skill description should explicitly mention:

- new iterations;
- full specs;
- focused fixes;
- patches to existing steps;
- behavioural corrections.

Suggested direction:

```yaml
name: spec-cycle
description: >
  Standard development cycle for this AI lab. Use for new project iterations,
  full specs, and focused patches to existing specs. Classifies work as SPEC,
  PATCH, or trivial direct fix, then applies the appropriate reproducible
  workflow.
```

Suggested additional triggers:

- `new patch`;
- `small fix`;
- `fix existing step`;
- `change current implementation`;
- `PATCH-NNN-XX`;
- `regression`;
- `behaviour correction`.

## 8.2 Add classification as Step 0

Insert a new first step:

```markdown
0. **Classify the change**
   - New capability or architectural evolution → SPEC
   - Local correction to an existing SPEC → PATCH
   - Typo or documentation-only non-behavioural fix → direct fix
```

The skill must require the AI developer to state the classification before
implementation.

For PATCH, it must identify the parent SPEC.

## 8.3 Split the workflow into two paths

The skill should contain:

```text
SPEC flow:
spec
→ feature branch
→ implementation
→ live verification
→ full journal
→ commit
→ --no-ff merge

PATCH flow:
patch note
→ patch branch
→ focused implementation
→ regression verification
→ parent journal update or standalone patch journal
→ commit
→ --no-ff merge
```

## 8.4 Preserve existing SPEC conventions

Do not weaken the current SPEC workflow.

The following must remain unchanged for full SPEC work:

- sequential `SPEC-NNN`;
- full specification sections;
- feature branch naming;
- live-model verification;
- full journal;
- conventional commit referencing the SPEC;
- `--no-ff` merge;
- first-parent history as the chronological step list.

## 8.5 Add PATCH conventions

Add a conventions section:

```text
Patch ID: PATCH-NNN-XX
Patch file: patches/SPEC-NNN/PATCH-NNN-XX-Title-Case.md
Patch branch: patch/PATCH-NNN-XX-slug
Patch journal: docs/journal/patches/PATCH-NNN-XX-slug.md
Patch commit: <imperative summary> (PATCH-NNN-XX)
Patch merge: Merge PATCH-NNN-XX: <summary>
```

## 8.6 Add PATCH checklist

Add a checklist similar to:

```markdown
### PATCH checklist

- [ ] Change classified as PATCH rather than SPEC
- [ ] Parent `SPEC-NNN` identified
- [ ] `PATCH-NNN-XX` document written
- [ ] Branch `patch/PATCH-NNN-XX-slug` created from up-to-date `main`
- [ ] Focused implementation completed
- [ ] Regression tests added or updated
- [ ] Existing successful behaviour verified
- [ ] Live-model verification completed when model behaviour is affected
- [ ] Parent journal updated or standalone PATCH journal created
- [ ] README updated if the change is user-visible
- [ ] Conventional commit references `PATCH-NNN-XX`
- [ ] `--no-ff` merge into `main`
```

## 8.7 Add anti-scope-creep rules

The skill must instruct the AI developer:

- do not combine unrelated fixes into one PATCH;
- do not introduce new capabilities through PATCH;
- do not perform opportunistic refactoring unless required by acceptance criteria;
- create another PATCH for a separate defect;
- escalate to a new SPEC when architectural scope grows;
- stop and explain the escalation before continuing.

---

# 9. `patches/README.md` Content Requirements

Create `patches/README.md` with at least these sections:

```text
# Patches

## Purpose
## PATCH vs SPEC
## PATCH numbering
## Directory and filename conventions
## Required PATCH sections
## Branch conventions
## Verification rules
## Journal rules
## Commit and merge conventions
## Escalation to SPEC
## Examples
```

Include at least one concrete example connected to an existing agent-related
SPEC.

Suggested example:

```text
PATCH-010-01 — Prevent repeated identical tool calls
```

Explain why it is a PATCH:

- the agent loop already exists;
- no new top-level capability is introduced;
- the change hardens an existing termination path;
- the parent architecture stays unchanged.

Also include an example that must become a new SPEC:

```text
Add persistent cross-session memory to the agent
```

Explain why it is not a PATCH:

- it introduces a new capability;
- it changes lifecycle and storage semantics;
- it requires new contracts and architecture.

---

# 10. Example PATCH

Create one example or template document similar to the following.

```markdown
# PATCH-010-01 — Prevent Repeated Identical Tool Calls

## Parent spec

`specs/SPEC-010-Agent.md`

## Problem

The agent may issue the same tool call with identical arguments repeatedly.
This can consume the iteration budget without making progress and produces an
unclear terminal result.

## Expected change

Detect consecutive identical tool calls and terminate the loop with an explicit,
diagnosable repeated-call reason.

## Constraints

- Do not redesign the agent loop.
- Do not introduce an orchestration framework.
- Keep the current tool execution contract unchanged.
- Detection is limited to exact tool name and normalized arguments.
- Do not implement semantic equivalence detection.

## Acceptance criteria

- Two consecutive calls with the same tool name and normalized arguments are
  detected.
- The second repeated call is not executed.
- The agent loop terminates with an explicit repeated-call reason.
- A normal sequence of different tool calls continues to work.
- Existing agent tests pass.
- A regression test covers the repeated-call case.
- The behaviour is verified with the live configured Ollama model.

## Files likely affected

- `agent.py`
- `tests/test_agent.py`

## Verification

- Run focused unit tests.
- Run the full test suite.
- Drive `python app.py` with a prompt that previously produced repeated calls.
- Capture the transcript and termination state.

## Journal strategy

Create a standalone PATCH journal because the observed agent behaviour depends
on model output.

## Out of scope

- semantic comparison of equivalent argument values;
- repeated-call detection across separate user requests;
- general planning-loop redesign;
- configurable repeated-call thresholds.
```

The example may be stored as documentation only and does not imply that this
specific PATCH must be implemented immediately.

---

# 11. Definition of Done for This Workflow Change

This workflow change is complete when:

- [ ] the existing `spec-cycle` skill supports SPEC, PATCH, and trivial direct-fix classification;
- [ ] the original full SPEC workflow remains intact;
- [ ] a `patches/` directory exists;
- [ ] `patches/README.md` documents the complete PATCH process;
- [ ] a PATCH template or example exists;
- [ ] PATCH numbering is linked to parent SPEC numbers;
- [ ] branch, commit, and merge conventions are documented;
- [ ] the hybrid journal policy is documented;
- [ ] `docs/journal/patches/` exists or its creation rule is documented;
- [ ] the parent journal indexing rule is documented;
- [ ] escalation from PATCH to SPEC is explicit;
- [ ] anti-scope-creep rules are explicit;
- [ ] the repository README or contributor documentation links to both the SPEC
      and PATCH workflows where appropriate;
- [ ] the changes are reviewed for consistency with existing repository naming
      and templates.

---

# 12. Expected Outcome

After this change, the project history should clearly distinguish major
evolutionary steps from focused corrections.

Example:

```text
Merge SPEC-009: MCP
Merge SPEC-010: Agent loop
Merge PATCH-010-01: prevent repeated tool calls
Merge PATCH-010-02: improve termination diagnostics
Merge SPEC-011: Agent reliability and observability
Merge SPEC-012: Skills
```

This preserves the project's core philosophy:

- large changes remain spec-driven;
- small behavioural changes remain explicit;
- every non-trivial change has documented intent;
- model-dependent behaviour remains reproducible;
- Git history remains chronological and readable;
- PATCH cannot silently become an unreviewed architectural feature.
