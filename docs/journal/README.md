# Iteration journal

One entry per step (`SPEC-NNN`), recording not just *what* changed but the
**model behavior observed at the time**. Git reproduces the code and the specs
capture the intent; this journal captures the piece that makes an *AI* step
reproducible — which model and parameters were used, and what the model actually
did.

Read chronologically, these entries replay how the lab evolved. See the
`spec-cycle` skill (`.claude/skills/spec-cycle/SKILL.md`) for the full workflow.

- File per step: `docs/journal/SPEC-NNN-slug.md`
- Entries begin at SPEC-002 (the first spec-driven iteration). Earlier scaffold
  work lives in git history only.

## Entry template

```markdown
# SPEC-NNN — <title>

- **Spec:** [SPEC-NNN](../../specs/SPEC-NNN-....md)
- **Date:** YYYY-MM-DD
- **Branch:** feature/SPEC-NNN-slug
- **Merge commit:** <short-sha>

## Hypothesis / intent
Why this step; what we expected to improve.

## What changed
Bullet summary of the change (files, behavior).

## Model & parameters (provenance)
- Model: <name> (digest <short>, <quant>, ctx <n>)
- Ollama: <version>
- Sampling: <params, or "defaults — no options set in llm.py">

## Verification
How it was tested against the acceptance criteria + observed result.
Include a short transcript excerpt.

## Outcome
Did it meet the acceptance criteria? What we learned.

## Follow-ups
Anything deferred or noticed for a future spec.
```
