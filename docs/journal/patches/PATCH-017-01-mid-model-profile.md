# PATCH-017-01 — `mid` model profile (`qwen3:14b`)

- **Patch:** [PATCH-017-01](../../../patches/SPEC-017/PATCH-017-01-Add-Mid-Model-Profile.md)
- **Parent spec:** [SPEC-017](../../../specs/SPEC-017-Model-Profiles.md)
- **Date:** 2026-08-12
- **Branch:** patch/PATCH-017-01-mid-model-profile
- **Merge commit:** <short-sha>

## Hypothesis / intent

SPEC-017 shipped two profiles at opposite ends of the machine's capability:
`qwen3:8b` decides in ~11 s per tool call, `qwen3:32b` in ~40–47 s. A third
model, `qwen3:14b`, was pulled onto the host to sit between them. The
hypothesis of this patch is narrow and structural, not about model quality: if
SPEC-017 got the seam right, adding a model should cost exactly one dictionary
entry and no change anywhere else — no transport change, no CLI change, no test
change.

That held. `config.MODEL_PROFILES` gained one line; `--profile` in both
`app.py` and `evals/runner.py`, the startup validation loop, and the
unknown-name error message all picked `mid` up on their own, because each
derives from the dict rather than restating it.

## What changed

- `config.py` — one entry between `fast` and `deep`:
  `"mid": ModelProfile("mid", "qwen3:14b", 180, 300, 40)`, with a comment
  recording that its deadlines are interpolated rather than measured.
- `README.md` — five places that enumerate profiles: requirements, the
  `ollama pull` block, the run examples, the profile table in
  «Профили моделей (SPEC-017)», and the `MODEL_PROFILES` block under
  «Конфигурация». The table now carries an explicit caveat that `mid`'s numbers
  are estimates.
- No change to `llm.py`, `app.py`, `agent.py`, `evals/runner.py`, prompts, tool
  declarations, the trace schema, or any test.

### Where the deadlines came from

Interpolated from SPEC-017 §2.1's measured table by parameter count — 14.8B
sits 0.27 of the way from 8.2B to 32.8B — keeping the headroom ratio the two
committed profiles already use:

| decision | `qwen3:8b` (measured) | `qwen3:14b` (estimated) | `qwen3:32b` (measured) |
| --- | --- | --- | --- |
| skill-routing response | ~3.6 s | ~5.7 s | ~11.3 s |
| agent decision emitting a tool call | ~11 s | ~20 s | ~40–47 s |

A four-call turn under those estimates is `5.7 + 4 × 20 + ~20 ≈ 104 s`; the
committed 300 s turn budget carries ~2.9× headroom, against 2.6× for `fast` and
2.5× for `deep`. Every `mid` deadline is strictly between its neighbours'.

## Model & parameters (provenance)

The model this patch makes selectable:

- qwen3:14b — digest `bdbd181c33f2`, 14.8B params (14 768 307 200), Q4_K_M,
  ctx 40960, capabilities `completion, tools, thinking`
- Ollama 0.31.1
- Sampling: defaults — `llm.py` still sets no `options`, so `mid` differs from
  its neighbours only by model and deadlines.

For comparison, unchanged from the SPEC-017 entry: qwen3:8b — digest
`500a1f067a9f`, 8.2B, Q4_K_M, ctx 40960; qwen3:32b — digest `030ee887880f`,
32.8B, Q4_K_M, ctx 40960.

## Verification

**Deviation, recorded deliberately:** adding a model normally requires
live-model verification (`patches/README.md` → Verification rules), and
SPEC-017's own method was to *measure* routing and one tool-emitting decision
before committing deadlines. The project owner chose to skip both the
measurement pass and the scripted live session for this patch and to exercise
`mid` personally. This entry therefore contains **no transcript and no observed
latency for `qwen3:14b`**, and the committed deadlines are estimates. That is
the honest state of this step, not an omission to be inferred later.

What was run:

```text
$ python -m pytest -q
592 passed, 29 skipped in 1.09s          # identical to the pre-patch baseline

$ python -m evals.runner --suite scripted
36/36 passed (0 failed)                  # profile-independent, unmoved

$ python app.py --profile huge
usage: app.py [-h] [--profile {deep,fast,mid}]
app.py: error: argument --profile: invalid choice: 'huge'
        (choose from 'deep', 'fast', 'mid')

$ python -c "…describe_profile for each committed profile…"
fast: qwen3:8b (request 120s, turn 180s, routing 30s)
mid: qwen3:14b (request 180s, turn 300s, routing 40s)
deep: qwen3:32b (request 300s, turn 600s, routing 60s)
default -> fast
```

`validate_model_profiles()` — the same startup check `app.py` runs — accepts
all three profiles, so `mid` satisfies `reliability.validate_reliability_config`
(`turn 300 >= min(request 180, TOOL_EXECUTION_TIMEOUT_SECONDS 30)`).

Two existing tests covered the new entry without modification:
`test_every_committed_profile_is_internally_coherent` validates every committed
profile, and `test_unknown_profile_names_the_valid_ones` iterates
`MODEL_PROFILES` and requires `mid` to appear in the failure message. No new
test was added, on the owner's instruction.

## Outcome

The structural claim is confirmed: a new model is one line, and SPEC-017's seam
held with no edit to the transport, the CLI, the eval runner, or the tests. The
default path is untouched — `python app.py` with no flag still runs `fast` /
`qwen3:8b` / 120/180/30, so every prior journal stays reproducible.

The behavioural claim is **not** made. Nothing in this entry supports any
statement about `qwen3:14b`'s latency, task performance, or timeout margin on
this host; the deadlines are arithmetic on someone else's measurements.

## Follow-ups

- Measure `mid` the way SPEC-017 measured its two profiles (routing response,
  one tool-emitting decision, warm) and correct 180/300/40 if the estimate is
  off. A `turn_timed_out` on ordinary `mid` latency is the signal, and the fix
  is PATCH-017-02 with the observed numbers — not a silent edit here.
- The live evals comparison SPEC-017 left open now has three profiles to run:
  `python -m evals.runner --suite live --profile fast|mid|deep`.
- Two stale doc references noticed while working here and deliberately left
  alone: `evals/README.md` still names the removed `config.MODEL_NAME`, and
  `README.md`'s skills configuration block still shows
  `SKILL_ROUTING_TIMEOUT_SECONDS` as a flat constant after it moved into
  `ModelProfile`.
