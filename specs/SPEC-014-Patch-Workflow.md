# SPEC-014: Patch Workflow

## Background

The project follows a strict spec-driven cycle (SPEC-003): one step = one
`SPEC-NNN`, with a feature branch, live-model verification, a full journal
entry, and a `--no-ff` merge. This is the right weight for a new capability
or architectural stage, but it is too heavy for a small correction to
something already built — a bug fix, an added safeguard, a diagnostics
improvement, a narrow refinement of an existing acceptance criterion.

Evidence this gap already existed in practice: `bc15fb8 Merge:
dependency-free .env loader for Tracker credentials` is a real merge with no
spec reference at all — exactly the kind of change this spec formalizes a
lighter path for, instead of leaving it undocumented.

The user drafted `PATCH-WORKFLOW-PROPOSAL.md` (repo root) describing a
**PATCH** tier: focused, parent-linked corrections to an existing SPEC, with
their own numbering, template, and reproducible-but-lighter cycle. This spec
adopts that proposal, adapted to this repo's actual conventions.

---

# Goal

Introduce a formal **PATCH workflow** alongside the existing SPEC cycle,
without weakening it:

```text
SPEC  = a new stage of project evolution
PATCH = a local correction or refinement of an existing SPEC
TYPO / DOCS FIX = a truly trivial direct change
```

The `spec-cycle` skill becomes the single entry point for both: it classifies
a change as SPEC, PATCH, or trivial direct fix, then follows the matching
flow. Chronological reproducibility, explicit intent, and traceability to the
originating SPEC are preserved for both tiers.

This spec does not change application behavior — it is process/tooling only.

---

# Functional requirements

## 1. `patches/` directory

- `patches/README.md` — PATCH vs SPEC decision rule, numbering
  (`PATCH-<SPEC>-<seq>`, local to the parent SPEC), directory/filename
  conventions, required PATCH sections, branch/verification/journal/commit/
  merge conventions, escalation rule to SPEC, and two worked examples (one
  PATCH-shaped, one SPEC-shaped).
- `patches/PATCH-TEMPLATE.md` — reusable template: Parent spec, Problem,
  Expected change, Constraints, Acceptance criteria, Files likely affected,
  Verification, Journal strategy, Out of scope.

## 2. `spec-cycle` skill update

Update `.claude/skills/spec-cycle/SKILL.md` (not a new competing skill):

- Frontmatter description and triggers cover both SPEC and PATCH work.
- A **Step 0 — Classify** section: SPEC / PATCH / trivial direct fix, with
  the parent SPEC named up front for PATCH work, and an explicit rule to
  escalate to a new SPEC if scope grows mid-patch.
- The existing 7-step **SPEC flow** is preserved unchanged.
- A new, shorter **PATCH flow**: patch note → patch branch
  (`patch/PATCH-NNN-XX-slug`) → focused implementation → regression
  verification (+ live-model verification only when model-facing behavior is
  affected) → journal update → commit (`… (PATCH-NNN-XX)`) → `--no-ff` merge
  (`Merge PATCH-NNN-XX: …`).
- Checklist and conventions reference extended with PATCH-specific entries.
- Anti-scope-creep rule: a PATCH covers exactly one correction; unrelated
  fixes get their own PATCH or SPEC.

## 3. Journal policy

`docs/journal/README.md` documents the hybrid PATCH journal rule:

- Append a `## Patches` subsection to the parent SPEC's journal entry when
  the change affects deterministic code only.
- Create a standalone `docs/journal/patches/PATCH-NNN-XX-slug.md` when the
  change affects observable model or agent behavior (directory created on
  first use — git does not track empty directories).
- Either way, index the patch under `## Patches` in the parent journal entry
  so it stays discoverable from the original step.

## 4. README

Root `README.md` (Russian, matching its existing language/style): add
`patches/` to the structure table, and a short paragraph in "Процесс
разработки" pointing at `patches/README.md` for the PATCH tier.

---

# Non-functional requirements

- Keep it as lightweight as the existing SPEC process — no new tooling,
  frameworks, or enforcement via hooks/CI.
- A PATCH document should fit on roughly one page; if it grows into a
  broader design proposal, that is itself the signal to escalate to a SPEC.
- English for skill/journal/patches docs, Russian for the root README —
  consistent with the existing split.

---

# Out of scope

- Implementing any actual PATCH (e.g. real repeated-tool-call detection) —
  `PATCH-010-01` in `patches/README.md` is illustrative only.
- Creating `docs/journal/patches/` as an empty directory ahead of the first
  real PATCH that needs it.
- Automating git operations or enforcing the process via hooks/CI.
- Modifying any existing `specs/*.md` file.

---

# Acceptance criteria

- `patches/README.md` and `patches/PATCH-TEMPLATE.md` exist and cover PATCH
  vs SPEC, numbering, conventions, journal rules, and escalation.
- `.claude/skills/spec-cycle/SKILL.md` classifies SPEC / PATCH / trivial fix,
  keeps the original SPEC flow intact, and adds the PATCH flow, checklist,
  and conventions.
- `docs/journal/README.md` documents the hybrid PATCH journal policy.
- Root `README.md` links `patches/README.md` from "Процесс разработки".
- This step itself is executed through the process it defines (its own
  branch, spec, journal entry, and `--no-ff` merge into `main`).

---

# Expected project structure

```text
patches/
  README.md
  PATCH-TEMPLATE.md
specs/
  SPEC-013-External-MCP-Yandex-Tracker-Read-Only.md
  SPEC-014-Patch-Workflow.md
docs/journal/
  README.md
  SPEC-013-tracker-mcp.md
  SPEC-014-patch-workflow.md
  patches/            (created on first real PATCH)
.claude/skills/spec-cycle/SKILL.md
PATCH-WORKFLOW-PROPOSAL.md
```

---

# Definition of Done

- `patches/` created as specified; `spec-cycle` skill, journal README, and
  root README updated as specified.
- Change delivered via its own feature branch, spec, journal entry, and
  `--no-ff` merge into `main`.
