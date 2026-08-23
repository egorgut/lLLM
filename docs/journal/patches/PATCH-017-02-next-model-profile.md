# PATCH-017-02 — `next` model profile (`qwen3.8:27b`)

- **Patch:** [PATCH-017-02](../../../patches/SPEC-017/PATCH-017-02-Add-Next-Model-Profile.md)
- **Parent spec:** [SPEC-017](../../../specs/SPEC-017-Model-Profiles.md)
- **Date:** 2026-08-23
- **Branch:** patch/PATCH-017-02-next-model-profile
- **Implementation commit:** `be319a4`
- **Merge commit:** `c1b14c9`

## Hypothesis / intent

`qwen3.8:27b` was pulled onto the host but unselectable — `MODEL_PROFILES` had
no entry, and `--profile` derives its choices from that dict.

Adding it was blocked on purpose. PATCH-018-01's problem statement required the
SPEC-018 verification debt to be closed "before using a new model generation as
a new project baseline". With that debt closed and PATCH-012-01 having taken
routing from tens of seconds to about one, a new profile could finally be
measured against a healthy baseline instead of a broken one.

Two claims were being tested. The structural one, which PATCH-017-01 already
made: a new model costs one dictionary entry and nothing else. And a new one:
that deadlines for a *different model generation* should be measured rather than
interpolated, because the size scale the other profiles share does not extend to
it.

## What changed

One entry in `config.MODEL_PROFILES`:

```python
"next": ModelProfile("next", "qwen3.8:27b", 250, 500, 50),
```

Plus one test in `tests/test_model_profiles.py` pinning exactly what this patch
claims — the model binds, and the host default did not move — and README
updates wherever profiles are enumerated.

No production code besides the dict entry. `--profile` (both `app.py` and
`evals/runner.py`), `validate_model_profiles()`, and `resolve_model_profile`'s
error message all picked up the fourth entry with no edit. The structural claim
holds a second time.

### The name

`next`, deliberately not a rung in the size scale. `fast`/`mid`/`deep` order one
generation by parameter count; `qwen3.8:27b` is a different one — family
`qwen35`, ctx 262144 against qwen3's 40960. Slotting it between `mid` (14.8B)
and `deep` (32.8B) on its 27.3B would have implied a capability ordering nobody
has verified, and the measurements below show why that would have misled: it is
*faster* than `deep` on both routing and decisions.

## Model & parameters (provenance)

- qwen3.8:27b (`next`) — digest `22130167c4c2`, family `qwen35`, 27.3B params,
  Q4_K_M, **ctx 262144**
- Ollama 0.32.15, SDK `ollama` 0.6.2
- Sampling: model defaults — `llm.py` still sets no `options`. Reported by
  `/api/show`: **temperature 1**, top_k 20, top_p 0.95, min_p 0,
  presence_penalty 0, repeat_penalty 1.

Worth flagging rather than burying: these defaults **differ from qwen3's**
(temperature 0.6), and the context window is 6.4× larger. Because the project
pins no sampling, `next` differs from its neighbours by more than model name and
deadlines. Whether sampling should be pinned per profile is a real question this
patch does not open — SPEC-017 §9 puts it out of scope, and opening it would
make this a SPEC.

Unchanged, for comparison: qwen3:8b `500a1f067a9f` 8.2B ctx 40960; qwen3:14b
`bdbd181c33f2` 14.8B ctx 40960; qwen3:32b `030ee887880f` 32.8B ctx 40960.

## Verification

Deliberately minimal at the owner's request — one measurement pass and one
confirmation pass, not the full live programme.

```text
$ python -m pytest -q
709 passed, 29 skipped in 1.23s          # 708 before, +1 for the new profile test

$ python -m evals.runner --suite scripted
40/40 passed (0 failed)                  # profile-independent, unmoved

$ python app.py --profile huge
usage: app.py [-h] [--profile {deep,fast,mid,next}]
app.py: error: argument --profile: invalid choice: 'huge'
        (choose from 'deep', 'fast', 'mid', 'next')

$ python -c "…describe_profile for each committed profile…"
fast: qwen3:8b (request 120s, turn 180s, routing 30s)
mid: qwen3:14b (request 180s, turn 300s, routing 40s)
deep: qwen3:32b (request 300s, turn 600s, routing 60s)
next: qwen3.8:27b (request 250s, turn 500s, routing 50s)
default -> fast
```

### Measurement pass

Run with `deep`'s values (300/600/60) as provisional headroom, through the
committed live path added by PATCH-018-01, which records per-request latency:

| run | total | model requests | routing requests |
| --- | --- | --- | --- |
| `skill-live-sales-001` (cold load) | 54.3 s | 31.7 s, 12.6 s | 1 |
| `skill-live-sales-001` (warm) | 45.2 s | 26.1 s, 16.6 s | 1 |
| `skill-live-tracker-001` (warm) | 103.7 s | 32.9 s, 65.4 s | 1 |

Routing measured separately, warm, three runs: **4.3 s / 3.6 s / 4.9 s**, correct
selection every time.

Against the profiles SPEC-017 measured:

| decision | `qwen3:8b` | `qwen3:32b` | `qwen3.8:27b` |
| --- | --- | --- | --- |
| skill-routing response | ~3.6 s | ~11.3 s | **3.6–4.9 s** |
| agent decision emitting a tool call | ~11 s | ~40–47 s | **26–33 s** |

It routes about as fast as the 8B model and decides roughly a third quicker than
the 32B one, at 27.3B. That inversion is the whole argument for not naming it by
size.

### Committed deadlines

`routing + 4 × decision + final answer` ≈ 4.3 + 4×33 + 65 ≈ 201 s; at the ~2.5×
margin `deep` carries, turn 500 s. Request 250 s, routing 50 s.

Routing is **not** a tight multiple of its measurement, on purpose. The
committed 30/40/60 were calibrated before PATCH-012-01, when routing cost
3.6–11.3 s; it now costs ~1–6 s on every profile. Scaling strictly off 4.9 s
would have given `next` a far tighter routing deadline than its neighbours for
no reason. 50 s keeps it on the committed scale with ~10× headroom.

### Confirmation pass, under the committed 250/500/50

| case | outcome | total | model requests | routing | selected |
| --- | --- | --- | --- | --- | --- |
| `skill-live-sales-001` | completed/final_answer | 56.9 s | 32.1 s, 19.4 s | 1 | `sales_analysis` |
| `skill-live-tracker-001` | completed/final_answer | 132.5 s | 42.5 s, 84.3 s | 1 | `tracker_read` |
| `skill-live-none-001` | completed/final_answer | 32.8 s | 28.6 s | 1 | `None` |

Worst single request observed: **84.3 s** against a 250 s request deadline
(≈3.0× headroom). Worst turn: **132.5 s** against 500 s (≈3.8×). Routing took
one request every time and selected correctly, `None` included.

Note the 84.3 s figure exceeds the 65 s used in the arithmetic above — the
margin absorbs it comfortably, but the honest reading is that the committed
numbers are derived from a handful of runs, not a distribution.

## Outcome

Every acceptance criterion is met. `next` binds `qwen3.8:27b`, passes the
startup coherence check, appears in `--profile`'s choices and in the unknown-name
error, and completes real turns under its committed deadlines. `python app.py`
with no flag is untouched — `fast` / `qwen3:8b` / 120/180/30 — so every prior
journal stays reproducible.

Unlike PATCH-017-01, a behavioural claim *is* made here, and only this one: the
committed deadlines are adequate for `qwen3.8:27b` on this host, measured. No
claim is made about task quality. Nothing in this entry says `next` answers
better than `deep` — the three cases exercised are smoke tests for latency, not
a comparison, and a comparison would need the committed cases run head-to-head.

The measurement also corrected an assumption worth writing down: parameter count
did not predict latency across generations. A 27.3B model of the newer family
routes like the 8B one and decides faster than the 32.8B one. Had these
deadlines been interpolated the way `mid`'s were, they would have been wrong in
the safe direction — too generous — but for the wrong reason.

## Follow-ups

- **Sampling is unpinned and now visibly divergent.** `next` runs at the model's
  own temperature 1 while the qwen3 profiles run at 0.6, and `llm.py` sets no
  `options`. If profiles are ever compared for quality, this confounds it.
  Pinning sampling is a SPEC, not a patch.
- **The 262144 context is unused.** `MAX_CONTEXT_MESSAGES` still caps history at
  20 messages for every profile.
- No head-to-head quality comparison across profiles exists yet — still the open
  item SPEC-017's own journal records.
