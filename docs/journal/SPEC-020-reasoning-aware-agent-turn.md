# SPEC-020 — Reasoning-Aware Agent Turn

- **Spec:** [SPEC-020](../../specs/SPEC-020-Reasoning-Aware-Agent-Turn.md)
- **Date:** 2026-08-26
- **Branch:** feature/SPEC-020-reasoning-aware-agent-turn
- **Implementation commit:** `557515a`
- **Merge commit:** `00f8125`

## Hypothesis / intent

The harness already paid for the agent model's hidden reasoning and then threw it
away. `ModelResponse.text_chunks()` read only `message.content`, and the assistant
tool-call message appended to `working_messages` carried the action but not the
plan behind it — so every decision after the first had to re-derive its own intent
from the action and the observation alone.

Three things were expected from making reasoning first-class *transient turn
state*:

1. better continuity across the decisions of one multi-step turn;
2. an answer to "where does the silence before the first token actually go?",
   which the trace could not answer at all — it recorded one `duration_ms` per
   model request and nothing else;
3. host control over reasoning effort, so `auto` / `off` / `low` / `medium` could
   be *measured* on Qwen3.8 rather than assumed.

The SPEC is explicit that it claims none of these outcomes in advance. It adds the
mechanism and the measurements.

## What changed

- **`config.py`** — `ReasoningMode` (`auto`/`off`/`low`/`medium`),
  `REASONING_MODES`, `DEFAULT_REASONING_MODE`, and `resolve_reasoning_think()`,
  which maps a mode to the SDK's own `think` value (`auto` → `None`, i.e. send
  nothing). Host-owned and fixed for the process, like a profile. No `high`/`xhigh`
  (§4.1).
- **`llm.py`** — `ModelStreamChunk(thinking, content)` and
  `ModelResponse.chunks()` replace the content-only stream as the primary
  contract; `text_chunks()` remains as the content-only view routing uses.
  `ModelResponseMetrics` captures Ollama's own `load` / `prompt_eval` / `eval`
  numbers from the final streamed chunk. `OllamaModel` gained a `reasoning_think`
  it applies in `respond()`; `text()` keeps passing `think=False` **explicitly**,
  so routing cannot inherit the agent mode even when one transport serves both
  SPEC-019 roles.
- **`agent.py`** — `_consume_model_response` accumulates reasoning and content
  independently and hands back a `_ModelDecision` carrying both plus per-request
  latencies. Only content reaches `Renderer.text`. `assistant_tool_message(call,
  thinking)` attaches the decision's own reasoning (omitting the field entirely
  when empty). `model_response_finished` gained `thinking_chars`,
  `first_model_output_ms`, `first_thinking_ms`, `first_content_ms`,
  `first_tool_call_ms`, `visible_ttft_ms` and five additive `ollama_*` fields —
  counts and timings only. `preserve_reasoning=True` is the §7.4 A/B switch.
- **`app.py`** — `--reasoning`, applied to the agent transport only; a
  `[reasoning] …` startup line; `reasoning_mode` on `run_started`.
- **`evals/runner.py`** — `--reasoning` and the evaluation-only
  `--no-reasoning-preservation`; `ReasoningSettings`; reasoning mode, preservation
  flag, and per-request metrics in every live result. `run_live_case` now writes
  to the shared trace sink instead of `NullTraceSink`, because the multi-step case
  that proves preservation runs on that path.
- **Tests** — `tests/test_reasoning_transport.py` (20) and
  `tests/test_reasoning_turn.py` (20), plus additive classes in
  `test_model_roles.py`, `test_agent_control_tools.py`, and `test_eval_runner.py`.
  The three duplicated per-file fakes of the SDK's streamed chunk collapsed into
  one `tests/support.sdk_chunk`, so the fake cannot drift from the real
  `Message` type (which is what made them fail to model `thinking` in the first
  place).

## Model & parameters (provenance)

- Agent model: `qwen3.8:27b` (digest `22130167c4c2`, family `qwen35`, Q4_K_M,
  27.3B, ctx 262144) — profile `next`
- Router model: `qwen3:8b` (digest `500a1f067a9f`, family `qwen3`, Q4_K_M, 8.2B,
  ctx 40960) — profile `fast`
- Ollama: 0.32.15 · Python client: `ollama==0.6.2`
- Sampling: defaults — no `options` are set anywhere in `llm.py`. The only
  request-shaping this step adds is `think`.
- Host: Apple M3 Max

## Verification

### Deterministic

```text
pytest                                  816 passed, 29 skipped
python -m evals.runner --suite scripted 40/40 passed
```

All twelve items of §7.1 are covered. The privacy assertion is the load-bearing
one: a turn is driven with a sentinel reasoning string, every emitted event is
serialized, and the sentinel, a 20-character prefix of it, and its sha256 are all
asserted absent — while `thinking_chars` and the timing fields are asserted
present.

### Live — where the silence actually goes

The first interactive run answered §2.3 on its own. Cold `next` + `fast`, one
no-tool question:

```text
duration_ms               74100
  ollama_load_ms           6698   model load
  ollama_prompt_eval_ms   35405   5644 prompt tokens
  first_model_output_ms   42124   ← reasoning starts (= load + prompt eval)
  thinking_chars            947
  first_content_ms        65111   ← visible TTFT
  ollama_eval_ms          31975   299 generated tokens
```

Of 65 s of silence, **42 s was load + prompt evaluation and 23 s was hidden
reasoning**. Before this step the whole 65 s would have been reported as "TTFT",
and the obvious remedy — reason less — would have addressed the smaller half.

The user-visible transcript of that same run, complete and unedited (no reasoning
appears, and none can):

```text
[model] agent next: qwen3.8:27b (request 250s, turn 500s)
[router] fast: qwen3:8b (request 120s, routing 30s)
[reasoning] auto (transient preservation on)
[mcp] connected: time (1 tool)
[mcp] connected: tracker (4 admitted, 35 filtered)
[sandbox] unavailable: Docker sandbox is unavailable.
[skills] code_workspace: omitted
[skills] 2 loaded: sales_analysis, tracker_read
Local AI chat
Enter /reset to clear the conversation, /bye to exit.

You: посчитай 2+2 и объясни
Qwen: **2 + 2 = 4**

Объяснение: берём два одинаковых количества — два и два — и складываем. Считаем
по одному: 1, 2, 3, 4. Итого получается **4**. Это одно из самых базовых действий
в арифметике: добавление двух чисел даёт их сумму.

You: Chat finished.
```

Privacy checks on that run: `grep -o '"thinking"'` over the trace → 0 hits (one
`thinking_chars` field); `data/chat_history.json` messages use exactly the keys
`{role, content}`.

### Live — §7.3 mode comparison

Bounded subset rather than the full live suite, and the journal says so plainly:
agent `next`, router `fast`, no Tracker credentials, one run per cell. Commands:

```bash
python -m evals.runner --suite live --profile next --router-profile fast \
    --reasoning <mode> --category no_tool_answer
python -m evals.runner --suite live --profile next --router-profile fast \
    --reasoning <mode> --category multi_tool
```

`no-tool-basic-001` (one model request, no tools):

| metric | auto | off | low | medium |
| --- | ---: | ---: | ---: | ---: |
| thinking chars | 176 | 0 | 347 | 108 |
| first model output (ms) | 16613 | 2893 | 19733 | 4799 |
| **visible TTFT (ms)** | 22760 | **2893** | 28763 | 9553 |
| prompt eval (ms) | 16530 | 2870 | 19652 | 4705 |
| eval (ms) | 10050 | 4580 | 12186 | 16413 |
| total turn (ms) | 26664 | **7474** | 31919 | 21213 |
| task result | correct | correct | correct | correct |

`multi-tool-001` (agent decisions + tools):

| metric | auto | off | low | medium |
| --- | ---: | ---: | ---: | ---: |
| model requests | 2 | 3 | 3 | 2 |
| thinking chars | 222, 9 | 0, 0, 0 | 151, 59, 84 | 195, 10 |
| first model output (ms) | 3245 | 5321 | 4510 | 6604 |
| **visible TTFT (ms)** | 12498 | **5321** | 11947 | 32816 |
| total turn (ms) | 13890 | **8663** | 24947 | 38179 |
| tool sequence | `sql_query` | `sql_query`, `python_calculate` | `sql_query`, `python_calculate` | `sql_query` |
| task result | **incorrect** | correct | correct | **incorrect** |

Routing duration is not broken out per mode because it cannot vary: the router
runs `think=False` under every one of them, which the deterministic tests assert
directly.

`skill-live-sales-001` through the real split-role skill turn: `auto` 170.0 s
(thinking 821 + 481 chars), `off` 29.9 s — both correct, both `sales_analysis`.

### Live — §7.4 preservation A/B

Same prompts, same commit, same models, `--reasoning auto`, preservation toggled
by the evaluation-only flag. More than one sample per arm, because the first pair
disagreed and one pair cannot tell a real effect from this case's run-to-run
spread:

| case | preservation | n | total ms (median, range) | post-tool thinking chars | tool calls | passed |
| --- | --- | ---: | --- | --- | --- | --- |
| multi-tool-001 | on | 5 | 15896 (13890–17475) | 9, 33, 57, 3, 37 (med 33) | 1,1,1,1,1 | 0/5 |
| multi-tool-001 | off | 4 | 15409 (11561–23568) | 54, 32, 34, 29 (med 33) | 2,1,2,1 | 2/4 |
| skill-live-sales-001 | on | 3 | 34459 (30482–170041) | 481, 56, 81 (med 81) | 1,1,1 | 3/3 |
| skill-live-sales-001 | off | 3 | 53329 (40561–63854) | 48, 34, 503 (med 48) | 1,1,3 | 3/3 |

## Outcome

Every acceptance criterion in §6 is met. The mechanism works, is measurable, and
leaks nothing.

**Does reasoning preservation help multi-step turns?** On this package and these
two cases, **no measurable effect either way, and no basis to claim one.** Median
turn duration is within noise on both cases (15.9 s vs 15.4 s; 34.5 s vs 53.3 s
with overlapping ranges and one 170 s outlier). Post-tool reasoning volume — the
quantity preservation is supposed to reduce — has an identical median on
`multi-tool-001` (33 chars either way). The only visible difference is in *tool
sequence*, and it points the wrong way for the hypothesis: with preservation on,
`multi-tool-001` never made the second tool call in 5 runs, while with it off it
did in 2 of 4. That is the confound §7.4 warned about — the case has two
defensible solutions (call `python_calculate`, or do the arithmetic inline) and
`next` picks between them run to run — so it is recorded as an observation to
re-measure on a case with a forced two-tool path, **not** as evidence that
preservation hurts. Preservation stays on by default: it is what the SPEC
specifies, it costs nothing measurable here, and turning it off on this evidence
would be reading noise.

**Which mode gives the best latency/quality trade-off?** `off` won every latency
column by a wide margin (3.6× faster visible TTFT and total turn on the no-tool
case, 2.4× faster total on the multi-tool case, 5.7× faster on the skill case) and
was correct on all three. `low` and `medium` were *not* ordered between `auto` and
`off` the way their names suggest: `low` was the **slowest** cell on the no-tool
case (28.8 s visible TTFT against `auto`'s 22.8 s), and `medium` produced the
worst multi-tool run of all (32.8 s visible TTFT, 38.2 s total, wrong tool
sequence). On the two tool cases the two failures were `auto` and `medium`, the
two passes `off` and `low` — with n=1 per cell that is not a quality ranking, only
a reason not to assume more reasoning buys better tool decisions here.

Nothing about this is encoded in the runtime. `auto` remains the default, and the
default run is byte-identical to the pre-SPEC-020 one.

**How much of perceived TTFT was hidden reasoning?** It depends on the request,
which is exactly why the split metric was needed. On the cold no-tool run it was
23 s of 65 s (35%) — load and prompt evaluation were the larger share. On the warm
`multi-tool-001` first decision under `auto` it was the *dominant* share: first
model output at 3.2 s, first tool call at 12.5 s, so ~9.3 s of the 12.5 s silence
was reasoning. A single "TTFT" number would have mixed these two situations
together and pointed at the wrong fix in at least one of them.

## Follow-ups

- Prompt evaluation is a bigger latency term than expected — 35.4 s for 5644
  prompt tokens on the cold run, and it swung 2.9 s → 19.7 s across warm runs of
  the *same* no-tool prompt. Worth its own step: the tool declarations and system
  prompt are most of that token count, and cache behavior across `think` settings
  is unexplained.
- The §7.4 A/B deserves a case with a forced two-tool path, so tool-sequence
  variance stops confounding the duration comparison.
- `low` measuring slower than `auto` on the no-tool case is not what the name
  implies. Either the effort names do not map monotonically onto this package's
  template, or one run per cell is simply too few. Re-measure with repeats before
  anyone reads the §7.3 table as an ordering.
- SPEC-020 §4.10 metadata is `null` for tool-emitting requests by design (the
  stream is abandoned at the tool call, before Ollama's final chunk). If
  prompt-eval cost per *decision* turns out to matter, that gap needs closing
  deliberately rather than by draining the stream.
