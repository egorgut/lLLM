# SPEC-013 — External MCP Integration: Yandex Tracker (Read-Only)

- **Spec:** [SPEC-013](../../specs/SPEC-013-External-MCP-Yandex-Tracker-Read-Only.md)
- **Date:** 2026-07-24
- **Branch:** feature/SPEC-013-tracker-mcp
- **Merge commit:** _(recorded after merge)_

## Hypothesis / intent

SPEC-009 proved the MCP host/client path against a local, trusted,
network-free reference server. The next real test is whether the same
protocol boundary holds against a **real external business system** that
exposes far more than the app should ever call: `aikts/yandex-tracker-mcp`
advertises ~40 Yandex Tracker operations, many of them mutating. The bet:
treat the upstream server as an untrusted capability provider and reduce its
catalog to four reviewed, read-only tools through a host-owned, exact-name
allowlist applied *before* anything reaches the global `ToolRegistry` — not
via a skill-level restriction alone, and not via any heuristic on tool name
or description. If that boundary holds, `lLLM` is no longer limited to
locally authored tools or demonstration MCP servers.

## What changed

- `mcp_integration/config.py` (new) — `McpServerConfig` (frozen dataclass:
  `server_id, command, args, env, enabled, allowed_tools, required_tools`,
  with `__post_init__` invariants), `env_bool`, and
  `load_tracker_server_config()`. This is the only module in the app that
  reads Tracker-related `os.environ`; it returns a fully disabled config
  immediately when `TRACKER_MCP_ENABLED` is unset/false, without touching
  `TRACKER_TOKEN` or any organisation-id variable at all. All failures raise
  the existing `McpStartupError` (two new `error_type`s:
  `tracker_configuration_missing`, `tracker_configuration_conflict`).
- `mcp_integration/policy.py` (new) — pure `filter_discovered_tools()`: the
  exact-name admission algorithm, shared by every server (including `time`,
  where `allowed_tools=None` preserves its historical unrestricted
  behavior). No I/O, no asyncio — deliberately factored out so the ~10
  case-sensitivity/duplicate/collision/required-tool properties are unit
  testable without a fake async session.
- `mcp_integration/client.py` — `McpClientManager` now takes
  `dict[str, McpServerConfig]` plus `run_id`/`trace_sink` (mirroring
  `AgentRunner`'s own tracing convention exactly); `start()` skips any
  `enabled=False` server before touching its params or env; discovery
  delegates to `filter_discovered_tools` and raises
  `mcp_required_tool_missing` before anything is registered; new trace events
  `mcp_server_starting` / `mcp_tool_admitted` (one per admitted tool) /
  `mcp_tool_filtered` (one bounded summary, not one per filtered tool — see
  deviations) / `mcp_server_ready`; `server_summaries()` reports every
  configured server, disabled or not, formatting a filtered server as
  `"tracker (N admitted, M filtered)"` vs. the unfiltered `"time (1 tool)"`.
- `mcp_integration/adapter.py` — `normalize_result()` gained a generic
  (not Tracker-specific) result-size bound: an oversized `data` payload is
  replaced with `{"truncated": true, "preview": "..."}` capped at
  `MCP_RESULT_MAX_CHARS`. This is what actually backs the spec's "large
  result must be bounded" requirement — the codebase had no result-size
  bounding for MCP results before this.
- `config.py` — added `TRACKER_MCP_SERVER_ID/COMMAND/PACKAGE/ARGS/
  REQUIRED_TOOLS`, `TRACKER_MAX_SEARCH_PAGE_SIZE`, `MCP_RESULT_MAX_CHARS`.
  Package pinned to `yandex-tracker-mcp==0.7.2` (confirmed the latest stable
  PyPI release, 2026-06-19, at implementation time). No `os.environ` reads
  added here.
- `app.py` — new `build_mcp_servers()` merging the static `time` entry with
  `load_tracker_server_config()`, reused identically by the live eval path;
  `main()` threads `run_id`/`trace_sink` into `McpClientManager`, computes
  `omit_skills = {} if tracker_enabled else {"tracker_read"}` for the skill
  loader, and moved `build_mcp_servers()`/`McpClientManager(...)`
  construction inside the same `try` that already catches `McpStartupError`
  (a real bug caught during manual verification — see below).
- `skill_runtime/loader.py` — `load_all()` gained an `omit: frozenset[str]`
  parameter. Directory-name validity is now checked for *every* discovered
  package (via a new `_validate_package_name()` helper), omitted or not,
  before the main loop skips SKILL.md parsing/tool-reference validation/
  registration for names in `omit`. This is a narrow, explicit, host-owned
  skip — not a general "ignore unknown tools" relaxation — so every other
  skill's fail-fast validation is unchanged.
- `skills/tracker_read/` (new) — `SKILL.md` (front matter: exactly the four
  `mcp_tracker__*` names; body covers tool selection, narrow-field/page-size
  guidance, untrusted-Tracker-content handling, mutation refusal,
  limitation reporting), `input.schema.json`, four `examples/*.md`, and a
  decorative `evals/cases.json` matching the `sales_analysis` precedent.
- Tests (all new): `test_mcp_config.py` (env validation, sanitised errors,
  pinned-package rejection), `test_mcp_policy.py` (pure admission algorithm),
  `test_mcp_client.py` + `tests/support_mcp.py` (a fake stdio-transport/
  session harness — the project had no MCP test coverage of any kind before
  this), `test_mcp_result_bounds.py` (size-cap behavior), `test_tracker_skill.py`
  (skill loads from the real `skills/` tree, declares exactly four tools,
  rejects both a local tool and an unapproved same-prefix Tracker tool,
  scripted-turn policy-violation stop). Extended `test_skill_loader.py` with
  six `omit`-parameter cases.
- `evals/runner.py` + `evals/cases.json` — extended the scripted-skill
  fixture registry with the four `mcp_tracker__*` names; `_run_live_cases()`
  now uses `build_mcp_servers()`; added `--category` (prefix match); added
  the spec's twelve scripted `tracker_*` categories plus four **live**-mode
  Tracker cases with `{tracker_issue_id}`-style placeholders rendered from
  `TRACKER_SMOKE_*` env vars (`_render_live_prompt`), so the live command has
  real content once an operator has credentials. Extended
  `test_eval_runner.py`'s frozen required-category set.
- `README.md` — new "Yandex Tracker MCP — только чтение" subsection
  (prerequisites, env vars, pinned version, startup output for all three
  states, sample prompts, troubleshooting table, live-smoke command);
  updated the skills section, structure table, and configuration snippet.
- `.env.example` (new) — placeholders only, including the three
  `TRACKER_SMOKE_*` live-fixture variables.

## Deviations from the spec

- **`env_bool`/Tracker env reads live in `mcp_integration/config.py`, not
  top-level `config.py`.** The spec's configuration snippet shows
  `TRACKER_MCP_ENABLED = env_bool(...)` as if it were a top-level constant.
  Top-level `config.py` had zero `os.environ` reads before this spec — a
  confirmed, deliberate invariant — so all Tracker env parsing was kept at
  the MCP integration boundary instead. The semantics (opt-in, default
  `False`, fail-fast when enabled and misconfigured) are unchanged.
- **`mcp_tool_filtered` is one bounded summary event, not one per filtered
  tool.** The spec's representative trace shows a per-tool event; its own
  "Tool filtering events" section explicitly sanctions a single bounded
  summary "to avoid excessive trace volume." With ~38 non-admitted upstream
  tools in the real catalog, the summary form was chosen; `discovered_count`/
  `admitted_count`/`filtered_count` on `mcp_server_ready` still satisfy the
  acceptance criterion asking for those counts.
- **No proactive `uvx` availability probe.** `load_tracker_server_config()`
  does not call `shutil.which("uvx")`; a missing executable is still caught,
  via the existing generic child-spawn-failure path in
  `McpClientManager.start()`, as `mcp_server_start_failed`. Verified manually
  (see below) — the message is clean and no traceback leaks.
- **`TRACKER_MAX_SEARCH_PAGE_SIZE` is advisory (skill-instruction guidance),
  not mechanically enforced on `issues_find` arguments.** Clamping a
  Tracker-specific argument would itself be the "Yandex-specific dispatch
  logic" the spec's own implementation outline says `register_mcp_tools`
  must not need. The generic `MCP_RESULT_MAX_CHARS` bound in `adapter.py` is
  the real backstop for the "large result must be bounded" acceptance
  criterion, applied identically to every MCP server.
- **Live tracker smoke stayed on the existing bare-`AgentRunner` live path**
  (`evals.runner --suite live`), not a new skill-aware live harness.
  `_run_live_cases()` never routed through `SkillTurnOrchestrator` even
  before this spec (a pre-existing gap affecting every skill, not introduced
  here); building that harness would have been a materially larger,
  cross-cutting change unrelated to Tracker admission, and could not be
  verified in this environment anyway. The four new live cases still
  exercise real tool registration/dispatch/no-mutation checks, which is what
  the spec's "Minimum live checks" actually require.
- **Real bug found and fixed during manual verification, not by tests:**
  the first draft called `build_mcp_servers()` (which can raise
  `McpStartupError` for a misconfigured Tracker) *before* entering the
  `try/except McpStartupError` block in `app.py::main()`, so a
  misconfiguration produced a raw traceback instead of the specified clean
  diagnostic. Fixed by moving server-map construction inside the same `try`
  and making `manager` tolerate being `None` in the closing `finally`. No
  unit test caught this because every test constructs `McpClientManager`
  directly, bypassing `main()`'s control flow — worth remembering as a gap
  in coverage, not a design flaw to re-open.

## Model & parameters (provenance)

- Model: qwen3:8b (digest `500a1f067a9f`, Q4_K_M, ctx 40960, 8.2B params)
- Ollama: 0.31.1
- SDKs: `ollama==0.6.2`, `mcp==1.28.1`; interpreter `venv/bin/python`; `pytest==9.1.1`
- Sampling: defaults — no options set in `llm.py`
- Pinned Tracker package: `yandex-tracker-mcp==0.7.2` (latest stable on PyPI
  at implementation time, 2026-06-19; console entry point confirmed as
  `yandex-tracker-mcp`; upstream env vars and all four target tool names
  confirmed against the real package README/PyPI metadata)

## Verification

Deterministic suite and scripted evals (no live Tracker, no `uv`/`uvx`, no
real token):

```text
$ pytest
231 passed in 0.64s

$ python -m evals.runner --suite scripted
27/27 passed (0 failed)
  ... tracker-issue-lookup-001 / tracker-issue-search-001 /
      tracker-queue-metadata-001 / tracker-comment-summary-001 /
      tracker-multi-read-001 / tracker-read-only-refusal-001 /
      tracker-filtered-tool-001 (stopped/skill_policy_violation) /
      tracker-auth-error-001 / tracker-permission-error-001 /
      tracker-not-found-001 / tracker-large-result-001 /
      tracker-prompt-injection-content-001 ...
```

Manual startup smoke (`python app.py`, piped input, no live model needed for
these three since they resolve before any chat turn):

```text
$ TRACKER_MCP_ENABLED unset (default)
[mcp] connected: time (1 tool)
[mcp] tracker: disabled
[skills] 1 loaded: sales_analysis

$ TRACKER_MCP_ENABLED=true, no TRACKER_TOKEN
MCP startup failed for server 'tracker': Tracker MCP is enabled but TRACKER_TOKEN is missing.
(exit 1, no traceback)

$ TRACKER_MCP_ENABLED=true, TRACKER_TOKEN set, no org id
MCP startup failed for server 'tracker': Tracker MCP is enabled but no organisation ID is set...
(exit 1)

$ TRACKER_MCP_ENABLED=true, both TRACKER_ORG_ID and TRACKER_CLOUD_ORG_ID set
MCP startup failed for server 'tracker': Tracker MCP requires exactly one organisation ID...
(exit 1)

$ TRACKER_MCP_ENABLED=true, valid-shaped token/org, no uvx installed
MCP startup failed for server 'tracker': The MCP server could not be started.
(exit 1, no traceback, no orphan process)
```

Live end-to-end against `qwen3:8b` (`python app.py`, piped dialogue, Tracker
disabled — this is real live-model verification, not scripted):

```text
You: What time is it now in UTC?
[tool 1/4] mcp_time__get_current_time
[args] {"timezone": "UTC"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "UTC", "datetime": "2026-07-24T20:34:25+00:00"}}
Qwen: The current time in UTC is 2026-07-24T20:34:25+00:00.

You: Use the tracker_read skill and show me issue DATA-142.
Qwen: I don't have access to a "tracker_read" skill or any issue-tracking system. ...
```

Confirms, on the real model, that disabling Tracker removes `tracker_read` as
an executable capability entirely (the model has no skill or tool to reach
for) while every existing local/MCP behavior is unaffected.

**Not executed this session:** `python -m evals.runner --suite live --category
tracker` and the spec's "Manual verification scenario" against a real Yandex
Tracker instance. This environment has neither `uv`/`uvx` installed nor real
Tracker credentials. All deterministic and scripted verification passed;
live Tracker verification is a **pending manual follow-up** for an operator
with both available (see `README.md`'s Tracker section and `.env.example`
for exact preconditions and commands).

## Outcome

Meets the acceptance criteria achievable without live Tracker access: exactly
the four approved tools are ever admitted (verified both by pure
admission-policy tests and end-to-end against a fake MCP session), every
other discovered tool — including six representative mutation tools — is
filtered before registration and never receives a handler or declaration,
required-tool validation fails startup individually for each of the four
names, secrets are never read outside `mcp_integration/config.py` and never
appear in startup errors or trace events (asserted directly), the
`tracker_read` skill is omitted deterministically (not a startup crash) when
disabled while every other skill still loads and validates normally, and a
real bug in the fail-fast wiring was caught by manual verification and fixed
before merge. Existing MCP (`time`), skill (`sales_analysis`), agent-loop,
and tracing behavior all remain green and unchanged.

## Follow-ups

- Live Tracker verification (`--suite live --category tracker` and the
  manual `python app.py` scenario) is owed once an operator has `uv`/`uvx`
  installed and real Tracker credentials; the pinned version
  (`yandex-tracker-mcp==0.7.2`) should be re-confirmed compatible at that
  time, per the spec's own "verified during implementation, re-verified
  before any future version bump" expectation.
- `evals.runner`'s live path does not route through the skill layer for any
  skill (not just `tracker_read`) — a pre-existing gap, not introduced here.
  A future step could add skill-orchestrator support to the live suite if
  automated live skill verification becomes valuable.
- `TRACKER_MAX_SEARCH_PAGE_SIZE` remains advisory only; if a future release
  needs a hard per-argument cap, it would need its own narrowly-scoped
  design discussion, since the current architecture deliberately avoids any
  Tracker-specific dispatch logic.
