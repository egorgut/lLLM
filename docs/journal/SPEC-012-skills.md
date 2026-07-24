# SPEC-012 — Skills

- **Spec:** [SPEC-012](../../specs/SPEC-012-Skills.md)
- **Date:** 2026-07-24
- **Branch:** feature/SPEC-012-skills
- **Merge commit:** a9aedf3

## Hypothesis / intent

Tools answer "how do I perform one operation?"; the model still had to
rediscover the whole procedure for every recurring class of task. This step adds
a **skill** layer above tools — a filesystem-backed, host-controlled, declarative
package describing how to solve one class of tasks with a restricted subset of
tools. The bet: a two-phase design (compact catalog for routing, lazy full
instruction after selection) gives reusable, inspectable capabilities without the
prompt growing with the library and without turning the project into a workflow
engine. Everything reuses the SPEC-011 bounded, observable `AgentRunner`.

## What changed

- New `skill_runtime/` package (runtime code, separate from declarative `skills/`
  data to avoid the package/data name collision):
  - `models.py` — frozen `SkillSpec`, `SkillCatalogEntry`, `SkillSelection`.
  - `loader.py` — fail-fast discovery + strict validation: path/symlink safety,
    a small in-house front-matter parser (see deviation below), required headings,
    a structural `input.schema.json` validator (documented Draft-2020-12 subset,
    no `$ref`), and a stable content fingerprint.
  - `registry.py` — exact-name registry, compact catalog, catalog fingerprint.
  - `policy.py` — `declarations_for_names` (filtered tool declarations) and
    `RestrictedToolExecutor` (rejects a call outside the allowlist before dispatch).
  - `router.py` — `parse_explicit_selection` + `SkillRouter` (injectable `RouteFn`,
    strict JSON, one repair attempt, host-owned timeout, trace events).
  - `prompting.py` — host-generated `<active_skill>`/`<active_skill_policy>` wrapper.
  - `orchestrator.py` — `SkillTurnOrchestrator`: one `TurnContext` before routing,
    zero-or-one selection, prompt/tool composition, hand-off to `AgentRunner` with
    the shared deadline; builds the terminal outcome for routing failures itself.
- `reliability.py` — five skill `TerminationReason`s + status/message mappings, a
  frozen `TurnContext`, and typed skill exceptions (incl. `SkillPolicyViolation`).
- `agent.py` — `run_turn` accepts `turn_context` / `selected_skill` /
  `skill_version` / `routing_model_requests`; `model_requests` = routing + agent;
  `turn_started`/`turn_finished` carry skill metadata; `SkillPolicyViolation` is
  mapped to `stopped/skill_policy_violation` (not folded into tool errors).
- `conversation.py` — `messages_for_model(additional_system=...)` folds the skill
  wrapper into the system message (never a user message); `latest_user_message`.
- `app.py` — load + validate skills after MCP registration (fail-fast startup);
  build the production `SkillRouter` and `SkillTurnOrchestrator`; per-turn
  orchestration; `[skill] <name>` printed only when selected.
- `config.py` — `SKILLS_ROOT` + skill bounds; `skill_runtime.validate_skill_config`.
- Reference skill `skills/sales_analysis/` (`SKILL.md`, `input.schema.json`,
  `examples/`, `evals/cases.json`); uses `sql_query` + `python_calculate`, forbids
  `mcp_time__get_current_time`.
- Tests: `test_skill_loader/registry/router/prompting/policy/turn.py`, extended
  `test_reliability.py`; evals: `run_scripted_skill_case` + six new categories in
  `evals/cases.json`, frozen category set updated in `test_eval_runner.py`.

## Deviations from the spec

- **Front matter parser.** The spec suggests `yaml.safe_load` *or equivalent*.
  Rather than add PyYAML (the project keeps only `ollama` + `mcp` as runtime
  deps and hand-rolls its JSON-schema check), the loader parses the *documented
  constrained subset* (top-level scalars + one block list) with a small in-house
  parser. It only ever yields strings/lists — no tags, anchors, or object
  construction — which makes the "unsafe YAML tags rejected" case hold by design.
- **Trace `SCHEMA_VERSION` kept at 1.** New event types and optional fields are
  additive, matching how SPEC-011 grew the trace; no reader break warranted a bump.
- **Skill eval categories are new** (not folded into existing ones), so skill
  coverage is explicit; the frozen required-category set was updated accordingly.

## Model & parameters (provenance)

- Model: qwen3:8b (digest `500a1f067a9f`, Q4_K_M, ctx 40960, 8.2B params)
- Ollama: 0.31.1
- SDKs: `ollama==0.6.2`, `mcp==1.28.1`; interpreter `venv/bin/python`; `pytest==9.1.1`
- Sampling: defaults — no options set in `llm.py`

## Verification

Deterministic suite and scripted evals (no live model/MCP):

```text
$ pytest
151 passed in 0.50s

$ python -m evals.runner --suite scripted
15/15 passed (0 failed)
  ... skill-explicit-001 / skill-auto-multitool-001 / skill-none-001 /
      skill-clarification-001 / skill-policy-violation-001 (stopped/skill_policy_violation) /
      skill-routing-repair-001 ...
```

Live end-to-end against `qwen3:8b` (`python app.py`, piped dialogue):

```text
You: Which music genre generated the most revenue, and what percentage of total revenue did it generate?
[skill] sales_analysis
[tool 1/4] sql_query
[args] {"query": "WITH GenreRevenue AS (...) SELECT ... ORDER BY TotalRevenue DESC LIMIT 1;"}
[result] {"ok": true, "rows": [["Rock", 826.65, 35.499...]], "truncated": false}
Qwen: ... Rock, contributing $826.65, which accounts for 35.5% of the total revenue. ...

You: Explain in one sentence what an agent loop is.
Qwen: An agent loop is the continuous cycle of perception, decision-making, action, and feedback ...   (no [skill] line)

You: Use the sales_analysis skill to compare revenue by the top 3 genres.
[skill] sales_analysis   (explicit — router model not called)
[tool 1/4] sql_query ... Qwen: Rock ($826.65), Latin ($382.14), Metal ($261.36) ...

You: Analyse sales.
[skill] sales_analysis
Qwen: To analyze sales effectively, please specify the metric ... and dimensions ...   (clarification, no tool call)
```

Trace (`data/traces/agent.jsonl`) for the auto sales turn — one shared
`run_id`/`turn_id` across both phases:

```text
skill_routing_started → skill_routing_response → skill_routing_finished
(selected_skill=sales_analysis, source=model, routing_requests=1)
→ skill_loaded (v1, allowed_tools=[sql_query, python_calculate])
→ skill_toolset_resolved → turn_started (available_tools=[sql_query, python_calculate])
→ ... → turn_finished (status=completed, model_requests=3, routing=1, agent=2)
```

The explicit turn recorded `source=explicit`, `routing_requests=0`,
`model_requests=2`; the no-skill turn recorded `selected_skill=None`.

## Outcome

Meets the acceptance criteria: deterministic startup discovery + fail-fast
validation, compact catalog / lazy full-instruction loading, zero-or-one routing
with explicit bypass and one repair attempt, defense-in-depth tool restriction
(only allowed declarations sent + executor guard → `skill_policy_violation`),
shared `turn_id`/deadline with `duration_ms` and `model_requests` covering
routing, ephemeral selection (routing protocol never persisted; rollback on any
non-completed outcome), and structured skill trace events without copying the full
`SKILL.md`. The live model routed, restricted itself to the skill's tools,
grounded its answer with the calculation basis, and asked a clarification when the
metric was absent. No SPEC-011 behavior regressed.

## Follow-ups

- The per-skill `skills/<name>/evals/cases.json` is committed as a fixture but not
  executed by `evals/runner.py` (which runs the top-level suite). A later step
  could load per-skill case files directly.
- Routing uses the same `MODEL_NAME` as the agent; a dedicated smaller router
  model remains a future option (out of scope here).
- Multiple skills, chaining, and per-user permissions stay non-goals (SPEC-012).
