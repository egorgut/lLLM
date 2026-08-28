# SPEC-018 — Mid-Turn Skill Activation

- **Spec:** [SPEC-018](../../specs/SPEC-018-Mid-Turn-Skill-Activation.md)
- **Date:** 2026-08-12
- **Branch:** feature/SPEC-018-mid-turn-skill-activation
- **Implementation commit:** `ea682d2`
- **Merge commit:** `9c3ca07`

## Hypothesis / intent

Since SPEC-012 the skill decision has been made exactly once per turn, and made
at the worst possible moment: `SkillTurnOrchestrator.run_turn` routes from the
user message plus six context messages, *before* a single tool has executed. No
row has been read, no issue fetched, no repository inspected. The router judges
the request by its wording alone, then freezes the view — prompt, tool
declarations, executor — for the rest of the turn.

For narrow, well-worded requests that is enough. The hypothesis of this step is
that it stops being enough for multi-step work: a request that looks like a
tracker question until the issue is read, and then turns out to be a code
question, is routed once and stays routed. The model can neither correct a
defensible-but-wrong initial guess nor progress from one procedure to the next.
Today the only way to spend two procedures on one task is to make the user send
two messages, splitting one conceptual turn across two whole-turn budgets and two
independent routing decisions.

The deliberate non-decision is just as important: **the router stays**. Retiring
it and letting the model reach for procedures entirely on its own was considered
and rejected for this step. A router asked one narrow question always answers it;
a model in the middle of solving a task may simply answer without reaching for a
procedure, and the failure would be silent — not an error, just an answer
produced without the skill's checks, format, or completion criteria. So this step
adds capability without touching the guarantee, and whether `activate_skill`
could replace routing outright stays a separate, evidence-led step.

## What changed

- **`agent.py`** — learns one *general* notion, not skills: a **control tool** is
  a tool the host handles itself because handling it changes the turn's own view.
  New `ControlResult` (frozen: `result`, plus `tools` / `executor` /
  `system_suffix`, each `None` = unchanged) and the `ControlToolHandler` protocol
  (`names`, `handle`). When a requested name is in `handler.names`, every existing
  pre-dispatch policy still runs first in the existing order — parallel-call
  rejection, `tool_call_requested`, repeated-call detection, the tool-call budget,
  the renderer, the whole-turn deadline — and only then the handler is invoked,
  under the same tool-execution deadline as any other tool, instead of
  `ToolExecutor.execute`. The frozen view became loop-local working state
  (`working_tools`, `working_executor` beside the existing `working_messages`), so
  a returned `ControlResult` can replace any part of it. `agent.py` contains no
  import from `skill_runtime` and no skill-specific logic.
- **The system-block seam.** The orchestrator used to bake
  `compose_active_skill(spec)` into the system message via
  `Conversation.messages_for_model(additional_system=...)`, which left the loop
  unable to tell base prompt from skill block afterwards. `run_turn` now takes
  `system_suffix` separately and `_set_system_suffix` always recomposes index 0
  from the base captured at the start of the turn — so a replacement can neither
  stack a second `<active_skill>` wrapper nor leave a stale one behind. The join
  is character-for-character the one `conversation.py` performs, verified: a turn
  with no activation is byte-identical in model-facing context to the SPEC-012
  path. `Conversation` itself is untouched.
- **`skill_runtime/activation.py`** (new) — `ACTIVATE_SKILL_TOOL_NAME`,
  `build_activate_skill_declaration(catalog)` (host-generated from the registry
  catalog; `None` when the catalog is empty, so the tool is never an offer the
  host cannot honor), and `SkillActivationHandler`, created fresh per turn because
  everything it holds is turn state. It reuses `compose_active_skill`,
  `declarations_for_names`, and `RestrictedToolExecutor` unchanged — this is a
  seam, not a second skill mechanism.
- **Replacement, not composition.** At most one skill is active at any instant. A
  new activation builds declarations + the reserved name, a fresh
  `RestrictedToolExecutor` over `allowed_tools | {activate_skill}` wrapping **the
  original global executor** (never the previous wrapper, so restrictions cannot
  accumulate), and a system block that replaces the previous one entirely. The
  model-facing result is a *receipt* (`ok`, `skill`, `version`, `replaced`,
  `available_tools`), never the instruction: that went to the system layer, and
  duplicating it would both waste context and weaken SPEC-012 §10's precedence.
- **Recoverable errors, no new `TerminationReason`.** An unknown name returns
  `{"ok": false, "error": "unknown_skill", "available_skills": [...]}` and an
  exhausted cap returns `{"ok": false, "error": "activation_limit", ...}`; in both
  cases the view is untouched and the turn continues. Name validation runs *before*
  the cap check, so a nonexistent skill is never reported as an exhausted budget.
  A tool outside the newly active allowlist is still terminal
  (`stopped/skill_policy_violation`), unchanged.
- **Two independent budgets.** An activation consumes one of
  `MAX_TOOL_CALLS_PER_TURN` — it is a model decision that cost a model request,
  and hiding it from that budget would let a thrashing model run unbounded — and
  counts against the new host-owned `MAX_SKILL_ACTIVATIONS_PER_TURN = 2`
  (validated at startup like every other skill bound). The router's own selection
  does not count against it.
- **Reserved name.** Startup rejects a registered tool *or* a skill package named
  `activate_skill`, both as `SkillPackageError` from `SkillPackageLoader.load_all`
  — one fail-fast place both `app.py` and `evals/runner.py` already call.
- **Tracing.** New `skill_activated` (with `replaced_skill`, `activation_index`,
  `source`, and whether the view was actually recomposed); `skill_loaded` and
  `skill_toolset_resolved` are emitted for a mid-turn activation exactly as for
  the router's selection, so one trace still reconstructs every view the turn ran
  under. `turn_finished.selected_skill` now means *the skill active when the turn
  ended*; the router's choice is preserved as the additive `initial_skill`, and
  `skill_activations` counts the switches (`0` for every turn that behaves as
  today). The runner stays skill-agnostic about this: `run_turn` takes a generic
  `extra_turn_fields` callable, read at emit time and guarded, since exactly one
  terminal event is guaranteed per turn.
- **`app.py` / `README.md`** — the `[skill] code_workspace (replacing tracker_read)`
  switch line, distinct from the initial `[skill]` line and driven by the same
  activity indicator; new «Смена навыка по ходу (SPEC-018)» section.
- **`evals/runner.py`** — `CaseResult` and the results JSON now carry
  `final_skill` / `skill_activations`, because `selected_skill` alone no longer
  describes a turn's skill decision.

## Model & parameters (provenance)

All three profiles are installed on this machine and were queried for provenance,
even though no live run was performed this step (see Verification):

- qwen3:8b (`fast`) — digest `500a1f067a9f`, 8.2B params, Q4_K_M, ctx 40960
- qwen3:14b (`mid`) — digest `bdbd181c33f2`, 14.8B params, Q4_K_M, ctx 40960
- qwen3:32b (`deep`) — digest `030ee887880f`, 32.8B params, Q4_K_M, ctx 40960
- Ollama 0.31.1
- Sampling: defaults — `llm.py` still sets no `options`.

## Verification

**Deliberately reduced for this step, at the user's direction: implementation
only, no new tests and no live-model evidence.** What was run:

- `python -m pytest -q` — **636 passed, 29 skipped**. One existing assertion was
  updated rather than added to: `turn_started.available_tools` in
  `tests/test_skill_turn.py` now ends with `activate_skill`.
- `python -m evals.runner --suite scripted` — **36/36 passed**, unchanged.
- Byte-identity of the no-activation path confirmed directly: the messages the
  runner composes from `messages_for_model()` + `system_suffix` compare equal to
  the SPEC-012 `messages_for_model(additional_system=...)` output.
- Startup rejection confirmed for both reserved-name collisions (a registered
  tool named `activate_skill`, and a `skills/activate_skill/` package).
- Mechanism smoke-checked end to end through the real `SkillTurnOrchestrator`
  with scripted doubles (script kept out of the repository): replacement swaps
  declarations, executor and system block with exactly one `<active_skill>`
  wrapper and the base prompt intact; a tool the *new* skill forbids is stopped
  before its handler; an unknown name and an exhausted cap both leave the turn
  running under the skill already active; a second activation still reaches a tool
  the first skill forbade, proving the wrapper does not accumulate; and an empty
  registry produces no declaration at all.

**Not done, and therefore not claimed:**

- §7.1's committed deterministic tests (control-tool dispatch, pre-dispatch
  ordering, replacement, cap, trace assertions) — the behavior above was checked
  by hand, but nothing guards it against regression.
- §7.1's new scripted eval cases (mid-turn activation, replacement, unknown-name
  recovery, cap exhaustion).
- §7.2's entire live programme: the coverage baseline and after-measurement on
  `fast` / `mid` / `deep`, the "does the model call the tool at all" case, and the
  measured prefix-reprefill cost from §4.8. Worth recording that §7.2 A could not
  have been run as written even with the time: the committed live suite drives a
  raw `AgentRunner` and never routes skills, so a live coverage baseline does not
  exist yet and would have to be built first.

The consequence is that this entry does **not** answer the two questions SPEC-018
was built to answer with numbers. The mechanism is implemented and its invariants
were checked; whether any profile actually reaches for the new tool is unknown.

## Outcome

Every functional and architectural acceptance criterion in §6 is met in code,
except the three that are about verification artifacts (§7.1's tests, the eval
cases, the live evidence). The router is untouched, so the coverage guarantee the
step was designed around holds by construction: a turn in which the model never
calls `activate_skill` behaves exactly as it did before, apart from one added
declaration and two additive trace fields.

The design decision worth keeping is the shape of the seam. Making the loop learn
"a host-handled tool that may change the turn's view" rather than "skills" kept
SPEC-012's dependency direction intact — `agent.py` still imports nothing from
`skill_runtime` — and turned two awkward problems into one small mechanism: the
system-block replacement became a *suffix slot* the loop recomposes from a
captured base, and the "which skill ended the turn" trace problem became a
generic `extra_turn_fields` callable rather than skill knowledge in the runner.

## Follow-ups

- **Write the §7.1 deterministic suite.** The invariants are currently protected
  by nothing but review: control-tool dispatch bypassing the executor, policy
  ordering, the single-wrapper rule, the original-executor rule, the cap, and the
  trace fields.
- **Add the four scripted eval cases** from §7.1.
- **Build a live skill-coverage path in `evals/runner.py`** (live cases through
  `SkillTurnOrchestrator`), which §7.2 A needs and which does not exist today.
- **Then run §7.2 A and B on all three profiles** and record the numbers,
  including the reprefill cost of §4.8. A profile on which the model never
  activates is a finding to record, not a blocker.
- Only after those numbers exist should §9's parked question — whether the router
  can be retired — be reopened.

## Patches

### PATCH-018-01 — Complete Mid-Turn Skill Activation Verification

Patch note: `patches/SPEC-018/PATCH-018-01-Complete-Mid-Turn-Skill-Activation-Verification.md`

This section **appends** evidence; it does not revise anything above it. Every
statement in the original entry about what was *not* verified was true when it
was written, and stays as the historical record of how the step was merged.

#### Why the debt existed

SPEC-018 was merged with verification deliberately reduced to implementation
only. §7.1's deterministic tests and eval cases were never committed, and §7.2's
live programme could not have been run as written: the live suite drove a raw
`AgentRunner`, so no live turn ever routed a skill, and none could activate one.
This patch closes that gap without changing the mechanism.

#### Tests added

**+58 deterministic tests**, all with injected doubles — no live model.

- `tests/test_agent_control_tools.py` (14) — the skill-agnostic half. Control
  dispatch bypasses the executor and appends its result to the turn transcript;
  all three pre-dispatch policies (parallel rejection, repeated-call detection,
  tool-call budget) still run *before* the handler; every control call increments
  `tool_calls_executed` and shares one budget with ordinary calls; the system
  suffix replaces rather than stacks; a runner with no handler dispatches
  normally.
- `tests/test_skill_activation.py` (31) — the skill half. Activation from no
  active skill replaces declarations, executor and system block together;
  replacement leaves exactly one `<active_skill>` wrapper and rebuilds the
  `RestrictedToolExecutor` over the *original* global executor, not the previous
  wrapper; a tool the new skill forbids is rejected before its handler; unknown
  name, malformed arguments and an exhausted `MAX_SKILL_ACTIVATIONS_PER_TURN` are
  all recoverable and keep the previously active skill; a refused activation does
  not consume budget; tracing carries `skill_activated`, `initial_skill`, final
  `selected_skill` and `skill_activations`; and the no-activation path still
  matches SPEC-012's composition exactly.
- `tests/test_skill_omission.py` (4) — skill omission when Tracker or the sandbox
  is unavailable.
- `tests/test_skill_loader.py` (+4) — the reserved-name collisions fail at
  startup, for both a registered tool and a skill package, with and without other
  packages present.
- `tests/test_eval_runner.py` (+5) — the two new expectation keys.

All 14 behaviours SPEC-018 §7.1 enumerated are covered.

#### Scripted eval cases added

The four §7.1 scenarios: `skill-activation-none-001`,
`skill-activation-replace-001`, `skill-activation-unknown-001`,
`skill-activation-cap-001`. `evaluate_expectation()` gained
`expected_final_skill` and `expected_activations` so a case asserts how the skill
decision *ended*, not only what the router picked.

#### Live eval path added

`run_live_skill_case()` and `_build_live_orchestrator()` in `evals/runner.py`
assemble the production turn exactly as `app.py` does — `app.omitted_skills`, the
real `SkillPackageLoader`, the real `SkillRouter` on the profile's own routing
deadline, the real `SkillTurnOrchestrator` under the host's own limits. The only
substitutions are a recording renderer and an in-memory trace sink. No routing or
activation semantics are reimplemented in the evaluation.

`tool_sequence` is read from the trace rather than from the recording executor:
a control tool never reaches an executor, so reading the executor would make
`activate_skill` invisible in the very measurement it is the subject of.

#### Commands

```bash
python -m pytest -q                                     # 694 passed, 29 skipped
python -m evals.runner --suite scripted                 # 40/40 passed
python -m evals.runner --suite live --profile {fast,mid,deep} --category skill_live_*
```

The 29 skips are the opt-in live Docker sandbox tests (`LLLM_SANDBOX_LIVE=1`),
unrelated to this patch.

#### Model & parameters (provenance)

- qwen3:8b (`fast`) — digest `500a1f067a9f`, 8.2B params, Q4_K_M, ctx 40960
- qwen3:14b (`mid`) — digest `bdbd181c33f2`, 14.8B params, Q4_K_M, ctx 40960
- qwen3:32b (`deep`) — digest `030ee887880f`, 32.8B params, Q4_K_M, ctx 40960
- Ollama **0.32.15** — note this differs from the 0.31.1 recorded above; the
  parent entry's numbers were taken under the older runtime.
- Sampling: model defaults, unchanged — `llm.py` still sets no `options`.
  Reported by `/api/show`: temperature 0.6, top_k 20, top_p 0.95,
  repeat_penalty 1. **No `num_predict` is set anywhere**, which turns out to
  matter (see the routing defect below).

#### A. Skill coverage

Not measured as a number, and deliberately not fabricated.

The historical pre-SPEC-018 baseline does not exist and cannot be reconstructed
without building artificial infrastructure: before this patch no live turn routed
a skill at all. What can be stated is narrower and load-bearing: `SkillRouter` is
untouched by SPEC-018, and a turn in which the model never calls `activate_skill`
composes byte-identically to the SPEC-012 path — asserted deterministically by
`TestNoActivationIsByteIdentical`. Live, the three single-skill cases
(`skill-live-none-001`, `skill-live-sales-001`, `skill-live-tracker-001`) routed
correctly and completed on all three profiles with zero activations, so coverage
did not decrease.

| case | fast | mid | deep |
|---|---|---|---|
| `skill-live-none-001` (no skill) | PASS 16.8s | PASS 43.0s | PASS 72.6s |
| `skill-live-sales-001` (`sales_analysis`) | PASS 20.8s | PASS 45.8s | PASS 158.5s |
| `skill-live-tracker-001` (`tracker_read`) | PASS 90.0s | PASS 77.4s | PASS 144.8s |

#### B. Does the model actually call `activate_skill`?

**No profile calls it spontaneously. All three call it correctly when told to.**

The router-routed cross case `skill-live-cross-001` never reached the agent loop
at all — it times out during *routing* on every profile (see the defect below),
so it answers nothing about activation. To get past that, the same task was run
through `skill-live-cross-explicit-001`, whose prompt names the skill
(`use the tracker_read skill`), which `parse_explicit_selection` resolves without
a routing model call at all — `routing_requests: 0`.

Spontaneous activation, committed deadlines:

| profile | initial | `activate_skill` | tool sequence | final | outcome | duration |
|---|---|---|---|---|---|---|
| fast | `tracker_read` (explicit) | **not called** | `mcp_tracker__issue_get` | `tracker_read` | completed/final_answer | 126.8s |
| mid | `tracker_read` (explicit) | **not called** | `mcp_tracker__issue_get` | `tracker_read` | completed/final_answer | 129.3s |
| deep | `tracker_read` (explicit) | **not called** | `mcp_tracker__issue_get` | `tracker_read` | completed/final_answer | 215.8s |

Every profile answered the Tracker half of a two-part request and ended the turn
rather than reaching for the second skill. The turns *completed* — this is not a
failure mode, it is a choice.

This was verified to be a model decision, not a wiring defect. The declarations
actually reaching the live model on the first request are:

```text
mcp_tracker__issue_get, mcp_tracker__issues_find,
mcp_tracker__queue_get_metadata, mcp_tracker__issue_get_comments,
activate_skill
```

`activate_skill` is present. The system block, however, is entirely
`tracker_read`'s own instruction, which says nothing about the possibility of
switching skills; the tool declaration is the model's only signal that switching
exists.

Instructed activation, committed deadlines
(`skill-live-activation-forced-001`, prompt names the tool explicitly):

| profile | tool sequence | activation event | final | outcome | duration |
|---|---|---|---|---|---|
| fast | `mcp_tracker__issue_get` → `activate_skill` → `sql_query` | `sales_analysis` replaces `tracker_read`, index 1, `recomposed: true` | `sales_analysis` | completed/final_answer | 111.2s |
| mid | `mcp_tracker__issue_get` → `activate_skill` → `sql_query` | idem | `sales_analysis` | completed/final_answer | 142.3s |
| deep | `mcp_tracker__issue_get` → `activate_skill` → `sql_query` | idem | `sales_analysis` | completed/final_answer | 369.2s |

This is exactly the target shape §7.2 B described, and it is identical on all
three profiles: the mechanism works end-to-end against real models, through the
real orchestrator, including the post-activation `sql_query` that `tracker_read`
would have forbidden.

The finding to carry forward is therefore precise: **the mechanism is sound; the
prompt contract is what does not yet invite its use.** No prompt was modified to
improve this result, per the patch's own constraint.

#### C. Activation / prefix-reprefill cost

Measured, with an explicit limitation.

Per-model-request latency for the instructed-activation turn, with the request
that follows the recomposed system block marked:

| profile | req 1 | req 2 | req 3 (post-activation) | req 4 | total |
|---|---|---|---|---|---|
| fast | 43.1s | 26.6s | **31.3s** | 9.5s | 111.2s |
| mid | 36.8s | 48.5s | **35.2s** | 20.8s | 142.3s |
| deep | 128.0s | 125.0s | **73.7s** | 41.7s | 369.2s |

The no-activation arm (`skill-live-cross-explicit-001`) ran 2 model requests and
1 tool call; the activation arm ran 4 requests and 3 calls. **The two arms do not
perform the same work, so their totals are not a reprefill measurement and no
such claim is made here.** Within a turn, the post-activation request is not an
outlier on any profile — on `deep` it is the fastest of the first three. Whatever
prefix rebuilding costs, it is not visible above the run-to-run variance of these
models at this instrumentation's resolution. Isolating it would need a controlled
arm that performs identical work with `recomposed: false`, which this patch did
not build.

#### Defect found (not fixed here): unbounded routing generation

`skill-live-cross-001` fails on all three profiles, 5/5 runs, each terminating at
exactly its profile's routing deadline — `skill_routing_timeout` at 30.0s / 40.0s
/ 60.0s. This is **not** a SPEC-018 regression: it is in SPEC-012's router, which
this patch is forbidden to change.

What the diagnosis showed, on `fast` with the two-part prompt:

- The router does eventually produce the **correct** answer —
  `{"skill": "tracker_read", "reason": "..."}`, 113 characters — but took
  **107.9s** to emit it, against a 30s deadline calibrated (`config.py:31`) on a
  measured ~3.6s warm routing response.
- With the routing deadline raised to 600s purely as a diagnostic, one routing
  request still exhausted it. The Ollama server log shows that call reaching
  **14,760 tokens over 9m58s**.
- The time is spent in thinking tokens. `llm.py:73` documents that
  `message.thinking` is deliberately never read, so those tokens are invisible in
  the text stream while still costing wall-clock. Nothing bounds them:
  `MAX_SKILL_ROUTING_RESPONSE_CHARS` is applied only to an already-received
  response when composing a repair message, and no `num_predict` is set.
- Single-intent prompts are unaffected — `skill-live-tracker-001`, whose prompt
  is the first half of the cross prompt, routes and completes on all three
  profiles. The trigger is a multi-intent request.

Second-order consequence worth recording: `run_with_deadline` abandons rather
than cancels, by design (`config.py:74-77`). A timed-out routing call returns
control to the host but the server keeps generating, so the abandoned run
continues to occupy the GPU and slows whatever runs next.

Per the patch note ("record the defect and classify the corrective work
separately") this is left unfixed and handed to a follow-up patch against
SPEC-012. Both the deadline calibration in SPEC-017 and the missing generation
bound are candidate corrections; which is right is that patch's question, not
this one's.

#### Deviations and limitations

- **§7.2 A produced no baseline number.** No pre-SPEC-018 live coverage data
  exists and none was invented.
- **§7.2 B's router-routed case could not run**, blocked by the routing defect
  above. The question was answered through explicit selection instead, which
  bypasses the routing model entirely — a different path to the same turn shape,
  and stated as such rather than presented as the original case passing.
- **§7.2 C is bounded by instrumentation**, as described above.
- **Two committed live cases currently fail by design**, and the live suite is
  opt-in with no CI: `skill-live-cross-001` (blocked on the routing defect; it
  will pass when that is fixed, and is kept as its regression target) and
  `skill-live-cross-explicit-001` (asserts an activation the models do not choose
  to perform — kept because tuning its expectation down would erase precisely the
  negative result this patch exists to record).
- **`TRACKER_SMOKE_ISSUE_ID` was set to a real issue** for this run. It lives in
  the git-ignored `.env`; the live Tracker cases are unrunnable without it.
- Ollama moved 0.31.1 → 0.32.15 between the parent entry and this one.

#### Outcome

Every deterministic acceptance criterion in the patch note is met: the §7.1 suite
is committed and green (694 passed), the four scripted cases are committed and
green (40/40), and the live path executes real turns through the real
`SkillTurnOrchestrator`.

No production *behaviour* changed. The one edit outside `evals/`, tests and
documentation is a pure extraction in `app.py`: the skill-omission rule moved
from `main()`'s body into `omitted_skills()`, unchanged, so the live evaluation
composes its skill registry by calling the application's own rule instead of
copying it. A second copy would drift, and a live skill measurement taken
against a different catalog would not describe the real app. The extracted
function is covered by `tests/test_skill_omission.py`.

The two substantive findings are both negative, and both are kept:

1. Models on all three profiles use `activate_skill` correctly when told, and
   none reach for it on their own. SPEC-018 §9's parked question — whether
   `SkillRouter` can eventually be retired — must stay closed: on this evidence
   the router is currently the *only* thing selecting a skill in practice.
2. Multi-intent prompts can drive the routing model past every profile's
   deadline, because nothing bounds its generation. This is the more urgent of
   the two, and it belongs to SPEC-012.

#### Commits

- Implementation: `c6acb0a` — Complete mid-turn skill activation verification (PATCH-018-01)
- Merge (`--no-ff`): `c3a94dc` — Merge PATCH-018-01: complete skill activation verification

### PATCH-018-02 — Activate a skill on a capability gap

Patch note: `patches/SPEC-018/PATCH-018-02-Activate-Skill-On-Capability-Gap.md`

Standalone journal: [PATCH-018-02](patches/PATCH-018-02-activate-skill-on-capability-gap.md)

This step built the escape hatch and PATCH-018-01 proved it works. What this
patch fixes is discovery: live, a single-turn two-phase request (summarise
DEV-498's comments *and* write a CSV) routed to `tracker_read` and the model
reported the write phase impossible, though `activate_skill` was declared for the
turn all along. The declaration's trigger asked for a *reclassification* — false
after a correct selection — and `<active_skill_policy>` asserted the closed tool
set three times without ever mentioning that the skill can be replaced. The
trigger is now a capability gap, and the policy says the skill is not the whole
session. Measured live: 4 of 7 runs activate mid-turn, against 0 of 5 before.
Mechanism, contracts, tool views, and budgets are unchanged.

#### Commits

- Implementation: `07915f5` — Trigger skill activation on a capability gap (PATCH-018-02)
- Merge (`--no-ff`): `MERGE_SHA`
