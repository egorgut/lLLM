# PATCH-010-01 — CLI Turn Activity Indicator

## Parent spec

`specs/SPEC-010-Agent-Loop.md`

## Problem

The CLI currently gives no visible feedback while the harness is waiting for the
next model-driven decision.

`CliRenderer` prints output only when one of these events occurs:

- a skill has already been selected and `[skill] ...` is announced;
- a tool call is emitted as `[tool N/MAX] ...`;
- a tool result is available as `[result] ...`;
- final assistant text starts streaming as `Qwen: ...`;
- the turn ends with an error.

Before the first such event — and again between a tool result and the model's
next decision — the terminal may remain completely silent.

With a small model this pause is often short enough to be unobtrusive. With
larger local models such as `qwen3:32b`, or with slower skill routing / agent
decisions, the pause can be long enough that the user cannot distinguish normal
processing from a stalled application.

The CLI therefore needs a lightweight activity indicator for periods in which a
turn is actively progressing but has not yet produced durable user-visible
output.

This is a UX correction to the agent-loop presentation introduced by SPEC-010.
It does not change agent policy, model prompts, tool contracts, or model
selection.

## Expected change

Add a **transient CLI activity indicator** for silent portions of an active user
turn.

The preferred user-facing behavior is conceptually:

```text
You: Which genre generated the most revenue?

⠋ Working...
```

When meaningful output becomes available, the transient status must disappear
cleanly before that output is rendered:

```text
[tool 1/4] sql_query
[args] {...}
[result] {...}

⠋ Working...

Qwen: Rock generated the most revenue...
```

The exact spinner glyph sequence is an implementation detail. A portable ASCII
spinner such as `| / - \` is acceptable if preferred.

The indicator represents **activity, not measurable progress**. The harness does
not know what percentage of an LLM decision has completed, so the patch must not
display invented percentages, ETAs, token counts, or fake progress bars.

### Required lifecycle

For an ordinary non-command user turn:

1. start the activity indicator when the harness begins processing the turn;
2. keep it active while the CLI would otherwise be silent waiting for routing or
   a model decision;
3. stop and clear it before printing any durable turn output;
4. after a tool result, restart it while waiting for the model's next decision;
5. stop it permanently when final answer streaming begins;
6. stop and clear it on every controlled error, timeout, unexpected exception,
   user interruption, or turn termination path.

Chosen refinement of step 4: the indicator also restarts after `[args]`, so tool
*execution* — silent for up to the tool timeout, and longer for a sandbox call —
is covered by the same mechanism. It is still cleared before `[result]`.

The indicator must never overwrite, interleave with, or corrupt:

- `[skill] ...`;
- `[tool N/MAX] ...`;
- `[args] ...`;
- `[result] ...`;
- `Qwen: ...` streamed answer text;
- application error messages;
- the next `You:` prompt.

### TTY behavior

Animation is required only for an interactive terminal.

When stdout is not a TTY, the implementation should avoid animation and carriage
return control sequences. Redirected output, test captures, logs, and pipes must
remain deterministic and readable.

A non-TTY implementation may either:

- omit the transient activity indicator entirely; or
- emit one deterministic static status line if that is simpler.

Choose one behavior and cover it with tests.

Chosen behavior: **omit the indicator entirely** when stdout is not a TTY. Not a
single byte is written, so redirected output, test captures, and the transcripts
already committed to `README.md` and the journals stay byte-for-byte unchanged.

## Constraints

- Preserve the architecture and intent of `SPEC-010`.
- Do not change `AgentRunner` decision policy or tool-call limits.
- Do not change skill-routing semantics.
- Do not change prompts or add model-facing status instructions.
- Do not change the Ollama request/response contract.
- Do not change conversation persistence.
- Do not change tracing semantics solely to support the indicator.
- Do not change the configured model as part of this patch.
- Do not add `rich`, `tqdm`, `alive-progress`, or another UI dependency.
- Prefer the Python standard library and the existing framework-free style.
- The activity indicator must be owned by CLI/presentation code, not by the
  model transport or individual tools.
- If animation uses a helper thread, that thread must be deterministic to stop,
  must not survive the turn, and must never write concurrently with normal CLI
  output.
- Do not expose hidden chain-of-thought or imply that spinner activity represents
  model reasoning content.

## Acceptance criteria

- [ ] After a normal user request, an interactive CLI shows immediate activity
      instead of remaining visually silent while waiting for the first model
      decision.
- [ ] The indicator is cleared before `[skill]`, `[tool]`, `[result]`, final
      `Qwen:` output, errors, or the next prompt are printed.
- [ ] A multi-tool agent turn can show activity again between a tool result and
      the next model decision.
- [ ] Final answer streaming remains incremental and visually clean.
- [ ] No spinner characters or carriage-return artifacts remain in captured
      non-TTY output.
- [ ] `/reset`, `/bye`, EOF, and `Ctrl+C` preserve their existing behavior.
- [ ] Model request timeout and whole-turn timeout paths leave no active
      indicator behind.
- [ ] An unexpected exception leaves no activity thread or corrupted terminal
      line behind.
- [ ] Existing agent, skills, tool, MCP, persistence, and reliability tests
      continue to pass.
- [ ] New regression tests cover indicator start/stop/clear behavior and at least
      one multi-tool wait cycle.
- [ ] No new third-party dependency is added.

## Files likely affected

Advisory, not restrictive:

- `app.py` — current `CliRenderer`, CLI output ownership, and top-level turn
  lifecycle.
- `agent.py` and/or `skill_runtime/` orchestration boundary — only if a small
  presentation callback is required to signal "waiting again" between durable
  outputs. Do not move presentation logic into these components.
- `tests/` — deterministic renderer/activity-indicator regression tests and
  existing turn-flow tests.
- `README.md` — update the CLI behavior description/examples if the indicator is
  user-visible in documented transcripts.
- `docs/journal/SPEC-010-agent-loop.md` — append the patch record according to the
  PATCH workflow.

The implementer must inspect the current code before deciding the final file
set. Avoid widening the patch merely to create a generalized event/UI framework.

## Verification

### Automated

Add deterministic tests that do not depend on animation timing as much as
possible.

At minimum verify:

1. **normal text turn**
   - activity starts while waiting;
   - activity stops before `Qwen:` text begins;
   - streamed answer chunks remain unchanged;

2. **tool-assisted turn**
   - activity stops before `[tool ...]`;
   - tool diagnostics render unchanged;
   - activity can restart after `[result]` while the next model decision is
     pending;
   - activity stops before the next tool call or final answer;

3. **error / timeout**
   - activity always stops;
   - the controlled error text is readable;
   - no background activity continues after the turn;

4. **non-TTY output**
   - no animated frames or terminal-control garbage are captured;

5. **commands / shutdown**
   - `/reset`, `/bye`, EOF, and interrupt behavior do not regress.

Use injected/fake model responses and deterministic test doubles for unit and
integration-style tests. Tests must not sleep merely to wait for spinner frames
unless a very small isolated test makes that unavoidable.

Run the full committed test suite after implementation.

### Live smoke test

A live Ollama run is useful for UX verification but this patch does **not**
change model behavior, prompts, tool selection, or model-visible results, so a
standalone model-behavior journal is not required.

Perform at least:

```text
1. one ordinary question that waits before final text;
2. one tool-assisted question;
3. preferably one multi-tool question.
```

Confirm visually that:

- the CLI never looks frozen during a normal wait;
- the indicator disappears cleanly before durable output;
- no spinner frame is mixed into streamed `Qwen:` text;
- the next `You:` prompt starts on a clean line.

If `qwen3:32b` is available locally, it is a useful manual stress case because
its longer decision latency makes the UX defect easier to observe, but installing
or selecting that model is not part of this patch.

## Journal strategy

Append a `PATCH-010-01` subsection to the parent SPEC-010 journal.

Reason: this patch changes deterministic CLI presentation only. It does not
change prompts, model-visible context, model decisions, tool-selection behavior,
or model-driven termination semantics.

Record:

- observed pre-patch silent-wait behavior;
- implemented indicator lifecycle;
- automated verification results;
- short live CLI smoke-test result;
- implementation commit SHA;
- `--no-ff` merge commit SHA.

A standalone `docs/journal/patches/PATCH-010-01-...md` is unnecessary unless the
implementation unexpectedly changes model-facing behavior. If that occurs,
reassess the patch scope before proceeding.

## Out of scope

Do not include any of the following in `PATCH-010-01`:

- switching `MODEL_NAME` from `qwen3:8b` to `qwen3:32b`;
- making the model configurable through CLI flags or profiles;
- token-per-second metrics;
- model load / RAM / GPU telemetry;
- percentage progress or ETA estimation;
- displaying hidden model reasoning;
- a full-screen terminal UI;
- colors, panels, Markdown rendering, or a general UI framework;
- changing trace format;
- changing timeout values;
- parallel tool execution;
- changes to agent-loop policy;
- changes to skill-selection policy;
- unrelated CLI cleanup or refactoring.

If richer runtime telemetry or a general terminal UI is desired later, treat
that as separate work rather than expanding this focused patch.

## Suggested branch and commit conventions

Per the repository PATCH workflow:

```text
branch:
patch/PATCH-010-01-cli-turn-activity-indicator

patch file:
patches/SPEC-010/PATCH-010-01-CLI-Turn-Activity-Indicator.md

implementation commit:
Add CLI turn activity indicator (PATCH-010-01)

merge:
Merge PATCH-010-01: CLI turn activity indicator
```
