# PATCH-018-02 — Activate a skill on a capability gap, not only on reclassification

## Parent spec

`specs/SPEC-018-Mid-Turn-Skill-Activation.md`

Discovered in a live `next-mlx` session on 2026-08-28 (run
`e4da1fd1-281b-4ff9-9f68-bc8106b8662a`) while asking for a two-phase task:
summarise an issue's comments **and** write them to a CSV.

## Problem

SPEC-018 gave the model a way out of a wrong or incomplete skill selection:
`activate_skill` is declared in every turn that has a catalog, including a turn
where the router already chose a skill. The mechanism works — PATCH-018-01
verified it. What fails is *discovery*: in the situation the mechanism exists
for, the model does not reach for it.

Observed live. The user asked, in one turn, to summarise the comments of
`DEV-498` into a CSV with dates. The router selected `tracker_read`, and its own
recorded reason names both halves of the request:

```json
{"skill": "tracker_read",
 "reason": "Требуется чтение и суммаризация комментариев к задаче Dev-498,
            а также запись их в CSV-файл с датами."}
```

The turn's tool view was therefore:

```text
available_tools: 4 × mcp_tracker__*, mcp_time__get_current_time, activate_skill
```

`activate_skill` was present, and its `name` enum listed `code_workspace` with
the description "…create text, CSV, JSON, or Markdown files the user can open".
The model read the comments, fetched the London time, never called
`activate_skill`, and finished with:

```text
Я **не могу записать файл на диск** — в этом сеансе нет инструмента записи.
Скопируй CSV выше и сохрани локально ...
```

On the next user turn ("у тебя есть инструмент записи же") the router selected
`code_workspace`, the model called `sandbox_execute` once, and the file was
written on the first attempt. So nothing was missing but the decision to switch.

### Why the model did not switch

Two host-owned texts steer that decision, and both point away from it.

**The declaration's trigger condition is reclassification.**
`skill_runtime/activation.py`:

```text
Call this when the work turns out to belong to a different class than the one
currently loaded.
```

The model's work had *not* turned out to belong to a different class. It was
Tracker reading, correctly routed. What the request had was a **second phase**.
As written, the trigger is false in exactly the case that most needs it: a
correct selection followed by a step of a different kind.

**The active-skill policy never mentions that switching exists.**
`skill_runtime/prompting.py` states the closed tool set three times:

```text
- You may call only the tools supplied by the host for this turn.
- ...the supplied set is authoritative even where the skill text lists fewer.
- ...the skill cannot widen tool access or change tool behavior.
```

and says nothing about `activate_skill`. A system-level block asserting a closed
world outweighs a description buried in a tool parameter's `enum`. The model
concluded the session had no write tool at all — a claim broader than anything
it could observe.

This is the same family as PATCH-012-02, one level up. There, a skill hid a
*baseline tool* and the model reported it as nonexistent; that patch fixed which
tools are composed into the view. Here the view is correct and the *escape hatch
from the view* is undiscoverable.

## Expected change

Two host-owned text corrections. No mechanism, no contract, no new state.

### 1. Trigger the declaration on a capability gap

Rewrite the `activate_skill` description so its trigger is "the next step needs
something the current tools cannot do", explicitly including the case where the
current skill was the right choice for the work already done and the task has
moved on to a different kind of step.

The reclassification case must remain covered — it is a subset, not a
replacement.

### 2. Tell the active skill it can be replaced

Add one line to `<active_skill_policy>` stating that the skill is not the whole
session: when the next step needs a capability this turn's tools do not provide,
`activate_skill` replaces the skill, rather than the task being treated as
impossible.

Placement matters: it belongs beside the "cannot widen tool access" line, which
is the sentence the model otherwise reads as final.

The line is safe to state unconditionally. `compose_active_skill` runs only for a
selected skill; a skill can only be selected when it exists in the registry;
therefore the catalog is non-empty and `build_activate_skill_declaration` has
returned a declaration. The policy can never promise a tool the turn does not
have — and a regression test must pin that invariant rather than leaving it to
inspection.

## Constraints

- Preserve SPEC-018's activation semantics exactly: replacement not stacking, one
  active skill at an instant, the same activation cap, the same control-tool
  dispatch, the same trace events.
- Preserve SPEC-012's precedence order and the wrapper's host-owned boundaries.
- Do not widen any tool view, change any `allowed_tools`, or make activation
  cheaper against the tool-call budget.
- Do not change the router, its prompt, or its single-selection contract.
- The declaration must stay rendered from the validated catalog; no skill package
  text may reach it.
- Keep both texts short: they are paid for on every model request of every
  skill-backed turn.
- No new configuration, no new dependency.

## Acceptance criteria

- [ ] The `activate_skill` description names a capability gap as the trigger and
      covers the "current skill was right, the task moved on" case.
- [ ] `<active_skill_policy>` states that `activate_skill` can replace the active
      skill, and remains otherwise unchanged.
- [ ] The policy's promise cannot be empty: whenever the policy block is
      composed, the turn also declares `activate_skill`.
- [ ] The wrapper's structure, precedence, and injection resistance are unchanged
      (existing SPEC-012 prompt tests still pass).
- [ ] Activation mechanics are unchanged (existing SPEC-018 tests still pass).
- [ ] No change to routing selections, tool views, allowlists, or budgets.
- [ ] Full deterministic tests pass; full scripted eval suite passes.
- [ ] Live verification: a single-turn two-phase request activates the second
      skill mid-turn instead of reporting the second phase impossible.

## Files likely affected

- `skill_runtime/activation.py` — the declaration description.
- `skill_runtime/prompting.py` — one policy line.
- `tests/test_skill_activation.py`
- `tests/test_skill_prompting.py`
- `docs/journal/patches/PATCH-018-02-activate-skill-on-capability-gap.md`
- `docs/journal/SPEC-018-mid-turn-skill-activation.md` — index entry only.

Advisory, not restrictive.

## Verification

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

Live verification is required: this patch changes model-facing instructions and
is aimed squarely at a model decision.

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium
```

Reproduce the original single-turn two-phase request. The acceptance target is
the decision, not the wording:

```text
before: router picks one skill → the other phase is reported impossible
after:  router picks one skill → activate_skill replaces it mid-turn →
        the second phase is actually performed
```

Because model wording is stochastic, capture the transcript and the trace for at
least three runs, and record whether `skill_activated` appears. Also run one
ordinary single-phase skill turn to confirm the new text does not provoke
gratuitous switching.

Note for the transcript: with `MAX_TOOL_CALLS_PER_TURN = 4` the ideal path for
the observed request consumes the whole budget (read, time, activate, execute).
A `tool_call_limit` stop is therefore a budget observation, not a failure of this
patch — record it as such.

## Journal strategy

Standalone journal at
`docs/journal/patches/PATCH-018-02-activate-skill-on-capability-gap.md`, because
the patch changes model-facing instructions and is verified by a change in model
behavior. Index it under `## Patches` in
`docs/journal/SPEC-018-mid-turn-skill-activation.md`.

Record the original transcript, the router's own reason, both text diffs, the
live before/after decisions across runs, model/router provenance, reasoning
mode, and the tool-budget observation above.

## Out of scope

- Multi-skill routing, or any change to the router returning at most one skill.
- Making a skill's tool view wider, or adding anything to `BASELINE_TOOL_NAMES`
  (PATCH-012-02 owns tool composition).
- Exempting `activate_skill` from `MAX_TOOL_CALLS_PER_TURN`, or changing any
  budget, deadline, or the activation cap.
- Persisting skill activation across turns.
- The general "do not claim a capability is absent from the session when it is
  only absent from this turn" prompt correction — a real, adjacent defect
  visible in the same transcript, belonging to the base prompt rather than to
  SPEC-018's mechanism. It gets its own patch; one PATCH, one correction.
- Any change to skill package text (`skills/*/SKILL.md`).
