# PATCH-012-02 — Compose Baseline Tools with Active Skills

## Parent spec

`specs/SPEC-012-Skills.md`

Parent journal:

`docs/journal/SPEC-012-skills.md`

Discovered through a live interactive session on 2026-08-27 using:

```text
agent profile: next-mlx
agent model: qwen3.8:27b-mlx
router profile: fast
reasoning: medium
```

The reproducer was an ordinary Tracker conversation in which the application
started with both Tracker and Time MCP connected, but selecting `tracker_read`
removed the Time tool from the model-facing tool set.

## Problem

SPEC-012 currently treats an active skill's `allowed_tools` as the complete tool
view for that turn.

That gives a useful security property — a skill cannot widen access to arbitrary
global tools — but it also means that selecting a domain skill removes safe,
general-purpose host capabilities that may be required to complete the domain
task correctly.

The observed failure is current-time reasoning inside `tracker_read`.

At application startup the host reports:

```text
[mcp] connected: time (1 tool)
[mcp] connected: tracker (... admitted tools ...)
```

The global registry therefore contains `mcp_time__get_current_time`.

However, once the router selects `tracker_read`,
`SkillTurnOrchestrator._run_with_skill()` builds the model-facing declarations
from only:

```text
spec.allowed_tools
+ activate_skill
```

and wraps the executor with the same restricted allowlist.

For `tracker_read`, `spec.allowed_tools` contains only the four Tracker read
operations. The skill instruction additionally states that
`mcp_time__get_current_time` does not belong to the skill.

The result is an inconsistent capability model:

```text
no active skill
    -> Time tool exists

tracker_read active
    -> Time tool disappears

next turn without tracker_read
    -> Time tool exists again
```

This is not merely cosmetic. A natural domain question can require both a domain
capability and a general utility capability:

```text
"Does Sergey have overdue tasks?"

Tracker data
    +
current date/time
    ->
overdue/not overdue
```

Under the current implementation the model can read deadlines but cannot obtain
the current date while the Tracker skill is active. It is then forced to either:

- ask the user for information the host already knows how to obtain;
- make a conditional answer;
- hallucinate the current date;
- or incorrectly claim that no Time tool exists.

The live reproducer exhibited all of these failure modes across adjacent turns.

### Root cause

The current composition rule is effectively:

```text
effective_tools = active_skill.allowed_tools
```

whereas the required rule is:

```text
effective_tools = host_baseline_tools + active_skill.allowed_tools
```

with `activate_skill` added as the existing host-owned control tool.

A selected skill should narrow **domain capabilities**, not erase safe baseline
utilities owned by the host.

### The same defect exists after mid-turn activation

SPEC-018 rebuilds the active tool view inside `SkillActivationHandler.handle()`.
Today that rebuild also uses only the newly activated skill's `allowed_tools`
plus `activate_skill`.

Therefore fixing only the initial `_run_with_skill()` path would leave a second
version of the same bug:

```text
turn starts with baseline Time available
    -> activate_skill(...)
    -> Time disappears again
```

PATCH-012-02 must fix both composition points with one shared rule.

## Expected change

Introduce one explicit, host-owned set of **baseline tools** that remain available
when a skill is active.

For this PATCH the baseline set is intentionally minimal:

```text
mcp_time__get_current_time
```

No other tool becomes baseline in this PATCH.

The effective model-facing tool set for an active skill becomes:

```text
skill-specific tools
+ host baseline tools
+ activate_skill
```

The effective executor allowlist must be identical to the declarations the model
sees.

### 1. Add a host-owned baseline tool configuration

Define one explicit configuration value, for example:

```python
BASELINE_TOOL_NAMES = (
    "mcp_time__get_current_time",
)
```

The exact identifier may differ if an existing project naming convention fits
better, but the semantics must be explicit:

- owned by the host, never by a skill;
- fixed at startup;
- not user-configurable through chat;
- not model-configurable;
- validated against the final tool registry after MCP registration;
- deterministic in order;
- deliberately small.

Do not infer baseline status from tool names, MCP server names, descriptions, or
whether a tool happens to be read-only.

Adding another baseline capability later must require an explicit repository
change and review.

### 2. Compose, do not replace

Create one reusable composition helper at the skill-runtime boundary, conceptually:

```text
compose_skill_tool_names(
    skill_tools,
    baseline_tools,
    activate_skill,
)
```

It must:

- preserve deterministic order;
- remove duplicates deterministically;
- reject unknown configured names rather than silently dropping them;
- produce the same effective set for model declarations and executor policy.

Do not duplicate the union logic independently in the initial-selection and
mid-turn-activation paths.

### 3. Initial skill selection keeps baseline tools

When `_run_with_skill()` builds the selected skill view, it must expose:

```text
spec.allowed_tools
+ BASELINE_TOOL_NAMES
+ activate_skill
```

and the `RestrictedToolExecutor` must permit exactly the same effective names.

Example for `tracker_read` after this PATCH:

```text
mcp_tracker__issue_get
mcp_tracker__issues_find
mcp_tracker__queue_get_metadata
mcp_tracker__issue_get_comments
mcp_time__get_current_time
activate_skill
```

The exact declaration order may preserve the current skill-first order; what
matters is that it is deterministic and identical wherever the effective view is
constructed.

### 4. Mid-turn skill replacement keeps baseline tools

`SkillActivationHandler` must receive or otherwise use the same host-owned
baseline set.

After every successful `activate_skill`, the rebuilt declarations and restricted
executor must still contain the baseline tools.

Conceptually:

```text
tracker_read
    -> tracker tools + time + activate_skill

activate_skill("sales_analysis")
    -> sales tools + time + activate_skill
```

The old skill's domain tools disappear, as today. The host baseline does not.

Do not stack prior skill tools across activations.

### 5. Keep the skill package schema unchanged

Do **not** add a new `baseline_tools`, `utility_tools`, `inherits`, or similar field
to `SKILL.md` front matter.

A skill remains responsible only for declaring the domain tools it needs through
`allowed_tools`.

Baseline capability ownership belongs to the harness, because a skill must never
be able to grant itself additional authority.

### 6. Remove contradictory skill wording

The host-generated active-skill policy already says that the model may call only
the tools supplied by the host for the turn. Extend or clarify that policy so the
model understands the distinction:

```text
skill-specific allowed tools
vs
host-supplied baseline utilities
```

The host-supplied effective tool declarations remain authoritative.

Update current skill prose only where it contradicts this model.

In particular:

- `skills/tracker_read/SKILL.md` must stop explicitly saying that
  `mcp_time__get_current_time` can never be called while this skill is active;
- `skills/sales_analysis/SKILL.md` must not describe its front-matter
  `allowed_tools` as excluding host baseline utilities.

Do not broaden the domain responsibilities of either skill. A pure current-time
question still should not route to `sales_analysis` or `tracker_read` merely
because Time is available while those skills are active.

### 7. Make the effective capability view observable

`skill_toolset_resolved.available_tools` must continue to report the actual tools
presented to the model, now including baseline tools.

Add an additive trace field if useful, for example:

```text
baseline_tools
```

so a trace can distinguish:

```text
skill domain tools
host baseline tools
effective tools
```

The `activate_skill` model-facing receipt must also report the actual effective
`available_tools` after activation. It must not tell the model that only the
skill-specific tools are available when Time is in fact still present.

## Architectural invariant after this PATCH

For every active skill:

```text
GLOBAL TOOL REGISTRY
        |
        +-- host baseline subset --------------------+
        |                                            |
        +-- active skill domain subset ----+         |
                                           |         |
                                           v         v
                                      EFFECTIVE TOOL VIEW
                                           +
                                      activate_skill
```

A skill can still only reduce access to domain capabilities.

It cannot:

- add a baseline tool;
- remove a baseline tool;
- register a tool;
- alter a tool contract;
- gain another skill's domain tools.

The host alone decides the baseline set.

## Required deterministic tests

Add regression coverage for the composition rule.

At minimum:

1. with `tracker_read` active, the declarations contain all four Tracker read
   tools, `mcp_time__get_current_time`, and `activate_skill`;
2. with `tracker_read` active, the restricted executor successfully dispatches
   `mcp_time__get_current_time`;
3. with `tracker_read` active, unrelated global domain tools remain forbidden,
   including at least `sql_query` and `python_calculate` when they are not part of
   the baseline;
4. with `sales_analysis` active, its SQL/calculation tools and Time are available,
   but Tracker tools are still forbidden;
5. a successful mid-turn activation preserves Time while replacing the previous
   skill's domain tools;
6. repeated activation does not duplicate Time or `activate_skill` declarations;
7. a baseline tool that is also present in a skill's explicit allowlist is
   deduplicated deterministically;
8. an unknown host-configured baseline tool fails deterministically during
   startup/validation rather than being silently omitted;
9. the no-skill path preserves the existing global-tool behavior;
10. `skill_toolset_resolved.available_tools` reflects the effective composed set;
11. the activation receipt's `available_tools` reflects the effective composed
    set after replacement;
12. existing skill-policy rejection still occurs before the underlying handler
    runs for a non-baseline, non-skill tool;
13. all existing SPEC-012 and SPEC-018 tests continue to pass.

Prefer deterministic doubles. No live model is required for these policy tests.

## Scripted regression case

Add one committed skill-aware scripted case representing the defect shape.

Suggested shape:

```text
user:
"Check the Tracker issue deadline and tell me whether it is overdue as of now."

router selection:
tracker_read

model decisions:
1. call Tracker read tool
2. call mcp_time__get_current_time
3. answer
```

The case must assert at least:

```text
initial_skill = tracker_read
final_skill = tracker_read
skill_activations = 0
required tools include Tracker + Time
turn outcome = completed
```

This case exists to prove that one active skill can use one domain capability and
one host baseline capability in the same turn without `activate_skill`.

## Live verification

Live verification is required because this PATCH changes the tool declarations a
real model receives under an active skill.

Reproduce the original failure shape using the current Qwen3.8 MLX path:

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium
```

Use a real Tracker query whose answer depends on the current date/time, for
example the original environment's overdue-task question:

```text
Find the open tasks in queue PUR and tell me which are overdue as of now.
We work in Moscow time.
```

Expected behavior:

```text
[skill] tracker_read
...
[tool] one or more Tracker read calls
...
[tool] mcp_time__get_current_time
...
final answer based on both observations
```

The exact tool order is model-owned and need not be fixed.

The verification passes when:

- the initial skill remains `tracker_read`;
- Time is available without leaving or replacing `tracker_read`;
- the model can actually execute Time through the restricted executor;
- the final answer no longer claims that the Time capability does not exist;
- no unrelated domain tool becomes available because of the PATCH.

If the live model sees Time but elects not to call it for a prompt where current
time is genuinely necessary, record that as model-behavior evidence. Do not widen
the PATCH into prompt tuning solely to force the call unless the host capability
contract itself is shown to be unclear.

Also run the existing skill-aware live suite under the profiles that are practical
on the development machine, including the current `next-mlx` + `fast` split-role
combination.

## Constraints

- Preserve SPEC-012's skill architecture and two-phase loading model.
- Preserve SPEC-018's `activate_skill` semantics.
- `agent.py` must remain skill-agnostic.
- A skill must still never widen global authority.
- Baseline tools are host-owned only.
- The initial baseline set for this PATCH is exactly
  `mcp_time__get_current_time`.
- Do not make `sql_query`, `python_calculate`, `sandbox_execute`, or any Tracker
  tool baseline in this PATCH.
- Do not infer baseline status automatically from read-only behavior or tool
  descriptions.
- Keep the skill package schema unchanged.
- Keep `allowed_tools` as the skill's domain-tool declaration.
- Keep declaration and executor policy in lockstep: the model must never see a
  tool the restricted executor rejects, and the executor must not silently permit
  a hidden non-control tool.
- Preserve existing tool-call budgets, activation budgets, deadlines, routing,
  reasoning, and model profiles.
- Do not introduce new third-party dependencies.
- Keep the implementation framework-free and simplicity-first.
- Prefer one shared composition helper over repeated set-union logic.
- Do not modify MCP tool implementations.

## Acceptance criteria

- [ ] The host has one explicit baseline tool configuration.
- [ ] The PATCH's baseline contains only `mcp_time__get_current_time`.
- [ ] Baseline names are validated against the final registered tool set.
- [ ] Active-skill model declarations are composed from skill tools + baseline
      tools + `activate_skill`.
- [ ] Active-skill executor policy permits exactly the same effective tool set.
- [ ] `tracker_read` can call Time without changing skill.
- [ ] `tracker_read` still cannot call Sales/SQL or Sandbox tools merely because
      Time became baseline.
- [ ] `sales_analysis` can see Time while still not seeing Tracker domain tools.
- [ ] Mid-turn skill replacement preserves baseline tools.
- [ ] Mid-turn skill replacement still removes the replaced skill's domain tools.
- [ ] Effective tool declarations contain no duplicates.
- [ ] Traces report the real effective tool view.
- [ ] `activate_skill` receipts report the real effective tool view.
- [ ] Current skill instructions no longer contradict host baseline composition.
- [ ] A committed scripted regression case covers Tracker + Time in one skill
      turn.
- [ ] `python -m pytest -q` passes in full.
- [ ] `python -m evals.runner --suite scripted` passes in full.
- [ ] Live verification on `next-mlx` + `fast` + `reasoning medium` demonstrates
      that Time remains available under `tracker_read`.
- [ ] Existing skill-aware live cases do not regress in skill selection or policy
      isolation.
- [ ] No new third-party dependency is added.

## Files likely affected

Advisory, not restrictive:

- `config.py`
  - host-owned baseline tool names;
- `app.py`
  - validate/inject the baseline set after the final registry exists;
- `skill_runtime/policy.py`
  - shared deterministic effective-tool composition helper if this is the
    narrowest ownership point;
- `skill_runtime/orchestrator.py`
  - compose baseline tools into the initial active-skill view;
- `skill_runtime/activation.py`
  - preserve the same baseline set across `activate_skill` replacement and return
    the correct receipt;
- `skill_runtime/prompting.py`
  - clarify host baseline vs skill-specific capabilities;
- `skills/tracker_read/SKILL.md`
  - remove the explicit contradiction that forbids Time while the host supplies
    it;
- `skills/sales_analysis/SKILL.md`
  - remove wording that incorrectly treats the skill front-matter list as the
    entire host-supplied tool view;
- `tests/test_skill_turn.py` and/or adjacent skill-policy tests;
- `tests/test_skill_activation.py` or the current activation test home;
- `evals/cases.json`;
- `evals/runner.py` only if a small assertion/capture addition is required;
- `docs/journal/patches/PATCH-012-02-compose-baseline-tools-with-active-skills.md`;
- `docs/journal/SPEC-012-skills.md` — short index entry only;
- `README.md` only if the documented capability semantics need a concise update.

## Verification commands

At minimum:

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

Live reproducer:

```bash
python app.py --profile next-mlx --router-profile fast --reasoning medium
```

If the live eval runner already supports the same split-role profile combination,
also run the relevant skill-live category through it and record the exact command
in the journal.

Record:

- initial skill;
- effective available tool names;
- tool sequence;
- whether Time executed successfully;
- final skill;
- activation count;
- turn outcome;
- representative final answer;
- model/profile provenance;
- implementation commit SHA;
- `--no-ff` merge SHA.

## Journal strategy

Standalone journal, because this PATCH changes observable model-facing capability
semantics and requires live verification.

Create:

`docs/journal/patches/PATCH-012-02-compose-baseline-tools-with-active-skills.md`

and add a short index entry under `## Patches` in:

`docs/journal/SPEC-012-skills.md`

The standalone journal must record:

- the original Tracker + Time reproducer;
- the before/after effective tool set under `tracker_read`;
- the baseline configuration chosen;
- deterministic tests added;
- scripted regression result;
- exact live verification command;
- representative trace/tool sequence;
- proof that unrelated domain tools remain inaccessible;
- proof that baseline survives one mid-turn skill activation;
- model provenance;
- deviations or negative model behavior;
- implementation commit SHA;
- merge commit SHA.

Do not rewrite the original SPEC-012 journal as if baseline composition had always
existed. Record this as a corrective evolution of the original restricted-tool
semantics.

## Out of scope

This PATCH must not include:

- persistence of tool calls or tool results across completed user turns;
- cross-turn agent-action memory or provenance memory;
- persistence of hidden reasoning across turns;
- changes to SPEC-020 reasoning preservation;
- injecting the current date/time automatically into every system prompt;
- automatically calling Time whenever a deadline appears;
- automatic timezone inference or persistence;
- changing `SkillRouter` or routing prompts;
- retiring the router;
- changing `activate_skill` behavior beyond preserving the baseline during its
  existing replacement operation;
- adding a "no skill" activation target;
- introducing multi-skill stacking;
- adding a new skill package format or inheritance mechanism;
- classifying tools dynamically as "safe" or "utility";
- adding baseline capabilities beyond Time;
- changing Tracker MCP implementation or Yandex Tracker query semantics;
- changing model profiles, reasoning modes, deadlines, or sampling parameters;
- prompt tuning whose only purpose is to make one model call Time more often.

The separate defect exposed by the same transcript — completed tool actions are
not persisted into cross-turn semantic history, so the model can later deny that
it called a tool — must be handled by a separate PATCH/SPEC decision. It is
explicitly not part of PATCH-012-02.

## Suggested branch and commit conventions

```text
branch:
patch/PATCH-012-02-compose-baseline-tools-with-active-skills

patch file:
patches/SPEC-012/PATCH-012-02-Compose-Baseline-Tools-with-Active-Skills.md

implementation commit:
Compose baseline tools with active skills (PATCH-012-02)

merge:
Merge PATCH-012-02: compose baseline tools with active skills
```
