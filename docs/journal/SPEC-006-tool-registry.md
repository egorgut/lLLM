# SPEC-006 — Tool Registry

- **Spec:** [SPEC-006](../../specs/SPEC-006-Tool-Registry.md)
- **Date:** 2026-07-14
- **Branch:** feature/SPEC-006-tool-registry
- **Merge commit:** _pending merge into `main`_

## Hypothesis / intent
Before the harness can execute tools (SPEC-007+: Python, SQL, MCP, agent loop), it
needs one authoritative place that describes *which tools exist and what each
accepts and returns*. Without it, tool names, descriptions, arguments, and result
shapes would be duplicated across `app.py`, `llm.py`, prompts, and ad-hoc
dictionaries. SPEC-006 introduces only the **contract** aspect of a tool — a
`ToolSpec` value object and a `ToolRegistry` — plus a conversion boundary into
Ollama's function-tool format. It deliberately implements **no execution**: no
handlers, no evaluation, no sending tools to Ollama during chat. The existing
streaming chat and its persisted JSON schema must remain untouched.

## What changed
- `tools/registry.py` (new): defines `ToolSpec` and `ToolRegistry`.
  - `ToolSpec` — `@dataclass(frozen=True)` with `name`, `description`,
    `input_schema`, `output_schema` (alias `JsonSchema = dict[str, Any]`). Data
    only: no callable, no Ollama objects, no execution/rendering/persistence.
  - `ToolRegistry` — backed by an insertion-ordered `dict[str, ToolSpec]`.
    `register()` validates then stores a `ToolSpec` holding `copy.deepcopy` of
    both schemas; `get()` returns the stored spec or raises `KeyError`;
    `list_tools()` returns a `tuple[ToolSpec, ...]` in registration order;
    `to_ollama_tools()` returns one `{"type": "function", "function": {...}}` dict
    per tool (parameters = a fresh deep copy of `input_schema`; `output_schema`
    omitted). Small `__len__` / `__contains__` conveniences. Module imports no
    Ollama client, prints nothing, reads no env, persists nothing.
- `tools/__init__.py`: exports `ToolRegistry, ToolSpec` (first `__all__` in the
  codebase).
- `README.md`: `tools/` row now describes the contract + registry; a status
  paragraph notes tools are **described but not executed yet**.
- `app.py`, `llm.py`, `conversation.py`, `storage.py`, `config.py`, `prompts.py`:
  **unchanged** (verified byte-identical to `main` via `git diff`). No registry is
  created in `app.py`; no tool feature flag added; the persisted JSON schema is
  unchanged.

## Final public API
```python
from tools import ToolRegistry, ToolSpec

registry = ToolRegistry()
registry.register(ToolSpec(name, description, input_schema, output_schema))
registry.get(name)            # -> ToolSpec           (KeyError if unknown)
registry.list_tools()         # -> tuple[ToolSpec, ...] (registration order)
registry.to_ollama_tools()    # -> list[dict]          (registration order)
len(registry)                 # -> int
name in registry              # -> bool
```

## Validation rules (enforced at registration)
- **Name:** `str`, non-empty, matches `^[a-z][a-z0-9_]*$`. Rejected examples:
  `""`, `" calculator"`, `"calculator "`, `"Calculator"`, `"sql-query"`,
  `"_sql_query"`, `"1st_tool"`. Names are never rewritten/stripped.
- **Description:** `str`, non-empty after strip, and not whitespace-padded.
- **Schemas (input & output):** must be a `dict`; top-level `type == "object"`;
  `properties` present and a `dict`; `required` (if present) a list of strings;
  `additionalProperties` (if present) a bool. No recursive JSON-Schema validation.
- Errors: `TypeError` for a non-`ToolSpec` argument / non-str name / non-str
  description; `ValueError` for malformed definitions and duplicate names;
  `KeyError` for unknown lookup. No error dicts; no catch-to-print.

## Duplicate-name behavior
Re-registering an existing name raises
`ValueError("Tool 'calculator' is already registered.")`. The registry is not
overwritten: it still contains exactly one `calculator`, and `get("calculator")`
returns the original object (identity preserved).

## Mutation-isolation verification
- Mutating the caller's original `input_schema` dict after registration does
  **not** add keys to the stored spec, and `to_ollama_tools()` output is unchanged
  (registration deep-copies both schemas). *(Scenario 6.)*
- Mutating a dict returned by `to_ollama_tools()` (including nested
  `function.parameters.properties` and `function.name`) does **not** affect a
  subsequent `to_ollama_tools()` call (each export deep-copies parameters).
  *(Scenario 7.)*
- `list_tools()` returns a `tuple`, so enumeration cannot mutate registry state.

## Exact Ollama declaration produced by the sample tool
For the `calculator` sample, `to_ollama_tools()` returns:
```json
[
  {
    "type": "function",
    "function": {
      "name": "calculator",
      "description": "Evaluate a mathematical expression and return the numeric result.",
      "parameters": {
        "type": "object",
        "properties": {
          "expression": {
            "type": "string",
            "description": "Expression to evaluate, for example: 17 * 24."
          }
        },
        "required": ["expression"],
        "additionalProperties": false
      }
    }
  }
]
```
The `output_schema` is internal metadata and is **not** included.

## Verification
**Registry behavior (AC-1…AC-13).** A standalone script exercised Manual
Verification Scenarios 1–7 as 38 assertions — **all 38 passed**: empty registry
(`list_tools() == ()`, `to_ollama_tools() == []`); register/inspect one tool with
the exact Ollama declaration above; deterministic 3-tool order
(`calculator, sql_query, tracker_search`) identical from `list_tools()` and
`to_ollama_tools()`; duplicate protection; every invalid name / description /
input+output schema rejected with a field-pointing error; unknown lookup raising
`KeyError` (not `None`); and both mutation-isolation scenarios. `ollama` was **not
in `sys.modules`** after the run, confirming the registry triggers **no Ollama
import and no network request** (AC-11/AC-13).

**No Ollama call during registry verification:** confirmed — the registry module
imports no client and the verification asserted `ollama` absent from `sys.modules`.

**Existing CLI regression (AC-14 / Scenario 8).** Drove `python app.py` on the
live model with a scripted dialogue (normal exchange → context follow-up →
`/reset` → another exchange → `/bye`). Streaming was unchanged and context
carried across turns:
```
You: My favorite number is 7. Reply with exactly: noted.
Qwen: noted
You: What number did I say was my favorite? Answer with just the number.
Qwen: 7
You: /reset
Conversation cleared.
You: Say exactly: fresh start.
Qwen: fresh start
You: /bye
Chat finished.
```
After the run, `data/chat_history.json` held only the post-`/reset` pair — no
system prompt, no tool metadata, schema `version: 1` unchanged:
```json
{
  "version": 1,
  "conversation_id": "default",
  "updated_at": "2026-07-14T17:19:16Z",
  "messages": [
    {"role": "user", "content": "Say exactly: fresh start."},
    {"role": "assistant", "content": "fresh start"}
  ]
}
```
Top-level keys unchanged (`version, conversation_id, updated_at, messages`); roles
`['user', 'assistant']` only; no `tool`/`function`/`parameters`/`ToolSpec` strings
present. The chat modules are byte-identical to `main`
(`git diff main -- app.py llm.py conversation.py storage.py config.py prompts.py`
is empty), so the unchanged behavior is expected as well as observed.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B, ctx 40960; capabilities include `tools`)
- Ollama: server 0.31.1; SDK `ollama==0.6.2`; reachable at `http://localhost:11434`
- Sampling: defaults — no `options` set in `llm.py` (unchanged)

## Outcome
Registry acceptance criteria AC-1…AC-13 and AC-15 met: `ToolSpec`/`ToolRegistry`
exist under `tools/`, valid definitions register and are retrievable, invalid and
duplicate definitions fail clearly, order is deterministic, input and output
contracts are explicit, Ollama declarations are generated without calling Ollama,
mutable schema data is isolated from callers, and no tool can be executed. AC-14
also met: the live streaming chat, `/reset`, persistence, and JSON schema are
unchanged and no tool metadata leaks into the store.

## Follow-ups (intentionally out of scope)
- Tool execution: handlers, argument validation of runtime values, result
  envelopes, `tool` role messages, returning results to the model (SPEC-007+).
- Sending tool declarations to Ollama and parsing model tool calls; agent loop.
- Full recursive JSON Schema validation; Python/SQL/MCP integrations.
