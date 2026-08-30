# SPEC-021 — Turn Budget Revision

- **Spec:** [SPEC-021](../../specs/SPEC-021-Turn-Budget-Revision.md)
- **Date:** 2026-08-30
- **Branch:** feature/SPEC-021-turn-budget-revision
- **Implementation commit:** `44d84e1`
- **Merge commit:** `a448205`

## Hypothesis / intent

`MAX_TOOL_CALLS_PER_TURN = 4` was set by SPEC-010 on 2026-07-23 with no derivation, when
the project had three tools, no skills, no Tracker, no sandbox and no deadlines. Three
heavier classes of work were added underneath it since, one of them (`activate_skill`,
SPEC-018) a new *consumer* of the same four calls. The limit had started firing on
correct behaviour, and when it fired the user received nothing at all.

Two things were expected from this step, and it claims neither a larger budget nor a
better agent in advance:

1. exhausting the budget should end in a real answer rather than a rolled-back turn;
2. the number, its scope, and whether `activate_skill` is charged against it should come
   out of measured turn data with the rule written down — so the next reader is not in
   the position this spec was in.

## What changed

- **`reliability.py`** — `TerminationReason.BUDGET_EXHAUSTED` mapping to
  `TurnStatus.COMPLETED`, and `TOOL_CALL_LIMIT` **removed**: after this step no path
  terminates on a spent budget, so keeping the name would leave vocabulary a trace
  reader could not distinguish from a live one (§4.3). `validate_reliability_config`
  gained an optional `max_control_calls` bound.
- **`agent.py`** — the whole behavioural change. The refusal is untouched: a call
  beyond a budget is still never dispatched. What follows it is now one final model
  request with **no tools declared**, carrying a host block
  (`TOOL_BUDGET_EXHAUSTED_POLICY`, shaped like `<active_skill_policy>`) that asks the
  model to answer with what it has and say what it could not finish. Implemented as a
  `continue` rather than a second request site, so the forced request inherits the
  whole-turn deadline, the model deadline, tracing, streaming and SPEC-020 reasoning
  handling unchanged. `_drive_loop` now returns `(text, reason)`; `run_turn` reports
  that reason instead of a hard-coded `FINAL_ANSWER`, which is what puts receipts on a
  budget-exhausted turn.
- **`agent.py`** — a second counter, `control_calls`, with its own bound. The
  closed-form maximum on model requests per turn is written beside the counters.
- **`config.py`** — the two comments this step had to correct (see below).
- **`skill_runtime/orchestrator.py`** — derives `max_control_calls` from the
  activation cap it already owns, so the two bounds cannot drift.
- **`app.py`** — *unchanged*. Its outcome branch keys on `TurnStatus`, not on
  `reason`, so `BUDGET_EXHAUSTED → COMPLETED` already persists the answer, commits
  sandbox artifacts and stores the receipts.
- **`scripts/analyze_turn_budget.py`** — new; the re-runnable measurement.
- **`evals/`** — both budget cases keep their scripts and change expectation to
  `completed/budget_exhausted`; new `activation-budget-guard-001` pins the
  control-call bound. The scripted suite now pins its own budget
  (`SCRIPTED_MAX_TOOL_CALLS`) instead of reading the global one, and
  `run_scripted_skill_case` honours `runner_overrides` — it silently ignored them
  before, which would have dropped the per-case pin on both skill budget cases.

### Corrections this step had to make

1. **SPEC-017 §4.1 misclassified the budget.** It called `MAX_TOOL_CALLS_PER_TURN` a
   bound on "host work, not model latency" and kept it global for that reason. The
   loop continues through exactly one path — an executed tool call — so the budget is
   also the bound on model requests per turn, and therefore on worst-case latency and
   on accumulated tool payload. Corrected in `config.py`.
2. **SPEC-021 §7.1 calls the trace history "committed".** It is not; `data/traces/` is
   git-ignored. See above.
3. **SPEC-021 §4.4's stated loop bound does not hold as written.** It gives
   `max_tool_calls + max_skill_activations + 1 + 1`. But
   `SkillActivationHandler.handle` returns a *recoverable* result for both
   `unknown_skill` and `activation_limit` **without incrementing `_activations`**. So
   exempting control calls from the work budget and bounding them by
   `MAX_SKILL_ACTIVATIONS_PER_TURN` would bound nothing: a model calling
   `activate_skill` with varied names would be limited only by the catalog
   (`MAX_SKILLS = 100`). The loop therefore counts control-tool **attempts** against
   `MAX_SKILL_ACTIVATIONS_PER_TURN + 1`. The `+1` is not slack — it is what keeps the
   handler's recoverable `activation_limit` answer reachable instead of dead code.
   Actual bound:

   ```text
   model requests in the loop <= max_tool_calls        (work)
                              +  max_control_calls     (= max_skill_activations + 1)
                              +  1  (the step whose call was refused)
                              +  1  (the forced answer, which declares no tools)
   ```

   Pinned by an adversarial-script test that asks for a tool at every opportunity.
4. **SPEC-021 §4.3 says to remove `TOOL_CALL_LIMIT` "together with its
   `USER_MESSAGE_BY_REASON` entry".** It never had one — that table holds only static
   messages, and the budget message was formatted inline in `agent.py`.

## Method — the selection rule, stated before the results

Recorded here **before** the §7.1 corpus was run, per §7.1 step 3:

> The budget covers the 95th percentile of `tool_calls_executed` over **completed** turns
> in the measured corpus, computed on the heavier subpopulation (skill-active turns),
> rounded up to an integer. Where historical data is right-censored at the current limit,
> the uncensored scenario-corpus run is authoritative and the historical distribution is
> corroborating only.

The rule matters more than the number: `scripts/analyze_turn_budget.py` is committed so a
future reader can re-run it and get the same answer.

### Correction: the trace history is not committed

SPEC-021 §7.1 says to extract the distribution "from the committed trace history". There
is no such thing. `data/traces/` is git-ignored (SPEC-011; see `.gitignore`), so the 79
local trace files are a local-only artifact of this machine. The reproducible artifact is
therefore **the script plus the numbers transcribed below**, following the precedent
PATCH-019-01 set for gitignored `data/evals/` results.

## Model & parameters (provenance)

- Agent model: `qwen3.8:27b-mlx` (digest `5642e97495e1`, safetensors / nvfp4, 27.8B) — profile `next-mlx`
- Router model: `qwen3:8b` (digest `500a1f067a9f`, Q4_K_M, 8.2B, ctx 40960) — profile `fast`
- Ollama: 0.32.15 · Python client: `ollama==0.6.2`
- Reasoning: `medium` (transient preservation on)
- Sampling: defaults — no `options` are set anywhere in `llm.py`
- Host: Apple M3 Max · Docker 29.6.1 · sandbox image `lllm-sandbox:spec-015` (`3d7c43f75e78`)
- Measurement-only configuration, reverted before commit: `MAX_TOOL_CALLS_PER_TURN = 12`
  and `next-mlx` `agent_turn_timeout_seconds` 500 → 1500

## Verification

### §7.1 step 1 — the historical distribution (censored)

`python scripts/analyze_turn_budget.py` over 79 local trace files, 228 `turn_finished`
events, 221 completed turns, spanning 2026-07-24 to 2026-08-28.

Completed turns, `tool_calls_executed` as recorded (an activation counts as a call):

| population | n | 0 | 1 | 2 | 3 | 4 | p95 |
| --- | ---: | --- | --- | --- | --- | --- | ---: |
| all | 221 | 84 (38%) | 82 (75%) | 32 (90%) | 15 (96%) | 8 (100%) | 3 |
| skill active | 121 | 28 (23%) | 44 (60%) | 30 (84%) | 12 (94%) | 7 (100%) | **4** |
| no skill | 100 | 56 (56%) | 38 (94%) | 2 (96%) | 3 (99%) | 1 (100%) | 2 |

By profile (joined to each turn's own `run_started` by `run_id`):

| profile | n | distribution | p95 | max |
| --- | ---: | --- | ---: | ---: |
| `deep` | 7 | 0:1 1:4 2:1 3:1 | 3 | 3 |
| `fast` | 17 | 0:10 1:6 3:1 | 3 | 3 |
| `mid` | 13 | 0:8 1:4 2:1 | 2 | 2 |
| `next` | 26 | 0:12 1:8 2:5 3:1 | 2 | 3 |
| `next-mlx` | 60 | 0:19 1:19 2:15 3:1 4:6 | 4 | 4 |
| `qwen3:8b` (pre-SPEC-017 runs, no profile field) | 98 | 0:34 1:41 2:10 3:11 4:2 | 3 | 4 |

Non-completed turns in the whole history: `stopped/tool_call_limit` ×3,
`failed/model_error` ×2, `timed_out/skill_routing_timeout` ×1, `timed_out/tool_timeout` ×1.

**All three `tool_call_limit` turns delivered `final_text_chars: 0`.** They ran 66.9 s,
142.2 s and 269.3 s respectively and the user received nothing from any of them. That is
§4.1's failure, measured rather than argued.

Three things this table cannot settle on its own, and the reason §7.1 step 2 is
load-bearing rather than optional:

- **It is right-censored at 4 by the very limit being revised.** No completed turn can
  report more calls than the budget allowed. Applying the stated rule to this data alone
  yields p95 = 3 — a budget *below* today's — which is an artefact of the ceiling, not a
  finding.
- **The tail is thin.** Only 23 of 221 completed turns used ≥2 calls.
- **Skill turns are the heavy population and they are the ones pressed against the
  ceiling** (p95 = 4, max = 4), while non-skill turns sit at p95 = 2.

Read in the post-SPEC-021 accounting (`--work-only`, subtracting `skill_activations`),
the skill-active population moves to `0:28 1:44 2:30 3:15 4:4`, p95 = 3 — i.e. roughly one
call of the observed pressure was activation overhead rather than work.

### §7.1 step 2 — scenario corpus under a raised temporary budget

Measurement-only configuration, reverted before anything was committed:
`MAX_TOOL_CALLS_PER_TURN = 12`, and `next-mlx`'s `agent_turn_timeout_seconds` raised
500 → 1500 so a twelve-call turn could not be cut short by the *deadline* and be
misread as evidence about the *budget*. Agent `next-mlx`, router `fast`, reasoning
`medium` — the §7.3 configuration. One turn per app run, so each run is its own trace.

**Round 1 was partly invalid, and the reason is worth recording.** The Docker daemon
dropped out after the fifth run. From `onetool-3` onwards every run started with
`[sandbox] unavailable` and `[skills] 2 loaded: sales_analysis, tracker_read` — so
`code_workspace` was not in the registry at all. That silently destroyed two of the
five scenario classes: the sandbox class had no sandbox, and the two-phase class
cannot activate a skill that was never loaded. Both were re-run (round 2) with the
sandbox confirmed ready. The no-tool, one-tool and Tracker classes are unaffected and
are reported from round 1.

The lesson generalises: a live corpus must assert its own preconditions per run. The
per-run `[sandbox]` line was the only reason this was caught rather than being written
up as "the model chose not to activate".

Valid runs, one turn each. `work` is tool calls excluding activations, i.e. the
quantity the post-SPEC-021 budget governs; `act` is skill activations.

| class | run | outcome | work | act | total | secs | tool sequence |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| no-tool | 1-3 | completed/final_answer | 0, 0, 0 | 0 | 0 | 71, 65, 45 | — |
| one-tool | 1-3 | completed/final_answer | 1, 1, 1 | 0 | 1 | 50, 60, 137 | `get_current_time` |
| Tracker, query correction | 1 | completed/final_answer | 2 | 0 | 2 | 307 | `issues_find` ×2 |
| Tracker, query correction | 2 | completed/final_answer | **4** | 0 | 4 | 211 | `issues_find` ×4 |
| Tracker, query correction | 3 | completed/final_answer | 1 | 0 | 1 | 132 | `issues_find` |
| sandbox | 1 | completed/final_answer | 1 | 1 | 2 | 83 | `activate_skill` → `sandbox_execute` |
| sandbox | 2-3 | completed/final_answer | 1, 1 | 0 | 1 | 125, 116 | `sandbox_execute` |
| two-phase | 1 | completed/final_answer | **3** | **2** | **5** | 459 | `get_current_time` → `activate_skill`(tracker_read) → `issue_get_comments` → `activate_skill`(code_workspace) → `sandbox_execute` |

**The two-phase run is the decisive one.** It is the request PATCH-018-02 recorded
dying at `stopped/tool_call_limit`, and it shows exactly why: five calls, of which
**two were activations**. Under SPEC-018 accounting it needed five of four and died
on `sandbox_execute` — the last step, with all the work already done. Under
SPEC-021 accounting it is three work calls out of four and two activations out of
three, and it completed with the CSV written.

Applying the stated rule to the corpus's skill-active turns (Tracker ×3, sandbox ×3,
two-phase ×1 — work calls 2, 4, 1, 1, 1, 1, 3): p95 = **4**. The historical
skill-active distribution read in the same accounting gives p95 = 3. The corpus was
run at a budget of 12, so nothing in it was censored: `tracker-2` was free to ask for
a fifth call and did not.

### Decision

**The budget stays at 4, and it stays global.** That is the measurement's answer, not
a decision to do nothing:

- **Value.** The rule yields 4 on uncensored data. Raising it would raise worst-case
  model requests, latency and accumulated tool payload in direct proportion (§2.4)
  with no measurement asking for it. SPEC-021 §1 is explicit that it does not claim a
  larger budget is better, and §5 forbids committing a value that cannot be justified
  from the data — which cuts both ways.
- **Scope: global.** Every profile's distribution sits in the same 3-4 band
  (`fast` p95 3, `mid` 2, `deep` 3, `next` 2, `next-mlx` 4), and all three historical
  `tool_call_limit` turns came from three *different* models. The requirement is
  model-independent, so §4.5's per-profile option is not taken — which also avoids
  §8.4's confounded comparison surface.
- **`activate_skill`: no longer charged**, per §4.4, with the bound correction above.
  This is what actually fixes the recorded failure. The two-phase request needs five
  calls and only three of them are work.

So the number SPEC-010 guessed turns out to have been defensible. What was not
defensible was leaving it underived, charging orchestration against it, and making
exhaustion cost the user their entire turn.

### Deterministic

```text
pytest                                  876 passed, 29 skipped   (861 before; 15 new)
python -m evals.runner --suite scripted  42/42 passed (0 failed)  (41 before; 1 new)
```

All eleven items of §7.2 are covered. The load-bearing ones:

- the over-budget call still never reaches the executor, asserted on the same turn
  that now completes;
- the forced request is made with an **empty tool list** — asserted on
  `ScriptedResponder`'s recorded `tools` argument, because "declares no tools" is the
  mechanism, not the instruction;
- exactly one forced request even when the scripted model keeps requesting tools on
  it, and its tool call is ignored rather than executed;
- the closed-form bound holds under an adversarial script that asks for a tool at
  every opportunity, alternating control and work calls;
- a forced request that returns nothing still fails through `empty_model_response`,
  and the whole-turn deadline still cuts the forced request off before it is made.

`tools=()` was verified against the live Ollama SDK before relying on it, alongside
`[]` and `None` — all three are accepted.

### Live

Agent `next-mlx` (`qwen3.8:27b-mlx`, safetensors/nvfp4, digest `5642e97495e1`),
router `fast` (`qwen3:8b`, Q4_K_M, digest `500a1f067a9f`), reasoning `medium`,
Ollama 0.32.15, Apple M3 Max. Sandbox `lllm-sandbox:spec-015` (`3d7c43f75e78`).

The before/after pair for the recorded failure:

**Before** — trace `agent-12302fc3-…`, turn `eaba555b-…`, 2026-08-28T20:30:26Z,
PATCH-018-02 run 5:

```text
stopped/tool_call_limit   tool_calls_executed=4  model_requests=6
initial_skill=tracker_read → selected_skill=code_workspace  skill_activations=1
final_text_chars=0        duration_ms=269257
```

Four minutes of work, and the user received nothing.

**After** — §7.1 two-phase run above, same prompt verbatim:

```text
completed/final_answer    work=3  activations=2  duration_ms=459157  chars=1817
get_current_time → activate_skill(tracker_read) → issue_get_comments
                 → activate_skill(code_workspace) → sandbox_execute
```

The turn that used to die on its last step now finishes it.

### What was NOT verified live — an honest gap

The live programme was cut short for time, and two of §7.3/§7.4's requirements are
**not** met. They are listed here rather than glossed, because the whole point of
this step was to stop inheriting unexamined claims:

1. **§7.3 asks for at least three runs of the two-phase request. One was completed**,
   and at the temporary measurement budget of 12 rather than the committed 4. Since
   that run used 3 work calls and 2 activations, both inside the committed bounds, its
   behaviour at 4 should be identical — but "should be" is inference, not measurement.
2. **§7.4 requires a verbatim budget-exhausted answer, and none was captured live.**
   The graceful path is proven deterministically (eleven tests, both eval cases), but
   whether the *model* actually says what it left undone — §8.2's risk, and the one
   claim in this step that is about model behaviour rather than host code — has no
   live evidence. The scripted eval cases assert their own scripted text, which proves
   plumbing, not honesty.
3. The §7.1 sandbox class never produced the "one script error and a retry" sub-case:
   the script succeeded first try in all three runs, so that row is 1 work call by
   luck, not by design.

Closing (1) and (2) is one command each and should happen before this is relied on:

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium   # two-phase ×3
# and one run with MAX_TOOL_CALLS_PER_TURN temporarily at 1, to force the path
```

### Process notes worth keeping

Docker Desktop restarted three times during the session. Each time, runs that started
during the outage came up with `[sandbox] unavailable` and `code_workspace` missing
from the registry — which silently invalidated ten runs of the first corpus, and would
have been written up as "the model chose not to activate" if the per-run `[sandbox]`
header had not been checked. The final corpus script probes Docker before each run and
marks a run INVALID rather than measuring it. **A live corpus must assert its own
preconditions per run.**

The Docker behaviour itself, and what could be done about it, is written up
separately in `docs/sandbox-docker-reliability.md` — it is a standing operational
problem rather than anything SPEC-021 introduced or should fix.

## Outcome

The mechanism works and is fully covered deterministically. Of §6's acceptance
criteria, all are met except the last two live ones (see the gap above).

The substantive finding is not the one the spec expected. SPEC-021 was written on the
premise that the budget was probably too small; the measurement says it was not. Every
uncensored population puts p95 at 3-4 work calls, and the corpus run at a budget of 12
never asked for a fifth call except once, at exactly 4. What actually broke
PATCH-018-02 run 5 was that **two of its four calls were spent on `activate_skill`**,
and that running out cost the user the entire turn. Fixing the accounting and the
consequence makes the recorded failure pass; raising the number would have hidden it
while paying for it in latency and context on every turn that never needed it.

The second finding is about the loop's bound. SPEC-021 §4.4 stated an arithmetic
guarantee that does not hold against the code it describes — the activation counter it
relies on is not incremented on a refused attempt. That was only caught by trying to
write the test that asserts the bound. An invariant nobody has written a test for is a
comment, not a bound.

## Follow-ups

- **Close the live gap** (items 1 and 2 above) before treating §6 as satisfied. In
  particular, get a verbatim budget-exhausted answer and check whether the model
  really admits what it left undone; if it does not, §4.2's instruction needs work and
  that is a PATCH.
- **Latency.** The two-phase run took 459 s against 269 s for the failing run it
  replaces — more of the work now actually happens. That is close to `next-mlx`'s
  500 s whole-turn deadline, and a budget-exhausted turn adds one more model request
  on top. Worth re-measuring: this step may have made `agent_turn_timeout_seconds`
  the next binding constraint, which is precisely the kind of undated inheritance
  SPEC-021 existed to stop.
- **Re-run the §7.1 measurement after some weeks of use** (§8.1). A rising mean is the
  signal that a graceful budget has made the agent lazier. `scripts/analyze_turn_budget.py`
  exists for exactly this and needs no new work.
- **The sandbox retry sub-case is still unmeasured.** A case whose script is *forced*
  to fail once would measure it deliberately rather than hoping the model errs.
- **`data/traces/` is git-ignored**, so every measurement in this journal is
  transcribed rather than reproducible from the repository. If budget derivation is
  going to be re-done periodically, a small committed corpus of trace summaries would
  make it genuinely replayable.
