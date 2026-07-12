# SPEC-003: Standardize Development Workflow

## Background

The project is an experimental AI lab. Its value lies not only in the final
code but in being able to **replay how it evolved, step by step** — including
the reasoning behind each change and the observed model behavior at the time.

Plain git history plus specs capture *code* and *intent*, but for an AI lab that
is not enough to reproduce a step: the same commit can produce different model
output because nowhere is it recorded *which model version and parameters were
used* or *what the model actually answered* and why the step was deemed a
success.

After two iterations (SPEC-002 + follow-ups) a de-facto process has emerged:
numbered spec → `feature/SPEC-NNN-slug` branch → implement → verify on the live
model → conventional commit → `--no-ff` merge. This spec makes that process
explicit and reproducible.

---

# Goal

Standardize and document the development workflow so every iteration is executed
the same way and the project can be reconstructed chronologically, step by step.

One **step** = one **SPEC-NNN**.

Introduce:

1. A `spec-cycle` skill — a guide + checklist for the standard iteration.
2. A per-iteration journal under `docs/journal/` that records intent, the
   change, model provenance + sampling parameters, and the observed result.

This spec does **not** change application behavior — it is process/tooling only.

---

# Functional requirements

## 1. `spec-cycle` skill

Create `.claude/skills/spec-cycle/SKILL.md` describing the standard cycle as a
guide with a scannable checklist. It documents conventions; it does not have to
run git for you.

The cycle it standardizes:

1. **Spec** — every step starts from `specs/SPEC-NNN-*.md`. If none exists,
   write it first (background, goal, functional requirements, acceptance
   criteria). One spec = one step.
2. **Branch** — `feature/SPEC-NNN-slug` off up-to-date `main`.
3. **Implement** — per the spec. Keep the project ethos: framework-free, simple,
   readable, no premature abstraction.
4. **Verify** — end-to-end against the spec's acceptance criteria on the live
   model (Ollama running), not only unit-level checks.
5. **Journal** — add `docs/journal/SPEC-NNN-slug.md` from the template.
6. **Commit** — conventional message: imperative subject, reference the spec,
   `Co-Authored-By` trailer.
7. **Merge** — `--no-ff` into `main` (`Merge SPEC-NNN: …`) so
   `git log --first-parent` reads as an ordered list of steps. Push.

## 2. Iteration journal

Create `docs/journal/` with:

- `README.md` — what the journal is + the entry template.
- One entry per step: `docs/journal/SPEC-NNN-slug.md`.

Each entry records: linked spec, date, branch/merge commit, hypothesis/intent,
what changed, **model provenance** (name, digest, quantization, context length,
ollama version) and **sampling parameters**, verification (how tested + observed
result / transcript excerpt), outcome, and follow-ups.

Backfill an entry for SPEC-002 so the journal starts where spec-driven
development began. Earlier scaffold work lives in git history only.

## 3. README

Reference the workflow (link the skill) and the journal from the root `README.md`.

---

# Non-functional requirements

- Keep it lightweight and educational — suitable for a solo experimental repo.
- No heavy process (no ADRs, no CHANGELOG, no CI gates) at this stage.
- The journal must capture model provenance, since that — not the code — is what
  makes an AI step reproducible.

---

# Out of scope

- Automating git operations inside the skill.
- Enforcing the process via hooks or CI.
- Retroactively writing specs for the pre-SPEC-002 scaffold.

---

# Acceptance criteria

- `.claude/skills/spec-cycle/SKILL.md` exists and describes the 7-step cycle with
  a checklist.
- `docs/journal/README.md` + a template exist.
- A backfilled `docs/journal/SPEC-002-*.md` entry exists with model provenance.
- This step itself is executed through the process it defines (its own branch,
  spec, journal entry, and `--no-ff` merge).
- Root `README.md` links the workflow and journal.

---

# Expected project structure

``` text
project/
  app.py
  conversation.py
  llm.py
  config.py
  prompts.py
  tools/
  specs/
    SPEC-002-Extract-Conversation-State.md
    SPEC-003-Standardize-Development-Workflow.md
  docs/journal/
    README.md
    SPEC-002-extract-conversation-state.md
    SPEC-003-standardize-development-workflow.md
  .claude/skills/spec-cycle/SKILL.md
```

---

# Definition of Done

- Skill and journal created as specified.
- SPEC-002 backfilled in the journal.
- README updated.
- Change delivered via its own feature branch and `--no-ff` merge into `main`.
