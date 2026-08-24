# SPEC-019 — Component-Specific Model Profiles

**Status:** Proposed  
**Step:** 19  
**Depends on:** SPEC-011, SPEC-012, SPEC-017, SPEC-018  
**Target repository:** `egorgut/lLLM`

---

## 1. Summary

SPEC-017 made the model selectable through a host-owned `ModelProfile`, and later
patches added `mid` (`qwen3:14b`) and `next` (`qwen3.8:27b`). The current runtime,
however, still selects exactly one profile per process and uses one
`OllamaModel` instance for two very different jobs:

```text
user request
    │
    ▼
same selected model
    ├── skill routing
    │
    ▼
    └── agent decisions
            │
            ├── tool call
            │     ↓
            │   result
            │     ↓
            └── next decision / final answer
```

The two jobs have different requirements.

Skill routing is a narrow classification task. PATCH-012-01 already made that
boundary explicit by disabling thinking only for `OllamaModel.text()` and
constraining its output to the routing JSON schema.

The agent loop is the opposite: it performs the actual task, chooses tools,
interprets observations, recovers from errors, and may benefit from a stronger
model and its normal reasoning behavior.

This step allows those two components to use **different existing model
profiles in the same run** while preserving the current one-profile behavior
when no router override is supplied.

Target split:

```text
User
 │
 ▼
router model
fast / qwen3:8b
 │
 ▼
SkillRouter
 │
 ▼
agent model
next / qwen3.8:27b
 │
 ├── tool
 │    ↓
 │   result
 │    ↓
 └── agent model
      continues the turn
```

Example:

```bash
python app.py --profile next --router-profile fast
```

`--profile` remains the primary / agent profile. `--router-profile` is an
optional override used only by skill routing.

Without `--router-profile`, the router must continue to use the same profile as
the agent, preserving SPEC-017 behavior.

This step introduces no automatic model choice, no new model, and no new
provider. It only makes the already-separated router and agent call sites
independently configurable.

---

## 2. Motivation

### 2.1 The runtime already contains two distinct model roles

SPEC-017 deliberately kept the transport seam small:

```text
SkillRouter  -> route=model.text
AgentRunner  -> respond=model.respond
```

Those call sites are already separate.

The current application nevertheless builds one transport and injects both
methods from that one object:

```python
model = OllamaModel.for_profile(profile)

router = SkillRouter(
    route=model.text,
    ...
)

orchestrator = SkillTurnOrchestrator(
    ...
    respond=model.respond,
    ...
)
```

So the architectural separation exists, but the configuration still couples the
roles.

SPEC-017 explicitly parked per-role models as a follow-up. This step completes
that seam without changing either component's own contract.

### 2.2 Routing and agent work now have intentionally different inference behavior

PATCH-012-01 established that skill routing is not general reasoning.

`OllamaModel.text()` currently:

- disables thinking with `think=False`;
- uses a strict JSON response schema;
- makes no tool declarations;
- returns one buffered classification response.

`OllamaModel.respond()` keeps the model's normal behavior for the agent loop.

The router therefore no longer needs to share the same capability/latency trade
off as the agent merely because both calls happen during one turn.

### 2.3 Current measurements justify testing the split, not assuming it

The project now has measured evidence across model generations.

From SPEC-017 and PATCH-017-02:

| decision | `fast` / qwen3:8b | `deep` / qwen3:32b | `next` / qwen3.8:27b |
| --- | ---: | ---: | ---: |
| historical routing before PATCH-012-01 | ~3.6 s | ~11.3 s | n/a |
| routing after routing-specific constraints | about 0.8 s observed on qwen3:8b | bounded by the same routing path | ~3.6–4.9 s |
| tool-emitting agent decision | ~11 s | ~40–47 s | ~26–33 s |

The exact numbers are host measurements, not universal model properties.

The important point is structural: the cheapest model can perform the narrow
routing decision much faster than the models used for deeper agent work.

However, PATCH-018-01 also produced an important negative finding: the tested
models did **not** autonomously call `activate_skill` in the cross-skill scenario
unless instructed. The initial router therefore remains a real coverage
boundary. A faster router is useful only if its selection quality is adequate.

This SPEC must therefore produce evidence for both:

```text
latency
AND
routing correctness
```

It must not simply assume `fast` is the correct router because it is cheaper.

### 2.4 Model comparison becomes cleaner

Today:

```text
--profile fast
```

means both routing and agent work use qwen3:8b, while:

```text
--profile next
```

means both use qwen3.8:27b.

A head-to-head task comparison therefore changes two variables at once.

After this step:

```text
router = fast
agent  = fast

router = fast
agent  = mid

router = fast
agent  = deep

router = fast
agent  = next
```

can hold the routing model constant while changing only the model performing the
actual agent task.

That makes future model-quality experiments materially more interpretable.

---

## 3. Goals

### 3.1 Functional

- Allow the skill router and the agent loop to use different existing
  `ModelProfile` values in one process.
- Preserve `--profile` as the existing primary profile selector.
- Add an optional `--router-profile` override to `app.py`.
- Add the same optional override to the live evaluation path.
- When the override is absent, preserve the current one-profile behavior.
- Keep the router's routing-specific generation contract from PATCH-012-01
  unchanged.
- Keep the agent's normal `respond()` behavior unchanged.
- Make the selected agent and router profiles visible in startup diagnostics,
  traces, and live-eval results.

### 3.2 Architectural

- Keep `ModelProfile` as the single model + deadline bundle introduced by
  SPEC-017.
- Do not create a second profile type for routers.
- Do not modify the `SkillRouter` `RouteFn` contract.
- Do not modify the `AgentRunner` `Respond` contract.
- Do not make `SkillTurnOrchestrator` know model names or profile names.
- Preserve one `TurnContext` and one whole-turn deadline shared by routing and
  agent execution.
- Keep profile choice host-owned and fixed for the duration of a run.
- Reuse one `OllamaModel` instance when router and agent resolve to the same
  profile; create a second transport only when the profiles differ.
- Add no third-party dependency.

### 3.3 Non-goals

Listed in §9.

---

## 4. Functional requirements

### 4.1 Role selection

The run resolves two roles:

```text
agent profile
router profile
```

The existing `--profile` argument remains authoritative for the agent role.

Conceptually:

```python
agent_profile = resolve_model_profile(args.profile)

router_profile = (
    resolve_model_profile(args.router_profile)
    if args.router_profile is not None
    else agent_profile
)
```

No new model profiles are introduced by this SPEC.

The committed set remains whatever exists in `MODEL_PROFILES`, currently:

```text
fast -> qwen3:8b
mid  -> qwen3:14b
deep -> qwen3:32b
next -> qwen3.8:27b
```

`DEFAULT_MODEL_PROFILE = "fast"` remains unchanged.

### 4.2 CLI contract

Add:

```bash
--router-profile <profile-name>
```

to `app.py`.

Examples:

```bash
# Historical behavior: one profile for both roles.
python app.py
python app.py --profile deep
python app.py --profile next

# New split behavior.
python app.py --profile next --router-profile fast
python app.py --profile deep --router-profile fast

# Allowed for experiments: default agent profile with an overridden router.
python app.py --router-profile next
```

Rules:

- omitted `--profile` => existing `DEFAULT_MODEL_PROFILE`;
- omitted `--router-profile` => use the resolved agent profile;
- both arguments accept only names from `MODEL_PROFILES`;
- an unknown value fails before the chat loop starts;
- do not add `--agent-profile`: `--profile` already owns that role and must
  remain backward compatible.

The same argument is added to the **live** eval CLI:

```bash
python -m evals.runner --suite live --profile next --router-profile fast
```

The scripted suite remains model-independent and pinned to its existing
deterministic profile behavior.

### 4.3 Transport construction

The application builds the agent transport first:

```python
agent_model = OllamaModel.for_profile(agent_profile)
```

If the router profile is the same resolved profile:

```python
router_model = agent_model
```

The historical path therefore continues to use one transport/client instance.

If the profiles differ:

```python
router_model = OllamaModel.for_profile(router_profile)
```

The component wiring becomes:

```python
router = SkillRouter(
    route=router_model.text,
    ...
)

orchestrator = SkillTurnOrchestrator(
    ...
    respond=agent_model.respond,
    ...
)
```

No role-specific branching belongs in `OllamaModel`, `SkillRouter`,
`AgentRunner`, or individual skills.

The entry point owns composition.

### 4.4 Deadline ownership

This boundary must be explicit because the whole-turn deadline starts **before**
skill routing.

Use:

```text
router_profile.model_request_timeout_seconds
    -> Ollama client used by router_model

router_profile.skill_routing_timeout_seconds
    -> SkillRouter component timeout

agent_profile.model_request_timeout_seconds
    -> Ollama client used by agent_model
    -> AgentRunner per-model-request deadline

agent_profile.agent_turn_timeout_seconds
    -> the one TurnContext whole-turn deadline
```

Routing time continues to count against the agent profile's whole-turn budget,
exactly as it does today.

Do not create separate turn clocks or reset the whole-turn deadline after
routing.

`TOOL_EXECUTION_TIMEOUT_SECONDS`, tool-call limits, skill limits, MCP limits,
and sandbox limits stay global and unchanged.

`validate_skill_config(...)` must receive the **router profile's**
`skill_routing_timeout_seconds`.

`SkillTurnOrchestrator` must receive the **agent profile's**
`model_request_timeout_seconds` and `agent_turn_timeout_seconds`.

### 4.5 Selected-pair validation

The existing `validate_model_profiles()` still validates every committed
profile independently.

Add a small startup validation for the selected role pair so an obviously
incoherent mixed configuration fails before chat.

At minimum:

```text
router routing timeout < agent whole-turn timeout
```

The exact helper shape is implementation-defined.

Do not invent role-specific safety policy beyond what is needed to preserve the
existing shared-deadline contract.

### 4.6 Routing behavior remains unchanged

A different router profile must not alter the routing protocol.

`router_model.text` still:

- runs with `think=False`;
- uses `ROUTING_RESPONSE_SCHEMA`;
- returns buffered text;
- performs no tool calls;
- passes its output through the existing authoritative `SkillRouter._parse`;
- remains subject to routing repair and size limits.

This SPEC changes **which model executes the route call**, not what routing is.

### 4.7 Agent behavior remains unchanged

`agent_model.respond` remains the only model transport used by `AgentRunner`
after routing.

The agent model continues to own:

- tool selection;
- tool-result interpretation;
- mid-turn `activate_skill` decisions;
- recovery after ordinary tool errors;
- final answer generation.

This SPEC must not change:

- prompts;
- tool declarations except through the already-existing skill mechanism;
- thinking behavior;
- agent-loop policy;
- tool-call limits;
- skill-activation policy.

### 4.8 Startup visibility

The user must be able to tell whether the run is monolithic or split.

When router and agent profiles are the same, preserve the existing startup model
line as closely as practical:

```text
[model] next: qwen3.8:27b (request 250s, turn 500s, routing 50s)
```

When they differ, make both roles explicit, for example:

```text
[model] agent next: qwen3.8:27b (request 250s, turn 500s)
[router] fast: qwen3:8b (request 120s, routing 30s)
```

Exact wording may differ, but:

- both profile names must be visible;
- both model names must be visible;
- the role assignment must be unambiguous;
- the no-override path must not misleadingly suggest two independent models.

### 4.9 Tracing

Preserve existing `run_started` fields:

```text
model_name
model_profile
```

Their meaning remains the primary / agent model so old trace consumers do not
break.

Add additive fields:

```text
router_model_name
router_model_profile
```

When both roles are the same, these fields may still be populated; document the
choice and test it consistently.

No existing trace field may be removed or renamed.

The trace must make it possible to reconstruct which model performed routing
and which model performed agent work without inspecting CLI arguments.

No model reasoning text is added to tracing.

### 4.10 Evaluation results

For live evaluation, preserve the existing `profile` field as the primary /
agent profile.

Add router identity in the results, at least:

```text
router_profile
router_model
```

The live skill-aware path introduced by PATCH-018-01 must use the same role
composition as `app.py`.

Do not build a second implementation of role selection inside evals.

If a helper is needed to resolve or build the two model roles, put it at an
appropriate shared host-composition boundary and reuse it.

The scripted suite does not contact a model and must stay reproducible.

### 4.11 No implicit optimization policy

This SPEC exposes explicit configuration only.

It must not implement logic such as:

```text
if task looks easy -> fast
if task looks hard -> next
if tool fails twice -> deep
```

The host operator chooses the two profiles before the run starts.

Automatic escalation is a separate architectural step.

---

## 5. Design constraints

- **Host owns model selection.** Neither router nor agent can change either
  profile.
- **One run, fixed roles.** Router and agent profiles do not change mid-session.
- **One whole-turn budget.** Routing and agent execution remain one transaction
  under one `TurnContext`.
- **No model knowledge in orchestration.** `SkillTurnOrchestrator` receives
  callables and numeric deadlines, not profile objects or model names.
- **No model knowledge in skills.** Skill packages remain unchanged.
- **No generic role framework.** This step supports the two model call sites that
  actually exist: router and agent. Do not introduce an arbitrary
  `"role" -> profile` registry for hypothetical future components.
- **Same-profile path stays simple.** When both roles resolve to one profile,
  reuse one transport rather than constructing two equivalent clients.
- **Routing protocol stays narrow.** PATCH-012-01's `think=False` + schema
  constraint is preserved exactly.
- **Default behavior stays reproducible.** `python app.py` still resolves to
  `fast` for both roles.
- Framework-free; standard library only.

---

## 6. Acceptance criteria

- [ ] `app.py` accepts `--router-profile` with choices derived from
      `MODEL_PROFILES`.
- [ ] `evals/runner.py --suite live` accepts the same override.
- [ ] With no router override, router and agent use the same resolved profile.
- [ ] `python app.py` remains `fast` / `qwen3:8b` for both roles.
- [ ] `python app.py --profile deep` remains `deep` for both roles.
- [ ] `python app.py --profile next --router-profile fast` routes on
      `qwen3:8b` and executes the agent turn on `qwen3.8:27b`.
- [ ] The same-profile path reuses one `OllamaModel` transport.
- [ ] A split path creates distinct transports bound to the correct models.
- [ ] `SkillRouter` receives `router_model.text`.
- [ ] `SkillTurnOrchestrator` / `AgentRunner` receive
      `agent_model.respond`.
- [ ] Routing uses the router profile's routing timeout.
- [ ] Agent model requests use the agent profile's request timeout.
- [ ] The shared whole-turn deadline uses the agent profile's turn timeout and
      still begins before routing.
- [ ] Tool, MCP, sandbox, activation, and call-count limits are unchanged.
- [ ] Routing still uses `think=False` and `ROUTING_RESPONSE_SCHEMA`.
- [ ] Startup output makes split role assignment explicit.
- [ ] Existing `run_started.model_name` / `model_profile` remain compatible and
      identify the agent model/profile.
- [ ] Trace output additionally identifies router model/profile.
- [ ] Live eval output/results identify both role profiles.
- [ ] Scripted eval behavior remains model-independent and reproducible.
- [ ] Unknown profile names fail before the chat loop.
- [ ] An incoherent selected role pair fails at startup.
- [ ] Existing deterministic tests pass, plus new tests for role selection and
      wiring.
- [ ] No new third-party dependency.

---

## 7. Verification

### 7.1 Automated

Run the full deterministic suite.

Add tests covering at least:

1. no arguments -> `agent=fast`, `router=fast`;
2. `--profile next` -> `agent=next`, `router=next`;
3. `--profile next --router-profile fast` -> correct split;
4. only `--router-profile next` -> default agent + explicit router;
5. unknown router profile -> startup/argument failure;
6. same-profile selection reuses one transport object;
7. split selection binds two transports to the expected model names;
8. router callable comes from the router transport;
9. agent callable comes from the agent transport;
10. router timeout comes from the router profile;
11. whole-turn and agent request timeouts come from the agent profile;
12. pair validation rejects an incoherent synthetic combination;
13. startup diagnostics for same and split modes;
14. `run_started` compatibility plus new router identity fields;
15. live-eval result serialization contains both role identities;
16. the scripted eval path remains unaffected.

No deterministic test contacts Ollama.

Run:

```bash
python -m pytest -q
python -m evals.runner --suite scripted
```

The pre-SPEC baseline currently has a fully passing deterministic suite and
40/40 scripted eval cases; record the actual before/after counts in the journal
rather than hard-coding them into implementation logic.

### 7.2 Live — prove the wiring

Run at least one real skill-routed, tool-assisted case in split mode:

```bash
python -m evals.runner \
  --suite live \
  --profile next \
  --router-profile fast
```

The trace/results must prove:

```text
routing model = qwen3:8b
agent model   = qwen3.8:27b
```

The turn must route, execute at least one real tool, return the tool result to
the agent model, and complete or terminate according to the existing typed
runtime contract.

A routing-only success is not enough to verify this SPEC.

### 7.3 Live — compare monolithic vs split

The milestone comparison uses the same agent model on both sides so only the
router changes.

Baseline:

```bash
python -m evals.runner --suite live --profile next
```

Split:

```bash
python -m evals.runner \
  --suite live \
  --profile next \
  --router-profile fast
```

Run the same committed skill-aware cases under the same commit and capability
availability.

At minimum include model-routed cases representing:

```text
sales_analysis
tracker_read          # when Tracker live fixtures are available
no skill
```

Explicit-skill cases do not measure router quality because they bypass the
routing model and must not be used as the only evidence.

Record per configuration:

- selected skill;
- selection source;
- routing requests;
- routing duration;
- final skill;
- skill activations;
- tool sequence;
- agent model requests;
- total turn duration;
- terminal status/reason.

Primary questions:

**A. Is the split real?**

Does the router execute on `fast` while every agent decision executes on
`next`?

**B. Does routing quality regress?**

On the committed cases, does `fast` select the expected skill / `None` at least
as reliably as the current `next` router?

A wrong selection is evidence, not something to hide by changing the prompt in
this SPEC.

**C. What latency changes?**

Compare routing duration and total turn duration.

Do not set a hard speedup acceptance threshold: model loading, cache state, and
stochastic generation introduce noise. Report measured values.

### 7.4 Warm/cold model residency observation

A split run may require Ollama to keep two models resident or switch between
them.

Record at least:

- first split run after model load;
- one warm repeated split run;
- whether an obvious model-load penalty appears between routing and agent work.

Do not add `keep_alive`, explicit preloading, or Ollama scheduler management in
this SPEC.

If model swapping erases the expected latency benefit, record it as a finding.

### 7.5 Journal

Create:

```text
docs/journal/SPEC-019-component-specific-model-profiles.md
```

The journal must include:

- hypothesis / intent;
- current architecture before the change;
- exact role-selection contract;
- files changed;
- automated test/eval counts;
- live commands;
- model provenance for every model exercised;
- same-profile compatibility evidence;
- monolithic `next/next` vs split `fast/next` comparison;
- routing correctness results;
- routing and total-turn latency;
- cold/warm observation;
- deviations / negative findings;
- implementation commit SHA;
- merge commit SHA.

The journal must distinguish:

```text
feature works
```

from:

```text
fast router is empirically the best default
```

This SPEC only needs to prove the former.

The latter is an evidence-led product/configuration decision and is not required
for merge.

---

## 8. Risks

### 8.1 A smaller router may select the wrong skill

PATCH-018-01 showed that autonomous mid-turn activation cannot currently be
treated as a fallback guarantee.

A wrong initial route may therefore materially affect task completion.

Mitigation:

- keep the existing default same-profile behavior;
- make split routing opt-in;
- compare routing correctness in §7.3;
- do not change router prompts merely to make `fast` win the benchmark.

### 8.2 Two resident models may erase the latency benefit

Even if routing itself is faster, Ollama may need to load or switch to the agent
model after routing.

Mitigation:

- measure cold and warm split runs;
- do not introduce memory-management policy in this step;
- record the actual end-to-end effect rather than extrapolating from isolated
  routing latency.

### 8.3 Mixed deadlines can become ambiguous

The router has its own component timeout while routing and agent execution share
one whole-turn deadline.

Mitigation:

- define ownership explicitly in §4.4;
- keep one `TurnContext`;
- validate the selected pair at startup;
- test numeric wiring directly.

### 8.4 Observability could become misleading

Existing traces currently name one model.

Mitigation:

- preserve old fields as the agent identity;
- add explicit router identity;
- make eval results carry both.

### 8.5 The comparison could accidentally change more than the router

`next` and qwen3 profiles have different model defaults, including sampling.

Mitigation:

- in the milestone comparison keep the **agent profile fixed to `next`**;
- change only the router profile;
- do not open sampling configuration in this step.

---

## 9. Out of scope

- Changing `DEFAULT_MODEL_PROFILE`.
- Making `fast/next` the new implicit default combination.
- Adding or removing model profiles.
- Changing `next` / qwen3.8 deadlines.
- Pinning or normalizing sampling parameters.
- Preserving or exposing Qwen3.8 thinking state across tool calls.
- Changing `OllamaModel.respond()` reasoning behavior.
- Retiring `SkillRouter`.
- Relying on `activate_skill` as a replacement for correct initial routing.
- Automatic model selection by task complexity.
- Dynamic escalation from `fast` to `deep` / `next`.
- Fallback to another model after an error or timeout.
- Separate models for planning, tool calling, final answer, or individual tools.
- Arbitrary N-role model maps or a generic model-routing framework.
- Runtime `/model` or `/router` switching inside an active chat session.
- Parallel model calls.
- Model preloading, `keep_alive`, eviction, or Ollama scheduler control.
- Remote/non-Ollama providers.
- Context-window changes or token-aware conversation memory.
- Vision/multimodal support.
- Agent-loop policy changes.
- Tool, MCP, sandbox, skill, or activation-limit changes.
