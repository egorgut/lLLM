# PATCH-017-02 — Add a `next` model profile for `qwen3.8:27b`

## Parent spec

`specs/SPEC-017-Model-Profiles.md`

Parent journal:

`docs/journal/SPEC-017-model-profiles.md`

## Problem

`qwen3.8:27b` is pulled on the target machine but cannot be selected:
`MODEL_PROFILES` has no entry for it, and `--profile` derives its choices from
that dict. Selecting it today would mean editing `config.py` per run — exactly
the situation SPEC-017 removed.

Adding it was deliberately blocked until now. PATCH-018-01's problem statement
required the SPEC-018 verification debt to be closed "before using a new model
generation as a new project baseline". That debt is closed, and PATCH-012-01
additionally took skill routing from tens of seconds to about one, so a new
profile is now measured against a healthy baseline rather than a broken one.

## Expected change

One additional entry in `config.MODEL_PROFILES`:

```python
"next": ModelProfile("next", "qwen3.8:27b", 250, 500, 50),
```

Nothing else in the code changes. `--profile` in both `app.py` and
`evals/runner.py` uses `choices=sorted(MODEL_PROFILES)`,
`validate_model_profiles()` iterates the whole dict, and
`resolve_model_profile`'s error message lists every valid name — a fourth entry
is picked up by all of them without edits. This is the structural claim
PATCH-017-01 confirmed; this patch re-exercises it.

### The name

`next`, not a rung in the size scale. `fast`/`mid`/`deep` order one generation
(qwen3) by parameter count. `qwen3.8:27b` is a different generation — family
`qwen35`, ctx 262144 against qwen3's 40960 — so slotting it between `mid`
(14.8B) and `deep` (32.8B) by its 27.3B would imply a capability ordering
nobody has verified. `next` says what it is: a candidate baseline.

### Where the deadlines come from

Unlike PATCH-017-01's, they are **measured on this host**, warm, through the
committed live eval path, not interpolated:

| decision | `qwen3:8b` | `qwen3:32b` | `qwen3.8:27b` (measured) |
| --- | --- | --- | --- |
| skill-routing response | ~3.6 s | ~11.3 s | **3.6–4.9 s** |
| agent decision emitting a tool call | ~11 s | ~40–47 s | **26–33 s** |

It routes and decides *faster* than `qwen3:32b` despite sitting between `mid`
and `deep` by parameter count — which is itself the reason not to order it by
size.

Applying the same margin the committed profiles carry over a four-call turn
(`routing + 4 × decision + final answer`; observed worst single request 65 s,
so 4.3 + 4×33 + 65 ≈ 201 s at ~2.5×) gives turn 500 s, with request 250 s and
routing 50 s.

Routing is deliberately **not** set to a tight multiple of its measurement.
The committed 30/40/60 were calibrated before PATCH-012-01, when routing cost
3.6–11.3 s; it now costs ~1–6 s everywhere, so scaling strictly off 4.9 s would
give `next` a routing deadline far tighter than its neighbours for no reason.
50 s keeps it on the committed scale with ~10× headroom over measurement.

## Constraints

- Preserve the parent SPEC's architecture and intent: profiles stay host-owned
  (SPEC-011 §10), the model never sees or influences a profile.
- `fast` stays the default and keeps its exact values, so every journal from
  SPEC-005 onwards remains reproducible.
- No existing profile's model or deadlines change.
- No new `ModelProfile` field. Sampling parameters, `num_ctx`, and `keep_alive`
  remain out of scope per SPEC-017 §9; adding one would make this a SPEC, not a
  PATCH. Note this matters more here than for `mid`: `qwen3.8:27b` ships
  different sampling defaults (temperature 1 against qwen3's 0.6) and a 262144
  context, and `llm.py` sets no `options`, so the model's own defaults apply.
  Whether the project should pin sampling across profiles is a separate
  question this patch does not open.
- No prompt, tool-declaration, agent-loop, or trace-schema change.
- Framework-free, standard library only, no new dependency.

## Acceptance criteria

- [ ] `MODEL_PROFILES["next"]` exists and binds `qwen3.8:27b`.
- [ ] It passes `reliability.validate_reliability_config` through the existing
      `validate_model_profiles()` startup check.
- [ ] `python app.py` with no flag is unchanged: `fast` / `qwen3:8b` /
      120/180/30.
- [ ] An unknown `--profile` value is rejected at startup and lists
      `deep, fast, mid, next`.
- [ ] A live turn completes under the committed deadlines, not the provisional
      ones used while measuring.
- [ ] The full test suite and the scripted eval suite pass.
- [ ] Model provenance for `qwen3.8:27b` is recorded.

## Files likely affected

- `config.py`
- `tests/test_model_profiles.py`
- `README.md`
- `docs/journal/patches/PATCH-017-02-next-model-profile.md`
- `docs/journal/SPEC-017-model-profiles.md` (index entry only)

## Verification

```bash
python -m pytest -q
python -m evals.runner --suite scripted
python -m evals.runner --suite live --profile next --category skill_live_sales
python -m evals.runner --suite live --profile next --category skill_live_tracker
python -m evals.runner --suite live --profile next --category skill_live_none
python app.py --profile huge     # must list deep, fast, mid, next
```

Live verification is required: this patch introduces a model, which is
model-facing by definition. Deliberately minimal at the owner's request — one
measurement pass plus a confirmation pass under the committed deadlines, not
the full live programme.

## Journal strategy

Standalone, per `patches/README.md` → Journal rules: a live measurement is
model-facing evidence.

Create `docs/journal/patches/PATCH-017-02-next-model-profile.md` and index it
under `## Patches` in `docs/journal/SPEC-017-model-profiles.md`.

## Out of scope

- Making `next` the default profile.
- Changing any existing profile.
- Pinning sampling parameters or `num_ctx` per profile.
- Comparing `qwen3.8:27b`'s task quality against `deep` — this patch makes the
  model *selectable* and claims nothing about which model is better.
- Re-running the full live skill programme across all profiles.
- The `activate_skill` prompt-contract question (PATCH-018-01 follow-up).

## Suggested branch and commit conventions

```text
branch:  patch/PATCH-017-02-next-model-profile
commit:  Add a next model profile for qwen3.8:27b (PATCH-017-02)
merge:   Merge PATCH-017-02: next model profile (qwen3.8:27b)
```
