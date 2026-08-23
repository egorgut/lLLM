# PATCH-012-01 — Bound Skill Routing Generation

## Parent spec

`specs/SPEC-012-Skills.md`

Parent journal:

`docs/journal/SPEC-012-skills.md`

Discovered by:

`patches/SPEC-018/PATCH-018-01-Complete-Mid-Turn-Skill-Activation-Verification.md`
(evidence in `docs/journal/SPEC-018-mid-turn-skill-activation.md`, §"Defect
found (not fixed here)")

## Problem

A multi-intent user request can drive the routing model past every profile's
routing deadline, so the turn dies before the agent loop ever starts.

Observed on the committed live case `skill-live-cross-001` — "Look up issue
`<key>` in Yandex Tracker and summarise what it is about. Then, using the sales
database, tell me which genre earns the most revenue." — which fails 5/5 runs,
on every profile, each terminating at exactly its own routing deadline:

| profile | deadline | outcome |
|---|---|---|
| fast | 30s | `timed_out` / `skill_routing_timeout` at 30.0s |
| mid | 40s | `timed_out` / `skill_routing_timeout` at 40.0s |
| deep | 60s | `timed_out` / `skill_routing_timeout` at 60.0s |

The user sees SPEC-012 §"User-visible behavior" case 8 — `Application error:
Skill routing timed out.` and a rolled-back turn — for a request the router is
perfectly capable of answering.

### Why it happens

The router's decision is not slow to *reach*; it is slow to *emit*. Measured
directly against `qwen3:8b` on the prompt above:

- The answer is correct and tiny: `{"skill": "tracker_read", "reason": "…"}`,
  172 characters.
- Producing it took **59.6s**, of which the visible answer is a rounding error:
  the same response carried **14,819 characters of `message.thinking`**.
- A separate run with the routing deadline raised to 600s purely as a diagnostic
  still exhausted it, reaching 14,760 tokens over 9m58s. The excursion length is
  highly variable and has no ceiling.

Nothing in the project bounds that generation:

- `llm.py::OllamaModel.respond` calls `client.chat(...)` with no `options`, no
  `think`, and no `format`. The model decides how long to think.
- `MAX_SKILL_ROUTING_RESPONSE_CHARS` (2,000) is checked in
  `SkillRouter._parse` **after** the full response has been received, so it
  bounds what is accepted, never what is generated.
- The profile deadlines (`ModelProfile.skill_routing_timeout_seconds`,
  SPEC-017) are the only ceiling, and they are caller-side.

The deadlines are not miscalibrated for the work SPEC-012 describes. `config.py`
records ~3.6s warm routing on `qwen3:8b`, and the single-intent live cases agree
— `skill-live-tracker-001`, whose prompt is literally the first half of the
failing one, routes and completes on all three profiles. The trigger is a
request with more than one intent, on which the model deliberates about a
decision SPEC-012 §"Core architectural decisions" 5 defines as narrow: *which
one skill, if any, best matches this request?*

### Second-order effect

`reliability.run_with_deadline` abandons rather than cancels, by design
(`config.py:74-77`). When routing times out the host regains control, but the
server keeps generating the abandoned response — so a timed-out routing call
continues to occupy the GPU and slows whatever runs next. This makes the failure
worse than a lost turn: it degrades the turns after it.

## Expected change

Bound the routing request so a routing decision cannot spend an unbounded amount
of wall-clock before the host sees it.

Measurements taken while diagnosing this defect (same prompt, same catalog,
`stream=False`, otherwise stock) point clearly at which lever is correct:

| lever | result |
|---|---|
| baseline (today) | 59.6s, 14,819ch thinking, correct answer |
| `think=False` | **0.8s**, 0ch thinking, correct answer |
| `options={"num_predict": 256}` | 4.8s, 1,168ch thinking, **0ch answer** |

`think=False` across both remaining profiles, two runs each: `qwen3:14b` 3.3s /
1.5s, `qwen3:32b` 5.9s / 3.4s — every run correct, every run far inside the
existing deadlines. The installed SDK (`ollama` 0.6.2) exposes `think` on
`Client.chat`.

Two conclusions the implementation should carry:

1. **Disabling thinking for the routing call is the fix.** It targets exactly
   the component SPEC-012 already defines as a narrow classifier, and leaves the
   agent loop — where deliberation may genuinely be earning its cost — untouched.
2. **A token cap alone is not a fix and must not be used as one.** `num_predict`
   truncates the thinking, not the answer: the call returned *nothing* to parse,
   converting a timeout into an empty response, which the router would then spend
   its one repair attempt on. A cap is acceptable only as a defense-in-depth
   floor *behind* the primary fix, never instead of it.

The change should be confined to the routing path. `OllamaModel.text` exists
precisely because routing needs a different request shape from the agent loop
("One buffered, tool-less response — the shape skill routing needs"), so it is
the natural seam; the agent loop's `respond` must keep its current behaviour.

### Secondary observation, decide explicitly

With `think=False`, `qwen3:32b` returned its JSON inside a ` ```json ` fence on
1 of 2 runs. `SkillRouter._parse` calls `json.loads` on the stripped text, so a
fenced response is a parse error that costs the one available repair attempt.
This is pre-existing and survivable, but it becomes more visible once thinking is
off. The implementation should either leave it alone deliberately or address it
through the SDK's `format` parameter (schema-constrained output), and say which
in the journal. Do **not** add ad-hoc fence-stripping string surgery to `_parse`.

## Constraints

- Preserve SPEC-012's architecture: the router stays a separate component making
  one narrow decision, and it still must not call tools, execute the task, or
  mutate history.
- Do not change the routing prompt's content or the JSON contract it asks for.
- Do not change `SkillRouter`'s public interface, its repair-attempt policy, or
  `MAX_SKILL_ROUTING_RESPONSE_CHARS`.
- Do not change explicit-selection bypass (SPEC-012 decision 6).
- Do not change the agent loop's model request shape — this patch is about the
  routing call only.
- Do not change model profiles, models, or sampling parameters.
- **Do not change `skill_routing_timeout_seconds`.** The evidence says the
  deadlines are sound and the generation is not; recalibrating deadlines to
  accommodate a 10-minute routing call would encode the defect as the contract.
  If implementation proves otherwise, stop and record that finding rather than
  quietly widening the deadlines.
- Do not change `run_with_deadline`'s abandon-not-cancel semantics — see Out of
  scope.
- Do not introduce new third-party dependencies.
- Keep it framework-free and confined to the smallest surface that fixes it.

## Acceptance criteria

- [ ] A routing request cannot generate unboundedly; the bound is host-owned and
      the model can neither see nor change it (SPEC-011 §10).
- [ ] `skill-live-cross-001` passes on `fast`, `mid`, and `deep` — or, if it
      still fails, fails for a reason that is *not* `skill_routing_timeout`, and
      that reason is recorded.
- [ ] Routing latency on a multi-intent prompt is measured on all three profiles
      and sits well inside each profile's existing deadline.
- [ ] Single-intent routing is unchanged in outcome: the existing live skill
      cases (`skill-live-none-001`, `skill-live-sales-001`,
      `skill-live-tracker-001`) still select the same skills and still pass.
- [ ] Routing accuracy did not regress — the selection is still correct on every
      committed live skill case, not merely faster.
- [ ] The agent loop's request shape is provably unchanged.
- [ ] Deterministic regression tests cover the new bound, including that it is
      applied to routing and not to the agent loop.
- [ ] `python -m pytest -q` passes in full.
- [ ] `python -m evals.runner --suite scripted` passes in full.
- [ ] The fenced-response question above is answered explicitly, either way.
- [ ] Model provenance recorded for every live profile exercised.
- [ ] No new third-party dependency.

## Files likely affected

Advisory, not restrictive:

- `llm.py` — the routing request shape (`OllamaModel.text`)
- `skill_runtime/router.py` — only if the bound belongs on this side of the seam
- `config.py` — if the bound is a host-owned constant
- `tests/test_skill_router.py`, `tests/test_model_profiles.py` (there is no
  `tests/test_llm.py` today; a new file for the transport's request shape may be
  the right home)
- `docs/journal/patches/PATCH-012-01-bound-skill-routing-generation.md`
- `docs/journal/SPEC-012-skills.md` — index entry only
- `README.md` only if user-visible behaviour or a documented command changes

## Verification

Deterministic:

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

Live — **required**, because this patch changes a model-facing request and the
model decision that selects a skill:

```bash
python -m evals.runner --suite live --profile fast --category skill_live
python -m evals.runner --suite live --profile mid  --category skill_live
python -m evals.runner --suite live --profile deep --category skill_live
```

Needs Ollama running with all three models, and `TRACKER_SMOKE_ISSUE_ID` set to
a real issue key in the git-ignored `.env` (the Tracker live cases render a
visible "not set" marker without it).

Record per profile: routing duration, `routing_requests`, selected skill, and
turn outcome — for both a single-intent and a multi-intent prompt, so the fix is
shown not to have traded accuracy for speed.

Note for whoever runs this: `skill-live-cross-explicit-001` is expected to fail
on model behaviour (no profile calls `activate_skill` spontaneously, per
PATCH-018-01) and is **not** this patch's responsibility. Do not treat it as a
regression, and do not tune it.

## Journal strategy

Standalone, per `patches/README.md` → Journal rules: this changes an
observable model decision, not deterministic code only.

Create `docs/journal/patches/PATCH-012-01-bound-skill-routing-generation.md`, and
add a short index entry with a link under `## Patches` in
`docs/journal/SPEC-012-skills.md`.

The journal must record: the before/after routing latency per profile, the
selected skill per live case (proving accuracy held), which lever was chosen and
why the rejected ones were rejected, the fenced-response decision, model
provenance, implementation commit SHA, and the `--no-ff` merge SHA.

## Out of scope

- Changing `run_with_deadline` to cancel abandoned requests instead of
  abandoning them. The GPU-occupancy consequence is real and is recorded in
  PATCH-018-01, but cancellation semantics belong to SPEC-011 and would be its
  own patch against a deliberate design decision.
- Disabling or bounding thinking in the **agent loop**. That is a separate
  question with a different cost/benefit, and answering it here would fold two
  unrelated corrections into one step.
- Recalibrating any model profile deadline (SPEC-017).
- Adding a routing model separate from the agent model.
- Any change to `activate_skill`, mid-turn activation, or SPEC-018 generally.
- Making the models call `activate_skill` on their own — a real open question
  from PATCH-018-01, but a prompt-contract question, not a routing one.
- Retiring `SkillRouter` (SPEC-012 §9's parked question stays closed).
- General routing-prompt redesign or few-shot examples.
- Caching routing decisions across turns.

## Suggested branch and commit conventions

```text
branch:
patch/PATCH-012-01-bound-skill-routing-generation

patch file:
patches/SPEC-012/PATCH-012-01-Bound-Skill-Routing-Generation.md

implementation commit:
Bound skill routing generation (PATCH-012-01)

merge:
Merge PATCH-012-01: bound skill routing generation
```
