# SPEC-017 — Model Profiles

**Status:** Proposed  
**Step:** 17  
**Depends on:** SPEC-010, SPEC-011, SPEC-012  
**Target repository:** `egorgut/lLLM`

---

## 1. Summary

Every step so far ran on exactly one model. `config.MODEL_NAME = "qwen3:8b"` is read
at import time by `llm.py`, which also builds one module-level `ollama.Client` whose
timeout comes from the same flat constants. Selecting a different model today means
editing a constant, and every host deadline in `config.py` silently keeps values that
were tuned for an 8B model.

This step makes the model a **selectable, host-owned profile** rather than a constant:
one named bundle carrying the model *and* the deadlines that model needs. It adds the
second profile the project has been waiting for — `qwen3:32b` — and produces the
comparative evidence that makes the addition a reproducible step rather than a
one-line edit.

The model itself gains no control over any of this: profiles, deadlines, and the
active selection stay host-owned exactly as SPEC-011 §10 requires.

---

## 2. Motivation

### 2.1 A model name alone is not a configuration

Measured on the target machine (Apple M3 Max, 68.7 GB; both models Q4_K_M, Ollama
0.31.1), warm, one decision per row:

| decision | qwen3:8b | qwen3:32b |
| --- | --- | --- |
| skill-routing response | ~3.6 s | ~11.3 s |
| agent decision emitting a tool call | ~11 s | ~40–47 s |
| model size / first token | 5.2 GB / 0.4 s | 20.2 GB / 1.3 s |

The current deadlines were chosen against the left column:

```text
MODEL_REQUEST_TIMEOUT_SECONDS = 120
AGENT_TURN_TIMEOUT_SECONDS    = 180
SKILL_ROUTING_TIMEOUT_SECONDS = 30
```

A four-call turn on `qwen3:32b` spends roughly `routing + 4 × decision + final answer`
before any tool execution is counted, which exceeds a 180-second whole-turn budget.
Routing is closer still: 11.3 s was measured on a minimal prompt, whereas the real
router sends the skill catalog plus up to six context messages and may spend a repair
attempt inside the same 30-second budget.

Swapping only the model name would therefore produce `turn_timed_out` outcomes that
look like model regressions in the journal but are artifacts of host configuration.
**The unit of selection must be the model together with its deadlines.**

### 2.2 The wiring is currently import-time

- `llm.py:15` builds `client = Client(host=OLLAMA_HOST, timeout=MODEL_REQUEST_TIMEOUT_SECONDS)`
  at import.
- `llm.py:51` passes `model=MODEL_NAME` read from the same import.
- `app.py` and `evals/runner.py` import the flat timeout constants directly.

Everything else in this project is injected — `respond`, `renderer`, `executor`,
`trace_sink`, `clock`. The model transport is the last component that reaches for a
global instead of receiving one. This step corrects that, and nothing more.

### 2.3 The milestone needs evidence, not a claim

`docs/journal/README.md` states the project's thesis: git reproduces code, specs
capture intent, and neither reproduces model behavior. A model change alters no
behavior in the code and potentially all behavior in the system. The committed
`evals/` suite (nine base categories plus six skill categories) already exists for
exactly this: running it against both profiles turns "we added a bigger model" into a
measurable, comparable result.

---

## 3. Goals

### 3.1 Functional

- Name a small set of model profiles in `config.py`, each carrying one model and its
  own request / turn / routing deadlines.
- Select the active profile per run, without editing a file, for both `app.py` and the
  live evaluation suite.
- Ship two profiles: `fast` (`qwen3:8b`, today's values) and `deep` (`qwen3:32b`).
- Show the active profile at startup beside the existing `[mcp]` / `[skills]` lines.
- Record the actually-used model in the trace and in evaluation results.

### 3.2 Architectural

- Remove import-time model and client construction from `llm.py`; the transport
  receives its model and client the way every other component receives its
  dependencies.
- Keep the default behavior byte-identical to today, so every journal from SPEC-005
  through PATCH-010-01 remains reproducible.
- Change no agent-loop policy, skill-routing semantics, prompt, tool contract,
  conversation persistence, or trace event schema (adding the profile name to an
  existing event is additive and optional).
- Add no third-party dependency; `argparse` is standard library and already used by
  `evals/runner.py`.

### 3.3 Non-goals

Listed in §9.

---

## 4. Functional requirements

### 4.1 The profile

A frozen dataclass in `config.py`:

```python
@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    model_request_timeout_seconds: float
    agent_turn_timeout_seconds: float
    skill_routing_timeout_seconds: float
```

`TOOL_EXECUTION_TIMEOUT_SECONDS`, the tool-call budget, and every skill/sandbox limit
stay global: they bound host work, not model latency, and must not drift per profile.

### 4.2 The committed profiles

```python
MODEL_PROFILES = {
    "fast": ModelProfile("fast", "qwen3:8b",  120, 180, 30),
    "deep": ModelProfile("deep", "qwen3:32b", 300, 600, 60),
}
DEFAULT_MODEL_PROFILE = "fast"
```

- `fast` must reproduce today's numbers exactly.
- `deep` values are proposals derived from §2.1 with headroom; the live run in §7
  validates or corrects them, and the journal records the final numbers with the
  observed latencies that justify them.

### 4.3 Selection

- `python app.py --profile deep`; omitted ⇒ `DEFAULT_MODEL_PROFILE`.
- `python evals/runner.py --suite live --profile deep`; same default.
- An unknown profile name is a startup error with the list of valid names, in the
  style of the existing MCP/skill startup failures — not a turn-time event.
- Every profile in `MODEL_PROFILES` is checked with the existing
  `reliability.validate_reliability_config` at startup, so an incoherent bundle fails
  fast rather than mid-turn.
- The **scripted** eval suite ignores `--profile` and keeps the `fast` deadlines: its
  committed results must stay comparable across runs, and it never contacts a model.

### 4.4 Transport

`llm.py` stops reading `config` at import:

- `ModelResponse` receives the client and the model name.
- One small holder — built once in `app.py` (and once in the live eval path) from the
  selected profile — owns the `ollama.Client` (whose timeout is the profile's request
  deadline) and exposes the `Respond`-shaped call the orchestrator already expects.
- The existing `Respond` / `RouteFn` signatures do not change, so `AgentRunner`,
  `SkillRouter`, and `SkillTurnOrchestrator` are untouched.

### 4.5 Visibility

- Startup prints one line, e.g. `[model] deep: qwen3:32b (request 300s, turn 600s)`.
- `run_started` carries the selected profile's model in the existing `model_name`
  field. Adding an optional `model_profile` field is permitted; removing or renaming
  any existing field is not.
- `evals/runner.py` writes the actually-used model into its results file rather than a
  constant read from config.

---

## 5. Design constraints

- Host-owned: the model never sees, supplies, or influences the profile or any
  deadline (SPEC-011 §10).
- No prompt, skill instruction, or tool declaration changes — the model must see the
  identical context on both profiles, otherwise the comparison in §7 measures the
  wrong thing.
- The default path must not change: with no flag, `python app.py` behaves exactly as
  it does today.
- Framework-free, standard library only.
- Both models may stay resident (5.2 + 20.2 GB against 68.7 GB), so switching profiles
  between runs must not require unloading; nothing in this step manages Ollama's
  `keep_alive`.

---

## 6. Acceptance criteria

- [ ] `MODEL_PROFILES` and `DEFAULT_MODEL_PROFILE` exist in `config.py`; `fast` carries
      today's values.
- [ ] `python app.py` with no flag produces exactly today's behavior and deadlines.
- [ ] `python app.py --profile deep` runs the whole turn — routing, decisions, tools,
      final answer — on `qwen3:32b` with the `deep` deadlines.
- [ ] An unknown `--profile` value fails at startup with a readable message listing the
      valid names, and never starts a chat loop.
- [ ] Every committed profile passes `validate_reliability_config` at startup.
- [ ] `llm.py` no longer reads `MODEL_NAME` or builds a client at import time.
- [ ] The startup banner shows the active profile and model.
- [ ] `run_started` and the eval results file report the actually-used model.
- [ ] `evals/runner.py --suite live --profile <name>` runs the live suite on that
      profile; the scripted suite is unaffected by the flag.
- [ ] The existing deterministic suite passes unchanged, plus new tests for profile
      lookup, validation of every committed profile, unknown-name failure, and the
      transport receiving the selected model.
- [ ] No new third-party dependency.

---

## 7. Verification

### 7.1 Automated

- Full `pytest` suite, plus new deterministic tests for §6. No test contacts a model.
- `python evals/runner.py --suite scripted` — unchanged pass count, proving the
  scripted path did not inherit profile plumbing.

### 7.2 Live — the milestone evidence

Run the **same** committed live suite twice, once per profile, and record both:

```bash
python evals/runner.py --suite live --profile fast
python evals/runner.py --suite live --profile deep
```

The journal must report, per profile: pass/fail per category, tool-call counts, turn
durations, and any timeout outcome. Any deadline that fires on `deep` is a signal that
§4.2's proposed numbers are wrong — correct them, rerun, and record both attempts.

Plus a short interactive session per profile covering a plain question, a single-tool
question, and a multi-tool question, to confirm the CLI experience end to end
(PATCH-010-01's activity indicator now covers the longer `deep` waits).

### 7.3 Journal

A full `docs/journal/SPEC-017-model-profiles.md`, including model provenance for
**both** models (name, digest, quantization, context length, parameter count from
`GET /api/tags`) and the side-by-side evaluation comparison. This journal is the
artifact that makes the milestone reproducible; the code change alone is not.

---

## 8. Risks

- **Deadlines still wrong for `deep`.** Most likely on routing, where a repair attempt
  shares the budget. Mitigation: §7.2 treats a timeout as a spec-level finding, not a
  model verdict.
- **Comparison contaminated by unrelated drift.** Both suites must run against the same
  commit, the same skills, and the same MCP/sandbox availability; the journal records
  which capabilities were present.
- **Longer turns expose latent UX or reliability edges** (e.g. sandbox turn-time
  margin against a 600 s budget). Any such finding gets its own PATCH rather than
  being folded in here.

---

## 9. Out of scope

- Per-role models (a small router model with a large agent model). The profile is
  deliberately one model; splitting it is a follow-up that will then be pure
  configuration if this step keeps the seam clean.
- Runtime profile switching inside a live session (`/model` command).
- Sampling parameters (`temperature`, `top_p`, `num_ctx`) and Ollama `options` — the
  project has never set them, and introducing them here would confound the comparison.
- Disabling qwen3 thinking (`think=False`), model preloading, or `keep_alive` control.
- Automatic profile choice by task, cost/latency routing, or any adaptive policy.
- Remote or non-Ollama providers.
- Changing `MAX_TOOL_CALLS_PER_TURN`, tool timeouts, or any skill/sandbox limit.
- Retiring `qwen3:8b` as the default — `fast` stays the default in this step, so every
  prior journal remains reproducible.
