# SPEC-021 — Turn Budget Revision

**Status:** Proposed  
**Step:** 21  
**Depends on:** SPEC-010, SPEC-011, SPEC-017, SPEC-018, SPEC-019  
**Target repository:** `egorgut/lLLM`

---

## 1. Summary

`MAX_TOOL_CALLS_PER_TURN = 4` is the only structural bound on the length of one agent
turn. It was introduced in SPEC-010 on 2026-07-23 with no derivation — the spec says
only:

```text
Add a configuration value: MAX_TOOL_CALLS_PER_TURN = 4
The exact default for this spec is `4`.
```

At that moment the project had three tools (`python_calculate`, `sql_query`,
`mcp_time__get_current_time`), no skills, no Tracker, no sandbox, and no deadlines —
SPEC-011 added those the next day. Four was the smallest number that turned a
one-tool-per-turn harness into an agent with room for one retry. It has not been
revisited since, while three qualitatively heavier classes of work were added on top
of it.

This step does two things.

**It changes what exhausting the budget means.** Today the budget is *terminal*:

```text
budget spent, model asks for one more tool
        ↓
stopped / tool_call_limit
        ↓
user message rolled back
        ↓
the user receives nothing at all
```

That is the wrong failure. At the moment the budget runs out the model usually holds
everything it needs to say something useful, and the host throws it away. It becomes
*graceful*:

```text
budget spent, model asks for one more tool
        ↓
the call is not executed (unchanged)
        ↓
one final request with no tools declared
        ↓
the model answers with what it has, and says what it could not finish
        ↓
completed / budget_exhausted
```

**It re-derives the number from measurement instead of inheriting it.** The value, its
scope (global or per model profile), and whether `activate_skill` should still be
charged against it are all decided from recorded turn data, and the reasoning is
written down so the next reader is not in the position this spec is in.

This SPEC does **not** claim a larger budget is better. Raising the budget raises
worst-case latency and context growth in direct proportion (§2.4). It replaces a guess
with a measurement and a catastrophic failure with a graceful one.

---

## 2. Motivation

### 2.1 The value was never derived, and its stated justification is a step out of date

SPEC-010 §"Safety and resource controls" calls the constant "the primary loop safety
control". That was true on 2026-07-23 and stopped being true on 2026-07-24, when
SPEC-011 added the bounds that actually do that job:

```text
TURN_TIMEOUT         agent_turn_timeout_seconds     runaway turn
MODEL_TIMEOUT        model_request_timeout_seconds  hung model
TOOL_TIMEOUT         TOOL_EXECUTION_TIMEOUT_SECONDS hung tool
REPEATED_TOOL_CALL   MAX_IDENTICAL_TOOL_CALLS = 2   thrashing on one call
TOOL_CALL_LIMIT      MAX_TOOL_CALLS_PER_TURN = 4    ← everything else
```

The residual case only the tool budget catches is narrow and real: a model making
varied, non-repeating, in-time calls that never converge. That is worth bounding. It is
not worth bounding at a value chosen for a different purpose in a different project.

### 2.2 Three heavier classes of work were added underneath an unchanged number

| date | step | new demand on the same four calls |
| --- | --- | --- |
| 2026-07-24 | SPEC-013, Tracker MCP | a search often costs 2–3 attempts to get the query language right |
| 2026-08-02 | SPEC-015/016, sandbox | `sandbox_execute` plus a retry when the script is wrong |
| 2026-08-12 | SPEC-018, `activate_skill` | **+1, by explicit decision** |

SPEC-018 (`ea682d2`) is the only place the budget was reconsidered at all, and it added
a consumer without raising the budget:

> An activation *also* consumes one of `MAX_TOOL_CALLS_PER_TURN`, because it is a model
> decision that cost a model request, and hiding it from that budget would let a
> thrashing model run unbounded.

The reasoning is sound in isolation. The arithmetic consequence was not examined.

### 2.3 The budget now fires on correct behavior, which makes it a capability ceiling wearing a safety label

Two recorded instances, both with the model behaving correctly:

**PATCH-018-02, live run 5.** A single-turn two-phase request. The ideal path is
exactly four calls:

```text
1/4 mcp_tracker__issue_get_comments
2/4 mcp_time__get_current_time
3/4 activate_skill  →  code_workspace replaces tracker_read
4/4 (should have been sandbox_execute)
```

The model spent 4/4 on one redundant time call and the turn ended
`stopped/tool_call_limit` — no answer, no CSV, nothing persisted, after roughly three
minutes of work the user watched happen. Zero slack was available for a single
imperfect step.

**PATCH-016-01.** `qwen3:8b` "spent two of four tool calls discovering that a later
turn cannot see an earlier turn's files" — half the turn's budget consumed by an
ordinary, recoverable misunderstanding.

A limit that fires on correct behavior is not a safety fuse. It is a functional
ceiling that reports itself as an error, and the user cannot tell the two apart:
`Agent stopped after 4 tool calls without a final answer` is printed whether the agent
malfunctioned or merely ran out of room.

### 2.4 The number silently controls latency and context, not just tool access

The loop in `agent.py` continues through exactly one path — an executed tool call.
Every other branch leaves the loop:

```text
step:
  deadline expired            → exit (turn_timeout)
  model request failed/hung   → exit (model_error / model_timeout)
  model returned text         → exit (final_answer)
  model returned nothing      → exit (empty_model_response)
  more than one call          → exit (parallel_tool_calls)
  same call repeated > 2      → exit (repeated_tool_call)
  budget spent                → exit (tool_call_limit)
  ───────────────────────────────────────────────────
  exactly one call, allowed   → tool_calls_executed += 1 → execute → next step
```

Therefore:

```text
model requests in the loop ≤ MAX_TOOL_CALLS_PER_TURN + 1
```

The budget is the loop bound. Raising it from 4 to 8 does not only permit more tools —
it doubles the worst-case number of model requests, and with `MCP_RESULT_MAX_CHARS =
20_000` it doubles the worst-case tool payload accumulated in the working transcript
(4 × 20 KB → 8 × 20 KB in the final request). SPEC-017 §4.1 classified the budget as
bounding "host work, not model latency" and kept it global for that reason. That
classification is wrong on its own terms, and this step must say so rather than quietly
depend on it.

This is precisely why the number cannot simply be raised: one constant is answering
three questions — how much work is permitted, how much latency is risked, and how much
context may accumulate.

---

## 3. Goals

### 3.1 Functional

1. Exhausting the tool budget produces a real answer to the user instead of a
   rolled-back turn, in exactly one additional model request.
2. The outcome vocabulary distinguishes "answered under a spent budget" from both an
   ordinary answer and a genuine failure.
3. The budget's value is derived from measured turn data, and the derivation rule is
   recorded in the spec's journal.
4. Whether `activate_skill` is charged against the work budget is decided explicitly,
   with the loop-bound arithmetic shown.
5. Whether the budget is global or per model profile is decided from the measurement,
   not assumed.

### 3.2 Architectural

- Preserve SPEC-010's loop shape: one model response is one decision, one tool call per
  response, the host owns every limit, the model never reads or writes one.
- Preserve SPEC-011's deadlines, repeated-call detection, outcome/trace contract, and
  the rule that every started turn produces exactly one outcome and one
  `turn_finished`.
- Preserve the rule that a call beyond the budget is **never executed**. This step
  changes what happens *after* the refusal, never the refusal.
- Keep the loop bounded by arithmetic, not by hope: the maximum number of model
  requests per turn must remain a small closed-form expression of host constants.
- No new framework, no new dependency, no new configuration file.

### 3.3 Non-goals

Listed in §9.

---

## 4. Functional requirements

### 4.1 Budget exhaustion becomes graceful

When the model requests a tool call and `tool_calls_executed >= max_tool_calls`:

1. the requested call is **not executed** — unchanged from SPEC-010 §2;
2. the existing `policy_violation` trace event with `policy="tool_call_limit"` is
   emitted — unchanged;
3. instead of raising a terminal error, the loop makes **one** final model request
   under the rules in §4.2 and returns its text as the turn's final answer.

If that final request yields no usable text, or fails, or exceeds a deadline, the turn
terminates through the existing failure paths. Graceful degradation is an attempt, not
a guarantee.

### 4.2 The forced-answer request

The final request differs from an ordinary one in exactly two ways:

- **No tools are declared.** The model is structurally unable to request another call,
  so this path cannot recurse. This is the mechanism that keeps the loop bounded; a
  prompt instruction alone would not be.
- **A host-owned instruction is appended** to the system-level block, in the style of
  the existing `<active_skill_policy>` wrapper, saying in substance:

```text
The tool budget for this turn is spent. Answer now using what you already have.
State plainly what you could not complete and what remains to be done, so the user
can ask for the rest in a new turn. Do not claim you completed work you did not.
```

The instruction is host text, is not persisted, and is not shown to the user, exactly
like every other host block.

All existing turn machinery applies to this request unchanged: the whole-turn deadline,
the model-request deadline, tracing, streaming to the renderer, and — because the turn
now completes — the SPEC-020 reasoning rules and the PATCH-010-05 action receipts for
the tools that did run.

### 4.3 Outcome and trace vocabulary

A new `TerminationReason` mapping to `TurnStatus.COMPLETED`:

```python
TerminationReason.BUDGET_EXHAUSTED  → TurnStatus.COMPLETED
```

Rationale: the user received an answer grounded in real work, so the turn completed and
`app.py` persists it exactly like any other completed turn. Reporting it as
`FINAL_ANSWER` would erase the fact that the answer was cut short; reporting it as
`STOPPED` would discard the answer.

- `TOOL_CALL_LIMIT` is retained only if some path still terminates on it; if none does,
  it is removed together with its `USER_MESSAGE_BY_REASON` entry rather than left as
  dead vocabulary.
- `turn_finished` gains no new required field. `reason` already carries the
  distinction, and the existing `tool_calls_executed` already says how much was spent.

### 4.4 `activate_skill` accounting

**Recommendation: stop charging `activate_skill` against the work budget.**

SPEC-018's concern — that hiding it lets a thrashing model run unbounded — is already
answered by a bound built for exactly that purpose:

```text
MAX_SKILL_ACTIVATIONS_PER_TURN = 2
```

An activation is orchestration, not work. Charging it against the work budget makes a
turn's capacity depend on how well the router happened to guess, which is the coupling
PATCH-018-02 already had to work around.

With this change the loop bound becomes:

```text
model requests in the loop ≤ MAX_TOOL_CALLS_PER_TURN
                           + MAX_SKILL_ACTIVATIONS_PER_TURN
                           + 1   (the step that hit the budget)
                           + 1   (the forced answer)
```

Still closed-form, still small, still host-owned. The implementation must state this
expression in `agent.py` next to the counter, so the next reader does not have to
re-derive it from the control flow as this spec did.

If measurement in §7.1 contradicts the recommendation, the implementation may keep the
current accounting — but the journal must then record the measurement that justified
it.

### 4.5 Where the budget lives

Two admissible outcomes, chosen by the §7.1 measurement:

- **Per profile** — `MAX_TOOL_CALLS_PER_TURN` moves into `ModelProfile` beside the
  deadlines, if the measured budget needed for the same scenarios differs materially
  between `fast` and `next-mlx`. This is consistent with SPEC-017's own premise that "a
  model is not a name on its own", and with §2.4: the budget is a latency bound, and
  latency bounds already live in the profile.
- **Global** — the constant stays in `config.py` with a corrected comment, if the
  measurement shows the requirement is model-independent.

Either way, SPEC-017 §4.1's claim that the tool budget "bounds host work, not model
latency" must be corrected in this step's journal, because the implementation now
depends on knowing it is false.

Whichever is chosen: the scripted eval suite keeps a pinned budget, exactly as it pins
the `fast` deadlines today, so its committed results stay comparable.

### 4.6 User-visible behavior

- `[tool N/MAX]` continues to render the live budget, whatever `MAX` becomes.
- A budget-exhausted turn prints the model's answer like any other turn. It carries no
  host-generated banner: §4.2's instruction makes the model say what it could not
  finish, which is more useful and more truthful than a fixed line.
- `Agent stopped after N tool calls without a final answer.` disappears from ordinary
  operation. If §4.3 retains a terminal path, its message must describe *that* path,
  not the budget.

### 4.7 Evaluation

- The existing scripted case that asserts `stopped/tool_call_limit`
  (`sandbox-budget-guard-001` in `evals/cases.json`) changes expectation to the new
  completed/budget-exhausted outcome, or is replaced by a case that pins whichever
  terminal path survives. It must not simply be deleted: budget behavior is exactly the
  kind of thing that regresses silently.
- At least one new scripted case covers a turn that exhausts the budget and still
  answers.

---

## 5. Design constraints

- A call beyond the budget is never executed. This step changes the consequence of the
  refusal, never the refusal itself.
- The forced-answer request declares no tools. Recursion is prevented structurally, not
  by instruction.
- Exactly one forced-answer attempt. No retry loop.
- Every started turn still produces exactly one `AgentTurnOutcome` and exactly one
  `turn_finished` event, including on this path.
- The model never learns any budget value, and no model text can raise, lower, or
  disable one.
- No change to `MAX_IDENTICAL_TOOL_CALLS`, any deadline, `MCP_RESULT_MAX_CHARS`, tool
  contracts, storage schema, `STORE_VERSION`, or router behavior.
- `agent.py` stays tool-agnostic: the budget path may not learn what any particular
  tool means.
- The chosen numbers must be traceable to §7.1's data in the journal. A value that
  cannot be justified from the measurement must not be committed — this step exists
  because that happened once already.

---

## 6. Acceptance criteria

- [ ] A turn that exhausts the tool budget returns a final answer to the user.
- [ ] That turn is `COMPLETED` with a reason distinguishing it from an ordinary answer.
- [ ] Its answer is persisted, with PATCH-010-05 receipts for the tools that ran.
- [ ] The call that exceeded the budget was never executed.
- [ ] The forced-answer request declares no tools, and no turn makes more than one.
- [ ] The maximum model requests per turn is a stated closed-form expression of host
      constants, asserted by a test.
- [ ] The budget's value is justified in the journal by §7.1's measured distribution.
- [ ] `activate_skill` accounting is decided explicitly and the arithmetic is recorded.
- [ ] The budget's scope (global or per profile) is decided from measurement, and
      SPEC-017 §4.1's misclassification is corrected in the journal.
- [ ] Turns that never reach the budget behave exactly as before — same outcomes, same
      counters, same traces.
- [ ] Deadlines, repeated-call detection, routing, storage, and CLI shape are unchanged.
- [ ] Full deterministic tests pass; full scripted eval suite passes.
- [ ] Live verification per §7.3.

---

## 7. Verification

### 7.1 Measurement first

Before any number is chosen, measure what real turns actually need. The data mostly
already exists: `turn_finished` records `tool_calls_executed` for every turn ever run,
in `data/traces/*.jsonl`.

1. Extract the distribution of `tool_calls_executed` over **completed** turns from the
   committed trace history, split by profile and by whether a skill was active.
2. Extend it with a scenario corpus run deliberately under a raised temporary budget,
   so turns that today die at 4 can reveal what they would have needed. At minimum:
   - a Tracker search requiring query-language correction;
   - a sandbox job with one script error and a retry;
   - the PATCH-018-02 two-phase request (read → time → activate → execute);
   - an ordinary one-tool question;
   - a no-tool question.
3. State the selection rule explicitly before reading the result — for example, "the
   budget covers the 95th percentile of successful turns in the corpus" — and record
   both the rule and the resulting number.

The rule matters more than the number. A future reader must be able to re-run this and
get the same answer, which is exactly what SPEC-010 did not provide.

### 7.2 Automated

Deterministic, using `ScriptedResponder` / `FakeToolExecutor` / `RecordingRenderer`:

1. budget exhausted → `COMPLETED` with the new reason, final text present;
2. the over-budget call never reaches the executor;
3. the forced request is made with an empty tool list;
4. exactly one forced request, even when the model would have kept calling;
5. the forced request carries the host instruction, and the instruction is neither
   persisted nor rendered;
6. receipts from the tools that did run are attached to the completed turn;
7. a forced request that returns nothing terminates through the existing failure path;
8. the whole-turn deadline still cuts the forced request off;
9. the closed-form maximum on model requests holds under an adversarial script that
   requests a tool at every opportunity;
10. `activate_skill` accounting matches §4.4's decision;
11. turns below the budget are byte-identical in outcome and trace to today.

### 7.3 Live

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium
```

Re-run the PATCH-018-02 two-phase request, which is the recorded failure this step
exists to fix, at least three times. Required: no run ends with the user receiving
nothing. Record for each run the tool sequence, the outcome reason, and whether the
answer honestly states what was left undone.

Also run one ordinary no-tool conversation and one single-tool question to confirm
unchanged behavior below the budget.

### 7.4 Journal

`docs/journal/SPEC-021-turn-budget-revision.md`, recording:

- the measured distribution from §7.1 and the selection rule, stated before the result;
- the chosen budget, its scope, and the `activate_skill` decision with its arithmetic;
- the correction to SPEC-017 §4.1's classification;
- before/after live transcripts of the two-phase request;
- model/router provenance, reasoning mode, and any latency change from the extra
  forced request;
- what a budget-exhausted answer actually looked like, verbatim — whether the model
  really does say what it left undone is a model-behavior claim and needs evidence.

---

## 8. Risks

### 8.1 A graceful budget invites a lazier agent

If exhausting the budget is no longer painful, a model may drift toward spending it.
The mitigation is that nothing about the budget is visible to the model — it learns
about exhaustion only after the fact, in a request where tools are already gone. Watch
the §7.1 distribution again after some weeks of use; a rising mean is the signal.

### 8.2 A truncated answer may read as a complete one

A budget-exhausted answer that does not admit its own incompleteness is worse than a
clean failure, because the user cannot tell. §4.2's instruction targets exactly this,
and §7.4 requires evidence rather than assumption that it works.

### 8.3 A larger budget degrades the last request

More calls means more tool payload in the working transcript, and quality can fall
before the budget does. If §7.1 suggests a large number, verify the last step still
produces good answers rather than assuming more room is strictly better.

### 8.4 Per-profile budgets multiply the comparison surface

If §4.5 chooses per-profile, then two profiles differ in both deadlines and budget, and
a comparison between them confounds the two. Keep the budget fixed when comparing
models, exactly as SPEC-019 §8.5 required for the router split.

### 8.5 The forced request could mask a genuine defect

A model looping through varied non-converging calls now ends with a plausible answer
instead of a visible stop. The distinct `reason` in §4.3 is what keeps it visible;
it must not be collapsed into `FINAL_ANSWER` for convenience.

---

## 9. Out of scope

- Any change to `MAX_IDENTICAL_TOOL_CALLS`, the deadlines, `MCP_RESULT_MAX_CHARS`, or
  `MAX_SKILL_ACTIVATIONS_PER_TURN`'s value.
- Token-based or cost-based budgets. A real currency for the loop is a reasonable
  future direction and a different design problem.
- Continuing an exhausted turn automatically in a new turn, or any form of
  multi-turn task resumption.
- Summarising or compacting the working transcript to fit more calls into one turn.
- Parallel tool calls, which SPEC-010 rejects and this step does not revisit.
- Making any budget model-writable or model-visible.
- Changing routing, skill selection, or which tools a skill may see.
- Persisting anything new to `chat_history.json`.
