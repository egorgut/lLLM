# PATCH-019-01 — Evaluate `qwen3.8:27b-mlx` as the agent runtime

## Parent spec

`specs/SPEC-019-Component-Specific-Model-Profiles.md`

Parent journal:

`docs/journal/SPEC-019-component-specific-model-profiles.md`

## Problem

SPEC-019 lets the project hold the router constant while changing only the
agent profile. The current candidate agent baseline is:

```text
next -> qwen3.8:27b
```

Ollama also exposes an MLX-backed package for the same model family on Apple
Silicon:

```text
qwen3.8:27b-mlx
```

The project has no local evidence that this package is a better runtime for
`lLLM`. It may improve prompt processing, generation speed, memory behavior, or
cold/warm latency, but that must be measured without changing the router,
prompts, tools, agent loop, or normal sampling behavior at the same time.

Do not assume the two Ollama packages are byte-for-byte equivalent. If
`/api/show` reveals different quantization, template, or packaged parameters,
record that explicitly: the result is then a comparison of two deployable
Ollama packages, not proof that every delta comes from MLX alone.

## Expected change

Add one experimental profile:

```python
"next-mlx": ModelProfile("next-mlx", "qwen3.8:27b-mlx", 250, 500, 50),
```

Keep the current profile unchanged:

```python
"next": ModelProfile("next", "qwen3.8:27b", 250, 500, 50),
```

Use the same conservative deadlines so timeout policy does not become another
variable.

The controlled comparison is:

```text
baseline:   router=fast / qwen3:8b   agent=next     / qwen3.8:27b
candidate:  router=fast / qwen3:8b   agent=next-mlx / qwen3.8:27b-mlx
```

Collect evidence for:

- end-to-end turn latency;
- per-agent-model request latency;
- warm output throughput (tokens/s);
- cold-load cost;
- live eval success / answer quality;
- tool-calling compatibility;
- model/package provenance.

This PATCH evaluates the candidate only. It does not make it the default.

## Constraints

- Preserve SPEC-019 role selection: `--profile` is the agent and
  `--router-profile` is the router override.
- Keep Ollama as the inference service. Direct `mlx-lm` integration is a
  separate architectural change and would require a SPEC.
- `fast`, `mid`, `deep`, `next`, and `DEFAULT_MODEL_PROFILE` stay unchanged.
- No new `ModelProfile` fields and no changes to `ModelRoles`, `OllamaModel`,
  `SkillRouter`, `AgentRunner`, or `SkillTurnOrchestrator` contracts.
- No prompt, thinking, tool, skill-routing, retry, or `activate_skill` change.
- No normal-runtime sampling, `num_ctx`, `keep_alive`, preload, or unload change.
- No MoE model in this PATCH.
- No new benchmark framework or third-party Python dependency.
- Record the installed Ollama version and `/api/show` provenance for both tags.
- If MLX support requires transport/API or lifecycle changes in the harness,
  stop and escalate to a new SPEC.

## Acceptance criteria

- [ ] `qwen3.8:27b-mlx` can be pulled and served on the target Apple Silicon
      host.
- [ ] Ollama provenance is recorded for both tags: digest, architecture/family,
      parameter count, context, quantization/format, template, and packaged
      parameters where available.
- [ ] Any package-level difference is called out before interpreting the
      performance result.
- [ ] `MODEL_PROFILES["next-mlx"]` binds `qwen3.8:27b-mlx` with the same
      250/500/50 deadlines as `next`.
- [ ] `python app.py` with no flags is unchanged.
- [ ] `python app.py --profile next-mlx --router-profile fast` starts with the
      correct role diagnostics.
- [ ] Full pytest and scripted eval suites pass.
- [ ] `next` and `next-mlx` are each run at least three times against the same
      model-routed live cases with `--router-profile fast`.
- [ ] Live cases preserve expected completion and tool behavior; regressions are
      recorded, not hidden by prompt or sampling changes.
- [ ] Median end-to-end `duration_ms` and `model_request_ms` are recorded for
      both profiles.
- [ ] A fixed direct-Ollama warm benchmark is run at least five times per tag;
      server `eval_count` / `eval_duration` are used for tokens/s and the median
      is recorded.
- [ ] One controlled cold request per tag records `load_duration`, with
      residency checked through `ollama ps`.
- [ ] Journal conclusion is explicit: `adopt candidate`, `keep experimental`,
      or `do not adopt`.
- [ ] No default profile changes in this PATCH.

If the MLX package fails basic compatibility, do not leave a broken
`next-mlx` profile in `main`; preserve the negative result in the journal.

## Files likely affected

- `config.py`
- `tests/test_model_profiles.py`
- `README.md` (only if selectable profiles are documented there)
- `patches/SPEC-019/PATCH-019-01-Evaluate-Qwen3.8-27B-MLX-Runtime.md`
- `scripts/bench_ollama_tag.py` (benchmark-only, never imported by the runtime)
- `docs/journal/patches/PATCH-019-01-evaluate-qwen38-27b-mlx-runtime.md`
- `docs/journal/SPEC-019-component-specific-model-profiles.md` (index only)

Reuse the existing eval path. Change `evals/runner.py` only if a required
measurement cannot be obtained from the fields it already records.

## Verification

### Availability and provenance

```bash
ollama --version
ollama pull qwen3.8:27b-mlx
ollama show qwen3.8:27b
ollama show qwen3.8:27b-mlx
ollama ps
```

### Regression

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

### Live A/B

Keep the router fixed on `fast` and run the same cases for both profiles:

```bash
python -m evals.runner --suite live --profile next --router-profile fast \
  --category skill_live_sales
python -m evals.runner --suite live --profile next --router-profile fast \
  --category skill_live_tracker
python -m evals.runner --suite live --profile next --router-profile fast \
  --category skill_live_none

python -m evals.runner --suite live --profile next-mlx --router-profile fast \
  --category skill_live_sales
python -m evals.runner --suite live --profile next-mlx --router-profile fast \
  --category skill_live_tracker
python -m evals.runner --suite live --profile next-mlx --router-profile fast \
  --category skill_live_none
```

Repeat the set at least three times per profile. Use the eval result files the
runner writes to `data/evals/` for pass/fail, `duration_ms`, `model_request_ms`,
skill selection, and tool sequence. Those files are local, not committed —
`.gitignore` excludes `data/evals/` and `data/traces/` — so the medians and case
outcomes must be transcribed into the journal, the way PATCH-017-02 and the
SPEC-019 journal record their evidence. If Tracker is unavailable, record that
limitation and substitute one existing tool-using live case rather than
inventing a favorable prompt.

### Raw throughput and cold load

Use Ollama directly for the engine measurement. For each tag, run the same fixed
long-answer prompt at least five warm times with benchmark-only settings:

```text
stream=false
think=false
temperature=0
num_predict=512
```

Record `eval_count`, `eval_duration`, `prompt_eval_count`,
`prompt_eval_duration`, `total_duration`, and `load_duration`.

Calculate:

```text
tokens_per_second = eval_count / (eval_duration / 1e9)
```

Use the median warm value. These explicit benchmark options must **not** be
added to normal `lLLM` runtime configuration.

For cold-load evidence, ensure the target model is not resident, confirm with
`ollama ps`, run one request, and record `load_duration`. Do not add an unload
policy or `keep_alive` behavior to the harness.

## Journal strategy

Standalone: this PATCH changes the model package used for live agent decisions
and produces model-facing quality/performance evidence.

Create:

`docs/journal/patches/PATCH-019-01-evaluate-qwen38-27b-mlx-runtime.md`

and index it under `## Patches` in:

`docs/journal/SPEC-019-component-specific-model-profiles.md`

Preserve positive and negative findings and the provenance needed to understand
what was actually compared.

## Out of scope

- Making `next-mlx` or `next` the default.
- Replacing Ollama with direct `mlx-lm`.
- A generic inference-provider abstraction.
- MoE model evaluation.
- Changing the router model.
- Sampling or context tuning in normal runs.
- `keep_alive`, preloading, explicit unload, or model-memory scheduling.
- Deadline tuning before the comparison exists.
- Changing eval prompts/scoring to favor either model.
- Claiming a pure MLX-engine speedup when package metadata differs.

## Suggested branch and commit conventions

```text
branch:  patch/PATCH-019-01-evaluate-qwen38-27b-mlx-runtime
commit:  Evaluate qwen3.8:27b-mlx as an agent profile (PATCH-019-01)
merge:   Merge PATCH-019-01: evaluate qwen3.8:27b MLX runtime
```
