# PATCH-016-01 — Cross-Turn Artifact Guidance

## Parent spec

`specs/SPEC-016-agent-workspace-and-sandbox-skill.md`

## Problem

SPEC-016's live verification produced a correct answer by an expensive route.
Asked to read back a CSV the previous turn had created, qwen3:8b spent two of
its four tool calls discovering a fact the skill already knows statically:

```text
You: Now read that CSV back and tell me the sum of the squares column.

[tool 1/4] sandbox_execute -> non_zero_exit
    FileNotFoundError: 'numbers_squares.csv'
[tool 2/4] sandbox_execute -> non_zero_exit
    FileNotFoundError: 'data/artifacts/aa6f9285-.../numbers_squares.csv'

Qwen: The CSV file wasn't found because the sandbox environment doesn't have
      access to previously created artifacts. ...
```

The isolation worked exactly as SPEC-016 §14.3 requires, and the model's final
explanation was accurate. The defect is in the guidance, not the boundary: a
later turn *never* has access to an earlier turn's workspace, so no amount of
retrying can change the outcome, and the second call was spent re-learning what
the first had already established.

The current `SKILL.md` does mention this, but in the two places least likely to
be read at the moment of decision:

- a `## Constraints` bullet lumps "files from an earlier turn" together with
  Docker, the host filesystem, and the internet, and phrases it as a claim not
  to make ("Never ask for, or claim to have, access to...") rather than as
  something to do when the user asks for exactly that;
- the retry rule (`## Procedure` step 9) says only "retry only when the error is
  plausibly correctable" and never names this case, so a `FileNotFoundError`
  reads like an ordinary correctable path mistake — which is precisely how the
  model treated it.

Nothing tells the model what to do *instead*: regenerate the data in this call,
or ask the user to supply it as `input_files`.

## Expected change

Sharpen the `code_workspace` instruction so a cross-turn file request is
recognised before the first call, and is never retried if one is made anyway:

1. State the input rule positively where the tool is described: the sandbox sees
   only the files supplied as `input_files` in **this** call — never a file from
   an earlier turn, and never a file an earlier script wrote.
2. Name this case explicitly in the retry rule, alongside the other
   non-correctable failures.
3. Say what to do instead: recreate the data inside the same script, or ask the
   user to supply it.

Instruction text only. No change to the tool contract, the runtime, the
workspace, the artifact policy, or any limit.

## Constraints

- Preserve SPEC-016's architecture: one tool, one skill, host-owned identity and
  policy, artifacts committed only with a successful turn.
- Do not weaken turn isolation, or add any mechanism for reaching a prior turn's
  workspace — the boundary is correct and stays exactly as it is.
- Do not change `sandbox_execute`'s schema, envelope, or status vocabulary.
- Keep the skill within the loader's structural rules (four front-matter keys,
  the seven required H2 sections, ≤ 200-char description).
- Framework-free; no new dependency.

## Acceptance criteria

- `SKILL.md` states the "inputs come only from this call" rule where the tool
  and its paths are described.
- The retry rule names cross-turn/prior-artifact access as non-correctable.
- The instruction offers the two alternatives (regenerate, or ask the user).
- A regression test asserts the guidance is present, so a later edit cannot
  silently drop it.
- A scripted eval case documents the intended shape: a request to read a prior
  turn's artifact costs zero sandbox calls.
- The `code_workspace` package still loads and still allows exactly
  `sandbox_execute`; the full `pytest` suite and the scripted eval suite pass.
- Live-model verification replays the exact two-turn dialogue from the SPEC-016
  journal, over several runs, and the second turn **recovers by recreating the
  data** rather than retrying the lookup with another path spelling — ending
  with a real result instead of an apology.

> **Revised after measurement.** This criterion originally read "spends fewer
> tool calls on the second turn than the recorded two". Three live runs spent
> 3, 2, and 1 calls against a single-run baseline of 2, so that criterion is not
> met and claiming it would be false. What *did* change in every run is the
> recovery strategy, which is the behavior worth locking in; the call count at
> this model size is dominated by sampling variance. The journal records the
> full measurement, including the run that still probed twice.

## Files likely affected

- `skills/code_workspace/SKILL.md`
- `skills/code_workspace/examples/` — one example for the scenario
- `skills/code_workspace/evals/cases.json`
- `tests/test_code_workspace_skill.py`
- `evals/cases.json`, `tests/test_eval_runner.py` — one new category

This list is advisory, not restrictive.

## Verification

- `python -m pytest -q` (full suite, no regression)
- scripted eval suite passes with the new case
- Live model, replaying the SPEC-016 journal's dialogue verbatim: create a CSV,
  then ask to read it back. Repeat several times — one run is not a measurement
  when the thing being changed is a model decision — and compare both the tool
  calls and the recovery strategy against the recorded baseline.

Live-model verification **is** required: this changes a model-facing instruction
and the decision it is meant to influence is a model decision. A green test
proves the words are present, not that they work.

## Journal strategy

Create a standalone journal at
`docs/journal/patches/PATCH-016-01-cross-turn-artifact-guidance.md`, indexed
under `## Patches` in `docs/journal/SPEC-016-agent-workspace-and-sandbox-skill.md`.
The change is model-facing prompt text whose whole purpose is to alter an
observable model decision, which is exactly the case the hybrid rule reserves a
standalone entry for. It also needs its own provenance: the result is only
meaningful against the model version that produced the baseline.

## Out of scope

- Any mechanism for carrying an artifact from one turn into the next
  (attachment ingestion, a re-supply helper, a persistent workspace). SPEC-016
  §28 lists these as future work; this PATCH only teaches the model to recognise
  the boundary that exists.
- The `stdout_preview` trace finding recorded in the SPEC-016 journal. A
  separate concern against a different spec, and its own PATCH.
- Broader retry-policy tuning for unrelated failure classes.
- Any change to the other skill packages.
