# SPEC-008 — SQLite SQL Query Tool

- **Spec:** [SPEC-008](../../specs/SPEC-008-SQL-Query-Tool.md)
- **Date:** 2026-07-23
- **Branch:** feature/SPEC-008-sql-query-tool
- **Merge commit:** 6b195b3

## Hypothesis / intent
SPEC-007 wired the complete one-tool-per-turn path with a single in-process
calculator. SPEC-008 proves the same path against a **real relational database**:
natural-language question → model-generated SQL → safe local execution →
structured rows → grounded answer. The iteration stays deliberately narrow — one
fixed local Chinook SQLite file, **read-only** queries, one SQL execution per
turn, schema supplied to the model in advance — and framework-free (stdlib
`sqlite3`, no ORM/server/network). Model-generated SQL is untrusted, so the
defense lives at the **connection boundary** (read-only mode + authorizer +
single-statement execute + resource limits), not in a SQL parser. Persistent
history stays semantic (user/assistant only).

## What changed
- `tools/sql_query.py` (new): `SQL_QUERY_SPEC` and a testable
  `create_sql_query_handler(database_path)` factory returning a `sql_query(arguments)`
  handler. The database path is captured by the factory — **never** a tool
  argument. Per call it opens a fresh read-only connection
  (`file:...?mode=ro`, `uri=True`), installs a `set_authorizer` allowing only
  `SQLITE_SELECT/READ/FUNCTION/RECURSIVE` (everything else → `SQLITE_DENY`),
  installs a `set_progress_handler` work limit, executes **one** statement via
  `connection.execute` (never `executescript`), bounds columns/rows/result-size,
  serializes JSON-compatible values, and closes the connection in `finally`.
  Returns the stable envelope; never leaks a traceback, path, or URI.
- `scripts/init_database.py` (new): reproducible initializer. Resolves
  project-relative paths from `config.py`, executes the **trusted** seed with
  `executescript` into a temp sibling, validates expected tables + non-empty
  `Artist/Track/Invoice`, then atomically `os.replace`s it into place. `--force`
  replaces an existing DB; default fails with a clear message; a failed build
  leaves no partial target. No Ollama/history/network.
- `config.py`: added `PROJECT_ROOT`, `CHINOOK_SEED_PATH`, `SQLITE_DATABASE_PATH`
  (`pathlib`, resolved from the file so they hold regardless of CWD). Existing
  string constants unchanged.
- `tools/__init__.py`: now also exports `SQL_QUERY_SPEC, create_sql_query_handler`.
- `app.py`: `build_executor` registers `SQL_QUERY_SPEC` and binds
  `create_sql_query_handler(SQLITE_DATABASE_PATH)`. The two bounded-turn
  `TurnError` messages were reworded off "SPEC-007". `run_turn` logic, rendering,
  and rollback are otherwise **unchanged** — dispatch is name-agnostic.
- `prompts.py`: added one authoritative `CHINOOK_SCHEMA` constant and concise
  `sql_query` guidance to `SYSTEM_PROMPT` (one read-only SELECT, use the schema,
  explicit joins, deterministic ordering, ground the answer in rows, disclose
  truncation). Schema defined once, not duplicated.
- `.gitignore`: ignore `data/chinook.sqlite` and the temp build pattern; the seed
  under `data/seed/` stays tracked.
- `README.md`: documented `sql_query`, the init command (+ `--force`), read-only
  behavior, one-tool-per-turn, recovery from `database_not_initialized`.
- `conversation.py`, `storage.py`, `llm.py`, `tools/registry.py`,
  `tools/executor.py`, `tools/python_calculate.py`: **unchanged**.

## Deviation from the spec
The spec's file list and AC-27 suggest a committed `tests/` suite. This repo has
never committed tests — every prior step verified behavior via a standalone
script whose results are recorded in the journal. Per that established
convention (and to keep the repo framework-free), SPEC-008 was verified with a
standalone harness driven against a throwaway copy of the real database, and the
results are captured below rather than committed under `tests/`. Handler behavior
and live-model behavior are both covered; only the delivery form differs from the
spec's wording.

## Final public API
```python
from tools import SQL_QUERY_SPEC, create_sql_query_handler

sql_query = create_sql_query_handler(SQLITE_DATABASE_PATH)
sql_query({"query": "SELECT COUNT(*) AS TrackCount FROM Track"})
# -> {"ok": True, "columns": ["TrackCount"], "rows": [[3503]],
#     "row_count": 1, "truncated": False}

sql_query({"query": "DELETE FROM Invoice"})
# -> {"ok": False, "error": {"type": "read_only_violation",
#     "message": "Only read-only SELECT queries are allowed."}}
```
Stable error categories: `invalid_arguments`, `database_not_initialized`,
`invalid_query`, `read_only_violation`, `resource_limit`, `unsupported_result`,
`database_error`, `internal_error`. Limits: `MAX_QUERY_LENGTH=10_000`,
`MAX_RESULT_ROWS=100`, `MAX_RESULT_COLUMNS=50`, `MAX_RESULT_BYTES=100_000`,
`PROGRESS_HANDLER_INTERVAL=1_000`, `MAX_PROGRESS_CALLBACKS=10_000`.

## Exact tool schema
```python
SQL_QUERY_SPEC = ToolSpec(
    name="sql_query",
    description=(
        "Run one read-only SQLite SELECT query against the local Chinook "
        "music-store database. Use the supplied schema. Return database rows for "
        "factual and analytical questions. Never use it for writes or schema changes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "One read-only SQLite SELECT ..."}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "error": {"type": "object"},
        },
        "required": ["ok"],
    },
)
```

## Runtime / execution location
- Interpreter: `venv/bin/python`, Python **3.14.6**, at the project venv.
- The SQL handler runs **in the same OS process and interpreter** as `python
  app.py`, through the stdlib `sqlite3` module against the configured local file
  `data/chinook.sqlite`. No subprocess, no external DB service, no network.
- Database built by `python scripts/init_database.py` from the trusted seed
  `data/seed/Chinook_Sqlite.sql` (Chinook 1.4.5); the generated file is git-ignored.

## Model & parameters (provenance)
- Model: qwen3:8b (digest 500a1f067a9f, Q4_K_M, 8.2B, ctx 40960; `tools` capable)
- Ollama: server 0.31.1; SDK `ollama==0.6.2`; reachable at `http://localhost:11434`
- Sampling: defaults — no `options` set in `llm.py`

## Verification

**Initializer (AC-1/2/3).** `python scripts/init_database.py` →
`Created Chinook database: data/chinook.sqlite`. Re-run without `--force` →
`Database already exists: … / Use --force to recreate it.` (exit 1). `--force` →
recreates. No `.tmp` sibling left behind. `data/chinook.sqlite` is git-ignored;
`data/seed/Chinook_Sqlite.sql` stays tracked.

**Handler harness (AC-7…AC-20), against a throwaway copy of the real DB — all PASS.**
- Success: `COUNT(*) FROM Track → 3503`; `Artist ORDER BY Name LIMIT 3`; the
  Artist⋈Album⋈Track group-by → `[["Iron Maiden",213],["U2",135],["Led Zeppelin",114]]`;
  a `WITH … SELECT` CTE; an empty result (`rows=[]`, `row_count=0`, `truncated=false`);
  a Unicode `LIKE` path.
- Read-only rejection (each `ok:false`): `INSERT`, `UPDATE`, `DELETE`, `DROP TABLE`,
  `CREATE TABLE`, `ALTER TABLE`, `ATTACH DATABASE ':memory:'`, `PRAGMA table_info` →
  `read_only_violation`.
- Multi-statement `SELECT …; DELETE FROM Artist;` → rejected, no second statement runs.
- Invalid: `SELEC * FROM Artist`, `SELECT * FROM MissingTable`,
  `SELECT MissingColumn FROM Artist` → `invalid_query`.
- Arguments: `None`, `{}`, `{"query":123}`, `{"query":""}`, `{"query":"   "}`,
  `{"query":"SELECT 1","database":"other.sqlite"}` → `invalid_arguments`
  (no model-controlled DB opened).
- Limits: over-length query → `resource_limit`; `SELECT TrackId, Name FROM Track`
  → `row_count=100`, `truncated=true`; 60-column select → `resource_limit`;
  runaway `WITH RECURSIVE … SELECT COUNT(*)` interrupted by the progress handler →
  `resource_limit`.
- Missing DB (non-existent configured path) → `database_not_initialized`.
- Error output for `SELECT * FROM MissingTable` contains no absolute path / `Traceback`.
- **DB immutability:** sha256 of the database file **identical** before and after
  the full harness (writes, DDL, attach, multi-statement all blocked).
- Regression: `registry.to_ollama_tools()` → `["python_calculate","sql_query"]`;
  `python_calculate({"expression":"173 * 284"}) → 49132`; executor routes both.

**Live CLI (AC-4/5/6/7/8/21/22/23), scripted stdin against a scratch history:**
```text
You: How many tracks are in the database?
[tool] sql_query
[args] {"query": "SELECT COUNT(*) FROM Track;"}
[result] {"ok": true, "columns": ["COUNT(*)"], "rows": [[3503]], "row_count": 1, "truncated": false}
Qwen: There are **3,503 tracks** in the database.

You: Which five genres generated the most revenue?
[tool] sql_query
[args] {"query": "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) AS TotalRevenue FROM InvoiceLine il JOIN Track t ON il.TrackId = t.TrackId JOIN Genre g ON t.GenreId = g.GenreId GROUP BY g.Name ORDER BY TotalRevenue DESC LIMIT 5;"}
[result] {"ok": true, "columns": ["Name", "TotalRevenue"], "rows": [["Rock", 826.65], ["Latin", 382.14], ["Metal", 261.36], ["Alternative & Punk", 241.56], ["TV Shows", 93.53]], "row_count": 5, "truncated": false}
Qwen: 1. Rock — $826.65  2. Latin — $382.14  3. Metal — $261.36  4. Alternative & Punk — $241.56  5. TV Shows — $93.53

You: Which customer spent the most money?
[tool] sql_query → [["Helena","Holý",49.62]]
Qwen: **Helena Holý**, with a total of **$49.62**.

You: Which employee supports the largest number of customers?
[tool] sql_query → [[3,"Jane","Peacock",21]]
Qwen: **Jane Peacock**, with **21 customers**.

You: Show the three longest tracks with their artists.
[tool] sql_query (Track⋈Album⋈Artist ORDER BY Milliseconds DESC LIMIT 3)
Qwen: Occupation / Precipice — Battlestar Galactica (5,286,953 ms); Through a Looking Glass — Lost; Greetings from Earth, Pt. 1 — Battlestar Galactica (Classic).

You: What is 173 multiplied by 284?
[tool] python_calculate
[args] {"expression": "(173 * 284)"}
[result] {"ok": true, "result": 49132}
Qwen: **49,132**.

You: Explain what SQLite is.
Qwen: SQLite is a lightweight, embedded relational database... (no [tool] block)

You: /bye
Chat finished.
```
Database questions selected `sql_query` with real schema names; arithmetic
selected `python_calculate`; the conceptual question answered with **no** tool;
final answers matched the returned rows; no claimed-write success; no second tool
call executed; no hidden reasoning printed; the lazy `Qwen:` prefix and `/reset`,
`/bye`, empty-input behavior were preserved.

**Semantic persistence (AC-23).** After the run the scratch history held only
`user`/`assistant` `{role, content}` pairs — no `tool_calls`, no `tool` role, no
SQL text, no rows (verified: `has tool_calls: False | has SELECT: False |
has role tool: False`). The real `data/chat_history.json` was backed up before
and restored after the run.

## Outcome
All acceptance criteria AC-1…AC-28 met (AC-27 satisfied via a journaled
standalone harness rather than a committed `tests/` suite — see Deviation). The
harness performs real SQL-assisted turns: registry → model tool selection →
one safe read-only SQLite query → structured rows → streamed grounded answer,
with writes/DDL/attach/pragma/multi-statement blocked at the connection boundary,
the database sha256-unchanged, bounded results, the calculator still working,
normal text still streaming, and history that stays user-facing. The
`create_sql_query_handler` seam is reusable and the one-bounded-tool-round
contract is unchanged.

## Follow-ups
- General agent loop with multiple/repeated tool calls per turn and SQL
  self-correction (STEP 10).
- Optional schema-introspection tool once multi-call turns exist.
- Additional tools (MCP) reusing the same execution path.
