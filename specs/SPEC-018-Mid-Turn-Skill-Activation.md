# SPEC-018 — Mid-Turn Skill Activation

**Status:** Proposed  
**Step:** 18  
**Depends on:** SPEC-011, SPEC-012, SPEC-016  
**Target repository:** `egorgut/lLLM`

---

## 1. Summary

SPEC-012 gave the harness a skill layer whose selection happens exactly once, before
the agent loop starts: `SkillTurnOrchestrator.run_turn` routes, loads one `SkillSpec`,
composes the prompt, filters the tools, and then hands a frozen view to `AgentRunner`.
A turn is therefore permanently one skill or none, decided from the user message and
six context messages — before a single tool has run.

This step keeps that router exactly as it is and adds one host-handled tool,
`activate_skill`, that lets the model **change the active skill while the turn is
running**. The router remains the mandatory entry decision, so skill coverage cannot
regress; the tool only removes the ceiling of one skill per turn.

The model gains no new authority: names still come only from the validated registry,
a skill can still only narrow the global tool set, and the instruction still enters
the system-level context inside the host-generated wrapper. What changes is *when*
that composition may happen — once per turn becomes up to a bounded number of times.

---

## 2. Motivation

### 2.1 The routing decision is made before the evidence exists

`SkillTurnOrchestrator` selects from `conversation.latest_user_message` plus a slice of
prior messages (`skill_runtime/orchestrator.py:110-121`). At that moment no tool has
executed, no row has been read, no repository has been inspected. The router judges the
request by its wording alone.

For narrow, well-worded requests that is enough. For the multi-step work the project is
moving toward it is not: a request that looks like a tracker question until the issue is
read, and then becomes a code question, is routed once and stays routed. The model can
neither correct a defensible-but-wrong initial guess nor progress from one procedure to
the next.

### 2.2 One skill per turn is now the binding constraint

SPEC-012 §3 fixed `selected_skill: str | None` for good reasons — one instruction at a
time keeps precedence unambiguous and tool restrictions easy to reason about. Those
reasons are about *simultaneity*, not about *immutability*. A turn that runs
`tracker_read` and then `code_workspace` in sequence never has two instructions active
and never has an ambiguous allowlist; it is simply outside what the current shape can
express.

Today the only way to spend two procedures on one task is to make the user send two
messages, which splits work that is conceptually one turn across two whole-turn budgets
and two independent routing decisions.

### 2.3 The router stays because coverage is the priority

The obvious alternative — retire the router and let the model activate skills entirely
on its own — was considered and rejected for this step. A router asked one narrow
question always answers it; a model in the middle of solving a task may simply answer
without reaching for a procedure, and on the default `fast` profile (`qwen3:8b`) that
risk is real. The failure would also be silent: not an error, just an answer produced
without the skill's checks, format, or completion criteria.

This step therefore adds capability without touching the guarantee. §7.2 measures
whether the model uses the new tool at all; retiring the router remains a separate,
evidence-led decision (§9).

---

## 3. Goals

### 3.1 Functional

- Introduce one host-handled tool, `activate_skill`, whose catalog is the validated
  `SkillRegistry` catalog.
- Allow the model to activate a skill mid-turn when the router selected none, and to
  replace the active skill when the router selected one.
- Keep exactly zero or one skill active at any instant; replacement never stacks.
- Bound activations per turn and make an exhausted budget recoverable, not terminal.
- Make an unknown skill name a recoverable tool error rather than a turn failure.
- Trace every activation, and report both the initial and the final skill of a turn.
- Show a skill switch in the CLI as clearly as the initial `[skill]` line.

### 3.2 Architectural

- `agent.py` must remain skill-agnostic. The loop learns the general notion of a
  *host-handled tool that may change the turn's view*; it learns nothing about skills.
  SPEC-012 §2's dependency direction is preserved.
- Reuse `compose_active_skill`, `declarations_for_names`, and `RestrictedToolExecutor`
  unchanged; this step adds a seam, not a second skill mechanism.
- No change to the skill package format, `SkillSpec`, the loader, the registry, or the
  router's own contract.
- A turn in which the model never calls `activate_skill` must behave exactly as it does
  today, byte for byte in the model-facing context.
- Framework-free, standard library only.

### 3.3 Non-goals

Listed in §9.

---

## 4. Functional requirements

### 4.1 The host-handled tool seam

`AgentRunner` gains one optional dependency describing tools it must not dispatch to
`ToolExecutor`, because handling them changes the turn itself:

```python
@dataclass(frozen=True)
class ControlResult:
    result: dict[str, Any]                  # model-facing tool result
    tools: tuple[dict[str, Any], ...] | None = None   # new declarations, None = unchanged
    executor: Any | None = None             # new executor, None = unchanged
    system_suffix: str | None = None        # new system-level block, None = unchanged


class ControlToolHandler(Protocol):
    names: frozenset[str]
    def handle(self, name: str, arguments: dict[str, Any]) -> ControlResult: ...
```

Loop behavior when the requested tool name is in `handler.names`:

1. every policy that already precedes dispatch still runs first, in the existing order —
   parallel-call rejection, repeated-call detection, the tool-call budget;
2. the handler is invoked instead of `ToolExecutor.execute`;
3. any non-`None` field of the returned `ControlResult` replaces the loop's working
   declarations, working executor, or the system-level block of `working_messages[0]`;
4. `result` is appended as the tool result message exactly as an ordinary tool result is.

The handler runs under the same tool-execution deadline as any other tool. The runner
knows nothing about what a control tool *means*; `skill_runtime` supplies the only
implementation.

### 4.2 The `activate_skill` declaration

Host-generated from `SkillRegistry.catalog()`, never from a skill package:

```json
{
  "type": "function",
  "function": {
    "name": "activate_skill",
    "description": "Load the working procedure for one class of task, replacing any procedure currently active. Call this when the work turns out to belong to a different class than the one currently loaded.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {
          "type": "string",
          "enum": ["code_workspace", "sales_analysis", "tracker_read"],
          "description": "code_workspace: <description> | sales_analysis: <description> | tracker_read: <description>"
        }
      },
      "required": ["name"],
      "additionalProperties": false
    }
  }
}
```

- `enum` and the per-name descriptions are rendered from the catalog at startup; they
  are the same compact name/description pairs the router receives, so SPEC-012 §4's
  two-phase loading is preserved — no full instruction reaches the model unactivated.
- `activate_skill` is a reserved name. Startup must reject a registered tool or a skill
  named `activate_skill`, alongside the existing duplicate-name checks.
- The declaration is appended to the model-facing tool list in **both** cases: when the
  router selected no skill, and when it selected one. It is never part of any skill's
  `allowed_tools` and is never removed by `RestrictedToolExecutor`.
- When the registry is empty, the declaration is not produced at all.

### 4.3 Activation semantics

On `activate_skill{"name": N}` the handler, in order:

1. resolves `N` by exact registry lookup — no filesystem path is ever built from model
   text (SPEC-012 §7);
2. builds the new declarations with `declarations_for_names(registry, spec.allowed_tools)`
   and appends the `activate_skill` declaration;
3. builds a fresh `RestrictedToolExecutor` over `spec.allowed_tools` **plus** the
   reserved name, wrapping the *original* global executor — never the previous
   restricted wrapper, so restrictions cannot accumulate;
4. builds the new system-level block with `compose_active_skill(spec)`, which replaces
   the previous block entirely;
5. returns the model-facing result.

The result is a receipt, not the instruction — the instruction went to the system layer,
and duplicating it would both waste context and weaken the precedence SPEC-012 §10
establishes:

```json
{
  "ok": true,
  "skill": "code_workspace",
  "version": "1",
  "replaced": "tracker_read",
  "available_tools": ["run_python", "activate_skill"]
}
```

`replaced` is `null` when no skill was active.

### 4.4 Replacement, not composition

At most one skill is active at any instant. Activating a second skill removes the first
one's instruction from the system block and its tools from the view. There is no stack,
no inheritance, and no union of allowlists — SPEC-012 §3 and §9 hold unchanged.

Activating the skill that is already active is not an error; it returns `ok: true` with
`replaced` equal to the same name and changes nothing. Repeating it is caught by the
existing repeated-call detection like any other tool.

### 4.5 Allowlist semantics

A skill activated mid-turn governs the turn **from that point forward**. Tools already
executed under a previous view are unaffected; they were legal when they ran.

Both SPEC-012 boundaries remain in force from the activation point: only the new
skill's declarations are sent, and the new `RestrictedToolExecutor` independently
rejects anything outside the allowlist. The effective set is still
`global registered tools ∩ active skill allowed tools`, plus the host's reserved
`activate_skill`. A skill can never widen access, and a mid-turn activation cannot
grant a tool the global registry does not hold.

### 4.6 Budgets

- An activation **counts** as one tool call against `MAX_TOOL_CALLS_PER_TURN`. It is a
  model decision that consumed a model request, and hiding it from the budget would let
  a thrashing model run unbounded.
- A new host-owned limit bounds activations independently:

  ```python
  MAX_SKILL_ACTIVATIONS_PER_TURN = 2
  ```

  Counted per turn, including the replacement of an already-active skill. The initial
  router selection does **not** count against it.
- Exceeding the limit is **recoverable**: the handler returns
  `{"ok": false, "error": "activation_limit", ...}` naming the active skill, the turn
  continues under the current view, and the model may still finish. It does not
  terminate the turn.
- Activation consumes no additional whole-turn budget beyond the model request and the
  handler's own work; the shared deadline from `TurnContext` is authoritative as before.

### 4.7 Errors

| Condition | Handling |
| --- | --- |
| unknown skill name | recoverable tool result `{"ok": false, "error": "unknown_skill", "available_skills": [...]}` |
| activation limit reached | recoverable tool result `{"ok": false, "error": "activation_limit", "active_skill": "..."}` |
| name is the reserved `activate_skill` | recoverable tool result `unknown_skill` |
| registry lookup fails after validated startup | terminal `failed/skill_load_error`, unchanged |
| a disallowed tool is called after activation | terminal `stopped/skill_policy_violation`, unchanged |

No new `TerminationReason` is introduced. The recoverable rows are deliberate: a wrong
name is exactly the kind of mistake the agent loop already recovers from for every other
tool, and killing a turn over it (as routing does under SPEC-012 §17) is a heavier
response than the failure warrants.

### 4.8 Prompt composition

`compose_active_skill` is unchanged. The host rewrites the system-level block of
`working_messages[0]` so that it holds the base `SYSTEM_PROMPT` plus at most one
`<active_skill>` wrapper. Two wrappers must never coexist, and a stale wrapper must
never survive a replacement.

Rewriting the leading system message invalidates the model's cached prefix for the rest
of the turn. This cost is accepted deliberately: the alternative — delivering the
instruction as tool-result content — would place trusted host configuration in the
channel the model is told to treat as observed data, contradicting SPEC-012 §10-11.
§7.2 records the measured cost so a future step can revisit the trade with numbers.

### 4.9 Tracing

New event, emitted by the handler:

```json
{
  "schema_version": 1,
  "event": "skill_activated",
  "run_id": "...",
  "turn_id": "...",
  "skill": "code_workspace",
  "skill_version": "1",
  "skill_fingerprint": "sha256:...",
  "replaced_skill": "tracker_read",
  "activation_index": 1,
  "source": "tool",
  "allowed_tools": ["run_python"]
}
```

Changes to existing events:

- `turn_finished.selected_skill` means **the skill active when the turn ended**. Two
  additive fields preserve what is otherwise lost: `initial_skill` (the router's
  selection, `null` if none) and `skill_activations` (count, `0` for every turn that
  behaves as today).
- `skill_loaded` and `skill_toolset_resolved` are emitted for a mid-turn activation
  exactly as they are for the router's selection, so one trace still reconstructs every
  view the turn ran under.
- SPEC-012's payload policy is unchanged: no full `SKILL.md`, no full schema, no
  catalog dump.

### 4.10 Conversation and persistence

Unchanged. Activation is ephemeral turn state (SPEC-012 §14): it is not persisted as a
semantic message, and the next user turn is routed again from scratch. A non-successful
turn still rolls back the tentative user message regardless of how many skills it
activated.

### 4.11 CLI

A mid-turn switch must be as visible as the initial selection:

```text
[skill] tracker_read
[tool 1/6] mcp_tracker__get_issue
[result] ...
[skill] code_workspace (replacing tracker_read)
[tool 2/6] run_python
```

---

## 5. Design constraints

- **Host owns identities.** Skill names reach the handler only as data validated against
  the registry (SPEC-012 §7).
- **Skills only narrow.** No activation may widen the effective tool set (SPEC-012 §9).
- **Precedence is unchanged.** Host safety and tool contracts > active skill > user
  request (SPEC-012 §11).
- **`agent.py` stays skill-agnostic.** It may know about host-handled tools; it may not
  import `skill_runtime`.
- **The router is untouched.** `SkillRouter`, `parse_explicit_selection`, the routing
  timeout, and the repair policy keep their current behavior and contracts.
- **Silent no-op default.** If the model never calls `activate_skill`, the turn's
  model-facing context, trace fields, and outcome must be identical to today apart from
  the added declaration and the additive `initial_skill` / `skill_activations` fields.
- Framework-free; no new third-party dependency.

---

## 6. Acceptance criteria

- [ ] `AgentRunner` accepts an optional control-tool handler and dispatches reserved
      names to it instead of `ToolExecutor`, after all existing pre-dispatch policies.
- [ ] `agent.py` contains no import from `skill_runtime` and no skill-specific logic.
- [ ] `activate_skill` is declared to the model whenever the registry is non-empty, both
      with and without a router-selected skill, and is absent when the registry is empty.
- [ ] Startup fails if a registered tool or a skill package is named `activate_skill`.
- [ ] Activating a skill mid-turn replaces the system-level `<active_skill>` block, the
      tool declarations, and the executor, with no stacking and no stale wrapper.
- [ ] The new restricted executor wraps the original global executor, never a previous
      restricted wrapper.
- [ ] `activate_skill` remains callable after an activation, so replacement is possible.
- [ ] A tool outside the newly active allowlist is rejected before its handler runs and
      produces `stopped/skill_policy_violation`.
- [ ] An unknown skill name returns a recoverable tool error listing valid names and does
      not terminate the turn.
- [ ] Activations are capped by `MAX_SKILL_ACTIVATIONS_PER_TURN`; exceeding the cap is
      recoverable and leaves the current skill active.
- [ ] Each activation counts as one tool call against the per-turn budget.
- [ ] `skill_activated` is traced with the replaced skill and activation index;
      `turn_finished` reports `initial_skill`, `selected_skill` (final), and
      `skill_activations`.
- [ ] A turn with no activation produces `skill_activations: 0` and behaves exactly as
      today.
- [ ] The CLI shows a switch line distinct from the initial `[skill]` line.
- [ ] Routing, rollback, persistence, whole-turn deadline, and `duration_ms` semantics
      are unchanged.
- [ ] The existing deterministic suite passes unchanged, plus the new tests in §7.1.
- [ ] README documents mid-turn activation and the one-active-skill rule.
- [ ] No new third-party dependency.

---

## 7. Verification

### 7.1 Automated

New deterministic tests, no live model:

1. control-tool dispatch bypasses `ToolExecutor` and appends a tool result message;
2. pre-dispatch policies still fire first for a control tool (parallel, repeated, budget);
3. activation with no prior skill: declarations, executor, and system block all change;
4. replacement: previous wrapper removed, exactly one wrapper present;
5. replacement wraps the original executor, verified by allowing a tool the first skill
   forbade and the second permits;
6. a tool forbidden by the newly active skill is rejected before the handler runs;
7. unknown name → recoverable result, turn still reaches `completed`;
8. activation cap → recoverable result, previously active skill still active;
9. each activation increments `tool_calls_executed`;
10. no-activation turn is byte-identical in model-facing context to the SPEC-012 path;
11. empty registry → no `activate_skill` declaration;
12. reserved-name collision fails at startup;
13. trace assertions for `skill_activated`, `initial_skill`, `skill_activations`;
14. the full SPEC-011/012/016 regression suite.

Plus `python -m evals.runner --suite scripted` with new cases for: mid-turn activation,
replacement, unknown name recovery, and cap exhaustion.

### 7.2 Live — the milestone evidence

Two questions must be answered with numbers, on all three profiles
(`fast`, `mid`, `deep`):

**A. Coverage baseline and coverage after.** The share of turns in which the correct
skill was active. Run the committed live suite before the change (current `main`) and
after, on the same commit-pinned skills and MCP availability. Coverage must not drop —
that is the guarantee this step is built to preserve.

**B. Does the model use the new tool at all?** A live case that cannot be completed
under a single skill — read an issue via `tracker_read`, then perform work that requires
`code_workspace` — run per profile. Record whether `activate_skill` was called, at which
step, and whether the turn completed.

A profile on which the model never activates is a finding to record, not a blocker: the
router guarantee means such a profile simply behaves as it does today.

The journal must also record the measured prefix-reprefill cost from §4.8: turn duration
for an activating turn against a non-activating turn of comparable shape.

### 7.3 Journal

`docs/journal/SPEC-018-mid-turn-skill-activation.md`, with model provenance for every
profile exercised (`GET /api/tags`: name, digest, quantization, context length), the
coverage comparison from §7.2 A, the activation transcripts from §7.2 B, and the
reprefill measurement. As with SPEC-017, the journal is the artifact that makes this
step reproducible — the code alone is not.

---

## 8. Risks

- **The model never calls the tool.** Then the step is a no-op with a small context cost
  (one declaration). Acceptable by construction: the router guarantee means nothing
  regresses. §7.2 B measures it, and a negative result on `fast` is recorded rather than
  worked around.
- **Thrashing between skills.** Two skills whose descriptions overlap could invite
  alternation. Bounded three ways: `MAX_SKILL_ACTIVATIONS_PER_TURN`, the tool-call
  budget, and existing repeated-call detection. If thrashing appears with distinct
  descriptions, the finding belongs to the catalog, not to this mechanism.
- **Reprefill cost dominates on `deep`.** Rewriting the system block invalidates the
  cached prefix. Measured in §7.2; if the cost is severe, the follow-up is a bounded
  variant of §4.8's rejected alternative, decided with numbers rather than in advance.
- **Allowlist confusion.** The model may retry a tool that was available before the
  activation. It receives the ordinary policy rejection, which is a correct answer, but
  the wording of the skill-policy message should make the mid-turn narrowing legible.
- **Trace consumers reading `selected_skill`.** Its meaning narrows from "the turn's
  skill" to "the skill active at the end". `initial_skill` preserves the old value; the
  eval runner and any journal tooling must be updated together with the change.

---

## 9. Out of scope

- **Retiring the router.** Deliberately kept. Whether `activate_skill` could replace it
  entirely is a separate, evidence-led step gated on §7.2's coverage numbers.
- Multiple simultaneously active skills, skill composition, chaining, nesting, or
  skills invoking other skills — SPEC-012's non-goals stand.
- Persisting the active skill across turns, or any sticky skill state (SPEC-012 §14).
- Deactivation without replacement (`activate_skill(null)`): the model can already
  finish a turn under a skill it no longer needs, and an explicit "return to no skill"
  adds a state transition without a demonstrated need.
- A separate routing model, or any per-role model split — parked by SPEC-017 §9 and
  independent of this step.
- Changing the skill package format, `SkillSpec`, the loader, the registry, the router
  contract, tool schemas, or conversation persistence.
- Semantic or embedding-based skill selection.
- Adding new skill packages; this step changes the mechanism, not the catalog.
