# SPEC-007: Python Calculation Tool

## Background

SPEC-006 introduced the tool contract and registry foundation.

The harness can now describe tools consistently:

```text
ToolSpec
    │
    ▼
ToolRegistry
    │
    ├── register
    ├── lookup
    ├── enumerate
    └── export for Ollama
```

However, the current application still behaves as a normal text-only streaming chat.

The model cannot yet:

- request a tool;
- pass structured arguments to a tool;
- receive a tool result;
- continue the conversation after tool execution.

The next step is to add the first real executable tool.

The first tool will perform controlled mathematical calculations through the local Python runtime.

This iteration is intentionally narrow. It introduces the complete tool-call path once, with one safe tool, before SQL, MCP, and a general agent loop are added.

---

## Goal

Implement one end-to-end tool-assisted conversation flow using a controlled Python calculation tool.

The target interaction is:

```text
User request
    │
    ▼
Ollama receives available tool declarations
    │
    ▼
Model either:
    ├── answers normally
    └── requests python_calculate
             │
             ▼
        Harness validates the call
             │
             ▼
        Local Python handler executes
             │
             ▼
        Harness sends tool result to Ollama
             │
             ▼
        Model produces the final user-facing answer
```

The CLI must visibly report that a tool was requested, executed, and completed.

The harness must not expose hidden model reasoning.

The Python code must execute locally in the same Python runtime and virtual environment as the CLI application.

The tool must not provide arbitrary `exec()` or unrestricted `eval()`.

---

## User-visible behavior

Example:

```text
You: What is the average of 12, 18 and 27?

[tool] python_calculate
[args] {"expression": "(12 + 18 + 27) / 3"}
[result] {"result": 19.0}

Qwen: The average is 19.
```

For a question that does not require a tool:

```text
You: Explain what a Python virtual environment is.

Qwen: A virtual environment is...
```

No tool status block is shown when the model does not request a tool.

The CLI exposes observable actions:

- selected tool name;
- structured arguments;
- execution status;
- structured result or error.

The CLI does not display:

- hidden chain of thought;
- private reasoning tokens;
- Ollama internal reasoning fields;
- speculative intermediate thoughts.

---

## Core architectural decisions

### 1. The model selects; the harness executes

The model is responsible for selecting a tool and producing structured arguments.

The model must never execute Python itself.

```text
Ollama model
    │
    │ structured tool call
    ▼
Harness
    │
    │ invokes registered handler
    ▼
Local Python runtime
```

### 2. The Python runtime is the current application runtime

For SPEC-007, the calculation handler runs:

- on the user's Mac;
- inside the same OS process as `python app.py`;
- through the same Python interpreter;
- inside the currently activated project virtual environment.

Conceptually:

```text
venv/bin/python app.py
        │
        ├── CLI
        ├── Conversation
        ├── Ollama client
        ├── ToolRegistry
        ├── ToolExecutor
        └── python_calculate handler
```

This is not a security sandbox.

Therefore the handler must expose only a tightly restricted calculation language.

Future iterations may move execution into:

- a subprocess;
- a Docker container;
- a restricted worker;
- a remote execution service.

Those are non-goals for SPEC-007.

### 3. No arbitrary Python execution

The tool is named `python_calculate`, but it is not a general Python REPL.

It accepts a restricted mathematical expression and evaluates only an allowlisted subset of Python syntax.

Forbidden examples include:

```python
__import__("os").system("rm -rf ...")
open("file.txt").read()
globals()
locals()
obj.attribute
some_function()
[x for x in values]
lambda x: x
```

The implementation must not use unrestricted:

```python
eval(expression)
exec(code)
```

### 4. One bounded tool round

SPEC-007 supports at most one tool execution per user turn.

Allowed:

```text
User
→ model tool call
→ one tool execution
→ model final answer
```

Not allowed yet:

```text
User
→ tool A
→ model
→ tool B
→ model
→ tool C
→ final answer
```

If the second model response requests another tool, the harness must stop the turn with a clear application error.

The general multi-step agent loop belongs to STEP 10.

### 5. Tool metadata and execution binding remain separate

`ToolSpec` describes the contract.

A handler performs the work.

The executor binds a registered tool name to a handler.

```text
ToolSpec
    │ metadata
    ▼
ToolRegistry

tool name
    │
    ▼
ToolExecutor
    │
    ▼
handler(arguments)
```

Do not add executable callables directly into the immutable `ToolSpec` introduced by SPEC-006.

---

## Target architecture

```text
┌──────────────┐
│    app.py    │
│ CLI + turn   │
│ orchestration│
└──────┬───────┘
       │
       ├──────────────► Conversation
       │
       ├──────────────► LLM client
       │                    │
       │                    ▼
       │                  Ollama
       │
       └──────────────► ToolExecutor
                            │
                            ├── validates tool name
                            ├── validates arguments
                            └── invokes handler
                                     │
                                     ▼
                              python_calculate
                                     │
                                     ▼
                              local Python AST
```

Expected module responsibilities:

```text
tools/registry.py
    tool contracts and provider declarations

tools/executor.py
    handler binding and dispatch

tools/python_calculate.py
    restricted calculation implementation

llm.py
    Ollama communication and tool-call transport

app.py
    CLI presentation and one-tool turn orchestration
```

---

## Tool definition

Register one tool:

```text
python_calculate
```

Suggested specification:

```python
PYTHON_CALCULATE_SPEC = ToolSpec(
    name="python_calculate",
    description=(
        "Evaluate a safe mathematical expression using the local Python runtime. "
        "Use it for arithmetic and supported numeric functions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": (
                    "Mathematical expression, for example: "
                    "(12 + 18 + 27) / 3"
                ),
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "result": {
                "description": "Calculated JSON-compatible result.",
            }
        },
        "required": ["result"],
        "additionalProperties": False,
    },
)
```

The exact description may be refined to improve model tool selection.

The tool accepts exactly one argument:

```json
{
  "expression": "17 * 24"
}
```

Successful result:

```json
{
  "result": 408
}
```

---

## Restricted expression language

### Supported literals

Support:

- integers;
- floating-point numbers;
- lists and tuples containing supported numeric values when needed by allowlisted aggregate functions.

Examples:

```python
17
3.14
[12, 18, 27]
(2, 4, 8)
```

Do not support arbitrary strings, dictionaries, sets, bytes, complex objects, or custom objects.

### Supported arithmetic operators

Support:

```text
+
-
*
/
//
%
**
```

Support unary:

```text
+
-
```

Examples:

```python
17 * 24
(12 + 18 + 27) / 3
2 ** 10
-5 + 12
```

### Supported functions

Use an explicit allowlist.

Required minimum:

```text
abs
round
min
max
sum
len
```

Recommended numeric functions from the standard `math` module:

```text
sqrt
ceil
floor
factorial
log
log10
exp
sin
cos
tan
```

Only direct calls to allowlisted function names are permitted.

Allowed:

```python
sqrt(81)
round(10 / 3, 2)
sum([12, 18, 27]) / len([12, 18, 27])
```

Forbidden:

```python
math.sqrt(81)
some_object.method()
__import__("math").sqrt(81)
```

### Supported constants

Optional allowlisted constants:

```text
pi
e
```

Example:

```python
pi * 5 ** 2
```

No other variable names are permitted.

### Explicitly forbidden syntax

Reject at least:

- imports;
- attribute access;
- subscripting unless specifically required and safely implemented;
- assignments;
- named expressions;
- comprehensions;
- generator expressions;
- lambdas;
- conditional expressions;
- boolean operations;
- comparisons;
- function definitions;
- class definitions;
- loops;
- context managers;
- exception handling;
- f-strings;
- arbitrary names;
- keyword expansion;
- positional expansion;
- access to builtins;
- access to globals or locals.

The evaluator must walk and evaluate an AST using an allowlist.

Do not compile and evaluate the original expression with unrestricted globals.

---

## Resource limits

Even a restricted arithmetic expression can consume excessive resources.

Apply simple deterministic limits.

Required:

- maximum expression length: 500 characters;
- maximum AST node count: 100;
- maximum nesting depth: 20;
- maximum integer exponent magnitude: 1000;
- maximum sequence length: 1000 elements;
- maximum factorial argument: 1000;
- reject non-finite numeric results such as `NaN` and infinity;
- reject a final result that cannot be serialized to JSON.

The exact limits may be constants in the tool module.

Do not add configuration flags unless there is a concrete reason.

A wall-clock timeout is not required because execution remains in-process and the allowlist must prevent unbounded operations.

---

## Tool executor

Introduce a small executor abstraction.

Suggested public interface:

```python
from collections.abc import Callable
from typing import Any


ToolArguments = dict[str, Any]
ToolResult = dict[str, Any]
ToolHandler = Callable[[ToolArguments], ToolResult]


class ToolExecutor:
    def register_handler(self, name: str, handler: ToolHandler) -> None:
        ...

    def execute(self, name: str, arguments: ToolArguments) -> ToolResult:
        ...
```

Equivalent explicit names are acceptable.

### Executor responsibilities

The executor must:

- bind a handler to a tool name already present in `ToolRegistry`;
- reject handler registration for an unknown tool;
- reject duplicate handler registration;
- look up a handler by exact tool name;
- verify that arguments are a dictionary;
- perform basic top-level argument validation;
- invoke the handler;
- verify that the handler returns a dictionary;
- return the structured result;
- convert predictable tool failures into a stable tool error result or typed execution error.

### Registry consistency

A tool may be described but have no handler.

Attempting to execute such a tool must fail clearly:

```text
No handler registered for tool: python_calculate
```

The executor must not silently ignore the request.

### Argument validation

For `python_calculate`, validate:

- arguments contain exactly `expression`;
- `expression` is a string;
- it is not empty;
- it has no leading or trailing whitespace unless the implementation deliberately normalizes it consistently;
- no additional arguments are accepted.

Full generic JSON Schema runtime validation is still not required.

The Python handler may perform tool-specific validation.

---

## Tool result envelope

Tool execution must return a JSON-compatible object.

Successful result:

```json
{
  "ok": true,
  "result": 408
}
```

Failed result:

```json
{
  "ok": false,
  "error": {
    "type": "unsafe_expression",
    "message": "Function '__import__' is not allowed."
  }
}
```

Use stable error categories.

Required minimum:

```text
invalid_arguments
invalid_expression
unsafe_expression
resource_limit
calculation_error
internal_error
```

The tool result sent to the model must not contain:

- Python traceback;
- absolute file paths;
- environment variables;
- object repr containing memory addresses;
- internal exception chains.

Detailed tracebacks may be useful for developers, but must not be exposed to the model or normal CLI output in SPEC-007.

---

## Ollama interaction

### First model request

The first request for a user turn must include:

- current conversation messages;
- tool declarations from `ToolRegistry.to_ollama_tools()`.

Conceptually:

```python
client.chat(
    model=MODEL_NAME,
    messages=messages,
    tools=registry.to_ollama_tools(),
    stream=...,
)
```

Use the actual API and response types from the installed Ollama SDK.

### Possible first response

The model may return:

1. normal assistant content;
2. one tool call;
3. both content and a tool call;
4. multiple tool calls.

SPEC-007 policy:

- normal content only: stream and commit the assistant response normally;
- exactly one supported tool call: execute it;
- content plus one tool call: treat the tool call as authoritative; optional pre-tool content must not be committed as the final answer;
- multiple tool calls: reject the turn as unsupported in this iteration;
- unknown tool: reject clearly;
- malformed arguments: return a structured tool error to the model when possible.

### Tool-call message preservation

When the model requests a tool, the assistant tool-call message must be retained in the temporary per-turn message sequence sent back to Ollama.

Then append the tool result message in the provider-required format.

Conceptually:

```text
user message
assistant message containing tool call
tool result message
```

Use the exact role and tool-call identifiers required by the installed Ollama SDK.

Do not invent a custom provider payload when the SDK already defines the structure.

### Second model request

After execution, call the model again with:

- original conversation context;
- current user message;
- assistant tool-call message;
- tool result message;
- the same available tool declarations.

The second response should be a normal user-facing answer.

If it requests another tool, stop with:

```text
Application error: Additional tool calls are not supported in SPEC-007.
```

No second tool is executed.

---

## Streaming behavior

Tool calls and text streaming are different response modes.

The implementation must preserve streaming for normal assistant text.

### Normal response

```text
first model response contains text
    │
    ▼
stream chunks to CLI
    │
    ▼
assemble complete assistant message
    │
    ▼
commit and persist
```

### Tool-assisted response

```text
first model response contains tool call
    │
    ▼
show tool status
    │
    ▼
execute tool
    │
    ▼
send result to model
    │
    ▼
stream final assistant text
    │
    ▼
commit and persist
```

The implementation may need to inspect Ollama streaming events for tool calls.

Use the behavior supported by the installed Ollama SDK and verify it against the real local model.

Do not display empty `Qwen:` prefixes before knowing whether the first response is text or a tool call.

The final assistant answer after tool execution must stream when supported.

---

## CLI presentation

Add small dedicated rendering helpers or keep simple presentation logic in `app.py`.

Expected output:

```text
[tool] python_calculate
[args] {"expression": "17 * 24"}
[result] {"ok": true, "result": 408}
```

On tool failure:

```text
[tool] python_calculate
[args] {"expression": "__import__('os')"}
[result] {
  "ok": false,
  "error": {
    "type": "unsafe_expression",
    "message": "Function '__import__' is not allowed."
  }
}
```

Requirements:

- use deterministic JSON formatting;
- do not use Python dictionary repr as the external format;
- use `json.dumps`;
- preserve Unicode;
- sort keys only if it improves reproducibility;
- keep output readable;
- do not print hidden reasoning;
- do not print raw Ollama response objects;
- do not print tracebacks.

No Rich/Textual dependency is required.

---

## Conversation and persistence policy

### Semantic conversation history

Persistent conversation history must continue to represent the user-visible conversation.

After a successful tool-assisted turn, persist:

```json
[
  {
    "role": "user",
    "content": "What is 17 multiplied by 24?"
  },
  {
    "role": "assistant",
    "content": "17 multiplied by 24 is 408."
  }
]
```

Do not persist internal tool protocol messages to `data/chat_history.json` in SPEC-007.

Specifically do not persist:

- assistant tool-call message;
- tool result message;
- tool arguments;
- raw model response events;
- intermediate content;
- Python expression separately.

Tool protocol messages exist only in the temporary message sequence used to complete the current turn.

This preserves the current JSON schema and keeps persistent memory user-facing.

### Failed turn rollback

If any stage fails before a complete final assistant answer is produced:

- remove the current user message from `Conversation`;
- do not persist the exchange;
- do not add an assistant message;
- leave the previous JSON file unchanged;
- return to the CLI input loop.

A tool failure may still be sent to the model so the model can explain the failure.

If the model successfully produces a final answer explaining the tool error, that turn may be committed as a successful user-visible exchange.

If the second model call itself fails, roll back the whole turn.

---

## System prompt guidance

Update the system prompt minimally so the model understands the tool contract.

It should instruct the model to:

- use `python_calculate` for calculations when useful;
- provide a valid restricted mathematical expression;
- avoid pretending a tool ran when it did not;
- use the returned tool result when composing the final answer;
- answer normally when no tool is needed.

Do not encode implementation details such as AST classes into the system prompt.

Do not tell the model to expose chain of thought.

---

## Files to add or modify

### `tools/python_calculate.py`

Add:

- `PYTHON_CALCULATE_SPEC`;
- restricted AST evaluator;
- resource limits;
- `python_calculate(arguments)`;
- stable result envelope;
- safe error handling.

This module must not:

- call Ollama;
- print to the CLI;
- access conversation state;
- read or write files;
- access network resources;
- launch subprocesses;
- import arbitrary user-selected modules.

### `tools/executor.py`

Add:

- `ToolExecutor`;
- handler registration;
- dispatch;
- registry consistency checks;
- basic argument/result type checks.

This module must not:

- know CLI formatting;
- call Ollama;
- persist conversation history.

### `tools/__init__.py`

Export the intended public API.

Likely exports:

```python
ToolExecutor
ToolRegistry
ToolSpec
PYTHON_CALCULATE_SPEC
python_calculate
```

Keep exports deliberate.

### `llm.py`

Extend the LLM transport to support:

- tool declarations;
- text response events;
- tool-call response data;
- second request after a tool result.

Do not move tool execution into `llm.py`.

`llm.py` translates between Ollama response structures and harness-level response structures.

A small response type is recommended, for example:

```python
@dataclass(frozen=True)
class ModelToolCall:
    id: str | None
    name: str
    arguments: dict[str, Any]
```

Avoid returning raw Ollama objects to `app.py`.

### `app.py`

Extend turn orchestration:

1. add user message;
2. make first model request with tools;
3. handle normal text or one tool call;
4. render tool status;
5. execute through `ToolExecutor`;
6. create temporary tool protocol messages;
7. make second model request;
8. stream final text;
9. commit one complete assistant message;
10. persist only after successful completion.

Keep existing:

- `/reset`;
- `/bye`;
- rollback principles;
- empty input behavior.

### `prompts.py`

Add concise tool-use guidance.

### `conversation.py`

No persistent schema expansion is required.

A helper may be added only if it improves temporary message handling without mixing provider-specific tool protocol into persistent semantic history.

Preferred: keep temporary provider messages outside `Conversation`.

### `storage.py`

No schema change.

### `config.py`

No new configuration is required unless expression limits are intentionally centralized.

Prefer tool-local constants for SPEC-007.

### `README.md`

Document:

- the first available tool;
- visible CLI tool status;
- Python executes locally in the current process and venv;
- the tool is a restricted calculator, not arbitrary Python execution;
- one tool call per turn is currently supported.

---

## Public interfaces

### Python calculation handler

Suggested:

```python
def python_calculate(arguments: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one restricted mathematical expression."""
```

### Tool executor

Suggested:

```python
executor = ToolExecutor(registry)
executor.register_handler("python_calculate", python_calculate)

result = executor.execute(
    "python_calculate",
    {"expression": "(12 + 18 + 27) / 3"},
)
```

Expected result:

```python
{
    "ok": True,
    "result": 19.0,
}
```

### LLM response abstraction

The exact design may vary, but application code must be able to distinguish:

```text
text response
```

from:

```text
tool call response
```

without inspecting raw Ollama SDK internals.

---

## Error handling

### Invalid model tool call

Examples:

- unknown tool;
- missing arguments;
- arguments not an object;
- multiple calls;
- invalid expression type.

The harness must not crash.

It should either:

- produce a structured tool error and let the model explain it;
- or fail the turn clearly when the protocol cannot be continued safely.

### Calculation errors

Examples:

```python
1 / 0
sqrt(-1)
factorial(-1)
```

Return:

```json
{
  "ok": false,
  "error": {
    "type": "calculation_error",
    "message": "Division by zero."
  }
}
```

Use concise stable messages.

### Unsafe expression

Examples:

```python
__import__("os")
open("/etc/passwd")
(1).__class__
```

Return:

```json
{
  "ok": false,
  "error": {
    "type": "unsafe_expression",
    "message": "The expression contains unsupported syntax."
  }
}
```

Do not reveal evaluator internals unnecessarily.

### Ollama failure

If either model request fails:

- print an application error;
- roll back the current user message;
- do not persist the turn;
- keep the application usable.

### Keyboard interruption

If interrupted during:

- first model generation;
- tool execution;
- second model generation;

then:

- print a short interruption message;
- roll back the turn;
- do not persist partial state;
- return to the input loop.

Because calculation is in-process and bounded, tool execution should be fast.

---

## Non-goals

Explicitly outside SPEC-007:

- arbitrary Python code execution;
- `exec`;
- unrestricted `eval`;
- filesystem access;
- network access;
- environment-variable access;
- subprocess execution;
- package installation;
- importing user-selected modules;
- persistent Python variables;
- notebooks;
- pandas;
- NumPy;
- plotting;
- file generation;
- Python sandbox containers;
- process isolation;
- OS-level resource controls;
- asynchronous tools;
- parallel tools;
- multiple tool calls in one response;
- repeated tool calls in one turn;
- autonomous planning;
- general agent loop;
- SQL;
- MCP;
- permissions UI;
- user confirmation before calculation;
- tool audit database;
- persistence of tool calls;
- tool-call replay;
- tool metrics;
- retries;
- third-party agent frameworks;
- full generic JSON Schema validation;
- exposing model chain of thought.

---

## Acceptance criteria

### AC-1: Tool registration

`python_calculate` is represented by a valid `ToolSpec`, registered in `ToolRegistry`, and bound to one handler in `ToolExecutor`.

### AC-2: Ollama receives tools

The first model request includes the registry-generated Ollama tool declarations.

No duplicate hand-written tool schema exists in `llm.py` or `app.py`.

### AC-3: Normal text regression

For a request that does not need calculation:

- the model answers normally;
- text streams progressively;
- no tool status is printed;
- one user and one assistant message are persisted.

### AC-4: Model-selected calculation

For a calculation request:

- the model requests `python_calculate`;
- the harness receives structured arguments;
- the handler executes;
- the result is returned to the model;
- the model produces a final answer.

### AC-5: CLI transparency

The CLI displays:

- tool name;
- arguments as JSON;
- result as JSON.

It does not display hidden reasoning or raw provider objects.

### AC-6: Correct local runtime

The calculation handler runs in the same Python process and interpreter as the CLI.

No request is sent to an external Python execution service.

### AC-7: Safe arithmetic

Expressions such as these succeed:

```python
17 * 24
(12 + 18 + 27) / 3
2 ** 10
round(10 / 3, 2)
sqrt(81)
sum([12, 18, 27]) / len([12, 18, 27])
```

### AC-8: Unsafe syntax rejection

Expressions such as these do not execute:

```python
__import__("os")
open("file.txt")
(1).__class__
globals()
lambda: 1
[x for x in range(10)]
```

The application remains usable.

### AC-9: No unrestricted evaluation

The implementation does not pass the user/model expression directly to unrestricted `eval()` or `exec()`.

### AC-10: Resource limits

Oversized, deeply nested, or excessive expressions fail with a stable `resource_limit` error.

### AC-11: Structured success result

Successful handler execution returns a JSON-compatible object containing:

```json
{
  "ok": true,
  "result": ...
}
```

### AC-12: Structured failure result

Predictable failures return:

```json
{
  "ok": false,
  "error": {
    "type": "...",
    "message": "..."
  }
}
```

No traceback is returned to the model.

### AC-13: Second model response

After tool execution, the model receives the tool-call context and tool result in the Ollama-supported message format.

It uses the result to generate the final answer.

### AC-14: One-tool limit

If the first model response contains multiple tool calls, or the second response requests another tool:

- no additional tool is executed;
- the turn fails clearly or returns a controlled explanation;
- the application does not enter an unbounded loop.

### AC-15: Persistent history remains semantic

After a successful tool-assisted turn, persistent JSON contains only:

- the user message;
- the final assistant answer.

It does not contain tool protocol messages.

### AC-16: Failure rollback

If the tool path cannot produce a complete final answer:

- the current user message is removed;
- no assistant message is added;
- JSON remains unchanged.

### AC-17: Commands unchanged

`/reset` and `/bye` continue to behave as before.

### AC-18: Architectural boundaries

- registry owns metadata;
- executor owns dispatch;
- handler owns calculation;
- LLM layer owns Ollama transport;
- CLI owns presentation and turn orchestration;
- storage remains unaware of tools.

---

## Manual verification scenarios

### Scenario 1: Direct handler arithmetic

Run a temporary Python snippet:

```bash
python - <<'PY'
from tools import python_calculate

print(python_calculate({"expression": "17 * 24"}))
print(python_calculate({"expression": "(12 + 18 + 27) / 3"}))
print(python_calculate({"expression": "sqrt(81)"}))
PY
```

Expected successful structured results.

### Scenario 2: Direct unsafe-expression checks

```bash
python - <<'PY'
from tools import python_calculate

expressions = [
    '__import__("os")',
    'open("file.txt")',
    '(1).__class__',
    'globals()',
    '[x for x in range(10)]',
]

for expression in expressions:
    print(expression)
    print(python_calculate({"expression": expression}))
PY
```

Expected:

- every expression is rejected;
- no side effect occurs;
- no traceback is exposed.

### Scenario 3: Executor dispatch

Create a registry and executor, register the spec and handler, then call:

```python
executor.execute(
    "python_calculate",
    {"expression": "2 ** 10"},
)
```

Expected:

```json
{
  "ok": true,
  "result": 1024
}
```

### Scenario 4: Unknown tool

Execute:

```python
executor.execute("unknown_tool", {})
```

Expected clear controlled failure.

### Scenario 5: Normal chat without tool

Run:

```bash
python app.py
```

Ask:

```text
Explain the difference between a list and a tuple.
```

Expected:

- no tool block;
- normal streamed answer;
- successful persistence.

### Scenario 6: Simple model-selected tool

Ask:

```text
What is 173 multiplied by 284?
```

Expected:

```text
[tool] python_calculate
```

with valid arguments and result, followed by a streamed final answer containing the correct value.

### Scenario 7: Aggregate calculation

Ask:

```text
Calculate the average of 12, 18, and 27.
```

Expected:

- the model calls the tool;
- the expression is accepted;
- the result is 19 or 19.0;
- the final answer is natural language.

### Scenario 8: Unsafe user request

Ask the model to calculate by using:

```text
Run __import__("os").listdir(".")
```

Expected:

- the operation is not executed;
- the tool returns an unsafe-expression error, or the model refuses to call it;
- no directory data is exposed;
- the CLI remains usable.

### Scenario 9: Persistence inspection

After a successful tool-assisted turn:

```bash
cat data/chat_history.json
```

Expected:

- exactly one user message for the request;
- exactly one final assistant message;
- no `tool` role;
- no raw expression metadata;
- no intermediate assistant tool-call record.

### Scenario 10: Ollama failure after tool execution

Force the second model call to fail.

Expected:

- the CLI may already show the tool result;
- the user message is rolled back;
- no final assistant message is persisted;
- previous JSON content remains unchanged;
- the next CLI turn works.

### Scenario 11: Reset and exit

After using the tool:

```text
/reset
/bye
```

Expected unchanged command behavior.

---

## Definition of done

SPEC-007 is complete when:

1. `python_calculate` is registered and executable.
2. The expression evaluator uses an AST allowlist.
3. Unsafe Python capabilities are inaccessible.
4. Resource limits are enforced.
5. `ToolExecutor` dispatches registered handlers.
6. Ollama can request the tool through its native tool-call format.
7. The result is sent back to the model.
8. The final answer streams to the CLI.
9. The CLI visibly reports the tool call, arguments, and result.
10. Hidden chain of thought is not displayed.
11. Only one tool execution is allowed per turn.
12. Persistent conversation history contains only semantic user-facing messages.
13. Existing commands and rollback behavior remain intact.
14. README and iteration journal are updated.
15. The implementation is verified against the real local model.

---

## Journal requirements

Create the normal iteration journal entry for SPEC-007.

Record at least:

- branch name;
- implementation files changed;
- exact tool schema;
- exact handler and executor public interfaces;
- Python interpreter and virtual environment used;
- confirmation that execution occurred in the CLI process;
- Ollama SDK and server versions;
- local model name and digest;
- observed Ollama tool-call payload shape;
- successful calculation examples;
- rejected unsafe-expression examples;
- resource-limit checks;
- CLI output example;
- persistence inspection;
- normal text regression result;
- failure rollback result;
- one-tool-limit verification;
- merge commit SHA after merge.

Suggested branch:

```text
feature/SPEC-007-python-calculation-tool
```

Suggested spec file:

```text
specs/SPEC-007-Python-Calculation-Tool.md
```

Suggested journal file:

```text
docs/journal/SPEC-007-Python-Calculation-Tool.md
```

---

## Expected outcome

After SPEC-007, the project performs its first real tool-assisted turn:

```text
User asks for calculation
        │
        ▼
Model requests python_calculate
        │
        ▼
Harness shows the request in CLI
        │
        ▼
ToolExecutor invokes the local safe handler
        │
        ▼
Result returns to the model
        │
        ▼
Model streams the final answer
```

This iteration proves the essential harness mechanism:

```text
Registry
→ model tool selection
→ controlled local execution
→ tool result
→ final model answer
```

The next steps can reuse the same execution path for SQL and later MCP tools without turning SPEC-007 into a full autonomous agent.
