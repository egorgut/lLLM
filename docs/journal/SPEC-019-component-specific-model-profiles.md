# SPEC-019 — Component-Specific Model Profiles

- **Spec:** [SPEC-019](../../specs/SPEC-019-Component-Specific-Model-Profiles.md)
- **Date:** 2026-08-24
- **Branch:** feature/SPEC-019-component-specific-model-profiles
- **Implementation commit:** `9462881`
- **Merge commit:** `42855c1`

## Hypothesis / intent

SPEC-017 made the model selectable per run, but a run still had exactly one
model. Two call sites use it for very different jobs:

```text
SkillRouter  -> route  = model.text     narrow classification, think=False, strict schema
AgentRunner  -> respond = model.respond the actual task, tools, recovery, final answer
```

PATCH-012-01 had already made their *inference contracts* different. Only the
configuration still coupled them. The hypothesis of this step is narrow and
structural: those two roles can be pointed at different committed profiles in one
process without either component learning that roles exist, and without changing
what routing or agent execution *is*.

Deliberately **not** claimed here: that a small router is the right default. This
step only proves the split is real and correctly wired. Which pair is best is an
evidence-led configuration decision, and SPEC-019 §7.5 asks for exactly that
separation.

## Architecture before the change

`app.py:main()` built one transport and injected two bound methods from it:

```python
model = OllamaModel.for_profile(profile)
router = SkillRouter(route=model.text, timeout_seconds=profile.skill_routing_timeout_seconds, ...)
orchestrator = SkillTurnOrchestrator(..., respond=model.respond, ...)
```

`evals/runner.py:_build_live_orchestrator` did the same with the same single
`model`. Every deadline came off one profile.

## The role-selection contract

```text
--profile          -> agent profile   (unchanged, still authoritative, still defaults to `fast`)
--router-profile   -> router profile  (optional; absent => the agent profile object itself)
```

Sameness is **object identity**, not equality: `MODEL_PROFILES` hands out one
frozen object per name, so `--profile deep --router-profile deep` collapses back
to one role and one transport rather than building two equivalent clients.

Deadline ownership:

| deadline | owner |
| --- | --- |
| `skill_routing_timeout_seconds` (SkillRouter component timeout, `validate_skill_config`) | **router** profile |
| `model_request_timeout_seconds` (agent client + per-request deadline) | **agent** profile |
| `agent_turn_timeout_seconds` (the one whole-turn budget) | **agent** profile |

One `TurnContext`, one whole-turn clock, still started before routing. Routing
still spends the agent profile's turn budget — no second clock, no reset.

## What changed

- **`config.py`** — added `ModelRoles` (frozen `agent` + `router`, with a `split`
  property) and `resolve_model_roles(agent, router)`. Not a second profile type
  and not a generic role registry: it names the two model call sites that exist.
  `ModelProfile`, `MODEL_PROFILES`, `DEFAULT_MODEL_PROFILE` untouched.
- **`app.py`** — `--router-profile`; `validate_model_roles()` for the selected
  pair; `ModelTransports` + `build_model_transports()` (the single home of the
  reuse rule); `describe_model_roles()`; `run_started_event()` extracted from
  `main()` so the trace fields are testable without starting a chat loop; `main()`
  rewired to take routing deadlines from the router role and everything else from
  the agent role.
- **`evals/runner.py`** — `--router-profile` on the live CLI, `parse_args()`
  extracted from `main()` so the CLI is testable; `ModelRoles` threaded through
  `run_suite` → `_run_live_cases` → `run_live_case` / `_build_live_orchestrator` /
  `run_live_skill_case`; the single `OllamaModel.for_profile` replaced by
  `app.build_model_transports` so the eval composes roles exactly as the
  application does rather than reimplementing the rule; `router_profile` /
  `router_model` added to `CaseResult` and to the results payload.
- **`tests/test_model_roles.py`** (new, 23 tests) — role selection, transport
  reuse/split, per-role callable provenance, deadline ownership, pair validation,
  diagnostics, `run_started` fields.
- **`tests/test_eval_runner.py`** — live CLI override, scripted-suite
  independence from both flags, both role identities in the serialized results.
- **`README.md`** (§ «Отдельный профиль для роутера»), **`evals/README.md`**.

Why `validate_model_roles` exists at all: no existing check covers the pair.
`validate_reliability_config` never looks at the routing timeout, and
`validate_skill_config` only checks it is positive. It matters because
`SkillRouter` clamps with `min(self._timeout_seconds, remaining)` — a router
routing timeout at or above the agent's turn timeout would silently hand the
bound to the turn budget, so routing could never fail *as routing*, it would just
consume the turn. No committed pair is incoherent; the test uses a synthetic one.

### Compatibility choices, made explicitly

- `run_started.model_name` / `model_profile` keep their SPEC-017 meaning — the
  agent model — so existing trace consumers read a split run unchanged. The new
  `router_model_name` / `router_model_profile` are additive and are emitted on
  **every** run, monolithic ones included (SPEC-019 §4.9 left this to the
  implementation): a consumer should never have to infer the router from a
  missing field.
- Eval results keep `profile` as the agent profile and the top-level `"model"` as
  the agent model. `SCHEMA_VERSION` stays 1 — the change is additive only, the
  same way PATCH-018-01 added `tool_sequence` / `activation_events` /
  `model_request_ms` without a bump.
- `describe_profile()` is byte-for-byte unchanged and still produces the whole
  single-profile startup line, so the no-override path cannot drift.

## Model & parameters (provenance)

Both models exercised in the split live run, read from the running instance
(`GET /api/tags`, `POST /api/show`):

- Agent model: `qwen3.8:27b` (digest `22130167c4c2`, Q4_K_M, family `qwen35`,
  27.3B, ctx 262144)
- Router model: `qwen3:8b` (digest `500a1f067a9f`, Q4_K_M, family `qwen3`,
  8.2B, ctx 40960)
- Ollama: 0.32.15
- Sampling: defaults — `llm.py` still sets no `options`. Server-side defaults
  differ per model (`qwen3:8b` temperature 0.6 / top_k 20 / top_p 0.95;
  `qwen3.8:27b` temperature 1 / top_k 20 / top_p 0.95 / min_p 0), which is one
  more reason the milestone comparison in §7.3 must hold the agent profile fixed.

## Verification

### Deterministic

```bash
python -m pytest -q                      # before: 709 passed, 29 skipped
                                         # after:  737 passed, 29 skipped
python -m evals.runner --suite scripted  # before: 40/40 · after: 40/40
```

No deterministic test contacts Ollama. The scripted suite is unchanged and still
pinned to `SCRIPTED_PROFILE`; a test asserts its summary is identical whether or
not both profile flags are passed.

### Same-profile compatibility

`python app.py` still prints one line and one profile for both roles:

```text
[model] fast: qwen3:8b (request 120s, turn 180s, routing 30s)
```

and its `run_started` names the same model in both roles:

```json
{"event":"run_started","model_name":"qwen3:8b","model_profile":"fast",
 "router_model_name":"qwen3:8b","router_model_profile":"fast"}
```

A deterministic test also asserts `transports.router is transports.agent` for a
single-profile run — one `OllamaModel`, one client, as before.

### Live — the split is real (SPEC-019 §7.2)

```bash
python -m evals.runner --suite live --profile next --router-profile fast \
  --category skill_live_sales
```

```text
[PASS] skill-live-sales-001 (completed/final_answer, 52560ms)
1/1 passed (0 failed).
```

The recorded result carries both role identities, a model-made routing decision,
and a real tool call — not a routing-only success:

```json
{"id":"skill-live-sales-001","status":"completed","reason":"final_answer",
 "selected_skill":"sales_analysis","selection_source":"model","routing_requests":1,
 "profile":"next","router_profile":"fast","router_model":"qwen3:8b",
 "model_requests":3,"tool_sequence":["sql_query"],"model_request_ms":[33152,14947]}
```

The same split driven through `app.py` end to end, with the on-disk trace:

```text
[model] agent next: qwen3.8:27b (request 250s, turn 500s)
[router] fast: qwen3:8b (request 120s, routing 30s)
You: [skill] sales_analysis
[tool 1/4] sql_query
[result] ok · 10 rows
Qwen: **Rock** earned the most revenue in the database: **$826.65**.
```

```text
run_started            model=qwen3.8:27b/next   router=qwen3:8b/fast
skill_routing_finished 2689 ms
model_response_finished 31968 ms
tool_call_requested    sql_query
model_response_finished 24017 ms
turn_finished          completed/final_answer, 3 model requests, 58691 ms
```

**Server-side confirmation.** Configuration fields alone only prove what was
*asked* for, so the decisive evidence is Ollama's own residency. Immediately
before the split run only the agent model was loaded; immediately after it, both
were:

```text
before:  qwen3.8:27b   18 GB   100% GPU
after:   qwen3.8:27b   18 GB   100% GPU
         qwen3:8b     9.0 GB   100% GPU
```

A monolithic `next` run cannot produce that second entry. Routing executed on
`qwen3:8b`; both agent decisions took 32 s and 24 s, inside the range PATCH-017-02
measured for `qwen3.8:27b` and far outside `qwen3:8b`'s.

### Not done, and therefore not claimed

By explicit decision this cycle covered §7.2 only.

- **§7.3 monolithic vs split comparison was not run.** No `next/next` baseline was
  taken, so this journal says nothing about whether `fast` routes as *correctly*
  as `next` on the committed cases, and nothing about end-to-end latency change.
  PATCH-018-01's negative finding still stands — the models do not autonomously
  call `activate_skill` — so a wrong initial route is not self-healing, and that
  question is open.
- **§7.4 cold/warm residency was not studied.** The `ollama ps` observation above
  is a single before/after snapshot used as proof of the split, not a measurement
  of load-penalty across cold and warm runs. Whether keeping two models resident
  erases the latency benefit is unmeasured.
- The single routing time recorded (2689 ms) includes loading `qwen3:8b` and is
  one sample. It is evidence that routing ran elsewhere, not a latency claim.

## Outcome

Acceptance criteria met for what this step set out to do: the router and the agent
loop can run on different committed profiles in one process; the no-override path
is unchanged down to the startup line, the single transport, and the trace; the
routing generation contract from PATCH-012-01 and every tool/MCP/sandbox/skill
limit are untouched; unknown names and incoherent pairs fail before any turn runs;
and both role identities are recoverable from traces and eval results without
consulting CLI arguments.

What is established is *"the split works"*. What is **not** established is
*"`fast` is the right default router"* — `DEFAULT_MODEL_PROFILE` stays `fast` for
both roles, and split routing stays opt-in.

## Patches

- `PATCH-019-01` — evaluates `qwen3.8:27b-mlx` as an agent runtime by adding one
  experimental profile, `next-mlx`, and holding the router on `fast` — the first
  use of SPEC-019's split for its intended purpose, changing only the agent role.
  The candidate won every measurement (1.9-4.0x warm throughput, 2.2-6.4x faster
  live medians, 10/10 live cases against the baseline's 8/9), but the two Ollama
  packages differ in quantization, format, parameter count, and projector
  packaging, so no engine claim is available. Conclusion: **keep experimental**,
  no default change.
  See [`docs/journal/patches/PATCH-019-01-evaluate-qwen38-27b-mlx-runtime.md`](patches/PATCH-019-01-evaluate-qwen38-27b-mlx-runtime.md).

## Follow-ups

- Run SPEC-019 §7.3 (`next/next` vs `fast/next` on the model-routed live cases:
  `skill_live_sales`, `skill_live_tracker`, `skill_live_none`) and §7.4 cold/warm
  residency. Only then is a default-configuration change discussable.
- `CaseResult` has no routing-duration field; §7.3's routing-latency comparison
  will want one, read from `skill_routing_finished` the way `model_request_ms` is
  read from `model_response_finished`.
- `model_request_ms` mixes nothing today because the router emits its own event
  family, but a split run makes the asymmetry worth naming explicitly in the eval
  output.
- `evals/README.md` still references `config.MODEL_NAME`, removed by SPEC-017.
  Unrelated to this step; left for a trivial doc fix.
