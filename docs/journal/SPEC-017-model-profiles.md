# SPEC-017 — Model Profiles

- **Spec:** [SPEC-017](../../specs/SPEC-017-Model-Profiles.md)
- **Date:** 2026-08-11
- **Branch:** feature/SPEC-017-model-profiles
- **Implementation commit:** `91b72ae`
- **Merge commit:** `c8d048f`

## Hypothesis / intent

Every step so far ran on one model, named by a constant that `llm.py` read at
import time while building a module-level `ollama.Client`. The milestone here is
not "qwen3:32b is now available" — it is that **the model stopped being a
constant**.

The design hypothesis was that a bare model name is not a configuration. The
deadlines in `config.py` were tuned against an 8B model, and a 32B model decides
roughly four times slower, so swapping only the name would convert ordinary
latency into `turn_timed_out` outcomes — failures that would read as model
regressions in this journal while actually being host-configuration artifacts.
The selectable unit therefore had to be a *profile*: one model together with the
deadlines that model needs.

## Measurements that shaped the design

Taken before implementation, warm, one decision per row (Apple M3 Max, 68.7 GB;
both models Q4_K_M; Ollama 0.31.1):

| decision | qwen3:8b | qwen3:32b |
| --- | --- | --- |
| skill-routing response | ~3.6 s | ~11.3 s |
| agent decision emitting a tool call | ~11 s | ~40–47 s |
| model size / first token | 5.2 GB / 0.4 s | 20.2 GB / 1.3 s |

Against the old flat budget (`MODEL_REQUEST 120 / TURN 180 / ROUTING 30`), a
four-call turn on qwen3:32b would exceed the whole-turn deadline before any tool
execution was counted, and the 30-second routing budget — which may also spend a
repair attempt — was measured at 11.3 s on a *minimal* prompt, far smaller than
the real router's catalog plus six context messages.

## What changed

- **`config.py`** — `ModelProfile` (frozen dataclass: name, model, request /
  turn / routing deadlines), `MODEL_PROFILES` with `fast` (qwen3:8b,
  120/180/30 — the project's original numbers, unchanged) and `deep` (qwen3:32b,
  300/600/60), `DEFAULT_MODEL_PROFILE = "fast"`, and `resolve_model_profile()`,
  which raises with the valid names rather than falling back to a default that
  would silently run the wrong model. `MODEL_NAME`,
  `MODEL_REQUEST_TIMEOUT_SECONDS`, `AGENT_TURN_TIMEOUT_SECONDS`, and
  `SKILL_ROUTING_TIMEOUT_SECONDS` are gone as flat constants; what remains
  (`TOOL_EXECUTION_TIMEOUT_SECONDS`, call limits, every skill/sandbox bound)
  bounds *host* work, which no model choice changes.
- **`llm.py`** — the module-level `client` and the import-time `MODEL_NAME` read
  are gone. `OllamaModel.for_profile(profile)` builds the transport once per run
  (client timeout from the profile); `respond()` matches the `Respond` callable
  the loop already expects and `text()` gives the router its buffered, tool-less
  shape. `ModelResponse` now receives `model` and `client` explicitly.
- **`app.py`** — `--profile` (argparse, choices from `MODEL_PROFILES`),
  `validate_model_profiles()` checking **every** committed profile against
  `reliability.validate_reliability_config` at startup (a broken profile is a
  repository defect, not something the first person to select it should discover
  mid-turn), a `[model] …` startup line beside `[mcp]`/`[skills]`, and the
  profile's deadlines threaded into `SkillRouter`, `validate_skill_config`, and
  `SkillTurnOrchestrator`. `main()` takes `argv` so the parser is testable.
- **`evals/runner.py`** — `--profile` for the live suite; the scripted suite is
  pinned to `SCRIPTED_PROFILE = MODEL_PROFILES["fast"]` regardless of the flag,
  so its committed results stay comparable. The results file records the model
  actually used instead of a constant read from config.
- **Tracing** — `run_started` carries the selected profile's model in the
  existing `model_name` field, plus an additive `model_profile`. No existing
  field was renamed or removed.

The agent loop, skill routing semantics, prompts, tool contracts, conversation
persistence, and the `Respond`/`RouteFn` signatures are untouched — `agent.py`,
`skill_runtime/`, `conversation.py`, and every skill package are unchanged.

## Model & parameters (provenance)

Both models, since the point of this step is that both are now selectable:

- qwen3:8b — digest `500a1f067a9f`, 8.2B params, Q4_K_M, ctx 40960
- qwen3:32b — digest `030ee887880f`, 32.8B params (32 762 123 264), Q4_K_M,
  ctx 40960
- Ollama 0.31.1
- Sampling: defaults — `llm.py` still sets no `options`, deliberately, so the two
  profiles differ only by model and deadlines.

## Verification

- `python -m pytest -q` — **592 passed, 29 skipped** (574 before, +18 new).
  `tests/test_model_profiles.py` covers: `fast` still carrying the historical
  values (the guard that keeps every earlier journal reproducible), `deep` being
  strictly larger on all three deadlines, default resolution, unknown-name error
  listing the valid names, every committed profile passing the reliability
  validator, an incoherent profile rejected *by name*, CLI parsing and the
  `[model]` line, and the transport binding — including two assertions that
  encode §4.4 directly: `llm` exposes no module-level `client` and `config` no
  longer exposes `MODEL_NAME`.
- `python -m evals.runner --suite scripted` — **36/36**, unchanged, proving the
  scripted path did not inherit profile plumbing.
- Live, default profile (no flag): a piped session answered normally; the
  startup banner reported `fast: qwen3:8b (request 120s, turn 180s, routing 30s)`.
- Live, `--profile deep`: a full skill-routed, tool-assisted turn end to end on
  qwen3:32b —

```text
[model] deep: qwen3:32b (request 300s, turn 600s, routing 60s)
...
You: Какой жанр принёс больше всего выручки?
[skill] sales_analysis

[tool 1/4] sql_query
[args] {"query": "SELECT g.Name AS GenreName, SUM(il.UnitPrice * il.Quantity) AS TotalRevenue FROM InvoiceLine il JOIN Track t ON il.TrackId = t.TrackId JOIN Genre g ON t.GenreId = g.GenreId GROUP BY g.GenreId ORDER BY TotalRevenue DESC LIMIT 1;"}
[result] {"ok": true, "columns": ["GenreName", "TotalRevenue"], "rows": [["Rock", 826.65]], "row_count": 1, "truncated": false}

Qwen: Жанр **Rock** принёс наибольшую выручку — **$826.65**.
```

  The run's first trace event confirmed the binding:
  `{"event": "run_started", "model_name": "qwen3:32b", "model_profile": "deep"}`.
  Notably the 32B answer volunteered the aggregation formula and a stated
  limitation ("нет фильтра по дате") unprompted — a behavioral difference worth
  recording, not a claim of general superiority on one sample.

## Deviation from the spec

**§7.2's comparative live evaluation was not run.** The spec asks for the full
committed live suite on both profiles as the milestone's evidence; the user
explicitly chose to skip it to start using `deep` immediately. What is recorded
above is a single live turn per profile — enough to prove the plumbing, *not*
enough to compare model behavior.

Consequences to keep in mind:

- The `deep` deadlines (300/600/60) remain **proposed**, validated only against
  the measurements in §"Measurements" and one live multi-tool turn. A four-call
  turn on `qwen3:32b` has not been observed end to end.
- No claim about qwen3:32b's task performance relative to qwen3:8b is made or
  supported by this entry.

The comparison stays open as a follow-up; running
`python -m evals.runner --suite live --profile fast|deep` and appending the
result to this journal would close it without any code change.

## Outcome

All acceptance criteria met except the live comparison recorded above as a
deviation. The default path is unchanged — `python app.py` with no flag runs
exactly the configuration every previous journal was recorded under — and the
model transport is now injected like every other component of this project
rather than resolved from a constant at import time.

## Follow-ups

- The §7.2 comparison, on both profiles, before treating `deep` as the default
  for anything.
- Per-role models (a small router with a large agent). SPEC-017 §9 deliberately
  kept `ModelProfile` at one model, but the two call sites (`route=model.text`,
  `respond=model.respond`) are already separate, so this is now a configuration
  question rather than an architectural one.
- Whether qwen3's thinking tokens should be bounded or disabled for routing:
  ~11 s of the 32B routing latency produced 27 characters of answer.

## Patches

- `PATCH-017-01` — a third profile, `mid` (`qwen3:14b`), between `fast` and
  `deep`. Confirms that adding a model costs one dictionary entry and nothing
  else; its deadlines are interpolated rather than measured.
  See [`docs/journal/patches/PATCH-017-01-mid-model-profile.md`](patches/PATCH-017-01-mid-model-profile.md).
