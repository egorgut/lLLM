# SPEC-012: Skills

> **Roadmap label:** STEP 13 — Skills  
> **Repository sequence:** SPEC-012, following SPEC-011  
> **Status:** Proposed

## Background

SPEC-006 introduced the shared tool contract and `ToolRegistry`.

SPEC-007 added the first executable local tool, `python_calculate`.

SPEC-008 added the read-only SQLite tool, `sql_query`.

SPEC-009 connected an MCP-backed tool through the same registry and executor.

SPEC-010 introduced a bounded agent loop in which the model may execute several
sequential tool calls before producing a final answer.

SPEC-011 made that loop observable and diagnosable through explicit outcomes,
timeouts, repeated-call detection, structured tracing, committed tests, and
repeatable evaluations.

The current runtime can now answer questions by reasoning over a set of atomic
operations:

```text
python_calculate
sql_query
mcp_time__get_current_time
```

Each tool answers one narrow question:

```text
How can the harness perform one operation?
```

Examples:

- `sql_query` executes one safe read-only SQL statement;
- `python_calculate` evaluates one restricted calculation;
- `mcp_time__get_current_time` obtains the current time from an MCP server.

This is enough for generic tool use, but the model still has to rediscover the
procedure for every recurring class of task.

For example, a reliable sales analysis usually requires more than merely knowing
that `sql_query` exists:

1. identify the requested period and metric;
2. inspect or query the relevant data;
3. aggregate at the correct grain;
4. calculate derived metrics only when needed;
5. verify totals;
6. disclose truncation, missing data, or assumptions;
7. return a result in a stable format.

Those steps are not properties of any one tool. They are a reusable problem-solving
procedure spanning multiple tools.

The next abstraction is therefore a **skill**.

A tool answers:

```text
How do I perform one operation?
```

A skill answers:

```text
How do I solve one defined class of tasks?
```

A skill may contain:

- a name and compact description;
- an instruction;
- an input contract;
- a list of allowed tools;
- constraints;
- a procedure;
- examples;
- completion criteria;
- evaluation cases.

A representative package is:

```text
skills/
└── sales_analysis/
    ├── SKILL.md
    ├── input.schema.json
    ├── examples/
    └── evals/
```

The runtime must not place every full skill instruction into the system prompt.
That would make prompt size grow linearly with the skill library and would expose
irrelevant procedures on every turn.

Instead, the model first receives a compact catalog:

```json
[
  {
    "name": "sales_analysis",
    "description": "Analyse sales and revenue data"
  },
  {
    "name": "database_exploration",
    "description": "Inspect and explain database contents"
  }
]
```

After one skill is selected, only that skill's complete instruction and permitted
tools are loaded for the user turn.

This specification introduces that two-phase skill layer without turning the
project into a generic workflow engine, plugin marketplace, planner framework, or
multi-agent system.

---

## Goal

Introduce a small, filesystem-backed, host-controlled skill layer above tools so
the harness can:

1. discover committed skill packages;
2. validate their metadata and contracts at startup;
3. expose only a compact skill catalog during routing;
4. select zero or one skill for a user turn;
5. load the full instruction only after selection;
6. restrict the agent to the skill's declared tools;
7. execute the existing bounded and observable `AgentRunner` unchanged in spirit;
8. trace the selected skill and routing outcome;
9. keep ordinary non-skill conversation working;
10. provide one committed example skill with deterministic tests and evaluations.

Target flow:

```text
User request
    │
    ▼
Skill routing
    │
    ├── no skill
    │      │
    │      ▼
    │   ordinary agent turn
    │
    └── selected skill
           │
           ▼
      load full SKILL.md
           │
           ▼
      validate input readiness
           │
           ▼
      restrict available tools
           │
           ▼
      bounded observable AgentRunner
           │
           ▼
      completion against skill criteria
```

The skill layer must reuse the existing:

- `ToolRegistry`;
- `ToolExecutor`;
- model transport;
- `AgentRunner`;
- reliability outcomes;
- trace sink;
- conversation rollback and persistence rules.

---

## User-visible behavior

### 1. A request matches a skill

Example:

```text
You: Which music genre generated the most revenue, and what percentage of total
revenue did it generate?
```

The router selects:

```text
sales_analysis
```

The CLI remains concise:

```text
[skill] sales_analysis

[tool 1/4] sql_query
[args] {"query": "..."}
[result] {"ok": true, ...}

Qwen: Rock generated the most revenue: $826.65, approximately 35.5% of total
revenue.

Basis:
- revenue = UnitPrice × Quantity;
- grouped by genre;
- compared with total invoice-line revenue.

Limitations:
- analysis uses the local Chinook sample database.
```

The full skill instruction is not printed by default.

### 2. A request does not need a skill

```text
You: Explain what an agent loop is.
```

The router chooses no skill:

```text
none
```

The request continues through the normal agent path.

No `[skill]` line is required when no skill is selected.

### 3. The user explicitly names a skill

```text
You: Use the sales_analysis skill to compare revenue by genre.
```

If the named skill exists, it is selected directly after host-side validation.

The model must not be allowed to invent a skill name.

### 4. Required information is missing

```text
You: Analyse sales.
```

The selected skill may require a metric or period that is not present.

The agent should ask a concise clarification instead of querying arbitrary data:

```text
Qwen: Which sales metric and period should I analyse?
```

A clarification is a valid completed turn. The user answer on the next turn is
routed again using the current semantic conversation history.

SPEC-012 does not introduce a persisted multi-step workflow state machine.

### 5. A skill references an unavailable tool

This is a startup configuration error, not a model-recoverable event:

```text
Application startup failed: Skill 'sales_analysis' references unknown tool
'write_text_file'.
```

The application must fail before entering the chat loop.

### 6. A skill package is malformed

Examples:

- missing `SKILL.md`;
- invalid skill name;
- malformed front matter;
- invalid JSON Schema;
- duplicate skill name;
- empty description;
- missing completion criteria;
- path escaping the `skills/` root.

The application must fail fast with a stable diagnostic identifying the package.

### 7. Routing produces an unknown skill

If the model returns:

```json
{"skill": "financial_super_agent"}
```

and that name is not in the catalog, the harness must reject it.

The harness may perform one bounded routing repair request. If the second response
is still invalid, the turn ends with a diagnosable routing failure.

No arbitrary directory lookup may be performed using model-generated text.

### 8. Routing times out or transport fails

Skill routing uses the same model transport boundary but has its own explicit
host-owned timeout.

Example:

```text
Application error: Skill routing timed out.
Run ID: ...
```

The user turn is rolled back.

### 9. Skill tool restrictions are enforced

If `sales_analysis` declares:

```yaml
allowed_tools:
  - sql_query
  - python_calculate
```

then the model receives only those two tool declarations for the agent turn.

Even if another tool exists in the global registry, it is unavailable within this
skill execution.

Defense must exist at both boundaries:

1. only allowed tool declarations are sent to the model;
2. execution rejects a call outside the selected skill's allowlist.

### 10. Trace output

A skill-backed turn adds events such as:

```json
{"event":"skill_routing_started","run_id":"...","turn_id":"..."}
{"event":"skill_routing_finished","selected_skill":"sales_analysis"}
{"event":"skill_loaded","skill":"sales_analysis","skill_version":"1"}
{"event":"turn_started","selected_skill":"sales_analysis","available_tools":["sql_query","python_calculate"]}
{"event":"turn_finished","status":"completed","reason":"final_answer","selected_skill":"sales_analysis"}
```

The trace stores the skill name, version, routing duration, and catalog fingerprint.

The complete `SKILL.md` content must not be copied into every trace event.

---

## Scope

This specification includes:

- a filesystem-backed skill package format;
- one `skills/` root;
- deterministic startup discovery;
- strict package validation;
- immutable `SkillSpec`;
- `SkillRegistry`;
- compact catalog rendering;
- zero-or-one-skill routing;
- explicit user-requested skill selection;
- one bounded routing repair attempt;
- lazy loading of the full skill instruction;
- per-skill tool allowlists;
- defense-in-depth enforcement of tool restrictions;
- composition of the selected skill instruction with the existing system prompt;
- routing and selection trace events;
- one example `sales_analysis` skill;
- a committed input schema for that skill;
- committed examples;
- committed skill evaluations;
- deterministic tests without live Ollama or MCP;
- README and journal updates.

---

## Non-goals

This specification does not introduce:

- multiple active skills in one turn;
- skill chaining;
- skill composition;
- nested skills;
- skills calling other skills;
- subagents;
- multi-agent delegation;
- a generic workflow engine;
- a DAG executor;
- persisted workflow state;
- resumable jobs;
- background execution;
- cron or event triggers;
- a visual skill builder;
- remote skill installation;
- a skill marketplace;
- downloading skill packages from the internet;
- arbitrary Python embedded in a skill;
- executable code inside `SKILL.md`;
- dynamic tool creation;
- per-user skill permissions;
- role-based access control;
- cryptographic package signing;
- semantic vector search over skills;
- embeddings for skill routing;
- LLM-generated skills;
- automatic mutation of skill files;
- hot reload during a running application;
- parallel skill routing;
- parallel tool calls;
- a second agent loop;
- hidden chain-of-thought persistence;
- a new telemetry backend;
- full JSON Schema coercion of natural-language user requests;
- automatic proof that completion criteria were semantically satisfied;
- an LLM-as-a-judge.

The architecture should leave room for several of these later, but SPEC-012 keeps
the first implementation local, explicit, inspectable, and deterministic.

---

## Terminology

### Tool

An atomic executable operation registered in `ToolRegistry`.

```text
sql_query
python_calculate
mcp_time__get_current_time
```

### Skill

A versioned, declarative package describing how the agent should solve one class
of tasks using a restricted subset of registered tools.

### Skill catalog

A compact list of skill names and descriptions used only for routing.

### Skill router

A host-owned component that selects zero or one catalog entry for a user request.

### Selected skill

The one validated `SkillSpec` active for a user turn.

### Skill instruction

The full trusted Markdown body loaded from `SKILL.md` after selection.

### Allowed tools

The exact tool names the selected skill permits for the agent turn.

### Skill package

One directory directly under `skills/` containing the files defined by this spec.

---

## Core architectural decisions

### 1. Skills are declarative guidance, not executable plugins

A skill package may define instructions and data files, but it must not contain
Python that the harness imports and executes as part of skill loading.

The first implementation treats skill content as trusted repository content but
still parses it defensively.

Allowed package content:

```text
SKILL.md
input.schema.json
examples/*.md
evals/*.json
```

Disallowed execution mechanisms include:

- Python entry points;
- shell commands;
- dynamic imports;
- templated `eval`;
- arbitrary hooks;
- executable front-matter expressions.

Tools remain the only execution boundary.

### 2. Skills sit above tools, not beside them

The dependency direction is:

```text
SkillRegistry
    │ references names
    ▼
ToolRegistry
    │
    ▼
ToolExecutor
```

`ToolRegistry` must not depend on skills.

A tool must remain usable without any skill.

`AgentRunner` must continue to reason over ordinary tool declarations. The skill
layer prepares the prompt and allowed tool view before constructing or invoking
the runner.

### 3. Zero or one skill per turn

The routing result is:

```python
selected_skill: str | None
```

Only one full instruction may be active for a turn.

This avoids ambiguous instruction precedence and keeps tool restrictions easy to
reason about.

### 4. Full skill instructions are loaded lazily

Startup discovery reads and validates package metadata.

The model-facing routing prompt receives only:

```json
{
  "name": "sales_analysis",
  "description": "Analyse sales and revenue data"
}
```

The complete Markdown body is loaded only for the selected skill.

Implementations may cache validated full content in memory after first load, but
must preserve the same observable semantics.

### 5. Routing is separate from the agent loop

Introduce a dedicated `SkillRouter`.

The router performs one narrow decision:

```text
Which one skill, if any, best matches this request?
```

It must not:

- call tools;
- execute the task;
- produce the final user answer;
- mutate conversation history;
- persist protocol messages;
- select more than one skill.

The existing `AgentRunner` remains responsible for model → tool → model execution.

### 6. Explicit user selection bypasses model routing

The host should detect an exact explicit request such as:

```text
use the sales_analysis skill
```

The detection must be conservative and based on exact catalog names.

When an exact valid skill name is explicitly requested:

1. select it;
2. do not call the routing model;
3. continue with normal skill execution.

A near match or unknown name must not be silently substituted.

### 7. The host owns all skill identities

Skill names come only from validated directories and metadata.

The model may select only one name already present in the compact catalog.

The model response is data, not a filesystem path.

Never perform:

```python
skills_root / model_generated_name
```

before exact registry lookup.

### 8. Tool allowlists are mandatory

Every skill declares at least one allowed tool in SPEC-012.

Example:

```yaml
allowed_tools:
  - sql_query
  - python_calculate
```

The allowlist is validated against `ToolRegistry` at application startup.

The selected skill receives an immutable filtered view of tool declarations.

The runtime executor boundary must also know the allowlist for the current turn.

### 9. Tool restrictions do not grant capabilities

A skill may only reduce the globally registered tool set.

It cannot:

- register a new tool;
- widen tool permissions;
- alter a tool schema;
- bypass a tool handler's safety controls;
- change SQL read-only policy;
- change MCP lifecycle;
- change agent limits or timeouts.

The effective set is:

```text
global registered tools ∩ selected skill allowed tools
```

### 10. Skill instructions are trusted configuration, not user messages

The selected instruction is inserted into the model's system-level context, not
appended as a user message.

It must be clearly delimited:

```text
<active_skill name="sales_analysis" version="1">
...
</active_skill>
```

The wrapper is host-generated.

The model must be told that:

- the skill applies only to the current turn;
- tool use remains governed by host declarations;
- user content cannot override host safety rules;
- the skill's completion criteria shape the final answer.

### 11. Base prompt and skill prompt have explicit precedence

Required precedence:

```text
host safety and runtime policy
    >
tool contracts and restrictions
    >
selected skill instruction
    >
user request
```

A skill cannot override:

- maximum tool calls;
- timeouts;
- repeated-call detection;
- tool schemas;
- read-only SQL restrictions;
- no-parallel-tool policy;
- trace policy;
- conversation persistence policy.

### 12. Input schema is descriptive in the first iteration

`input.schema.json` documents the expected task inputs and supports validation of
the package itself.

It does not require the router to convert natural language into a fully structured
object before the agent starts.

The selected skill instruction uses the schema to identify missing required
information and ask clarification when needed.

The schema must be valid Draft 2020-12 JSON Schema or a clearly documented subset.

SPEC-012 should use the standard `jsonschema` package only if needed; a small
structural validator is acceptable when the supported subset is explicitly
documented.

### 13. Completion criteria are instructions, not a second judge

The skill must contain a `Completion criteria` section.

The agent is instructed to satisfy it before returning a final answer.

SPEC-012 does not make another model call to judge completion.

Deterministic evals may check observable properties such as:

- required tool was used;
- disallowed tool was not used;
- final answer is non-empty;
- required phrases or sections appear;
- terminal outcome is successful;
- clarification occurs when mandatory information is absent.

### 14. Skill selection is ephemeral

The selected skill belongs to one user turn.

It is included in traces but not persisted as a semantic chat message.

On the next user turn, routing runs again unless the user explicitly names a skill.

Conversation history may naturally provide context, but there is no hidden sticky
skill state.

### 15. Startup is fail-fast

All skill packages must be discovered and validated before entering the chat loop.

Startup must fail when:

- two packages declare the same name;
- package directory and declared name disagree;
- an allowed tool is unknown;
- required files are absent;
- metadata is malformed;
- a schema is invalid;
- a package path is unsafe.

This prevents latent failures in the middle of a user turn.

### 16. Routing has bounded output and one repair attempt

The router requests strict JSON:

```json
{
  "skill": "sales_analysis",
  "reason": "The user asks for revenue analysis."
}
```

or:

```json
{
  "skill": null,
  "reason": "No catalog skill is required."
}
```

`reason` is diagnostic only and must not be exposed as hidden reasoning.

Validation rules:

- response must be one JSON object;
- `skill` must be `null` or an exact catalog name;
- no extra executable fields are accepted;
- response size is bounded;
- routing has a host-owned timeout.

If the first response is malformed, the host may send one repair request containing:

- the invalid response;
- the validation error;
- the exact allowed names;
- the required JSON shape.

No third request is permitted.

### 17. Routing failure is a first-class termination reason

Extend reliability outcomes with skill-specific reasons, for example:

```python
class TerminationReason(StrEnum):
    ...
    SKILL_ROUTING_TIMEOUT = "skill_routing_timeout"
    SKILL_ROUTING_ERROR = "skill_routing_error"
    INVALID_SKILL_SELECTION = "invalid_skill_selection"
    SKILL_LOAD_ERROR = "skill_load_error"
    SKILL_POLICY_VIOLATION = "skill_policy_violation"
```

Expected status mapping:

```text
skill_routing_timeout   -> timed_out
skill_routing_error     -> failed
invalid_skill_selection -> failed
skill_load_error        -> failed
skill_policy_violation  -> stopped
```

A package validation failure at startup does not create a turn outcome because no
turn has started.

### 18. One turn identifier covers routing and execution

`turn_id` must be created before skill routing.

All routing and agent events for the same user request share:

```text
run_id
turn_id
```

This allows one trace to reconstruct:

```text
request
→ route
→ load skill
→ run agent
→ terminal outcome
```

### 19. Skill routing counts toward the whole-turn deadline

The host-owned whole-turn deadline begins before routing.

Routing has a component timeout, but it may not cause the total turn to exceed
`AGENT_TURN_TIMEOUT_SECONDS`.

Before each stage, the remaining whole-turn time is checked.

A turn with a selected skill must not receive a fresh independent whole-turn
budget after routing.

### 20. Existing agent reliability remains authoritative

After skill selection, the existing protections still apply:

- model request timeout;
- tool execution timeout;
- whole-turn timeout;
- maximum tool calls;
- repeated identical-call detection;
- one tool call per model response;
- typed terminal outcomes;
- rollback on unsuccessful turns;
- structured tracing.

SPEC-012 must not regress any SPEC-011 behavior.

---

## Skill package format

### Directory layout

Each skill is one direct child directory:

```text
skills/
└── <skill_name>/
    ├── SKILL.md
    ├── input.schema.json
    ├── examples/
    │   ├── basic.md
    │   └── clarification.md
    └── evals/
        └── cases.json
```

Only `SKILL.md` and `input.schema.json` are mandatory.

`examples/` and `evals/` are mandatory for the committed reference skill but may
be optional for later packages if validation policy explicitly permits that.

No recursive skill discovery is required.

### Skill name

The canonical name must match:

```regex
^[a-z][a-z0-9_]{2,63}$
```

Examples:

```text
sales_analysis
database_exploration
```

Invalid:

```text
Sales Analysis
sales-analysis
../sales
sales/analysis
```

The package directory name must exactly equal the declared skill name.

### `SKILL.md` front matter

`SKILL.md` must begin with constrained YAML front matter:

```yaml
---
name: sales_analysis
description: Analyse sales and revenue data
version: "1"
allowed_tools:
  - sql_query
  - python_calculate
---
```

Required fields:

| Field | Type | Rules |
|---|---|---|
| `name` | string | canonical skill name |
| `description` | string | 1–200 characters, single concise sentence |
| `version` | string | non-empty, opaque repository-controlled version |
| `allowed_tools` | array[string] | non-empty, unique exact tool names |

Unknown front-matter fields must be rejected in SPEC-012 unless explicitly added
to the contract.

Use a safe YAML parser. `yaml.safe_load` or equivalent is required.

### `SKILL.md` body

Required headings:

```markdown
# Sales Analysis

## Use when

## Do not use when

## Input

## Available tools

## Procedure

## Constraints

## Completion criteria
```

Optional headings:

```markdown
## Output format
## Examples
## Failure handling
```

The parser must validate required headings case-sensitively.

The body is trusted text committed to the repository, but size must be bounded.

Recommended initial limit:

```python
MAX_SKILL_INSTRUCTION_CHARS = 20_000
```

### `input.schema.json`

Example:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "SalesAnalysisInput",
  "type": "object",
  "properties": {
    "metric": {
      "type": "string",
      "description": "Requested sales metric, for example revenue or quantity."
    },
    "period": {
      "type": ["string", "null"],
      "description": "Requested reporting period when applicable."
    },
    "dimensions": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["metric"],
  "additionalProperties": false
}
```

The file must:

- contain one JSON object;
- declare top-level type `object`;
- have a valid `properties` object;
- have a valid `required` array when present;
- be within a configured size limit;
- parse as UTF-8 JSON.

### Examples

Examples are documentation and evaluation fixtures, not prompt content loaded by
default.

A Markdown example should contain:

```markdown
# Example: Revenue by genre

## User request

...

## Expected behavior

...

## Expected tools

- sql_query

## Expected answer properties

...
```

### Skill evals

`skills/<name>/evals/cases.json` contains cases specific to the skill.

Example:

```json
{
  "schema_version": 1,
  "skill": "sales_analysis",
  "cases": [
    {
      "id": "revenue-by-genre",
      "user_message": "Which genre generated the most revenue?",
      "expected_selection": "sales_analysis",
      "expected_tools": ["sql_query"],
      "forbidden_tools": ["mcp_time__get_current_time"],
      "expected_status": "completed",
      "expected_answer_contains": ["revenue"]
    }
  ]
}
```

Global agent evals remain under top-level `evals/`.

---

## Reference skill: `sales_analysis`

### Purpose

The first committed skill demonstrates a real multi-step procedure over existing
tools without requiring a new capability.

It uses:

```text
sql_query
python_calculate
```

It must not use:

```text
mcp_time__get_current_time
```

### Proposed `SKILL.md`

```markdown
---
name: sales_analysis
description: Analyse sales, quantity, and revenue data from the available database
version: "1"
allowed_tools:
  - sql_query
  - python_calculate
---

# Sales Analysis

## Use when

Use this skill when the user asks to analyse sales, revenue, quantities, rankings,
shares, trends, or comparisons that can be answered from the available database.

## Do not use when

Do not use this skill for general database discovery, current-time questions,
arbitrary arithmetic unrelated to sales, or questions that do not require the
sales dataset.

## Input

Identify:
- the requested metric;
- the requested dimensions;
- the requested period when the dataset contains time;
- the requested comparison, ranking, or derived measure.

Ask one concise clarification when a required element is absent and cannot be
safely inferred.

## Available tools

- `sql_query` for reading and aggregating source data;
- `python_calculate` only for derived calculations that are clearer or safer
  outside SQL.

## Procedure

1. Restate the metric and grain internally before querying.
2. Use only tables and columns present in the supplied database schema.
3. Query aggregated data through `sql_query`.
4. Prefer one complete SQL query when it remains readable and verifiable.
5. Use `python_calculate` only for derived calculations not already returned by
   SQL.
6. Verify totals, denominators, ordering, and units before answering.
7. Check the tool result for `truncated`, missing rows, nulls, and errors.
8. Base the answer only on returned tool observations.
9. State important assumptions and limitations.
10. Return a concise answer followed by the calculation basis.

## Constraints

- Never invent tables, columns, periods, values, or units.
- Never imply access to data outside the available database.
- Never use a tool outside the declared allowlist.
- Never hide truncation or missing data.
- Never present a derived percentage without identifying its denominator.
- Do not expose raw chain-of-thought.

## Completion criteria

Return:
- the result;
- the metric and grouping basis;
- the calculation basis for derived values;
- important assumptions;
- truncation, missing-data, or dataset limitations.

When the request cannot be answered from available data, say so explicitly.
```

---

## Data model

### `SkillSpec`

Introduce an immutable contract, for example:

```python
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    version: str
    allowed_tools: tuple[str, ...]
    instruction: str
    input_schema: Mapping[str, Any]
    package_path: Path
    fingerprint: str
```

Required semantics:

- immutable after registration;
- normalized UTF-8 content;
- `allowed_tools` preserves declared order;
- input schema is deep-copied or read-only;
- fingerprint changes when relevant package content changes;
- package path is resolved under the configured root;
- no model-facing mutable references.

### `SkillCatalogEntry`

Routing should use a smaller model:

```python
@dataclass(frozen=True)
class SkillCatalogEntry:
    name: str
    description: str
```

Do not expose:

- package paths;
- full instruction;
- input schemas;
- allowed-tool implementation details;
- fingerprints.

### `SkillSelection`

```python
@dataclass(frozen=True)
class SkillSelection:
    skill_name: str | None
    reason: str
    source: str
    routing_requests: int
    duration_ms: int
```

`source` is one of:

```text
explicit
model
none
```

The `reason` field is bounded diagnostic text, not hidden chain-of-thought.

---

## Skill registry

### Responsibilities

`SkillRegistry` owns:

- validated skill identities;
- lookup by exact name;
- duplicate rejection;
- compact catalog generation;
- filtering by exact registry contents;
- stable catalog fingerprint.

It does not own:

- model routing;
- tool execution;
- conversation state;
- trace writing;
- filesystem hot reload;
- agent loop policy.

### Suggested interface

```python
class SkillRegistry:
    def register(self, skill: SkillSpec) -> None: ...
    def get(self, name: str) -> SkillSpec: ...
    def contains(self, name: str) -> bool: ...
    def list_skills(self) -> tuple[SkillSpec, ...]: ...
    def catalog(self) -> tuple[SkillCatalogEntry, ...]: ...
    def catalog_fingerprint(self) -> str: ...
```

Registration order must be deterministic.

Startup discovery should sort package directories by name before registration.

### Package loader

Introduce a separate loader:

```python
class SkillPackageLoader:
    def load_all(
        self,
        skills_root: Path,
        tool_registry: ToolRegistry,
    ) -> SkillRegistry: ...
```

This separation keeps filesystem parsing out of the in-memory registry.

---

## Skill router

### Router input

The router receives:

- current user request;
- a bounded slice of semantic conversation context;
- compact skill catalog;
- exact output schema;
- remaining deadline.

It must not receive:

- full contents of every skill;
- tool results from old transient turns;
- trace files;
- package paths.

### Routing prompt

Example system instruction:

```text
You are a skill router.

Choose zero or one skill from the supplied catalog for the current user request.

Rules:
- return null when no skill is necessary;
- select only an exact catalog name;
- do not solve the task;
- do not call tools;
- do not invent skills;
- prefer no skill when the request is general conversation or can be answered
  without a specialized procedure;
- return one JSON object only.
```

Catalog:

```json
[
  {
    "name": "sales_analysis",
    "description": "Analyse sales, quantity, and revenue data from the available database"
  }
]
```

Required output:

```json
{
  "skill": "sales_analysis",
  "reason": "The request asks for revenue analysis from the database."
}
```

or:

```json
{
  "skill": null,
  "reason": "No specialized skill is needed."
}
```

### Router interface

```python
class SkillRouter:
    def select(
        self,
        *,
        user_message: str,
        conversation_context: list[dict[str, Any]],
        catalog: Sequence[SkillCatalogEntry],
        deadline: float,
    ) -> SkillSelection: ...
```

The router must be injectable so deterministic tests can use a scripted router.

### Explicit selection parser

Suggested conservative forms:

```text
use the <name> skill
use skill <name>
with the <name> skill
```

Matching requirements:

- case-insensitive wrapper phrase;
- exact canonical skill token;
- token boundaries;
- no fuzzy matching;
- no path separators;
- registry lookup before selection.

The original user message remains unchanged when sent to the agent.

---

## Prompt composition

### Base system prompt

The existing base prompt remains the authoritative general instruction.

### Catalog prompt

The compact catalog belongs only to routing and should not remain in the main agent
prompt after selection unless needed for diagnostics.

### Active skill prompt

For a selected skill, compose:

```text
<active_skill name="sales_analysis" version="1">
[validated body of SKILL.md]
</active_skill>

<active_skill_policy>
- This skill applies only to the current user turn.
- You may call only the tools supplied by the host.
- Host safety rules and tool contracts override the skill.
- Ask a concise clarification when required inputs are absent.
- Do not claim completion until the completion criteria are satisfied.
</active_skill_policy>
```

The front matter itself need not be repeated to the model because trusted metadata
is already represented by the wrapper and filtered tools.

### No selected skill

When no skill is selected, no empty skill wrapper should be added.

The ordinary base prompt and global tool behavior remain available.

For the first implementation, a no-skill turn may receive the full global tool set,
preserving current behavior.

---

## Tool filtering and enforcement

### Filtered declarations

Add an operation such as:

```python
def declarations_for_names(
    registry: ToolRegistry,
    names: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    ...
```

The result must:

- preserve skill-declared order;
- contain deep-copied declarations;
- reject unknown names;
- contain no extra tools.

### Execution guard

Introduce a turn-scoped executor view or policy wrapper:

```python
class RestrictedToolExecutor:
    def __init__(
        self,
        executor: ToolExecutor,
        allowed_tools: frozenset[str],
    ) -> None: ...

    def execute(self, name: str, arguments: dict[str, Any]) -> dict:
        ...
```

A disallowed call must not reach the underlying handler.

It should raise a typed runtime policy error mapped to:

```text
status = stopped
reason = skill_policy_violation
```

The trace must include:

```json
{
  "event": "policy_violation",
  "policy": "skill_tool_allowlist",
  "skill": "sales_analysis",
  "requested_tool": "mcp_time__get_current_time"
}
```

---

## Application lifecycle

### Startup

Proposed startup order:

```text
load configuration
    │
    ▼
create ToolRegistry
    │
    ▼
register local tools
    │
    ▼
start MCP manager and register discovered MCP tools
    │
    ▼
load and validate SkillRegistry against final ToolRegistry
    │
    ▼
create router and application services
    │
    ▼
enter CLI
```

Skills must be validated after all configured tools, including MCP tools, are
registered.

If skill validation fails, MCP resources already started during startup must still
be closed deterministically.

### User turn

Proposed flow:

```text
append user message tentatively
    │
    ▼
create turn_id and start whole-turn deadline
    │
    ▼
detect exact explicit skill request
    │
    ├── found ──────────────┐
    │                       │
    └── not found           │
           │                │
           ▼                │
       SkillRouter          │
           │                │
           └────────────────┘
                    │
                    ▼
         selected SkillSpec or None
                    │
                    ▼
      compose system prompt + tool view
                    │
                    ▼
            AgentRunner.run_turn
                    │
                    ▼
           AgentTurnOutcome
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      completed            non-success
          │                   │
          ▼                   ▼
 persist user+assistant     rollback user
```

Routing protocol content must remain ephemeral.

### Shutdown

No new child process or background thread is required solely for skills.

Existing deterministic MCP shutdown remains unchanged.

---

## Reliability integration

### Configuration

Add host-owned configuration:

```python
SKILLS_ROOT = BASE_DIR / "skills"
SKILL_ROUTING_TIMEOUT_SECONDS = 30
MAX_SKILL_ROUTING_RESPONSE_CHARS = 2_000
MAX_SKILL_INSTRUCTION_CHARS = 20_000
MAX_SKILL_SCHEMA_BYTES = 100_000
MAX_SKILLS = 100
MAX_SKILL_DESCRIPTION_CHARS = 200
SKILL_ROUTING_REPAIR_ATTEMPTS = 1
```

All values must be validated at startup.

`SKILL_ROUTING_REPAIR_ATTEMPTS` means additional attempts after the initial
request; value `1` therefore permits at most two total routing requests.

### Whole-turn timing

The current `AgentRunner` creates its own turn start and deadline. SPEC-012 should
refactor deadline ownership just enough that routing and execution share one
whole-turn budget.

Acceptable approaches:

1. create a `TurnContext` before routing and pass its absolute deadline into the
   runner; or
2. pass the remaining timeout into the runner after routing while preserving one
   original turn start for outcome duration.

Preferred model:

```python
@dataclass(frozen=True)
class TurnContext:
    run_id: str
    turn_id: str
    started_at: float
    deadline: float
```

The runner should accept this context rather than silently generating an unrelated
deadline for skill-backed turns.

### Outcome duration

`AgentTurnOutcome.duration_ms` must cover:

```text
routing + skill load + agent execution
```

### Model request count

Choose and document one semantic.

Preferred:

```text
model_requests = routing model requests + agent model requests
```

Add optional trace fields separating:

```text
routing_model_requests
agent_model_requests
```

This preserves the meaning “all model requests made for the user turn.”

### Failure handling

Expected failures:

| Failure | Outcome |
|---|---|
| routing timeout | `timed_out/skill_routing_timeout` |
| router transport error | `failed/skill_routing_error` |
| invalid output after repair | `failed/invalid_skill_selection` |
| selected package unavailable after validated startup | `failed/skill_load_error` |
| disallowed tool request | `stopped/skill_policy_violation` |
| ordinary agent timeout | existing SPEC-011 reason |
| ordinary tool error | existing SPEC-011 behavior |

No partial final answer may be persisted for any non-successful outcome.

---

## Tracing

### New events

Add:

```text
skill_routing_started
skill_routing_response
skill_routing_repair_started
skill_routing_finished
skill_loaded
skill_not_selected
skill_toolset_resolved
```

### Event fields

Representative routing event:

```json
{
  "schema_version": 1,
  "event": "skill_routing_finished",
  "run_id": "...",
  "turn_id": "...",
  "selected_skill": "sales_analysis",
  "selection_source": "model",
  "routing_requests": 1,
  "duration_ms": 82,
  "catalog_fingerprint": "sha256:..."
}
```

Representative load event:

```json
{
  "schema_version": 1,
  "event": "skill_loaded",
  "run_id": "...",
  "turn_id": "...",
  "skill": "sales_analysis",
  "skill_version": "1",
  "skill_fingerprint": "sha256:...",
  "allowed_tools": ["sql_query", "python_calculate"]
}
```

### Payload policy

Traces may contain:

- selected skill name;
- version;
- fingerprint;
- allowed tool names;
- bounded routing reason;
- timing and counters.

Traces must not contain by default:

- full `SKILL.md`;
- full catalog when large;
- full input schema;
- complete user message beyond existing preview policy;
- hidden model reasoning.

A routing response preview must use existing preview-and-hash behavior.

### Existing terminal event

`turn_finished` should include:

```json
{
  "selected_skill": "sales_analysis",
  "skill_version": "1"
}
```

or:

```json
{
  "selected_skill": null
}
```

---

## Validation rules

### Root validation

- skills root must be a directory;
- symlink behavior must be explicit;
- recommended MVP: reject symlinked skill directories and files;
- maximum direct child count is bounded;
- non-directory entries under `skills/` are rejected except an optional README.

### Path safety

For every package file:

```python
resolved_path.is_relative_to(resolved_skills_root)
```

must be true.

No `..` traversal is permitted.

### Metadata validation

- required fields exist;
- no unknown fields;
- types are exact;
- name matches regex;
- directory and name match;
- description is trimmed and bounded;
- version is trimmed and bounded;
- allowed tools are unique and non-empty;
- every allowed tool exists.

### Markdown validation

- UTF-8;
- bounded size;
- front matter is first;
- one H1;
- required H2 sections appear once;
- instruction body is non-empty;
- NUL bytes rejected.

### Schema validation

- UTF-8 JSON;
- bounded size;
- object root;
- supported draft or subset;
- structurally valid;
- no external `$ref` retrieval;
- local references only if explicitly supported;
- no network access during validation.

### Determinism

Given the same repository contents and tool registry:

- skill ordering is stable;
- catalog JSON is stable;
- fingerprints are stable;
- validation errors are stable enough for tests.

---

## Suggested module layout

```text
skills/
├── __init__.py
├── models.py
├── registry.py
├── loader.py
├── router.py
├── prompting.py
├── policy.py
└── sales_analysis/
    ├── SKILL.md
    ├── input.schema.json
    ├── examples/
    │   ├── revenue_by_genre.md
    │   └── missing_metric.md
    └── evals/
        └── cases.json
```

There is a naming collision between the Python package `skills/` and skill data
directories.

Preferred resolution for SPEC-012:

```text
skill_runtime/
├── __init__.py
├── models.py
├── registry.py
├── loader.py
├── router.py
├── prompting.py
└── policy.py

skills/
└── sales_analysis/
    ...
```

This keeps runtime code separate from declarative content.

### Proposed files

| File | Responsibility |
|---|---|
| `skill_runtime/models.py` | immutable skill contracts |
| `skill_runtime/loader.py` | filesystem loading and validation |
| `skill_runtime/registry.py` | in-memory exact-name registry and catalog |
| `skill_runtime/router.py` | explicit selection and model routing |
| `skill_runtime/prompting.py` | active-skill prompt composition |
| `skill_runtime/policy.py` | tool filtering and executor restriction |
| `skills/sales_analysis/SKILL.md` | reference skill instruction |
| `skills/sales_analysis/input.schema.json` | reference input contract |
| `skills/sales_analysis/examples/` | committed examples |
| `skills/sales_analysis/evals/cases.json` | skill-specific evals |

---

## Required changes to existing modules

### `app.py`

- load skills after local and MCP tools are registered;
- create one turn context before routing;
- route explicit or model-based selection;
- select full skill spec from the registry;
- build the active prompt;
- filter tool declarations;
- wrap executor with the allowlist;
- invoke the agent with the shared deadline;
- preserve rollback and persistence semantics;
- render `[skill] <name>` only when selected;
- close MCP resources on startup skill failure.

### `agent.py`

- accept a host-created turn context or absolute whole-turn deadline;
- accept selected-skill metadata for trace enrichment;
- preserve existing loop behavior;
- count routing model requests according to the chosen outcome semantics;
- map skill policy errors to explicit outcomes;
- do not implement routing internally.

### `reliability.py`

- add skill-specific termination reasons;
- add status and user-message mappings;
- optionally introduce `TurnContext`;
- validate routing timeout configuration.

### `tracing.py`

- support new routing and skill events;
- preserve safe sink semantics;
- apply preview-and-hash to router output;
- add selected skill metadata to terminal events.

### `prompts.py`

- keep base system policy independent of any one skill;
- add a host-generated active-skill wrapper through a dedicated composer;
- avoid embedding all full skill instructions;
- document instruction precedence.

### `tools/registry.py`

- optionally add exact-name filtered declaration generation;
- do not depend on skill modules.

### `tools/executor.py`

- remain the global dispatcher;
- allow a turn-scoped restriction wrapper;
- do not place skill policy into individual tool handlers.

### `config.py`

Add skill paths and bounds.

### `README.md`

Document:

- tool versus skill;
- package layout;
- two-phase catalog/full-instruction loading;
- reference `sales_analysis` interaction;
- startup validation;
- tool allowlists;
- test and eval commands.

### `docs/journal/`

Add the normal implementation journal entry after the step is implemented.

---

## Testing strategy

All committed tests must run without:

- live Ollama;
- network access;
- live MCP server;
- real wall-clock waits;
- generated SQLite database unless a test explicitly uses a deterministic fixture.

Use scripted model responses, fake clocks, temporary directories, and fake tool
registries.

### Unit tests: package loader

Cover:

1. valid package loads;
2. missing `SKILL.md`;
3. missing input schema;
4. invalid YAML;
5. unsafe YAML tags rejected;
6. missing metadata;
7. unknown metadata field;
8. invalid skill name;
9. directory/name mismatch;
10. duplicate allowed tool;
11. unknown allowed tool;
12. empty description;
13. oversized description;
14. oversized instruction;
15. missing required heading;
16. duplicate required heading;
17. invalid UTF-8;
18. NUL byte;
19. invalid JSON schema;
20. external schema reference rejected;
21. symlink policy;
22. path traversal defense;
23. deterministic fingerprint.

### Unit tests: registry

Cover:

1. register valid skill;
2. reject duplicate;
3. exact lookup;
4. unknown lookup;
5. deterministic order;
6. compact catalog excludes full instruction;
7. catalog fingerprint stability;
8. immutable return values.

### Unit tests: explicit selection

Cover:

1. exact valid phrase;
2. case-insensitive wrapper;
3. unknown name;
4. near match not accepted;
5. path-like name rejected;
6. ordinary mention not misclassified;
7. exact explicit selection bypasses router.

### Unit tests: router

Cover:

1. selects valid skill;
2. selects none;
3. malformed JSON repaired once;
4. unknown name repaired once;
5. second invalid response fails;
6. oversized response fails;
7. timeout;
8. transport failure;
9. empty catalog returns none without model call;
10. router does not receive full instructions;
11. bounded conversation context;
12. injected scripted transport.

### Unit tests: prompt composition

Cover:

1. selected skill wrapper;
2. no wrapper for none;
3. correct version and name;
4. front matter omitted;
5. base policy precedes skill;
6. instruction size already validated;
7. user content cannot alter wrapper boundaries.

### Unit tests: tool restriction

Cover:

1. only allowed declarations sent;
2. declaration order follows skill;
3. disallowed execution rejected before handler;
4. allowed execution passes through;
5. global registry remains unchanged;
6. policy violation outcome;
7. policy violation trace.

### Integration tests: turn lifecycle

Cover:

1. no-skill ordinary answer;
2. explicit skill selection;
3. model-selected skill;
4. one SQL call and final answer;
5. SQL then Python calculation;
6. clarification with no tool call;
7. invalid routing causes rollback;
8. routing timeout causes rollback;
9. disallowed tool causes rollback;
10. selected skill not persisted as chat message;
11. routing protocol not persisted;
12. final assistant answer persisted only on success;
13. whole-turn duration includes routing;
14. routing and agent share one `turn_id`;
15. existing repeated-call and tool-call budgets still apply.

### Regression tests

The full existing SPEC-011 suite must continue passing.

No current tool or non-skill chat behavior may require a skill.

---

## Evaluation strategy

### Scripted suite

Extend the existing deterministic eval runner with skill cases.

Required categories:

```text
explicit selection
automatic selection
no skill
clarification
single-tool skill execution
multi-tool skill execution
disallowed tool
invalid routing output
routing timeout
completion-format behavior
```

The scripted suite must be safe for CI:

```bash
python -m evals.runner --suite scripted
```

### Live suite

The optional live suite uses the configured Ollama model and real local tools:

```bash
python -m evals.runner --suite live --category skills
```

Live cases should measure observable behavior, not hidden reasoning.

Example assertions:

- selected skill equals expected;
- selected tool names are allowed;
- at least one required tool is used;
- no forbidden tool is used;
- terminal outcome matches expected;
- final answer mentions calculation basis;
- result is grounded in returned rows;
- clarification appears for underspecified request.

### Reference cases

At minimum:

1. “Which genre generated the most revenue?”
2. “What percentage of total revenue did that genre generate?”
3. “Compare the top five genres by revenue.”
4. “Analyse sales.” → clarification.
5. “What time is it in UTC?” → no `sales_analysis`.
6. Explicitly request `sales_analysis`.
7. Script model attempts MCP time tool inside `sales_analysis` → policy stop.
8. Malformed router output → one repair.
9. No applicable skill → ordinary answer.

### Evaluation result

Machine-readable results should include:

```json
{
  "case_id": "revenue-by-genre",
  "selected_skill": "sales_analysis",
  "selection_source": "model",
  "routing_requests": 1,
  "tool_calls": ["sql_query"],
  "status": "completed",
  "reason": "final_answer",
  "passed": true
}
```

---

## Security considerations

### Prompt injection

Skill instructions are trusted repository content.

User-provided content remains untrusted.

The active skill wrapper must not imply that text quoted from the user becomes a
system instruction.

Tool safety remains enforced in code, not only in prompts.

### Filesystem safety

The model never supplies a path.

All package paths are discovered by the host from one configured root.

Resolved files must remain inside that root.

### YAML safety

Use safe parsing only.

Do not construct Python objects from YAML tags.

### Schema safety

Do not resolve network references.

Do not execute schema annotations.

### Capability safety

A skill cannot widen access.

Allowlist enforcement occurs before tool dispatch.

### Sensitive traces

Do not duplicate complete instructions, schemas, SQL rows, or user data into
skill trace events.

Use existing truncation and hash mechanisms.

### Supply-chain boundary

SPEC-012 loads only packages committed in the local repository.

Remote installation is a non-goal.

---

## Performance and prompt-size requirements

### Startup

Loading the first local skill should be negligible compared with model startup.

Startup cost must be bounded by:

- maximum number of skills;
- maximum instruction size;
- maximum schema size;
- deterministic local filesystem reads.

### Routing prompt

Prompt growth should be approximately proportional to compact catalog metadata,
not full skill bodies.

For `N` skills:

```text
routing prompt ≈ base router instruction + Σ(name + description)
```

not:

```text
routing prompt ≈ Σ(full SKILL.md)
```

### Agent prompt

The main agent prompt contains:

```text
base prompt + zero or one full skill instruction
```

### Catalog limits

With `MAX_SKILLS = 100` and descriptions capped at 200 characters, catalog size
remains explicitly bounded.

---

## Migration and compatibility

### Existing users

No migration of `data/chat_history.json` is required.

### Existing tools

No tool schema changes are required.

### Existing traces

Trace schema versioning must handle the new optional skill fields.

Old trace readers should tolerate unknown fields or schema version should be
incremented according to current project convention.

### Existing evals

Top-level eval cases remain valid.

Skill-specific cases extend rather than replace the suite.

### Empty skill registry

The application should support an empty `skills/` root in development.

Behavior:

```text
router is not called
selected_skill = None
ordinary agent behavior continues
```

The committed repository, however, includes the reference `sales_analysis` skill.

---

## Implementation sequence

### Phase 1 — Contracts and loader

1. create `skill_runtime/models.py`;
2. define `SkillSpec`, `SkillCatalogEntry`, `SkillSelection`;
3. create safe front-matter parser;
4. validate package paths, metadata, Markdown headings, and schema;
5. calculate stable fingerprints;
6. add loader tests.

### Phase 2 — Registry and tool policies

1. create `SkillRegistry`;
2. validate allowed tools against the final `ToolRegistry`;
3. add filtered declaration helper;
4. add restricted executor wrapper;
5. add policy tests.

### Phase 3 — Router

1. implement explicit exact-name selection;
2. implement compact catalog prompt;
3. parse strict JSON result;
4. add one repair attempt;
5. integrate timeout and whole-turn deadline;
6. add router tests.

### Phase 4 — Turn integration

1. create turn context before routing;
2. compose selected skill prompt;
3. pass filtered declarations and executor;
4. enrich outcomes and traces;
5. preserve rollback/persistence behavior;
6. add integration tests.

### Phase 5 — Reference skill and evals

1. add `sales_analysis`;
2. add schema and examples;
3. add scripted eval cases;
4. add optional live cases;
5. update README;
6. add journal.

---

## Acceptance criteria

### Package and registry

- [ ] The repository contains a top-level declarative `skills/` directory.
- [ ] The runtime code for skills is separate from declarative packages.
- [ ] `sales_analysis/SKILL.md` exists and follows the required format.
- [ ] `sales_analysis/input.schema.json` exists and validates.
- [ ] Skill discovery order is deterministic.
- [ ] Duplicate skill names fail startup.
- [ ] Unknown allowed tools fail startup.
- [ ] Unsafe paths or symlinks are handled according to the documented policy.
- [ ] Full instructions are bounded and validated.
- [ ] `SkillRegistry.catalog()` contains only name and description.
- [ ] Registry results are immutable or safely copied.

### Routing

- [ ] An exact explicitly requested known skill bypasses model routing.
- [ ] The router selects zero or one exact catalog skill.
- [ ] The router never receives all full skill instructions.
- [ ] Empty catalog skips the routing model call.
- [ ] Malformed output receives at most one repair request.
- [ ] Unknown skill output is rejected.
- [ ] Routing has a host-owned timeout.
- [ ] Routing consumes the same whole-turn deadline as agent execution.
- [ ] Routing failures produce typed terminal outcomes.
- [ ] Routing protocol messages are not persisted.

### Prompt and tools

- [ ] Only the selected skill's full instruction is loaded.
- [ ] No skill wrapper is added when no skill is selected.
- [ ] Host policy precedence is explicit.
- [ ] A selected skill receives only its allowed tool declarations.
- [ ] A disallowed tool call is rejected before handler execution.
- [ ] A skill cannot register or modify tools.
- [ ] Existing tool safety boundaries remain unchanged.

### Agent lifecycle

- [ ] A skill-backed request uses the existing bounded `AgentRunner`.
- [ ] No second agent loop is introduced.
- [ ] Existing model, tool, and whole-turn timeouts still apply.
- [ ] Existing repeated-call and tool-call limits still apply.
- [ ] Every request has one shared `run_id` and `turn_id` across routing and execution.
- [ ] `AgentTurnOutcome.duration_ms` includes routing.
- [ ] Non-successful skill turns roll back the tentative user message.
- [ ] Successful turns persist only semantic user and assistant messages.
- [ ] Skill selection is ephemeral and rerun on the next turn.

### Tracing

- [ ] Skill routing and loading emit structured events.
- [ ] Events include selected skill, version, fingerprints, and timing.
- [ ] Full skill instructions are not copied into traces.
- [ ] Terminal trace events include selected skill or `null`.
- [ ] Trace sink failure remains non-fatal as defined by SPEC-011.

### Tests and evals

- [ ] Deterministic tests run without live Ollama or MCP.
- [ ] Loader, registry, router, prompt, policy, and lifecycle tests are committed.
- [ ] Existing SPEC-011 regression tests remain green.
- [ ] Scripted skill evals are committed.
- [ ] Optional live skill evals are documented.
- [ ] The reference skill demonstrates SQL use.
- [ ] The reference skill demonstrates a clarification case.
- [ ] The reference skill demonstrates enforcement of a forbidden tool.
- [ ] Evaluation output records selected skill and tool calls.

### Documentation

- [ ] README explains tool versus skill.
- [ ] README explains compact catalog and lazy full loading.
- [ ] README documents package structure and startup validation.
- [ ] README includes a `sales_analysis` example.
- [ ] A journal entry records implementation decisions and deviations.

---

## Definition of done

SPEC-012 is complete when a user can ask a sales-analysis question and the harness:

```text
1. creates one turn context;
2. selects `sales_analysis` from a compact catalog;
3. loads only that skill's full instruction;
4. exposes only `sql_query` and `python_calculate`;
5. runs the existing bounded and observable agent loop;
6. rejects any tool outside the skill allowlist;
7. returns a grounded answer satisfying the skill completion criteria;
8. records routing, skill, tool, and terminal events in one trace;
9. persists only the successful semantic exchange;
10. passes deterministic tests and scripted evals without a live model.
```

The architectural result should be:

```text
User request
    │
    ▼
compact Skill Catalog
    │
    ▼
Skill Router
    │
    ├── none ────────────────┐
    │                        │
    └── selected SkillSpec   │
             │               │
             ▼               │
      full instruction       │
      + allowed tools        │
             │               │
             └───────┬───────┘
                     ▼
             observable AgentRunner
                     │
                     ▼
                ToolExecutor
```

Tools remain atomic operations.

Skills become reusable, inspectable, repository-controlled agent capabilities
built from those operations.
