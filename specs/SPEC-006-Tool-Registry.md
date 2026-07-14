# SPEC-006: Tool Registry

## Background

The application is currently a local CLI chat built around Ollama.

The current runtime flow is:

```text
User input
    │
    ▼
Conversation
    │
    ▼
LLM client
    │
    ▼
Ollama
    │
    ▼
Streamed assistant response
```

The model can generate text, but the harness does not yet have a formal concept of a tool.

The project roadmap introduces tools incrementally:

```text
Tool Registry
    │
    ▼
Tool execution
    │
    ▼
Python tool
    │
    ▼
SQL tool
    │
    ▼
MCP
    │
    ▼
Agent loop
```

Before the harness can execute tools, it needs one authoritative place that describes which tools exist and what contract each tool exposes.

Without a registry, later integrations would likely define tool names, descriptions, arguments, and return formats in different places. That would create duplicated metadata and make it difficult to:

- show the model which tools are available;
- validate tool definitions consistently;
- find a tool by name;
- execute the correct implementation later;
- document tool inputs and outputs;
- add Python, SQL, and MCP tools through one common interface.

The purpose of this iteration is to introduce that foundation without implementing tool execution or agent behavior yet.

---

## Goal

Create a small, explicit, framework-independent tool registry.

The registry must provide a single source of truth for:

- tool name;
- human-readable description;
- input contract;
- output contract;
- lookup by tool name;
- deterministic enumeration of registered tools;
- conversion of registered input contracts into the function-tool format expected by Ollama.

After this iteration, application code must be able to define tool metadata, register it, inspect the available tools, retrieve one tool by name, and generate Ollama-compatible tool declarations.

The registry must not execute tools in SPEC-006.

---

## Core architectural decision

A tool has two distinct aspects:

```text
Tool contract
```

and:

```text
Tool execution
```

They are related, but they are not the same responsibility.

The tool contract answers:

```text
What is this tool called?
What does it do?
What arguments does it accept?
What result does it return?
```

Tool execution answers:

```text
Which Python function should run?
How are arguments validated at runtime?
How are errors represented?
How is the result returned to the model?
```

SPEC-006 implements only the first aspect.

```text
SPEC-006
Tool definitions + registry + Ollama declarations

SPEC-007 and later
Handlers + execution + tool results + model interaction
```

The registry must therefore remain useful before any real handler exists.

---

## Target architecture

```text
                           ┌──────────────────────────┐
                           │       ToolRegistry       │
                           │                          │
                           │ register(ToolSpec)       │
                           │ get(name)                │
                           │ list_tools()             │
                           │ to_ollama_tools()        │
                           └────────────┬─────────────┘
                                        │
                                        │ contains
                                        ▼
                           ┌──────────────────────────┐
                           │         ToolSpec         │
                           │                          │
                           │ name                     │
                           │ description              │
                           │ input_schema             │
                           │ output_schema            │
                           └──────────────────────────┘
```

Future iterations will extend the architecture:

```text
ToolRegistry
    │
    ├── ToolSpec
    │
    └── execution binding      ← later
            │
            ▼
        Python / SQL / MCP
```

SPEC-006 must not force the project to redesign the registry when execution is added, but it also must not implement speculative execution abstractions now.

---

## Design principles

### 1. One authoritative registry

The application must have one registry abstraction responsible for all available tool definitions.

Tool metadata must not be duplicated across:

- `app.py`;
- `llm.py`;
- system prompts;
- individual tool modules;
- ad hoc dictionaries created at call sites.

Future code should ask the registry for the available tools.

### 2. Tool definitions are data

A tool definition must be represented as a simple immutable data object.

Preferred representation:

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
```

An equivalent simple typed representation is acceptable if it preserves the same responsibilities.

Do not introduce a deep class hierarchy.

### 3. JSON Schema is the contract language

Tool arguments and tool results must be described using JSON-Schema-like dictionaries.

Example input schema:

```python
{
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "Mathematical expression to evaluate.",
        }
    },
    "required": ["expression"],
    "additionalProperties": False,
}
```

Example output schema:

```python
{
    "type": "object",
    "properties": {
        "result": {
            "type": "number",
        }
    },
    "required": ["result"],
    "additionalProperties": False,
}
```

SPEC-006 stores and checks the basic shape of these schemas.

It does not implement full JSON Schema validation of runtime values.

### 4. Internal contract and provider format are separate

The internal `ToolSpec` contains both input and output contracts.

Ollama function declarations expose the tool name, description, and input parameters.

The registry must provide a conversion boundary:

```text
Internal ToolSpec
        │
        ▼
Ollama-compatible tool declaration
```

Ollama-specific dictionary construction must be localized in one method or function.

The internal model must not be reduced to the exact Ollama payload shape because future MCP or other providers may need additional metadata.

### 5. No framework dependency

Use only:

- Python standard library;
- existing project dependencies where already required.

Do not introduce:

- LangChain;
- LangGraph;
- AutoGen;
- CrewAI;
- Pydantic solely for this registry;
- external JSON Schema validators;
- dependency injection frameworks;
- plugin frameworks;
- MCP libraries.

The goal is to understand and own the minimal harness abstraction directly.

### 6. Deterministic behavior

Registry enumeration must preserve registration order.

The same registered definitions must produce the same ordered Ollama tool list.

Deterministic ordering simplifies:

- debugging;
- prompt inspection;
- manual verification;
- future tests;
- reproducible journal entries.

---

## Domain model

### `ToolSpec`

Introduce an immutable tool specification.

Suggested interface:

```python
from dataclasses import dataclass
from typing import Any


JsonSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JsonSchema
    output_schema: JsonSchema
```

The exact alias name may differ.

The object must not contain:

- an executable Python callable;
- Ollama client objects;
- model messages;
- CLI rendering logic;
- persistence logic;
- mutable registry state.

### Tool name

A tool name is the stable machine-facing identifier used for lookup and future model tool calls.

Examples:

```text
python_execute
sql_query
tracker_search
calendar_list_events
```

Required rules:

- must be a non-empty string;
- leading and trailing whitespace must not be accepted silently;
- must use lowercase ASCII letters, digits, and underscores;
- must start with a lowercase letter;
- must be unique within one registry.

Recommended validation pattern:

```text
^[a-z][a-z0-9_]*$
```

The registry must fail clearly for invalid names.

Do not automatically rewrite names. For example, do not silently turn `SQL Query` into `sql_query`.

### Description

The description is written for the model and for humans inspecting the registry.

It must:

- be a non-empty string;
- explain what the tool does;
- be concise enough to send to the model later.

Leading and trailing whitespace must not be accepted silently.

The registry does not need to enforce a maximum description length in this iteration.

### Input schema

`input_schema` describes arguments accepted by the tool.

Minimum required shape:

```python
{
    "type": "object",
    "properties": {...},
}
```

Rules:

- it must be a dictionary;
- its top-level `type` must equal `"object"`;
- `properties` must exist and be a dictionary;
- `required`, when present, must be a list of strings;
- `additionalProperties`, when present, must be a boolean.

Do not implement full recursive JSON Schema validation.

The registry is responsible only for catching malformed top-level contracts early.

### Output schema

`output_schema` describes the successful result returned by the future tool implementation.

It follows the same minimum top-level rules as `input_schema`:

```python
{
    "type": "object",
    "properties": {...},
}
```

The output schema is internal harness metadata in SPEC-006.

It is not included in the Ollama function declaration because the current Ollama tool format is centered on callable function parameters.

The output schema will be used by later iterations to define and validate tool result envelopes.

---

## Registry behavior

### Construction

Suggested interface:

```python
registry = ToolRegistry()
```

A new registry starts empty.

No global mutable singleton is required.

The application may create a registry explicitly where composition occurs.

### Registration

Suggested interface:

```python
registry.register(tool_spec)
```

On successful registration:

- the tool becomes available by name;
- it appears once in registry enumeration;
- registration order is preserved.

Registration must fail if:

- the argument is not a `ToolSpec`;
- the name is invalid;
- the description is empty or padded with whitespace;
- either schema has an invalid top-level shape;
- another tool with the same name is already registered.

Do not overwrite an existing tool silently.

A duplicate name must raise a clear exception such as:

```python
ValueError("Tool 'python_execute' is already registered.")
```

### Lookup

Suggested interface:

```python
tool = registry.get("python_execute")
```

For a known name, return the exact registered `ToolSpec`.

For an unknown name, raise a clear lookup error.

Preferred behavior:

```python
KeyError("Unknown tool: python_execute")
```

Returning `None` is not preferred because it moves a configuration error farther from its source.

### Enumeration

Suggested interface:

```python
tools = registry.list_tools()
```

Required behavior:

- returns all registered definitions;
- preserves registration order;
- does not expose the registry's mutable internal collection;
- callers cannot mutate registry state by modifying the returned collection.

Returning a tuple is preferred:

```python
tuple[ToolSpec, ...]
```

### Length and membership

Small convenience behavior is acceptable:

```python
len(registry)
"name" in registry
```

These are optional, not required.

Do not add broad collection emulation without a concrete use case.

### Ollama declarations

Suggested interface:

```python
ollama_tools = registry.to_ollama_tools()
```

For each registered `ToolSpec`, return a dictionary in this conceptual form:

```python
{
    "type": "function",
    "function": {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.input_schema,
    },
}
```

Required behavior:

- one declaration per registered tool;
- registration order is preserved;
- `output_schema` is not included;
- returned dictionaries are safe for callers to inspect or modify without mutating the stored `ToolSpec`;
- an empty registry returns an empty list.

The conversion method must not call Ollama.

It only prepares provider-compatible data.

---

## Validation strategy

Validation must happen when a tool is registered.

This gives one clear lifecycle:

```text
Create ToolSpec
    │
    ▼
Register ToolSpec
    │
    ├── invalid → fail immediately
    │
    └── valid   → registry may expose it
```

Do not defer obvious contract errors until model invocation.

### Required validation errors

The implementation must produce clear errors for at least:

- empty name;
- padded name;
- invalid characters in name;
- uppercase characters in name;
- name beginning with a digit or underscore;
- empty description;
- padded description;
- non-dictionary schema;
- top-level schema type other than `object`;
- missing or non-dictionary `properties`;
- non-list `required`;
- `required` containing non-string values;
- non-boolean `additionalProperties`;
- duplicate tool name.

Exact error wording may differ, but errors must identify the invalid field or tool.

### Schema copying and immutability

`ToolSpec` being frozen does not make nested schema dictionaries immutable.

The implementation must prevent accidental external mutation from changing the registered contract.

At registration time, the registry must store an independent deep copy of both schemas, or otherwise provide equivalent isolation.

Likewise, exported Ollama declarations must not share mutable nested dictionaries with registry-owned state.

Use the Python standard library, for example `copy.deepcopy`.

Do not introduce a custom immutable JSON tree abstraction in this iteration.

---

## Example definition

SPEC-006 must include one metadata-only sample definition for manual verification.

The example is not a real executed tool.

Recommended example:

```python
CALCULATOR_SPEC = ToolSpec(
    name="calculator",
    description="Evaluate a mathematical expression and return the numeric result.",
    input_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Expression to evaluate, for example: 17 * 24.",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "result": {
                "type": "number",
                "description": "Calculated numeric result.",
            }
        },
        "required": ["result"],
        "additionalProperties": False,
    },
)
```

This sample proves that the registry can describe a future tool.

It must not evaluate the expression.

The real Python/calculation implementation belongs to the next tool-execution iteration.

The sample may live only in a manual verification script or documentation rather than production startup code.

---

## Files to add or modify

### `tools/registry.py`

Add the registry implementation.

Responsibilities:

- define `ToolSpec`;
- define the JSON schema type alias if useful;
- validate tool metadata at registration;
- store isolated definitions;
- reject duplicate names;
- support lookup;
- support deterministic enumeration;
- convert definitions into Ollama-compatible declarations.

This module must not:

- import the Ollama client;
- execute Python;
- execute SQL;
- print to the terminal;
- read environment variables;
- persist data;
- mutate conversation history.

### `tools/__init__.py`

Export the intended public API, for example:

```python
from tools.registry import ToolRegistry, ToolSpec

__all__ = ["ToolRegistry", "ToolSpec"]
```

Keep the package surface small.

### `README.md`

Update the project structure description to explain that `tools/` now contains the tool contract and registry foundation.

Add a short status note that tools are described but not executed yet.

Do not document future Python or SQL behavior as if it already exists.

### `app.py`

No runtime integration is required.

The normal chat behavior must remain unchanged.

Do not create and populate a registry in `app.py` only to leave it unused.

### `llm.py`

No changes are required.

Do not send tool declarations to Ollama in SPEC-006.

That integration belongs to the iteration that introduces actual tool selection and execution.

### `conversation.py`

No changes are required.

Tool calls and tool results are not conversation messages yet.

### `storage.py`

No changes are required.

The persisted JSON schema must remain unchanged.

### `config.py`

No changes are required.

Do not introduce tool feature flags in this iteration.

---

## Public interface

Expected usage:

```python
from tools import ToolRegistry, ToolSpec


registry = ToolRegistry()

registry.register(
    ToolSpec(
        name="calculator",
        description="Evaluate a mathematical expression and return the numeric result.",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "result": {"type": "number"},
            },
            "required": ["result"],
            "additionalProperties": False,
        },
    )
)

calculator = registry.get("calculator")
all_tools = registry.list_tools()
ollama_tools = registry.to_ollama_tools()
```

Expected `ollama_tools` value:

```python
[
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": (
                "Evaluate a mathematical expression and return the numeric result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    }
]
```

This code is illustrative.

The implementation may use equivalent method names only if the resulting API remains explicit and simple.

---

## Error handling

Registry errors are configuration or development errors, not conversational model errors.

They should fail immediately and clearly.

Preferred standard exceptions:

- `TypeError` for a wrong Python object type;
- `ValueError` for malformed definitions and duplicate names;
- `KeyError` for unknown tool lookup.

Do not catch these exceptions inside the registry merely to print messages.

Do not return error dictionaries such as:

```python
{"ok": False, "error": "..."}
```

Tool result envelopes will be designed when execution is introduced.

---

## Non-goals

The following are explicitly outside the scope of SPEC-006:

- executing any tool;
- storing Python callables in the registry;
- evaluating mathematical expressions;
- Python sandboxing;
- SQL connections or query execution;
- MCP clients or servers;
- sending tools to Ollama during normal chat;
- asking the model to select a tool;
- parsing model tool calls;
- adding `tool` role messages;
- returning tool results to the model;
- retries;
- timeouts;
- permissions;
- authentication;
- authorization;
- rate limiting;
- audit logging;
- persistence of registry definitions;
- dynamic discovery from the filesystem;
- decorators for tool registration;
- automatic schema generation from function signatures;
- importing tools by module scanning;
- hot reload;
- tool versioning;
- namespacing;
- aliases;
- deprecation metadata;
- full JSON Schema validation;
- Pydantic models;
- agent loops;
- LangChain or another agent framework;
- MCP schema translation;
- unit test framework introduction solely for this step.

These concerns may be introduced later when there is a concrete requirement.

---

## Acceptance criteria

### AC-1: Empty registry

Given a newly created `ToolRegistry`:

- `list_tools()` returns an empty immutable collection;
- `to_ollama_tools()` returns an empty list;
- normal chat behavior remains unchanged.

### AC-2: Valid registration

Given a valid `ToolSpec`:

- registration succeeds;
- the tool is available by its exact name;
- enumeration contains the tool exactly once.

### AC-3: Deterministic order

Given tools registered in order:

```text
calculator
sql_query
tracker_search
```

`list_tools()` and `to_ollama_tools()` return them in the same order.

### AC-4: Duplicate protection

Registering a second tool with the same name fails clearly.

The first registered definition remains unchanged.

The registry does not silently overwrite it.

### AC-5: Name validation

Invalid names are rejected, including at least:

```text
""
" calculator"
"calculator "
"Calculator"
"sql-query"
"_sql_query"
"1st_tool"
```

A valid name such as `sql_query` is accepted.

### AC-6: Description validation

An empty or whitespace-padded description is rejected.

A concise non-empty description is accepted.

### AC-7: Input schema validation

Registration rejects an input schema when:

- it is not a dictionary;
- top-level `type` is not `object`;
- `properties` is missing;
- `properties` is not a dictionary;
- optional `required` is not a list of strings;
- optional `additionalProperties` is not a boolean.

### AC-8: Output schema validation

The same minimum top-level validation is applied to `output_schema`.

### AC-9: Known lookup

`get(name)` returns the registered tool definition for a known exact name.

### AC-10: Unknown lookup

`get(name)` for an unknown tool raises a clear `KeyError`.

It does not return `None`.

### AC-11: Ollama conversion

For every registered tool, `to_ollama_tools()` returns:

```python
{
    "type": "function",
    "function": {
        "name": ...,
        "description": ...,
        "parameters": ...,
    },
}
```

The parameters equal the registered input contract.

The output contract is not included.

No Ollama network request occurs.

### AC-12: Mutation isolation

After registration, mutating the original schema dictionaries does not change the registry-owned definition.

Mutating a dictionary returned by `to_ollama_tools()` does not change future registry output.

Mutating the collection returned by enumeration is not possible or does not affect registry state.

### AC-13: No execution

The registry does not:

- hold or invoke a tool handler;
- evaluate the calculator example;
- call Python execution APIs;
- connect to a database;
- call Ollama.

### AC-14: Existing chat regression

Running:

```bash
python app.py
```

continues to provide the same streaming chat behavior, including:

- normal responses;
- `/reset`;
- `/bye`;
- persistence;
- rollback on failed generation.

SPEC-006 must not alter the conversation JSON schema.

### AC-15: Architectural boundaries

- `tools/registry.py` does not import or call Ollama.
- `llm.py` does not own registry state.
- `app.py` does not duplicate tool schemas.
- `conversation.py` does not know about tool definitions.
- `storage.py` does not persist the registry.
- no real tool implementation is added.

---

## Manual verification scenarios

### Scenario 1: Register and inspect one tool

From the project root, run a temporary Python snippet:

```bash
python - <<'PY'
from tools import ToolRegistry, ToolSpec

registry = ToolRegistry()

registry.register(
    ToolSpec(
        name="calculator",
        description="Evaluate a mathematical expression and return the numeric result.",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "result": {"type": "number"},
            },
            "required": ["result"],
            "additionalProperties": False,
        },
    )
)

print(registry.get("calculator"))
print(registry.list_tools())
print(registry.to_ollama_tools())
PY
```

Expected:

- the definition is printed;
- one tool appears in enumeration;
- one Ollama function declaration is produced;
- no model request occurs;
- no expression is evaluated.

### Scenario 2: Registration order

Register three valid definitions in this order:

```text
calculator
sql_query
tracker_search
```

Print their names from both:

```python
registry.list_tools()
registry.to_ollama_tools()
```

Expected:

```text
calculator
sql_query
tracker_search
```

in both cases.

### Scenario 3: Duplicate name

Register `calculator` twice.

Expected:

- the second registration raises `ValueError`;
- the registry still contains exactly one `calculator`;
- the original definition remains available.

### Scenario 4: Invalid names

Try registering each of:

```text
Calculator
sql-query
_sql_query
1st_tool
```

Expected:

- every registration fails clearly;
- no invalid tool appears in the registry.

### Scenario 5: Invalid schemas

Try definitions with:

```python
{"type": "string", "properties": {}}
```

and:

```python
{"type": "object"}
```

and:

```python
{
    "type": "object",
    "properties": {},
    "required": "expression",
}
```

Expected:

- registration fails;
- the error points to the malformed schema field.

### Scenario 6: Original schema mutation

Create a valid input schema in a variable, register the tool, then mutate the original variable:

```python
input_schema["properties"]["unexpected"] = {"type": "string"}
```

Expected:

- the registered tool does not gain `unexpected`;
- exported Ollama declarations remain unchanged.

### Scenario 7: Export mutation

Call:

```python
exported = registry.to_ollama_tools()
```

Mutate a nested field in `exported`, then call `to_ollama_tools()` again.

Expected:

- the second export contains the original registered contract;
- caller mutation did not corrupt registry state.

### Scenario 8: Existing application regression

Run:

```bash
python app.py
```

Perform:

1. one normal chat exchange;
2. one follow-up that uses previous context;
3. `/reset`;
4. another normal exchange;
5. `/bye`.

Expected:

- streaming behavior is unchanged;
- complete successful messages are persisted;
- the system prompt is not persisted;
- no tool metadata appears in `data/chat_history.json`.

---

## Definition of done

SPEC-006 is complete when:

1. `ToolSpec` and `ToolRegistry` exist under `tools/`.
2. Valid tool definitions can be registered and retrieved.
3. Invalid and duplicate definitions fail clearly.
4. Registry order is deterministic.
5. Input and output contracts are represented explicitly.
6. Ollama-compatible function declarations can be generated without calling Ollama.
7. Mutable schema data is isolated from callers.
8. No tool can be executed yet.
9. Existing streaming chat behavior and persistence remain unchanged.
10. README documentation reflects the new registry foundation.
11. A journal entry records implementation decisions and manual verification results.

---

## Journal requirements

Create the normal iteration journal entry for SPEC-006.

Record at least:

- branch name;
- implementation files changed;
- final public API;
- validation rules implemented;
- duplicate-name behavior;
- mutation-isolation verification;
- exact Ollama declaration produced by the sample tool;
- confirmation that no Ollama call occurred during registry verification;
- regression result for the existing CLI chat;
- model and Ollama versions used for the regression check;
- merge commit SHA after merge.

Suggested branch:

```text
feature/SPEC-006-tool-registry
```

Suggested spec file:

```text
specs/SPEC-006-Tool-Registry.md
```

Suggested journal file:

```text
docs/journal/SPEC-006-Tool-Registry.md
```

---

## Expected outcome

After SPEC-006, the project has a clear vocabulary and contract for tools:

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

The harness still behaves as a normal streaming chat.

No tool is executed yet.

The next iteration can add execution deliberately, using the registry as the source of truth rather than inventing a second parallel tool model.
