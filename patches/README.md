# Patches

Companion to the `spec-cycle` skill (`.claude/skills/spec-cycle/SKILL.md`).
SPEC is the primary mechanism for project evolution; PATCH is a lighter,
still-reproducible workflow for local corrections to an already implemented
SPEC.

## Purpose

Not every meaningful change deserves a new architectural step and a new
sequential `SPEC-NNN`. Small bug fixes, behavioral corrections, diagnostics
improvements, additional safeguards, and narrow refinements to an already
implemented step get a **PATCH** instead — chronologically reproducible,
explicit about intent, traceable to its parent SPEC, still verified, but
proportional in size to the change.

```text
SPEC  = a new stage of project evolution
PATCH = a local correction or refinement of an existing SPEC
TYPO / DOCS FIX = a truly trivial direct change
```

## PATCH vs SPEC

Use a **PATCH** when most of the following hold:

- the parent SPEC's original goal is unchanged;
- the change fixes or strengthens existing behavior — fix, correct, clarify,
  guard, harden, improve, prevent, complete;
- no independent new capability is introduced;
- the public contract stays substantially the same;
- it can be verified with focused regression checks.

Use a new **SPEC** when any of the following hold:

- a new user-visible capability appears;
- a new architectural boundary is introduced;
- a public contract changes materially;
- several layers must change for one shared new purpose;
- new configuration or lifecycle semantics are introduced;
- the change needs substantial design background.

A truly trivial change (typo, broken Markdown link, comment spelling, doc
formatting) can be committed directly with no PATCH document — as long as it
does not change runtime behavior, a public contract, model-facing
instructions, tests, or acceptance criteria. When unsure, write a PATCH.

## PATCH numbering

`PATCH-<SPEC number>-<sequential patch number>`, local to the parent SPEC:

```text
SPEC-010-Agent-Loop
├── PATCH-010-01
├── PATCH-010-02
└── PATCH-010-03
```

Not a global sequence (`PATCH-001`, `PATCH-002`, …) — the parent-linked id
makes the origin of the change visible on its own.

## Directory and filename conventions

```text
patches/
├── README.md                  (this file)
├── PATCH-TEMPLATE.md
└── SPEC-NNN/
    └── PATCH-NNN-XX-Title-Case.md

docs/journal/patches/
└── PATCH-NNN-XX-slug.md       (only when a standalone journal is needed — see below)
```

## Required PATCH sections

See `patches/PATCH-TEMPLATE.md`: Parent spec, Problem, Expected change,
Constraints, Acceptance criteria, Files likely affected, Verification,
Journal strategy, Out of scope. A PATCH should fit on roughly one page — if
it grows into a broad design proposal with multiple new contracts or changes
across several layers, stop and write a SPEC instead.

## Branch conventions

From an up-to-date `main`:

```bash
git switch main && git pull
git switch -c patch/PATCH-NNN-XX-slug
```

## Verification rules

Proportional to the change, but must prove both that the PATCH behavior
works and that existing successful behavior has not regressed: relevant
automated tests, a focused regression scenario, the project-level test suite
where practical.

Live-model verification (same procedure as the SPEC flow — see `SKILL.md`
step 4) is required when the PATCH affects agent decisions, model-facing
prompts, tool selection, conversation history sent to the model, model-driven
loop termination, or model-visible tool results.

## Journal rules

Hybrid policy — see `docs/journal/README.md` for the template and full
rule. In short:

- **Append** a `## Patches` subsection to the parent SPEC's journal
  (`docs/journal/SPEC-NNN-slug.md`) when the change affects deterministic
  code only, with no model-facing or model-decision impact.
- **Create a standalone** `docs/journal/patches/PATCH-NNN-XX-slug.md` when
  the change affects observable model or agent behavior (prompts, history
  construction, tool-call loop semantics, termination conditions driven by
  model actions).
- When a standalone journal is created, add a short index entry (with a
  link) under `## Patches` in the parent SPEC's journal so it stays
  discoverable from the original step.

## Commit and merge conventions

```text
Commit subject: <imperative summary> (PATCH-NNN-XX)
Merge subject:  Merge PATCH-NNN-XX: <summary>
```

`--no-ff` merge into `main`, same as SPEC, so the PATCH remains a visible
boundary in `git log --first-parent --oneline`:

```bash
git switch main
git merge --no-ff patch/PATCH-NNN-XX-slug -m "Merge PATCH-NNN-XX: <summary>"
git push
```

Only commit when the user explicitly asks. Preserve the existing
`Co-Authored-By` trailer convention.

## Escalation to SPEC

If implementation reveals a broader architectural change than the PATCH
document described, stop, note the discovery in the PATCH document, and
open a new sequential SPEC instead. Do not let a PATCH grow into an
unreviewed architectural feature, and do not fold unrelated fixes into one
PATCH — give each defect its own PATCH or SPEC.

## Examples

**`PATCH-010-02` — Prevent repeated identical tool calls** (PATCH, not SPEC):
parent `specs/SPEC-010-Agent-Loop.md` already defines the agent loop; this
would only harden an existing termination path (detect consecutive identical
tool calls, terminate with an explicit reason) — no new capability, no
contract change. Because the fix depends on model output, it needs a
standalone journal: `docs/journal/patches/PATCH-010-02-repeated-tool-call-detection.md`.
This is illustrative only — no such PATCH has been written or implemented
(`PATCH-010-01` is the CLI activity indicator, and its number is taken).

**"Add persistent cross-session memory to the agent"** (SPEC, not PATCH):
introduces a new capability, new storage and lifecycle semantics, and new
contracts across multiple layers — not subordinate to any single existing
SPEC, so it needs a full `SPEC-NNN` with its own branch and journal.
