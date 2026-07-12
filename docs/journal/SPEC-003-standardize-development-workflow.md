# SPEC-003 — Standardize development workflow

- **Spec:** [SPEC-003](../../specs/SPEC-003-Standardize-Development-Workflow.md)
- **Date:** 2026-07-12
- **Branch:** feature/SPEC-003-standardize-workflow
- **Merge commit:** this step's own `--no-ff` merge into `main` (locate via `git log --first-parent`)

## Hypothesis / intent
A de-facto process emerged over SPEC-002 (spec → branch → implement → verify →
`--no-ff` merge). Making it explicit — plus a journal that records model
behavior — should let the lab be reconstructed chronologically, step by step,
including the pieces git alone can't reproduce.

## What changed
- `.claude/skills/spec-cycle/SKILL.md`: guide + checklist for the 7-step cycle.
- `docs/journal/`: `README.md` (purpose + entry template) and one entry per step.
- Backfilled `docs/journal/SPEC-002-*.md`.
- Root README links the workflow and the journal.
- Process/tooling only — no application behavior changed.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, ctx 40960)
- Ollama: 0.6.2
- Sampling: defaults — no `options` set in `llm.py`
- N/A to runtime: this step touches docs/skill only; the model was not invoked
  as part of the change. Provenance recorded for continuity.

## Verification
This is a process step, so "verification" is structural: the change is delivered
through the very process it defines — its own spec (SPEC-003), feature branch,
journal entries, and a `--no-ff` merge into `main`. The app is unchanged, so
`python app.py` behavior from SPEC-002 still holds; no new runtime behavior to
exercise.

## Outcome
Workflow and journal established. `git log --first-parent` now reads as an
ordered list of steps, each paired with a spec and a journal entry.

## Follow-ups
- If manual discipline slips, a future spec could add a hook/CI check that a
  merge into `main` carries a matching spec + journal entry.
- Consider pinning the model digest in `config.py` (carried over from SPEC-002).
