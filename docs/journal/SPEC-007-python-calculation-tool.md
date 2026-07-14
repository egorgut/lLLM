# SPEC-007 — Python Calculation Tool

- **Spec:** [SPEC-007](../../specs/SPEC-007-Python-Calculation-Tool.md)
- **Date:** 2026-07-14
- **Branch:** feature/SPEC-007-python-calculation-tool
- **Merge commit:** 87beb0a

## Hypothesis / intent
SPEC-006 gave the harness a contract-only tool layer (`ToolSpec` + `ToolRegistry`)
but nothing executable — the CLI was still a text-only streaming chat and the
model was never even offered a tool. SPEC-007 wires the **complete tool-call path
once**, with a single safe tool, before SQL/MCP/agent-loop iterations: the model
selects a tool and emits structured arguments, the harness validates and executes
a **restricted, in-process Python calculator**, the result is sent back, and the
model streams the final answer. The tool is deliberately narrow — an AST allowlist,
never `eval`/`exec` — and at most one tool execution per turn. Persistent history
must stay semantic (user/assistant only); no tool protocol messages on disk.

## What changed
- `tools/python_calculate.py` (new): `PYTHON_CALCULATE_SPEC` and the
  `python_calculate(arguments)` handler. Evaluates a restricted expression by
  walking an `ast` allowlist (no `eval`/`exec`/`compile` of the expression).
  Allowlist: int/float literals, `+ - * / // % **` and unary `+ -`, lists/tuples,
  bare calls to `abs, round, min, max, sum, len, sqrt, ceil, floor, factorial,
  log, log10, exp, sin, cos, tan`, and constants `pi, e`. Everything else
  (attribute access, subscripts, comprehensions, lambdas, names, keyword args,
  strings/dicts/sets, booleans, comparisons, …) is rejected. Deterministic limits:
  expression ≤ 500 chars, ≤ 100 AST nodes, depth ≤ 20, integer exponent magnitude
  ≤ 1000, sequence length ≤ 1000, factorial argument ≤ 1000; non-finite and
  non-JSON results rejected. Returns the stable envelope; never leaks a traceback.
- `tools/executor.py` (new): `ToolExecutor` binds a handler to a registered tool
  name (rejects unknown/duplicate binds), dispatches by exact name, hard-fails via
  `ToolExecutionError` for an unregistered handler or a non-dict handler result,
  and returns an `invalid_arguments` envelope for non-dict arguments. No CLI /
  Ollama / persistence knowledge.
- `tools/__init__.py`: now also exports `ToolExecutor, PYTHON_CALCULATE_SPEC,
  python_calculate`.
- `llm.py`: added `ModelToolCall` (frozen dataclass; `id` always `None` in this
  SDK) and `ModelResponse`, a streaming primitive reused for both requests that
  separates streamed text (`text_chunks()`) from collected `tool_calls` and never
  reads `message.thinking`. Replaces the old `stream_chat_with_model`.
- `app.py`: one-tool turn orchestration (`run_turn`) plus `[tool]/[args]/[result]`
  rendering helpers and a lazy `Qwen:` prefix. Builds the registry + executor once
  at startup and passes `registry.to_ollama_tools()` to every request. Rollback
  discipline (`remove_last_message`), `/reset`, `/bye`, and empty-input behavior
  preserved.
- `prompts.py`: added concise `python_calculate` usage guidance to `SYSTEM_PROMPT`.
- `README.md`: documented the first tool (local execution, restricted calculator,
  one call per turn, CLI status block).
- `conversation.py`, `storage.py`, `config.py`: **unchanged** — temporary tool
  protocol messages live only in `app.py`'s per-turn list; persistent JSON schema
  (`version: 1`) is untouched.

## Final public API
```python
from tools import (
    ToolRegistry, ToolSpec, ToolExecutor,
    PYTHON_CALCULATE_SPEC, python_calculate,
)

registry = ToolRegistry(); registry.register(PYTHON_CALCULATE_SPEC)
executor = ToolExecutor(registry)
executor.register_handler("python_calculate", python_calculate)
executor.execute("python_calculate", {"expression": "(12 + 18 + 27) / 3"})
# -> {"ok": True, "result": 19.0}

python_calculate({"expression": "17 * 24"})  # -> {"ok": True, "result": 408}
```

`ModelToolCall(id: str | None, name: str, arguments: dict)` and
`ModelResponse(messages, tools).text_chunks() -> Iterator[str]` /
`.tool_calls: list[ModelToolCall]` are the harness-level transport types; `app.py`
never touches raw Ollama objects.

## Exact tool schema
```python
PYTHON_CALCULATE_SPEC = ToolSpec(
    name="python_calculate",
    description=(
        "Evaluate a safe mathematical expression using the local Python runtime. "
        "Use it for arithmetic and supported numeric functions such as sqrt, "
        "round, min, max, sum and len."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Mathematical expression, for example: (12 + 18 + 27) / 3",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"result": {"description": "Calculated JSON-compatible result."}},
        "required": ["result"],
        "additionalProperties": False,
    },
)
```

## Runtime / execution location
- Interpreter: `venv/bin/python`, Python **3.14.6**, at
  `/Users/egorgutorov/developer/test_ollama/venv` (project virtual environment).
- The calculation handler runs **in the same OS process and interpreter** as
  `python app.py` — confirmed by driving `run_turn` inside the CLI process; no
  subprocess and no external Python execution service. The allowlist, not an OS
  sandbox, is the safety boundary.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B, ctx 40960; `tools` capable)
- Ollama: server 0.31.1; SDK `ollama==0.6.2`; reachable at `http://localhost:11434`
- Sampling: defaults — no `options` set in `llm.py`

## Observed Ollama tool-call payload shape
Streaming (`stream=True`, `tools=registry.to_ollama_tools()`): qwen3 emits its
server-side thinking first (empty `message.content`, no tool call), then the tool
call arrives on a later chunk — in one probe at chunk 107 with **empty content**:
```
message.tool_calls = [ ToolCall(function=Function(
    name='python_calculate',
    arguments={'expression': '173 * 284'})) ]
```
There is no per-call `id` in this SDK version (so `ModelToolCall.id is None`), and
`arguments` already arrives as a decoded mapping. Streaming delivery of tool calls
was verified against the live model, so the streaming-first design (needed for the
normal-text streaming regression, AC-3) holds.

Second request messages (temporary, not persisted):
```text
[…system + history…,
 {"role": "assistant", "content": "",
  "tool_calls": [{"function": {"name": "python_calculate",
                               "arguments": {"expression": "173 * 284"}}}]},
 {"role": "tool", "tool_name": "python_calculate",
  "content": "{\"ok\": true, \"result\": 49132}"}]
```

## Verification
Driven end-to-end on the live model from within the CLI process.

**Direct handler (AC-7/8/9/11/12).** Successful: `17 * 24 → 408`,
`(12 + 18 + 27) / 3 → 19.0`, `2 ** 10 → 1024`, `round(10/3, 2) → 3.33`,
`sqrt(81) → 9.0`, `sum([12,18,27]) / len([12,18,27]) → 19.0`, `pi * 5 ** 2`,
`factorial(5) → 120`. Rejected (all `ok:false`, no traceback): `__import__("os")`
and `open("file.txt")` → `unsafe_expression` "Function '…' is not allowed.";
`(1).__class__`, `[x for x in range(10)]`, `lambda: 1`, `{1:2}`, `{1,2}`,
`1 if 2 else 3`, `1 == 1`, `a and b`, `True`, `"hi"` → `unsafe_expression`;
`x` → "Name 'x' is not allowed."; `math.sqrt(81)` → "Only direct calls …";
`sqrt(x=1)` → "Keyword arguments are not allowed."

**Resource limits (AC-10).** `2 ** 100000` → `resource_limit` "Exponent magnitude
may not exceed 1000."; `factorial(5000)` → `resource_limit`; a 501-char expression
→ `resource_limit` "Expression exceeds 500 characters." Calculation errors:
`1/0` → `calculation_error` "Division by zero."; `sqrt(-1)`, `factorial(-1)` →
`calculation_error`. `eval`/`exec`/`compile` absent from `tools/` source (AC-9).

**Executor (AC-1/AC-14).** `execute("python_calculate", {"expression": "2 ** 10"})`
→ `{"ok": True, "result": 1024}`; `execute("unknown_tool", {})` raises
`ToolExecutionError("No handler registered for tool: unknown_tool")`; binding an
unknown tool or a duplicate handler raises `ValueError`.

**One-tool limit (AC-14).** With injected responses: two tool calls in the first
response → turn aborts "Multiple tool calls are not supported in SPEC-007."; a tool
call in the *second* response → "Additional tool calls are not supported in
SPEC-007." No second tool executes.

**Live CLI (AC-2/3/4/5/6/13/15/17).** Scripted stdin against a scratch history:
```text
You: What is 173 multiplied by 284?

[tool] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}

Qwen: The result of 173 multiplied by 284 is **49,132**.

You: Explain the difference between a list and a tuple in one sentence.

Qwen: A list is a mutable ordered collection of items, while a tuple is an
immutable ordered collection, meaning tuples cannot be modified after creation.

You: /reset
Conversation cleared.

You: Calculate the average of 12, 18 and 27.

[tool] python_calculate
[args] {"expression": "(12 + 18 + 27) / 3"}
[result] {"ok": true, "result": 19.0}

Qwen: The average of 12, 18, and 27 is **19**.

You: /bye
Chat finished.
```
Normal turn showed no `[tool]` block and streamed; the tool turns showed the
status block then a streamed final answer using the result. An unsafe request
("evaluate `__import__(\"os\").listdir(\".\")`") produced no execution — the model
declined to call the tool and explained why; the CLI stayed usable (AC-8 / Scen. 8).

**Persistence (AC-15 / Scenario 9).** After the run, the scratch history held only
the post-`/reset` pair — `role` values `user`/`assistant` only, no `tool` role, no
`tool_calls`, no raw expression metadata, schema `version: 1`:
```json
"messages": [
  {"role": "user", "content": "Calculate the average of 12, 18 and 27."},
  {"role": "assistant", "content": "The average of 12, 18, and 27 is **19**. ..."}
]
```

**Failure rollback (AC-16 / Scenario 10).** Forcing the *second* model call to
raise after tool execution: the `[tool]/[args]/[result]` block was shown, then
`Application error: [Errno 61] Connection refused`, the user message was rolled
back, and the seeded history file was **byte-identical** before and after
(shasum match). The next turn (`/bye`) worked.

## Outcome
All acceptance criteria AC-1…AC-18 met. The project performs its first real
tool-assisted turn: registry → model tool selection → controlled local execution
→ tool result → streamed final answer, with one tool call per turn, no arbitrary
Python, no leaked reasoning or tracebacks, and persistent history that stays
user-facing. The `ModelResponse`/`ToolExecutor` seam is reusable for SQL and MCP
tools without turning this step into a full agent loop.

## Follow-ups
- General agent loop with multiple/repeated tool calls per turn (STEP 10).
- Additional tools (SQL, MCP) reusing the same execution path.
- Optional stronger isolation (subprocess/container) if a less-restricted tool is
  ever needed — explicitly a non-goal here.
