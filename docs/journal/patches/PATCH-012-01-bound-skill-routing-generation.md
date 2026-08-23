# PATCH-012-01 — Bound skill routing generation

- **Patch:** [PATCH-012-01](../../../patches/SPEC-012/PATCH-012-01-Bound-Skill-Routing-Generation.md)
- **Parent spec:** [SPEC-012](../../../specs/SPEC-012-Skills.md)
- **Date:** 2026-08-23
- **Branch:** patch/PATCH-012-01-bound-skill-routing-generation
- **Implementation commit:** `af7e4c9`
- **Merge commit:** `83aca80`

## Hypothesis / intent

PATCH-018-01's live programme found that a multi-intent request kills the turn
during *routing*: `skill-live-cross-001` failed 5/5 runs on every profile, each
terminating at exactly its own routing deadline (30s / 40s / 60s). The router was
not deciding badly — it was not finishing. Measured on `qwen3:8b`, it produced
the correct answer (`{"skill": "tracker_read", …}`, 172 characters) after **59.6
s**, behind **14,819 characters** of hidden `message.thinking`; a diagnostic run
with the deadline raised to 600s reached 14,760 tokens over 9m58s without
returning.

The hypothesis: routing is a *narrow classification* by SPEC-012's own design
(§"Core architectural decisions" 5 — "which one skill, if any, best matches this
request?"), so the deliberation is not buying anything, and removing it should
make routing finish comfortably inside deadlines that are otherwise sound. The
deadlines were suspected to be *correct*, not miscalibrated: single-intent
prompts routed fine, and `skill-live-tracker-001` — literally the first half of
the failing prompt — passed on all three profiles.

## What changed

Two changes, both confined to the routing request. The second exists only
because the first exposed it.

- **`llm.py::OllamaModel.text()` disables thinking** (`think=False`). `text()`
  was already documented as "the shape skill routing needs", so it is the seam
  that owns this. The agent loop is untouched: `respond()` keeps `think=None` —
  the SDK's own default, meaning "the model decides".
- **`llm.py::OllamaModel.text()` constrains generation to
  `ROUTING_RESPONSE_SCHEMA`.** With thinking off, `qwen3:32b` began wrapping its
  JSON in a ` ```json ` fence, which `SkillRouter._parse` rejects. `_parse`
  remains authoritative — the schema cannot express "an exact name from *this*
  turn's catalog" — so this narrows what the model may emit without moving the
  contract.
- `respond()` and `ModelResponse` gained keyword-only `think` / `response_format`
  passthroughs, both defaulting to the SDK's own defaults so the `Respond`
  callable `AgentRunner` holds is unchanged in signature and behaviour.
- **`tests/test_llm.py`** is new (14 tests): the transport's request shape, with
  both sides of the asymmetry asserted — routing asks for no thinking and a
  constrained shape, the agent loop asks for neither.

No change to `SkillRouter`, the routing prompt, the JSON contract, the repair
policy, `MAX_SKILL_ROUTING_RESPONSE_CHARS`, any profile deadline, any model, or
any sampling parameter.

### Why not a token cap

`num_predict` was measured and **rejected**, and a test now pins that decision.
On the same prompt and model, `options={"num_predict": 256}` returned in 4.8s
having generated 1,168 characters of thinking and **zero characters of answer**:
the cap truncates the reasoning, not the response, so the router would get an
empty string to parse and would spend its single repair attempt on it. A cap
turns a timeout into a different failure rather than into a decision. The
profile's routing deadline remains the backstop.

### The second defect, and a correction

Diagnosing the fence, I first measured it on the *cross* prompt, saw it once in
six runs, and judged it a one-off not worth addressing. That was wrong, and the
live suite caught it: on `deep`, `skill-live-sales-001` — which passed before
this patch — failed with `invalid_skill_selection` after `routing_requests: 2`,
both the initial response and the repair rejected. Re-measured on the *sales*
prompt, `qwen3:32b` fenced **5 of 6** runs. At that rate two consecutive fences
are likely, and two fences exhaust the one repair attempt and fail the turn.

The selection itself was correct in every one of those runs. Only its packaging
was not. Constraining the response fixed 27 of 27 probe runs across all three
profiles, `null` selections included.

## Model & parameters (provenance)

- qwen3:8b (`fast`) — digest `500a1f067a9f`, 8.2B params, Q4_K_M, ctx 40960
- qwen3:14b (`mid`) — digest `bdbd181c33f2`, 14.8B params, Q4_K_M, ctx 40960
- qwen3:32b (`deep`) — digest `030ee887880f`, 32.8B params, Q4_K_M, ctx 40960
- Ollama 0.32.15, SDK `ollama` 0.6.2
- Sampling: model defaults, unchanged — `llm.py` still sets no `options`.
  Reported by `/api/show`: temperature 0.6, top_k 20, top_p 0.95,
  repeat_penalty 1. `think` and `format` are request-shape, not sampling.

## Verification

```bash
python -m pytest -q                        # 708 passed, 29 skipped
python -m evals.runner --suite scripted    # 40/40 passed
python -m evals.runner --suite live --profile {fast,mid,deep} --category skill_live
```

The 29 skips are the opt-in live Docker sandbox tests, unrelated to this patch.

### The routing call itself

One prompt, one model (`qwen3:8b`), measured directly:

| configuration | duration | thinking | answer |
|---|---|---|---|
| before (as shipped) | 59.6s | 14,819ch | correct |
| `think=False` | **0.8s** | 0ch | correct |
| `options={"num_predict": 256}` | 4.8s | 1,168ch | **empty** |

With `think=False` and the schema, three runs per prompt per profile — 27 of 27
parsed, every selection correct, including the `null` "no skill needed" case:

| profile | sales prompt | no-skill prompt | cross prompt |
|---|---|---|---|
| fast | 0.9–4.1s | 0.8–0.9s | 0.9–1.0s |
| mid | 1.4–3.4s | 0.8–1.3s | 1.5–2.0s |
| deep | 3.2–4.8s | 2.4–2.6s | 3.8–6.1s |

Every figure sits an order of magnitude inside its profile's deadline.

### Live suite, before and after

| case | fast before → after | mid before → after | deep before → after |
|---|---|---|---|
| `skill-live-none-001` | 16.8s → 11.1s | 43.0s → 20.0s | 72.6s → 72.7s |
| `skill-live-sales-001` | 20.8s → 19.0s | 45.8s → 32.6s | 158.5s → 115.1s |
| `skill-live-tracker-001` | 90.0s → 23.1s | 77.4s → 50.7s | 144.8s → 113.5s |
| `skill-live-cross-001` | **timeout 30.0s → completed 29.2s** | **timeout 40.0s → completed 82.0s** | **timeout 60.0s → completed 159.3s** |

The defect is gone: `skill-live-cross-001` no longer times out on any profile.
Every routing decision in the final run took **one** request (`routing_requests:
1`) and selected the correct skill — `None` for the no-skill case,
`sales_analysis` for sales, `tracker_read` for tracker and cross. No repair
attempt was needed anywhere.

**Turn-level durations are not a clean before/after.** The "before" column comes
from PATCH-018-01's isolated invocations, which alternated 8b/14b/32b and paid
cold-load and cold-cache costs; the "after" column ran six cases back-to-back on
one warm model. Some of that improvement is warm state, not this patch. The
clean measurement is the routing-call table above, same prompt, same conditions.
The agent loop was separately confirmed to still think — 2,509 characters of
`thinking` on a tool-calling request — so its share of these turns did not
change by design.

### What still fails, and why it is not this patch

`skill-live-cross-001` now **completes** and fails its *expectation* instead: the
model answers the Tracker half and never calls `activate_skill`. That is
PATCH-018-01's recorded finding about model behaviour, explicitly out of scope
here, and the acceptance criterion was written for exactly this outcome — the
failure reason is no longer `skill_routing_timeout`. `skill-live-cross-explicit-001`
fails for the same reason, as expected.

One case behaved non-deterministically: `skill-live-activation-forced-001` failed
once on `fast` in the final suite run, having passed before. Routing was not
involved — that case selects explicitly, `routing_requests: 0`, so `text()` is
never called and this patch cannot reach it. Re-run three times on `fast`
afterwards: 3/3 passed. Recorded as observed flakiness of `qwen3:8b` at
temperature 0.6 on that prompt, not a regression.

## Outcome

Every acceptance criterion is met. Routing generation is bounded by two
host-owned request properties the model can neither see nor change;
`skill-live-cross-001` no longer fails on `skill_routing_timeout` on any profile;
multi-intent routing latency is measured on all three and sits far inside the
existing deadlines; single-intent routing is unchanged in outcome and selection;
the agent loop's request shape is provably untouched; and no deadline, profile,
model, sampling parameter or third-party dependency changed.

Two things worth carrying forward.

The deadlines were never the problem, and the temptation to "fix" this by
widening them would have encoded a ten-minute routing call as the contract. The
evidence pointed the other way: the same decision now takes 0.8s on the profile
whose deadline was allegedly too tight.

And the fence taught the more useful lesson. I closed that question early on six
runs of the wrong prompt; the live suite reopened it with a real regression on
`deep`. A behaviour measured at 1-in-6 on one prompt was 5-in-6 on another, and
nothing but running the committed cases on real models would have shown it.

## Follow-ups

- The models still do not call `activate_skill` on their own (PATCH-018-01). That
  is a prompt-contract question, untouched here, and SPEC-012 §9's parked
  question about retiring `SkillRouter` stays closed.
- `reliability.run_with_deadline` abandons rather than cancels, so a timed-out
  routing call keeps generating server-side and slows the next turn. Much less
  reachable now that routing finishes in about a second, but the semantics are
  unchanged and belong to SPEC-011.
- Whether the *agent loop* should bound its thinking is a separate question with
  a different cost/benefit, deliberately not answered here.
- `skill-live-activation-forced-001` is flaky on `fast`. If it proves noisy
  enough to obscure real regressions, it needs a repeat-count policy rather than
  a weakened expectation.
