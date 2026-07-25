# PATCH-NNN-XX — Title

## Parent spec

`specs/SPEC-NNN-Title-Case.md`

## Problem

Describe the observed defect, missing safeguard, ambiguity, or incomplete
implementation. State the current behavior and why it is undesirable.

## Expected change

Describe the smallest behavior change that resolves the problem.

## Constraints

- Preserve the parent SPEC's architecture and intent.
- Do not introduce unrelated abstractions.
- Keep existing public contracts unchanged unless explicitly stated.
- Follow the project's framework-free, simplicity-first ethos.

## Acceptance criteria

- Observable criterion 1.
- Observable criterion 2.
- Existing successful flows continue to work.
- Relevant regression tests pass.
- Live-model verification is performed when model behavior is affected.

## Files likely affected

- `path/to/file.py`
- `tests/test_file.py`

This list is advisory, not restrictive.

## Verification

Describe the automated checks, regression tests, and end-to-end scenario;
state whether a live Ollama model is required and, if so, the expected
termination state or transcript fragment.

## Journal strategy

Choose one (see `patches/README.md` → Journal rules):

- append a `## Patches` subsection to the parent SPEC journal;
- create a standalone journal at `docs/journal/patches/PATCH-NNN-XX-slug.md`.

## Out of scope

Explicitly list nearby changes that must not be included in this PATCH.
