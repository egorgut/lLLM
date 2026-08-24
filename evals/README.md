# Agent evaluations (SPEC-011)

Unit tests (`tests/test_agent_runner.py`) verify that the harness enforces its
own policies (budgets, repeated-call detection, timeouts, ...). Evaluations
verify something different: that the *assembled* agent can complete a small
set of representative tasks acceptably.

```text
unit test:  did the harness enforce the policy?
evaluation: did the model + harness complete the task acceptably?
```

## Cases

`cases.json` is the committed, version-controlled set of evaluation cases.
Each case has a stable `id`, a `category`, the suites it applies to
(`modes`), a `prompt`, an `expectation` (objective, deterministic assertions
only — no LLM judge), and for the scripted suite a `script` of canned model
decisions plus `tool_results` to feed a fake tool executor.

Nine required categories are committed: `no_tool_answer`, `calculator`,
`sql_single_query`, `sql_recovery`, `multi_tool`, `mcp_time`,
`repetition_guard`, `tool_call_budget_guard`, and `timeout`. The last four
(`sql_recovery`, `repetition_guard`, `tool_call_budget_guard`, `timeout`) are
scripted-only: forcing a live model to error, repeat itself, or hang on
purpose isn't something the eval should require of a real model.

## Running

```bash
python -m evals.runner --suite scripted   # default; no Ollama, no MCP server, no DB
python -m evals.runner --suite live       # optional; needs Ollama + MCP running
python -m evals.runner --suite live --profile deep --category skill_live
python -m evals.runner --suite live --profile next --router-profile fast
```

`--profile` selects the profile the agent loop runs on; `--router-profile`
(SPEC-019) optionally moves skill routing onto a different one, so a comparison
can hold the routing model fixed while changing only the model doing the work.
Omitted, both roles share one profile and one transport. Each live result records
`profile` (the agent) beside `router_profile` / `router_model`. Both flags are
ignored by the scripted suite, which stays pinned to `SCRIPTED_PROFILE`.

A case marked `skill_case: true` runs through the real `SkillTurnOrchestrator`
rather than a raw `AgentRunner` — the router selects a skill, the tool view is
restricted to it, and the model may replace it mid-turn through `activate_skill`
(SPEC-018). This holds in **both** suites: scripted skill cases drive the real
orchestrator with a scripted router and model, and live skill cases
(PATCH-018-01) drive it with the real ones, assembled from the same production
components `app.py` uses — `app.omitted_skills`, `SkillPackageLoader`,
`SkillRouter`, and the profile's own deadlines. The evaluation reimplements no
routing or activation semantics of its own.

`skill-live-cross-001` needs `TRACKER_SMOKE_ISSUE_ID` set to a real issue key;
an unset placeholder renders as a visible "not set" marker so a misconfigured
run fails obviously.

The scripted suite drives `AgentRunner` with the same fixtures as the
committed unit tests (`tests/support.py`) — no live Ollama, no live MCP
server, no real Chinook database. It is safe to run in CI and finishes in a
fraction of a second.

The live suite exercises the real model (`config.MODEL_NAME`), the real local
tools, and the real MCP server via `app.build_executor()` and
`McpClientManager`. It is optional, run manually, and its results may vary
between runs — it is never part of a default/CI gate.

Both modes exit non-zero if any applicable case fails.

## Results

Each run writes one versioned JSON file to `data/evals/<timestamp>-<suite>.json`
(git-ignored; only the case definitions and this runner are committed):

```json
{
  "schema_version": 1,
  "suite": "scripted",
  "started_at": "2026-07-24T08:31:34Z",
  "model": "scripted",
  "summary": {"total": 9, "passed": 9, "failed": 0},
  "cases": [
    {
      "id": "calc-basic-001",
      "passed": true,
      "status": "completed",
      "reason": "final_answer",
      "tool_calls": ["python_calculate"],
      "duration_ms": 0,
      "failures": []
    }
  ]
}
```

## Assertions

`evaluate_expectation()` in `runner.py` supports: expected status, expected
termination reason, required tool names, allowed tool names, forbidden tool
names, minimum/maximum tool-call count, answer contains substring
(case-insensitive), answer matches a regular expression, and a maximum duration.
Answer checks only run for a `completed` outcome.

Skill cases add four more: `expected_selection` and `selection_source` (the
router's decision, SPEC-012), plus `expected_final_skill` and
`expected_activations` (how that decision *ended*, SPEC-018 — the router's
choice alone no longer describes what a turn ran under).

A live skill case also records, per case: `profile`, `model_requests`, the full
`tool_sequence` read from the trace (so `activate_skill` appears in its real
position — a control tool never reaches an executor and would otherwise be
invisible), `activation_events`, and `model_request_ms` per model request.
