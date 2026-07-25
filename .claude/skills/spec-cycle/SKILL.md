---
name: spec-cycle
description: The standard development cycle for this AI lab. Use when starting or working on a new iteration, spec, focused patch, or trivial fix — it classifies the change as SPEC, PATCH, or a trivial direct fix, then walks through the matching reproducible flow (spec/patch → branch → implement → verify → journal → commit → merge) so the project history reads as an ordered list of steps. Trigger on "new spec", "SPEC-NNN", "start a feature", "next iteration", "new patch", "small fix", "fix existing step", "PATCH-NNN-XX", "regression", "behaviour correction".
---

# spec-cycle — standard iteration for this AI lab

This project is an experiment that must be **reproducible chronologically, step
by step**. One step = one `SPEC-NNN`, or one focused `PATCH-NNN-XX` against an
existing SPEC. Follow this cycle for every change beyond a trivial typo. This is
a guide + checklist — you (or the user) still run git.

## Why this exists

Git reproduces *code*; specs capture *intent*. Neither reproduces *model
behavior*. So every step also gets a journal entry recording the model version,
sampling parameters, and what the model actually did. That is the artifact that
makes an AI step replayable.

## Step 0 — Classify

Before touching code, decide which of the three applies:

- **SPEC** — a new user-visible capability, a new architectural boundary, a
  material public-contract change, or work needing substantial design
  background. Full cycle below.
- **PATCH** — a focused correction, safeguard, or refinement of behavior an
  existing SPEC already introduced (fix/correct/clarify/guard/harden/improve),
  with no new capability and no material contract change. Name the parent
  SPEC before doing anything else. See `patches/README.md` for the full
  decision rule and the PATCH flow below.
- **Trivial direct fix** — typo, broken link, doc formatting, comment
  spelling: no behavior, contract, test, or model-facing change. Commit
  directly, no document required.

If a PATCH turns out to need a new capability or contract once you're
implementing it, stop, note the discovery, and escalate to a new SPEC instead
of letting the PATCH grow past its scope.

## SPEC flow

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

## PATCH flow

Full rules, templates, and examples live in `patches/README.md` and
`patches/PATCH-TEMPLATE.md` — this is the condensed version.

1. **Patch note** — write `patches/SPEC-NNN/PATCH-NNN-XX-Title-Case.md` from
   `patches/PATCH-TEMPLATE.md` before implementing: parent spec, problem,
   expected change, constraints, acceptance criteria, files likely affected,
   verification, journal strategy, out of scope.
2. **Branch** — from an up-to-date `main`:
   `git switch main && git pull && git switch -c patch/PATCH-NNN-XX-slug`.
3. **Implement** — the smallest change that satisfies the patch note; preserve
   the parent SPEC's architecture; don't bundle unrelated cleanups or
   opportunistic refactors; stay framework-free.
4. **Verify** — regression tests proving both the fix and no regression in
   existing behavior. Add live-model verification only when the patch affects
   agent decisions, model-facing prompts, tool selection, conversation history
   sent to the model, model-driven termination, or model-visible tool results.
5. **Journal** — append a `## Patches` subsection to the parent
   `docs/journal/SPEC-NNN-slug.md` for deterministic-code-only changes, or
   create a standalone `docs/journal/patches/PATCH-NNN-XX-slug.md` (indexed
   back from the parent journal) when model behavior is affected.
6. **Commit** — `... (PATCH-NNN-XX)` subject, short *why* body,
   `Co-Authored-By: Claude ...` trailer. Only commit when the user asks.
7. **Merge** — `git switch main && git merge --no-ff patch/PATCH-NNN-XX-slug
   -m "Merge PATCH-NNN-XX: …"` then `git push`.

## Checklist

### SPEC

- [ ] `specs/SPEC-NNN-*.md` written (or already exists)
- [ ] Branch `feature/SPEC-NNN-slug` off up-to-date `main`
- [ ] Implemented per spec; framework-free, simple, readable
- [ ] Verified end-to-end on the live model; acceptance criteria met
- [ ] `docs/journal/SPEC-NNN-slug.md` written with model provenance + transcript
- [ ] README updated if the change is user-visible
- [ ] Conventional commit referencing SPEC-NNN
- [ ] `--no-ff` merge into `main` + push

### PATCH

- [ ] Change classified as PATCH rather than SPEC; parent `SPEC-NNN` identified
- [ ] `patches/SPEC-NNN/PATCH-NNN-XX-Title-Case.md` written
- [ ] Branch `patch/PATCH-NNN-XX-slug` off up-to-date `main`
- [ ] Focused implementation; no unrelated fixes folded in
- [ ] Regression tests added/updated; existing behavior verified
- [ ] Live-model verification done if model behavior is affected
- [ ] Parent journal updated, or standalone PATCH journal written + indexed
- [ ] README updated if the change is user-visible
- [ ] Conventional commit referencing PATCH-NNN-XX
- [ ] `--no-ff` merge into `main` + push

## Conventions reference

- Branch: `feature/SPEC-NNN-slug`
- Spec file: `specs/SPEC-NNN-Title-Case.md`
- Journal file: `docs/journal/SPEC-NNN-slug.md`
- Merge subject: `Merge SPEC-NNN: <summary>`
- Patch branch: `patch/PATCH-NNN-XX-slug`
- Patch file: `patches/SPEC-NNN/PATCH-NNN-XX-Title-Case.md`
- Patch journal: `docs/journal/patches/PATCH-NNN-XX-slug.md` (standalone case)
- Patch commit subject: `<imperative summary> (PATCH-NNN-XX)`
- Patch merge subject: `Merge PATCH-NNN-XX: <summary>`
- Step = spec or patch: don't merge unrelated concerns into one step; if a fix
  appears mid-branch, note it in that step's journal or give it its own
  spec/patch. A PATCH covers exactly one correction — a second unrelated
  defect gets its own PATCH or SPEC, never folded in.
