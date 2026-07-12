---
name: spec-cycle
description: The standard development cycle for this AI lab. Use when starting or working on a new iteration, spec, or feature — it walks through spec → branch → implement → verify → journal → commit → merge so every step is reproducible and the project history reads as an ordered list of steps. Trigger on "new spec", "SPEC-NNN", "start a feature", "next iteration".
---

# spec-cycle — standard iteration for this AI lab

This project is an experiment that must be **reproducible chronologically, step
by step**. One step = one `SPEC-NNN`. Follow this cycle for every change beyond a
trivial typo. This is a guide + checklist — you (or the user) still run git.

## Why this exists

Git reproduces *code*; specs capture *intent*. Neither reproduces *model
behavior*. So every step also gets a journal entry recording the model version,
sampling parameters, and what the model actually did. That is the artifact that
makes an AI step replayable.

## The cycle

1. **Spec** — start from `specs/SPEC-NNN-*.md`. If it doesn't exist, write it
   first: Background, Goal, Functional requirements, Acceptance criteria, Out of
   scope. Number sequentially. One spec = one step.
2. **Branch** — from an up-to-date `main`:
   `git switch main && git pull && git switch -c feature/SPEC-NNN-slug`.
3. **Implement** — per the spec, honoring the project ethos: framework-free
   (no LangChain/LangGraph/CrewAI/etc.), simple, readable, no premature
   abstraction. Confine conversation history changes to `conversation.py`.
4. **Verify** — end-to-end against the spec's acceptance criteria on the **live
   model**, not just unit checks. Ollama must be running with the configured
   model; drive `python app.py` (e.g. pipe a scripted dialogue via stdin) and
   confirm the observed behavior. Capture the transcript for the journal.
5. **Journal** — add `docs/journal/SPEC-NNN-slug.md` from the template in
   `docs/journal/README.md`. Fill model provenance + sampling params from the
   running Ollama instance (`GET /api/tags` gives name/digest/quant/ctx). Record
   the verification transcript and the outcome.
6. **Commit** — conventional message: imperative subject that references the spec
   (`... (SPEC-NNN)`), a short body explaining *why*, and the
   `Co-Authored-By: Claude ...` trailer. Only commit when the user asks.
7. **Merge** — `git switch main && git merge --no-ff feature/SPEC-NNN-slug -m
   "Merge SPEC-NNN: …"` then `git push`. The `--no-ff` merge commit marks the
   step boundary, so `git log --first-parent --oneline` is the list of steps.
   Delete the branch if you like.

## Checklist

- [ ] `specs/SPEC-NNN-*.md` written (or already exists)
- [ ] Branch `feature/SPEC-NNN-slug` off up-to-date `main`
- [ ] Implemented per spec; framework-free, simple, readable
- [ ] Verified end-to-end on the live model; acceptance criteria met
- [ ] `docs/journal/SPEC-NNN-slug.md` written with model provenance + transcript
- [ ] README updated if the change is user-visible
- [ ] Conventional commit referencing SPEC-NNN
- [ ] `--no-ff` merge into `main` + push

## Conventions reference

- Branch: `feature/SPEC-NNN-slug`
- Spec file: `specs/SPEC-NNN-Title-Case.md`
- Journal file: `docs/journal/SPEC-NNN-slug.md`
- Merge subject: `Merge SPEC-NNN: <summary>`
- Step = spec: don't merge unrelated concerns into one step; if a fix appears
  mid-branch, note it in that step's journal or give it its own spec.
