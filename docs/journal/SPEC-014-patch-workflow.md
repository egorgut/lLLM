# SPEC-014 — Patch workflow

- **Spec:** [SPEC-014](../../specs/SPEC-014-Patch-Workflow.md)
- **Date:** 2026-07-25
- **Branch:** feature/SPEC-014-patch-workflow
- **Merge commit:** this step's own `--no-ff` merge into `main` (locate via `git log --first-parent`)

## Hypothesis / intent

The full SPEC cycle is the right weight for new capabilities, but too heavy
for small corrections to already-implemented steps — real precedent already
existed in `bc15fb8 Merge: dependency-free .env loader for Tracker
credentials`, a merge with no spec reference. Introducing a lighter,
parent-linked PATCH tier should let small fixes stay reproducible and
traceable without inflating the SPEC sequence with one-line corrections.

## What changed

- `patches/README.md` and `patches/PATCH-TEMPLATE.md` (new): PATCH vs SPEC
  rule, `PATCH-<SPEC>-<seq>` numbering, conventions, hybrid journal policy,
  escalation rule, two worked examples.
- `.claude/skills/spec-cycle/SKILL.md`: added Step 0 classification (SPEC /
  PATCH / trivial fix), kept the existing 7-step SPEC flow unchanged, added a
  parallel PATCH flow, split checklist, extended conventions reference.
- `docs/journal/README.md`: documented the hybrid PATCH journal rule (append
  to parent journal vs. standalone `docs/journal/patches/PATCH-NNN-XX-slug.md`)
  and the parent-journal index-entry rule.
- Root `README.md`: added `patches/` to the structure table and a short
  paragraph in "Процесс разработки" linking `patches/README.md`.
- `PATCH-WORKFLOW-PROPOSAL.md` (the user's original proposal, previously
  untracked) committed alongside as the historical source document.
- Process/tooling only — no application behavior changed.

## Model & parameters (provenance)

- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, ctx 40960)
- Ollama: 0.31.1
- Sampling: defaults — no `options` set in `llm.py`
- N/A to runtime: this step touches docs/skill only; the model was not
  invoked as part of the change. Provenance recorded for continuity, as in
  SPEC-003.

## Verification

Structural, as in SPEC-003: the change is delivered through the very process
it defines (its own spec, branch, journal entry, `--no-ff` merge). Checked
for internal consistency by re-reading `SKILL.md` end-to-end and confirming
the original SPEC flow section is byte-for-byte unchanged; cross-checked
naming conventions (branch/file/journal/commit/merge patterns) match across
`patches/README.md`, `PATCH-TEMPLATE.md`, `SKILL.md`, and `docs/journal/README.md`.

## Outcome

Acceptance criteria met: PATCH tier documented end-to-end, SPEC flow
preserved, journal policy and README updated, delivered via its own
SPEC-014 branch/journal/merge.

## Follow-ups

The `PATCH-010-01` example in `patches/README.md` is illustrative only — no
actual PATCH has been implemented yet. The first real PATCH will be the
first live test of this workflow and may surface adjustments.
