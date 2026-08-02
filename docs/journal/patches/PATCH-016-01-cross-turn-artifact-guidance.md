# PATCH-016-01 — Cross-Turn Artifact Guidance

- **Patch:** [PATCH-016-01](../../../patches/SPEC-016/PATCH-016-01-Cross-Turn-Artifact-Guidance.md)
- **Parent spec:** [SPEC-016](../../../specs/SPEC-016-agent-workspace-and-sandbox-skill.md)
- **Date:** 2026-08-02
- **Branch:** patch/PATCH-016-01-cross-turn-artifact-guidance
- **Merge commit:** _(filled in at merge)_

## Hypothesis / intent

SPEC-016's live run ended well but expensively: asked to read back a CSV the
previous turn had made, qwen3:8b spent two of four tool calls discovering that a
later turn cannot see an earlier turn's files — a fact the skill knows
statically. The hypothesis was that the guidance, not the model, was at fault:
the rule existed only as a "never claim access" bullet buried among three
unrelated ones, and the retry rule never named this case, so a
`FileNotFoundError` read like an ordinary correctable path mistake.

Expected: naming it in the retry rule and stating the alternative would stop the
second doomed call; naming it in the ordered procedure would stop the first.

## What changed

`skills/code_workspace/SKILL.md` only — instruction text, no code:

- **`## Available tools`** — a positive statement of the input rule: each run
  starts empty, the only files present are this call's `input_files`, and when
  the task needs data that already went out as an artifact, recreate it or ask
  the user.
- **`## Procedure` step 2 (new)** — a check *before* writing any script: if the
  request names a file from an earlier turn, decide now to recreate it or ask
  for it, and never write a script that opens it.
- **`## Procedure` step 11 (new)** — cross-turn/prior-run file access joins the
  named non-correctable failures, beside network, package, host path, and
  unavailable runtime.
- **`## Constraints`** — the retry duplicate removed (the procedure now owns
  retry policy in one place), and the access bullet split so the cross-turn case
  is its own rule covering how a user actually phrases it: "that CSV", "the file
  you just made", a path quoted back from a previous answer.

Plus `examples/prior_turn_file.md`, four regression tests asserting the guidance
is present, and one scripted eval case (`sandbox_cross_turn_refusal`).

## Model & parameters (provenance)

- Model: qwen3:8b (digest `500a1f067a9f`, Q4_K_M, ctx 40960, 8.2B params)
- Ollama: 0.31.1
- Sampling: defaults — `llm.py` sets no `options`
- Sandbox image: `lllm-sandbox:spec-015`, image ID
  `sha256:1bc9f8b485749ca1573754b7d36a612b018a4410d77b722de93ed2c52822450d`
  (rebuilt during this patch; Docker Desktop had lost the SPEC-016 build)

## Verification

`555 passed, 29 skipped`; scripted evals `36/36`.

Live: the SPEC-016 dialogue replayed verbatim, three times.

| | turn 2 calls | what turn 2 did |
|---|---|---|
| **baseline** (SPEC-016, 1 run) | 2 | lookup → **lookup again with another path** → apology, no artifact |
| run 1 | 3 | lookup → lookup again → **recreate** → 55 + artifact |
| run 2 | 2 | lookup → **recreate** → 55 + artifact |
| run 3 | 1 | **recreate straight away** → 55 + artifact |

Run 3, the intended behavior, in full:

```text
You: Now read that CSV back and tell me the sum of the squares column.

[skill] code_workspace

[tool 1/4] sandbox_execute
[args] {"input_files": [], "language": "python",
        "source": "import csv\n\n# Create squares.csv\nwith open('squares.csv', 'w'...
[result] {"ok": true, "status": "succeeded", "exit_code": 0, ...
          "artifacts": [{"name": "squares.csv", "media_type": "text/csv", ...}]}

Qwen: The sum of the squares column (1² + 2² + 3² + 4² + 5²) is **55**.
```

## Outcome

**Partially successful, and the original acceptance criterion was not met.**

The criterion said the second turn must spend fewer than the baseline's two
calls. Measured across three runs it spent 3, 2, and 1 — mean 2.0, i.e. no
improvement in call count against a single-run baseline. That criterion was
revised in the patch note to the claim the evidence supports, rather than
quietly left standing.

What *did* change, in every run: the recovery strategy. The baseline responded
to `FileNotFoundError` by retrying the same lookup with a different path
spelling and then giving up. After the patch, every run recovered by
**recreating the data** and finished with a real result and an artifact instead
of an apology. That is the behavior the skill now names explicitly, and it is
the difference between a wasted turn and a useful one.

The first doomed call was not eliminated. Two of three runs still probed for the
file before recreating it, and run 1 probed twice — so procedure step 2 is not
reliably read as a precondition at this model size. Honest reading: at 8B with
default sampling, a mid-instruction procedural check competes with the much
stronger prior that "read a CSV" means "open a CSV", and variance across runs
(1-3 calls) is larger than the effect being measured. Three runs is a small
sample; it is enough to say the strategy changed and not enough to claim the
count did.

Kept rather than reverted because it is strictly better guidance at zero cost:
the non-correctable list is now accurate, the alternatives are stated, retry
policy lives in one place instead of two, and the tests keep it from drifting
back. But it is a nudge, not a guarantee — worth remembering the next time a
prompt change is expected to fix a behavior.

## Follow-ups

- **`input_files: {}` costs a tool call — separate defect, found during this
  patch's live runs.** qwen3:8b sometimes sends an empty JSON *object* where the
  schema wants an array, and `sandbox_execute` rejects it with
  `invalid_request`, burning a call before any container starts:

  ```text
  [tool 1/4] sandbox_execute
  [result] {"ok": false, "status": "invalid_request",
            "stderr": "'input_files' must be an array.", ...}
  ```

  It happened in two of three runs, on turn 1 — unrelated to cross-turn access,
  which is why it is not fixed here (a PATCH covers exactly one correction).
  Accepting `{}` as "no input files" is unambiguous and would recover a call;
  that belongs in its own PATCH-016-02 against SPEC-016, with its own live
  verification.
- Whether an equally short instruction placed in `## Use when` (where routing
  context is read) would prevent the first probe more reliably than a procedure
  step. Worth one experiment before assuming the ceiling is the model.
