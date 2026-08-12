# SPEC-018 — Mid-Turn Skill Activation

- **Spec:** [SPEC-018](../../specs/SPEC-018-Mid-Turn-Skill-Activation.md)
- **Date:** 2026-08-12
- **Branch:** feature/SPEC-018-mid-turn-skill-activation
- **Implementation commit:** `<pending>`
- **Merge commit:** `<pending>`

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
