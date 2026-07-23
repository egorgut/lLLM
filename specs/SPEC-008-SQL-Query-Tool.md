# SPEC-008: SQLite SQL Query Tool

## Background

SPEC-006 introduced the shared tool contract and registry:

```text
ToolSpec
    │
    ▼
ToolRegistry
```

SPEC-007 completed the first executable tool path:

```text
User request
    │
    ▼
Model selects a tool
    │
    ▼
Harness executes a registered handler
    │
    ▼
Tool result is returned to the model
    │
    ▼
Model produces the final answer
```

The current application now has:

- `ToolSpec`;
- `ToolRegistry`;
- `ToolExecutor`;
- Ollama function-tool declarations;
- model-selected structured tool calls;
- one bounded tool execution per user turn;
- temporary tool protocol messages;
- streamed final answers;
- semantic conversation persistence;
- the executable `python_calculate` tool.

The next step is to connect the harness to a real relational database.

There is currently no local database server or project dataset. For this iteration, the repository contains the trusted Chinook SQLite seed script:

```text
data/seed/Chinook_Sqlite.sql
```

Chinook is a compact sample database representing a digital music store. It includes artists, albums, tracks, customers, employees, invoices, and invoice lines. It is sufficiently relational to demonstrate filtering, aggregation, joins, grouping, sorting, and business-style analytical questions without introducing database-server infrastructure.

This iteration must prove the complete path:

```text
natural-language question
→ model-generated SQL
→ safe local execution
→ structured rows
→ grounded natural-language answer
```

The iteration remains deliberately narrow. It uses one fixed local SQLite database, read-only queries, one tool execution per turn, and a schema supplied to the model in advance.

---

## Goal

Add a read-only `sql_query` tool backed by a local Chinook SQLite database.

The harness must:

1. create `data/chinook.sqlite` reproducibly from the trusted seed script;
2. describe the Chinook schema to the model;
3. allow the model to generate one SQL query;
4. execute that query locally through Python's standard `sqlite3` module;
5. enforce read-only access and deterministic resource limits;
6. return a structured JSON-compatible result;
7. send that result back to the model;
8. stream the model's final user-facing answer;
9. preserve the existing semantic conversation-history policy.

Target interaction:

```text
User: Which five countries generated the most revenue?

[tool] sql_query
[args] {"query": "SELECT BillingCountry, ROUND(SUM(Total), 2) AS Revenue FROM Invoice GROUP BY BillingCountry ORDER BY Revenue DESC LIMIT 5"}
[result] {"ok": true, "columns": ["BillingCountry", "Revenue"], "rows": [["USA", 523.06], ["Canada", 303.96], ...], "row_count": 5, "truncated": false}

Qwen: The United States generated the most revenue, followed by Canada...
```

No external database service is required.

No network request is made by the SQL handler.

No third-party Python database package is required.

---

## User-visible behavior

### Successful analytical query

```text
You: Which three artists have the largest number of tracks?

[tool] sql_query
[args] {"query": "SELECT ar.Name AS Artist, COUNT(t.TrackId) AS TrackCount FROM Artist ar JOIN Album al ON al.ArtistId = ar.ArtistId JOIN Track t ON t.AlbumId = al.AlbumId GROUP BY ar.ArtistId, ar.Name ORDER BY TrackCount DESC, Artist ASC LIMIT 3"}
[result] {"ok": true, "columns": ["Artist", "TrackCount"], "rows": [["Iron Maiden", 213], ["U2", 135], ["Led Zeppelin", 114]], "row_count": 3, "truncated": false}

Qwen: Iron Maiden has the most tracks in the database with 213...
```

The exact returned values must come from the local database and must not be hard-coded.

### Normal non-tool response

```text
You: What is a relational database?

Qwen: A relational database stores data in tables...
```

No SQL tool status is shown when the model does not request a tool.

### Invalid SQL

```text
You: Delete all invoices.

[tool] sql_query
[args] {"query": "DELETE FROM Invoice"}
[result] {"ok": false, "error": {"type": "read_only_violation", "message": "Only read-only SELECT queries are allowed."}}

Qwen: I cannot delete the invoices because the database tool is read-only.
```

The application must remain usable after the failed tool execution.

---

## Core architectural decisions

### 1. SQLite is the database engine for SPEC-008

Use Python's standard-library `sqlite3` module.

Runtime database:

```text
data/chinook.sqlite
```

Trusted source script:

```text
data/seed/Chinook_Sqlite.sql
```

The application must not require:

- SQLite CLI installation;
- Docker;
- PostgreSQL;
- SQL Server;
- SQLAlchemy;
- an ORM;
- a database server;
- a database username or password;
- a network connection.

Conceptually:

```text
venv/bin/python app.py
        │
        ├── Ollama client
        ├── ToolRegistry
        ├── ToolExecutor
        └── sql_query handler
                 │
                 ▼
          Python sqlite3
                 │
                 ▼
       data/chinook.sqlite
```

### 2. The seed script is trusted; model-generated SQL is untrusted

The repository seed script is a developer-controlled input used only to initialize the local database.

It may contain schema creation, inserts, indexes, transactions, or trusted setup statements.

Model-generated SQL is a separate trust boundary.

Never execute model-generated SQL through:

```python
connection.executescript(...)
```

The model query must be executed through a single-statement API such as:

```python
connection.execute(query)
```

Initialization and runtime query execution must be implemented as separate code paths.

### 3. The model generates SQL; the harness controls the connection

The tool accepts only:

```json
{
  "query": "SELECT ..."
}
```

The model must not supply:

- database path;
- connection URI;
- driver;
- username;
- password;
- host;
- port;
- timeout;
- SQLite pragmas;
- row limit configuration.

The fixed database path comes from harness configuration.

Incorrect:

```json
{
  "database": "/tmp/other.sqlite",
  "query": "SELECT * FROM secret_table"
}
```

Correct:

```json
{
  "query": "SELECT Name FROM Artist ORDER BY Name LIMIT 10"
}
```

### 4. Runtime database access is read-only

Open the runtime connection in SQLite URI read-only mode.

Conceptually:

```python
sqlite3.connect(
    database_uri,
    uri=True,
)
```

The URI must include:

```text
mode=ro
```

The connection must not create the database when the file is missing.

A missing database is a setup error, not an empty database.

Read-only mode is mandatory but not the only protection.

Also use a SQLite authorizer callback to reject operations outside the intended query surface.

At minimum, reject:

- `INSERT`;
- `UPDATE`;
- `DELETE`;
- `CREATE TABLE`;
- `CREATE INDEX`;
- `DROP TABLE`;
- `DROP INDEX`;
- `ALTER TABLE`;
- `REINDEX`;
- `ANALYZE`;
- `VACUUM`;
- `ATTACH`;
- `DETACH`;
- transaction control initiated by the query;
- writable pragmas;
- extension loading.

`PRAGMA` statements should be rejected entirely for SPEC-008.

The tool is intended for read-only analytical `SELECT` statements, including `WITH ... SELECT ...`.

### 5. One SQL statement per tool call

The query must contain exactly one executable SQL statement.

Use `connection.execute`, not `executescript`.

Examples that must not execute:

```sql
SELECT * FROM Artist;
DELETE FROM Artist;
```

```sql
ATTACH DATABASE '/tmp/other.sqlite' AS other;
SELECT * FROM other.secret;
```

A trailing semicolon on one valid statement may be accepted consistently.

Do not implement a custom SQL parser unless necessary.

Rely on:

- single-statement execution;
- read-only connection mode;
- SQLite authorizer;
- explicit argument checks;
- resource limits.

Simple prefix checks such as `query.upper().startswith("SELECT")` are not sufficient as the primary security control because valid queries may begin with `WITH`, and SQL can include comments or misleading text.

A lightweight early check may improve error messages, but the actual enforcement must occur at the SQLite connection boundary.

### 6. The schema is supplied before tool selection

SPEC-008 still supports at most one tool execution per user turn.

Therefore this sequence is not available yet:

```text
model requests schema
→ harness returns schema
→ model writes SQL
→ harness executes SQL
```

That would require multiple tool calls and belongs to the future agent loop.

For SPEC-008, the model receives a concise, static Chinook schema description before deciding whether to call `sql_query`.

The schema guidance must include at least the tables and columns needed for common questions:

```text
Artist(
    ArtistId,
    Name
)

Album(
    AlbumId,
    Title,
    ArtistId
)

Track(
    TrackId,
    Name,
    AlbumId,
    MediaTypeId,
    GenreId,
    Composer,
    Milliseconds,
    Bytes,
    UnitPrice
)

Genre(
    GenreId,
    Name
)

MediaType(
    MediaTypeId,
    Name
)

Playlist(
    PlaylistId,
    Name
)

PlaylistTrack(
    PlaylistId,
    TrackId
)

Employee(
    EmployeeId,
    LastName,
    FirstName,
    Title,
    ReportsTo,
    BirthDate,
    HireDate,
    Address,
    City,
    State,
    Country,
    PostalCode,
    Phone,
    Fax,
    Email
)

Customer(
    CustomerId,
    FirstName,
    LastName,
    Company,
    Address,
    City,
    State,
    Country,
    PostalCode,
    Phone,
    Fax,
    Email,
    SupportRepId
)

Invoice(
    InvoiceId,
    CustomerId,
    InvoiceDate,
    BillingAddress,
    BillingCity,
    BillingState,
    BillingCountry,
    BillingPostalCode,
    Total
)

InvoiceLine(
    InvoiceLineId,
    InvoiceId,
    TrackId,
    UnitPrice,
    Quantity
)
```

Important relationships:

```text
Album.ArtistId          → Artist.ArtistId
Track.AlbumId           → Album.AlbumId
Track.GenreId           → Genre.GenreId
Track.MediaTypeId       → MediaType.MediaTypeId
PlaylistTrack.PlaylistId→ Playlist.PlaylistId
PlaylistTrack.TrackId   → Track.TrackId
Customer.SupportRepId   → Employee.EmployeeId
Invoice.CustomerId      → Customer.CustomerId
InvoiceLine.InvoiceId   → Invoice.InvoiceId
InvoiceLine.TrackId     → Track.TrackId
Employee.ReportsTo      → Employee.EmployeeId
```

The schema may live in `prompts.py`, the SQL tool module, or a dedicated small schema module.

There must be one authoritative schema-description constant, not duplicated independently across several files.

### 7. The tool returns data, not prose

The SQL handler must not summarize business results.

It returns structured data:

```json
{
  "ok": true,
  "columns": ["Country", "Revenue"],
  "rows": [
    ["USA", 523.06],
    ["Canada", 303.96]
  ],
  "row_count": 2,
  "truncated": false
}
```

The model is responsible for converting this data into a user-facing answer.

This separation must remain clear:

```text
sql_query
    executes and serializes rows

model
    interprets and explains rows
```

### 8. One bounded tool round remains unchanged

SPEC-008 does not introduce the general agent loop.

Allowed:

```text
User
→ model requests sql_query
→ one SQL execution
→ model final answer
```

Not allowed:

```text
User
→ sql_query
→ model
→ sql_query again
→ model final answer
```

If the second model response requests another tool, preserve the existing SPEC-007 behavior and fail the turn clearly.

### 9. Existing tool architecture must be reused

Do not add SQL-specific dispatch logic directly to `app.py`.

The SQL tool must follow the same pattern as `python_calculate`:

```text
SQL_QUERY_SPEC
      │
      ▼
ToolRegistry.register(...)
      │
      ▼
ToolExecutor.register_handler(...)
      │
      ▼
sql_query(arguments)
```

`ToolSpec` remains metadata-only.

The executable handler remains separate.

`ToolExecutor` remains generic.

---

## Target architecture

```text
┌────────────────┐
│     app.py     │
│ CLI + one-tool │
│ orchestration  │
└───────┬────────┘
        │
        ├────────────────► Conversation
        │
        ├────────────────► JsonConversationStore
        │
        ├────────────────► ModelResponse / Ollama
        │
        └────────────────► ToolExecutor
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
                    ▼                           ▼
           python_calculate                sql_query
                    │                           │
                    ▼                           ▼
              local AST                 Python sqlite3
                                                │
                                                ▼
                                     data/chinook.sqlite
```

Expected responsibilities:

```text
scripts/init_database.py
    creates the runtime SQLite database from the trusted seed script

tools/sql_query.py
    SQL tool contract, validation, read-only connection, execution,
    limits, result serialization, stable tool errors

tools/registry.py
    unchanged generic tool metadata and registry behavior

tools/executor.py
    unchanged generic handler binding and dispatch behavior

tools/__init__.py
    deliberate exports for the SQL tool

config.py
    fixed project-relative paths for seed and runtime database

prompts.py
    concise tool-selection instructions and authoritative Chinook schema guidance

app.py
    register the SQL ToolSpec and bind its handler

README.md
    setup, initialization command, example usage, limitations

tests/
    focused unit/integration tests when the repository testing approach permits
```

---

## Database initialization

### Command

Add a reproducible initialization command:

```bash
python scripts/init_database.py
```

The command must:

1. resolve paths relative to the project rather than the current shell directory;
2. locate `data/seed/Chinook_Sqlite.sql`;
3. fail clearly if the seed script is missing;
4. create the parent directory for the runtime database if needed;
5. avoid leaving a partially initialized target database;
6. execute the trusted seed script;
7. validate that expected tables exist;
8. close the connection;
9. report the resulting database path.

Expected success output may be:

```text
Created Chinook database: data/chinook.sqlite
```

### Safe replacement policy

The initializer must not silently destroy an existing database.

Use one clear policy:

- default: fail if `data/chinook.sqlite` already exists;
- optional explicit flag: `--force` replaces it.

Suggested behavior:

```bash
python scripts/init_database.py
```

```text
Database already exists: data/chinook.sqlite
Use --force to recreate it.
```

```bash
python scripts/init_database.py --force
```

The exact CLI implementation may use `argparse`.

When `--force` is used:

1. build a new database at a temporary sibling path;
2. validate it;
3. atomically replace the target where practical.

At minimum, a seed failure must not leave a runtime database that appears valid.

### Initialization validation

After running the seed, verify at least these tables exist:

```text
Artist
Album
Track
Genre
Customer
Employee
Invoice
InvoiceLine
```

Recommended additional validation:

- ensure `Artist` contains at least one row;
- ensure `Track` contains at least one row;
- ensure `Invoice` contains at least one row.

Do not hard-code exact row counts unless the supplied seed version is intentionally pinned and the counts are documented.

### Runtime database in Git

The generated file should not be committed:

```text
data/chinook.sqlite
```

Add it to `.gitignore`.

Keep the seed script under version control:

```text
data/seed/Chinook_Sqlite.sql
```

This makes the environment reproducible while avoiding a generated binary database in Git.

---

## Configuration

Add project-relative database paths.

Suggested:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

CHINOOK_SEED_PATH = PROJECT_ROOT / "data" / "seed" / "Chinook_Sqlite.sql"
SQLITE_DATABASE_PATH = PROJECT_ROOT / "data" / "chinook.sqlite"
```

Use names consistent with the existing configuration style.

Requirements:

- do not depend on the current working directory;
- do not expose the path to the model;
- do not accept a path from tool arguments;
- keep one configured runtime database in SPEC-008.

The SQL handler may accept the configured path through a small factory or explicit dependency injection if that improves testability.

Examples:

```python
handler = create_sql_query_handler(SQLITE_DATABASE_PATH)
```

or:

```python
def sql_query(arguments: dict[str, Any]) -> dict[str, Any]:
    ...
```

with the configured path imported from `config.py`.

Prefer a design that allows tests to use a temporary SQLite database without modifying global files.

---

## Tool definition

Register one additional tool:

```text
sql_query
```

Suggested contract:

```python
SQL_QUERY_SPEC = ToolSpec(
    name="sql_query",
    description=(
        "Run one read-only SQLite SELECT query against the local Chinook music-store "
        "database. Use the supplied schema. Return database rows for factual and "
        "analytical questions. Never use it for writes or schema changes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "One read-only SQLite SELECT statement, optionally beginning "
                    "with WITH. Include explicit joins, grouping, ordering, and a "
                    "reasonable LIMIT when appropriate."
                ),
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "columns": {
                "type": "array",
                "items": {"type": "string"},
            },
            "rows": {
                "type": "array",
                "items": {"type": "array"},
            },
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "error": {"type": "object"},
        },
        "required": ["ok"],
    },
)
```

Adjust the schema to the actual `ToolSpec` validation rules introduced in SPEC-006.

The tool accepts exactly:

```json
{
  "query": "SELECT Name FROM Artist ORDER BY Name LIMIT 10"
}
```

No additional properties are allowed.

---

## Argument validation

Validate before opening or executing the query.

Required:

- `arguments` is a dictionary;
- it contains exactly one key: `query`;
- `query` is a string;
- `query` is not empty;
- query length is bounded;
- NUL bytes are rejected;
- no additional arguments are accepted.

Suggested maximum query length:

```text
10,000 characters
```

A smaller deterministic limit is acceptable if documented.

Whitespace may be normalized only at the edges:

```python
query = query.strip()
```

Do not rewrite the internal SQL.

Do not automatically append clauses or modify model-generated semantics.

Do not automatically add `LIMIT` to the SQL text. Enforce output limits during row fetching instead.

---

## SQL execution policy

### Supported query shape

Support one read-only result-producing SQLite query.

Common examples:

```sql
SELECT Name
FROM Artist
ORDER BY Name
LIMIT 10;
```

```sql
SELECT
    g.Name AS Genre,
    ROUND(SUM(il.UnitPrice * il.Quantity), 2) AS Revenue
FROM InvoiceLine AS il
JOIN Track AS t
    ON t.TrackId = il.TrackId
JOIN Genre AS g
    ON g.GenreId = t.GenreId
GROUP BY g.GenreId, g.Name
ORDER BY Revenue DESC
LIMIT 5;
```

```sql
WITH customer_totals AS (
    SELECT
        CustomerId,
        SUM(Total) AS Revenue
    FROM Invoice
    GROUP BY CustomerId
)
SELECT
    c.FirstName || ' ' || c.LastName AS Customer,
    ROUND(ct.Revenue, 2) AS Revenue
FROM customer_totals AS ct
JOIN Customer AS c
    ON c.CustomerId = ct.CustomerId
ORDER BY Revenue DESC
LIMIT 5;
```

SQLite built-in scalar and aggregate functions needed for normal analytics should remain available.

Examples:

```text
COUNT
SUM
AVG
MIN
MAX
ROUND
LENGTH
LOWER
UPPER
COALESCE
DATE
STRFTIME
```

Do not register custom Python SQL functions in SPEC-008.

### Forbidden operations

The following must not execute:

```sql
INSERT INTO Artist(Name) VALUES ('Injected');
UPDATE Customer SET Email = 'x@example.com';
DELETE FROM Invoice;
DROP TABLE Track;
ALTER TABLE Artist ADD COLUMN Secret TEXT;
CREATE TABLE NewTable(Id INTEGER);
CREATE INDEX idx_test ON Track(Name);
ATTACH DATABASE '/tmp/other.sqlite' AS other;
DETACH DATABASE other;
VACUUM;
PRAGMA writable_schema = 1;
```

Also reject or prevent:

- multiple SQL statements;
- extension loading;
- file-backed attachment;
- writes through writable CTEs if supported by the engine;
- schema mutation;
- transaction mutation;
- access to another database file;
- runtime changes to connection behavior.

The database must remain unchanged after every tool call.

---

## SQLite authorizer

Use `sqlite3.Connection.set_authorizer(...)` as a defense-in-depth boundary.

The callback should allow only the SQLite actions needed for read-only queries and built-in functions.

Implementation details may vary by Python and SQLite version, but the policy must be explicit and covered by tests.

Expected allowed action categories include those needed for:

- `SELECT`;
- reading table columns;
- calling built-in SQLite functions;
- recursive query processing required by valid SELECT statements.

Expected denied categories include those needed for:

- insert/update/delete;
- schema create/drop/alter;
- attach/detach;
- pragma;
- transaction changes;
- reindex/analyze;
- virtual-table creation or deletion.

Return the SQLite deny code for forbidden actions.

Translate authorization failures into:

```json
{
  "ok": false,
  "error": {
    "type": "read_only_violation",
    "message": "Only read-only SELECT queries are allowed."
  }
}
```

Do not expose numeric SQLite authorizer codes to the model.

Do not expose callback internals.

---

## Resource limits

Even a read-only query can consume excessive CPU or produce excessive output.

Apply deterministic limits.

Required minimum:

- maximum query length;
- one statement only;
- maximum returned rows;
- maximum returned columns;
- maximum total serialized result size or an equivalent conservative bound;
- SQLite progress-handler limit;
- no unbounded `fetchall()`.

Suggested defaults:

```text
MAX_QUERY_LENGTH = 10_000
MAX_RESULT_ROWS = 100
MAX_RESULT_COLUMNS = 50
MAX_RESULT_BYTES = 100_000
PROGRESS_HANDLER_INTERVAL = 1_000 virtual-machine instructions
MAX_PROGRESS_CALLBACKS = 10_000
```

The exact constants may be adjusted after local testing, but must remain small, deterministic, and documented.

### Row limit

Fetch at most:

```text
MAX_RESULT_ROWS + 1
```

If the extra row exists:

- return only `MAX_RESULT_ROWS`;
- set `truncated` to `true`;
- set `row_count` to the number of rows actually returned.

Example:

```json
{
  "ok": true,
  "columns": ["TrackId", "Name"],
  "rows": [
    [1, "For Those About To Rock (We Salute You)"]
  ],
  "row_count": 100,
  "truncated": true
}
```

`row_count` means returned row count, not total rows in the full unbounded query.

Do not run a second `COUNT(*)` query to discover the omitted total.

### Column limit

If a query returns more than the configured maximum number of columns, return a structured resource-limit error rather than a partial ambiguous row shape.

### Execution-work limit

Use `Connection.set_progress_handler(...)` to interrupt a query after a bounded amount of SQLite virtual-machine work.

Translate interruption caused by the configured limit into:

```json
{
  "ok": false,
  "error": {
    "type": "resource_limit",
    "message": "The query exceeded the execution limit."
  }
}
```

Do not expose raw operational-error messages when they contain implementation details.

### Result-size limit

Ensure the complete tool result remains reasonably small before sending it to Ollama.

A deterministic JSON-size check is acceptable:

```python
len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
```

If the result exceeds the limit, either:

- stop adding rows and mark the result truncated, while preserving complete rows;
- or return a structured `resource_limit` error.

Prefer truncating by complete rows when practical.

Never cut a JSON string at an arbitrary byte boundary.

---

## Result serialization

### Successful result

Return:

```json
{
  "ok": true,
  "columns": ["Artist", "AlbumCount"],
  "rows": [
    ["Iron Maiden", 21],
    ["Led Zeppelin", 14]
  ],
  "row_count": 2,
  "truncated": false
}
```

Requirements:

- `columns` preserves cursor column order;
- each row is an array preserving column order;
- `row_count == len(rows)`;
- `truncated` is always present on success;
- all values are JSON-compatible;
- no cursor or SQLite objects escape the handler.

Expected SQLite values:

- `null`;
- integer;
- finite float;
- string.

If bytes or another unsupported value appears, return a safe structured error unless an explicit deterministic conversion is implemented.

Reject non-finite floats.

Do not stringify every value indiscriminately.

### Empty result

A valid query returning no rows is successful:

```json
{
  "ok": true,
  "columns": ["Name"],
  "rows": [],
  "row_count": 0,
  "truncated": false
}
```

### Query without a result set

A statement with no cursor description must not be treated as success.

Return a read-only or invalid-query error as appropriate.

---

## Tool result envelope

Use the result convention established in SPEC-007.

### Success

```json
{
  "ok": true,
  "columns": ["Name"],
  "rows": [["AC/DC"], ["Accept"]],
  "row_count": 2,
  "truncated": false
}
```

### Failure

```json
{
  "ok": false,
  "error": {
    "type": "invalid_query",
    "message": "The SQL query is invalid."
  }
}
```

Required stable error categories:

```text
invalid_arguments
database_not_initialized
invalid_query
read_only_violation
resource_limit
unsupported_result
database_error
internal_error
```

Suggested meanings:

- `invalid_arguments`: malformed tool argument object;
- `database_not_initialized`: configured SQLite file does not exist or cannot be opened read-only;
- `invalid_query`: SQL syntax, missing table, missing column, ambiguous column, or another user/model query defect;
- `read_only_violation`: write, schema mutation, pragma, attach, transaction, or another forbidden operation;
- `resource_limit`: query length, work, columns, rows/result size, or another configured bound;
- `unsupported_result`: returned value cannot be represented safely;
- `database_error`: controlled runtime database failure not caused by ordinary invalid SQL;
- `internal_error`: unexpected implementation failure.

Messages must be:

- concise;
- stable enough for tests;
- useful to the model;
- free of secrets and internal stack information.

The tool result must not contain:

- Python traceback;
- absolute filesystem paths;
- environment variables;
- connection URI;
- SQLite object representations;
- memory addresses;
- exception chains;
- seed-script content;
- arbitrary local file content.

Developer logs are outside the scope unless already supported by the project.

---

## Error classification

SQLite may use similar exception types for different failures.

Classify errors deliberately.

Examples:

### Invalid SQL syntax

Input:

```sql
SELEC Name FROM Artist
```

Result:

```json
{
  "ok": false,
  "error": {
    "type": "invalid_query",
    "message": "The SQL query is invalid."
  }
}
```

### Missing table

Input:

```sql
SELECT * FROM Orders
```

Result:

```json
{
  "ok": false,
  "error": {
    "type": "invalid_query",
    "message": "The query references an unknown table or column."
  }
}
```

The exact stable message may differ, but must not include absolute paths or internal state.

### Write attempt

Input:

```sql
DELETE FROM Invoice
```

Result:

```json
{
  "ok": false,
  "error": {
    "type": "read_only_violation",
    "message": "Only read-only SELECT queries are allowed."
  }
}
```

### Missing database

Result:

```json
{
  "ok": false,
  "error": {
    "type": "database_not_initialized",
    "message": "The local Chinook database is not initialized."
  }
}
```

The message may tell the user to run:

```bash
python scripts/init_database.py
```

The tool result itself should remain concise. The README and CLI error can provide the setup command.

### Query interrupted by limit

Result:

```json
{
  "ok": false,
  "error": {
    "type": "resource_limit",
    "message": "The query exceeded the execution limit."
  }
}
```

---

## Connection lifecycle

Open a fresh read-only SQLite connection for each tool execution.

Advantages:

- no cross-turn transaction state;
- no stale cursor state;
- simpler cleanup;
- easier tests;
- clear trust boundary.

Required lifecycle:

```text
validate arguments
→ verify database file
→ open read-only connection
→ configure protections
→ execute one statement
→ fetch bounded rows
→ serialize result
→ close connection
```

Use a context manager or `try/finally`.

The connection must close for:

- success;
- invalid SQL;
- authorization failure;
- resource-limit interruption;
- keyboard interruption;
- unexpected exception.

Do not keep a global mutable connection in SPEC-008.

---

## System prompt guidance

Update the system prompt minimally.

The model should understand:

- `sql_query` accesses the local Chinook music-store database;
- it should use the tool for questions whose answer depends on database contents;
- it must generate SQLite syntax;
- it has exactly one SQL execution opportunity per turn;
- it should use the supplied schema rather than invent tables or columns;
- it should use explicit joins;
- it should qualify ambiguous columns;
- it should aggregate only when the user's question requires aggregation;
- it should include deterministic ordering where ranking is requested;
- it should use a reasonable `LIMIT` for lists;
- it must not request writes or schema changes;
- it must base the final answer on returned rows;
- it must mention when the result was truncated;
- it must not pretend the query succeeded when the tool returned an error;
- it should answer normally when no database access is needed.

The prompt should not instruct the model to expose chain of thought.

Do not include Python implementation details such as authorizer action codes.

Do not teach the model how to bypass restrictions.

---

## Tool selection behavior

Both tools are available:

```text
python_calculate
sql_query
```

Expected selection examples:

| User request | Expected behavior |
|---|---|
| `What is 173 * 284?` | use `python_calculate` |
| `How many tracks are in the database?` | use `sql_query` |
| `Which genre generated the most revenue?` | use `sql_query` |
| `What is SQLite?` | answer normally |
| `Delete all customers.` | preferably refuse or call `sql_query` and receive a read-only error; never mutate data |
| `Calculate the square root of 81.` | use `python_calculate` |
| `What is the average invoice total in Chinook?` | use `sql_query`, because the source values are in the database |

Do not add hard-coded keyword routing in the harness.

The model remains responsible for tool choice.

---

## Ollama interaction

Reuse the SPEC-007 flow.

### First request

Send:

- current semantic conversation messages;
- `python_calculate` declaration;
- `sql_query` declaration;
- system prompt containing concise schema/tool guidance.

### Possible first response

Preserve existing policies:

- normal text only: stream normally;
- exactly one supported tool call: execute it;
- content plus tool call: tool call is authoritative;
- multiple tool calls: reject as unsupported;
- unknown tool: fail clearly;
- malformed arguments: return structured error when protocol continuation is safe.

### Tool result

Append temporary provider-format messages:

```text
assistant tool-call message
tool result message
```

Then make the second model request.

### Second request

The second model response must produce the final user-facing answer.

If it requests another tool, stop with the existing bounded-turn error.

Do not execute a second SQL query.

Do not automatically retry invalid SQL.

SQL self-correction through repeated tool calls belongs to STEP 10.

---

## CLI presentation

Reuse existing rendering:

```text
[tool] sql_query
[args] {"query": "SELECT COUNT(*) AS TrackCount FROM Track"}
[result] {"ok": true, "columns": ["TrackCount"], "rows": [[3503]], "row_count": 1, "truncated": false}
```

Requirements:

- deterministic JSON via `json.dumps`;
- Unicode preserved;
- no Python dictionary repr;
- no raw cursor;
- no raw Ollama object;
- no traceback;
- no hidden reasoning;
- no database absolute path.

The existing lazy `Qwen:` prefix behavior must remain correct.

---

## Conversation and persistence policy

Persistent history remains semantic.

After a successful SQL-assisted turn, persist only:

```json
[
  {
    "role": "user",
    "content": "How many tracks are in the database?"
  },
  {
    "role": "assistant",
    "content": "The Chinook database contains 3,503 tracks."
  }
]
```

Do not persist:

- generated SQL separately;
- tool-call protocol message;
- tool-result protocol message;
- raw rows;
- cursor metadata;
- database path;
- internal error details.

Tool calls remain visible in the current CLI session but are not part of the saved semantic chat history.

### Failed-turn rollback

Preserve SPEC-007 behavior.

If no complete final assistant response is produced:

- remove the current user message;
- do not persist a partial exchange;
- do not add an assistant message;
- leave previous history unchanged;
- return to the CLI loop.

A SQL tool error may still be sent to the model.

If the model successfully explains the error, the user-visible exchange may be persisted.

---

## Files to add or modify

### `data/seed/Chinook_Sqlite.sql`

Already supplied before implementation.

Requirements:

- treat as trusted initialization input;
- keep under version control;
- do not modify unless necessary for compatibility;
- document any modification explicitly.

### `scripts/init_database.py`

Add:

- project-relative path resolution;
- seed-file validation;
- runtime database creation;
- optional `--force`;
- safe failure behavior;
- expected-table validation;
- concise CLI output.

This module must not:

- call Ollama;
- register tools;
- modify chat history;
- download anything from the internet.

### `tools/sql_query.py`

Add:

- `SQL_QUERY_SPEC`;
- SQL-specific constants;
- argument validation;
- read-only connection creation;
- SQLite authorizer;
- progress handler;
- bounded row fetching;
- result serialization;
- stable error envelopes;
- `sql_query(arguments)` or a testable handler factory.

This module must not:

- call Ollama;
- print to the CLI;
- access conversation state;
- write to the database;
- choose a database path from model input;
- access network resources;
- launch subprocesses;
- execute multiple statements;
- use `executescript` for model SQL.

### `tools/__init__.py`

Export the intended SQL public API.

Likely exports:

```python
SQL_QUERY_SPEC
sql_query
```

or:

```python
SQL_QUERY_SPEC
create_sql_query_handler
```

Keep exports deliberate.

### `config.py`

Add the trusted project-relative runtime database path and seed path if the initializer imports configuration.

Do not add credentials.

Do not add connection strings from environment variables for SPEC-008.

### `app.py`

Extend startup wiring:

```python
registry.register(PYTHON_CALCULATE_SPEC)
registry.register(SQL_QUERY_SPEC)

executor.register_handler("python_calculate", python_calculate)
executor.register_handler("sql_query", sql_query)
```

Equivalent dependency-injected wiring is acceptable.

Do not duplicate the SQL schema or SQL safety logic in `app.py`.

Do not change the one-tool-per-turn orchestration unless required for generic correctness.

### `prompts.py`

Add:

- concise SQL tool guidance;
- authoritative Chinook schema description;
- SQLite dialect note;
- one-query constraint;
- grounding instruction.

Avoid excessive prompt size and repetition.

### `.gitignore`

Add:

```text
data/chinook.sqlite
```

Also ignore any temporary initializer database pattern if one is introduced.

Do not ignore the seed SQL file.

### `README.md`

Document:

- Chinook is the local sample database;
- the seed file location;
- initialization command;
- optional `--force`;
- generated database location;
- read-only SQL behavior;
- one tool call per turn;
- visible CLI example;
- no external database server is needed;
- how to recover from `database_not_initialized`.

### `tests/`

Add focused tests consistent with the project.

Suggested files:

```text
tests/test_init_database.py
tests/test_sql_query.py
```

Do not introduce a heavy testing framework if the repository does not already use one. Python's standard `unittest` is acceptable.

### `docs/journal/SPEC-008-SQL-Query-Tool.md`

Create the implementation journal after completing the work, following the existing project convention.

The journal must record:

- hypothesis;
- implementation;
- deviations from this spec;
- tests;
- live-model observations;
- branch;
- commits;
- merge commit.

---

## Public interfaces

Suggested handler:

```python
def sql_query(arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one bounded read-only query against the local Chinook database."""
```

More testable alternative:

```python
def create_sql_query_handler(database_path: Path) -> ToolHandler:
    ...
```

Usage:

```python
sql_handler = create_sql_query_handler(SQLITE_DATABASE_PATH)
executor.register_handler("sql_query", sql_handler)
```

Expected result:

```python
{
    "ok": True,
    "columns": ["TrackCount"],
    "rows": [[3503]],
    "row_count": 1,
    "truncated": False,
}
```

The exact internal class structure may vary.

Avoid adding abstractions for multiple database engines before they are needed.

---

## Testing requirements

### Database initializer tests

Test at least:

1. missing seed file fails clearly;
2. successful initialization creates a valid SQLite database;
3. expected tables exist;
4. selected tables contain rows;
5. existing target without `--force` is preserved;
6. `--force` recreates the database;
7. seed failure does not leave a valid-looking partial target.

Use temporary directories where practical.

Do not overwrite the developer's real `data/chinook.sqlite` during unit tests.

### SQL handler success tests

Test at least:

```sql
SELECT COUNT(*) AS TrackCount FROM Track
```

```sql
SELECT Name FROM Artist ORDER BY Name LIMIT 3
```

```sql
SELECT
    ar.Name AS Artist,
    COUNT(t.TrackId) AS TrackCount
FROM Artist AS ar
JOIN Album AS al
    ON al.ArtistId = ar.ArtistId
JOIN Track AS t
    ON t.AlbumId = al.AlbumId
GROUP BY ar.ArtistId, ar.Name
ORDER BY TrackCount DESC, Artist ASC
LIMIT 3
```

```sql
WITH totals AS (
    SELECT CustomerId, SUM(Total) AS Revenue
    FROM Invoice
    GROUP BY CustomerId
)
SELECT COUNT(*) AS CustomerCount
FROM totals
```

Verify:

- `ok` is true;
- columns are correct;
- row shape matches columns;
- values come from the database;
- empty results are successful;
- Unicode survives serialization.

### Read-only enforcement tests

Verify these do not mutate the database:

```sql
INSERT INTO Artist(Name) VALUES ('Should Not Exist')
```

```sql
UPDATE Artist SET Name = 'Changed'
```

```sql
DELETE FROM Artist
```

```sql
DROP TABLE Artist
```

```sql
CREATE TABLE Unexpected(Id INTEGER)
```

```sql
ALTER TABLE Artist ADD COLUMN Unexpected TEXT
```

```sql
ATTACH DATABASE ':memory:' AS other
```

```sql
PRAGMA table_info(Artist)
```

For every forbidden query:

- `ok` is false;
- error type is `read_only_violation` or another documented stable type;
- the database remains unchanged;
- the handler does not crash.

### Multiple-statement tests

Verify this does not execute:

```sql
SELECT COUNT(*) FROM Artist;
DELETE FROM Artist;
```

No statement after the first may run.

### Invalid-query tests

Test:

```sql
SELEC * FROM Artist
```

```sql
SELECT * FROM MissingTable
```

```sql
SELECT MissingColumn FROM Artist
```

Return `invalid_query`.

### Resource-limit tests

Test:

- query longer than maximum;
- result larger than row limit;
- result with too many columns;
- computationally expensive recursive CTE interrupted by progress handler;
- result-size limit;
- no use of unbounded `fetchall()` in the implementation path.

### Argument tests

Test:

```python
None
{}
{"query": 123}
{"query": ""}
{"query": "   "}
{"query": "SELECT 1", "database": "other.sqlite"}
```

All must return `invalid_arguments` without opening a model-controlled database.

### Missing-database test

Use a non-existent configured path.

Expected:

```json
{
  "ok": false,
  "error": {
    "type": "database_not_initialized",
    "message": "The local Chinook database is not initialized."
  }
}
```

### Existing-tool regression tests

Verify:

- `python_calculate` still registers and executes;
- normal text-only model responses still stream;
- tool declarations contain both tools;
- one tool call still works;
- multiple calls remain rejected;
- semantic persistence remains unchanged;
- `/reset`, `/bye`, and empty input behavior remain unchanged.

### Live Ollama verification

Run manual end-to-end checks with the configured local model.

Required prompts:

```text
How many tracks are in the database?
```

```text
Which five genres generated the most revenue?
```

```text
Which customer spent the most money?
```

```text
Which employee supports the largest number of customers?
```

```text
Show the three longest tracks with their artists.
```

```text
What is 173 multiplied by 284?
```

```text
Explain what SQLite is.
```

Verify:

- database questions select `sql_query`;
- arithmetic selects `python_calculate`;
- conceptual questions can answer without tools;
- generated SQL uses real schema names;
- final answers match returned rows;
- the model does not claim writes succeeded;
- second tool calls are not executed;
- no hidden reasoning is printed.

Record observed model weaknesses in the journal rather than adding brittle keyword routing.

---

## Non-goals

Explicitly outside SPEC-008:

- PostgreSQL;
- Microsoft SQL Server;
- MySQL;
- ClickHouse;
- DuckDB;
- SQLAlchemy;
- ORM models;
- Docker database containers;
- remote database access;
- corporate database credentials;
- secret management;
- multiple databases;
- user-selected database paths;
- writes;
- inserts;
- updates;
- deletes;
- DDL;
- migrations beyond local sample initialization;
- transactions controlled by the model;
- stored procedures;
- arbitrary pragmas;
- SQLite extensions;
- extension loading;
- custom SQL functions;
- schema-discovery tool;
- automatic schema introspection during a turn;
- multiple SQL calls per turn;
- SQL repair retries;
- autonomous query planning;
- multi-step agent loop;
- query approval UI;
- human confirmation before read-only execution;
- per-table permissions;
- row-level security;
- column masking;
- corporate RBAC;
- audit database;
- persistent tool-call history;
- query caching;
- connection pooling;
- asynchronous database execution;
- parallel tools;
- natural-language-to-chart generation;
- pandas DataFrames;
- CSV/XLSX export;
- business semantic layer;
- vector search;
- RAG;
- MCP;
- third-party agent frameworks;
- exposing chain of thought.

---

## Acceptance criteria

### AC-1: Reproducible database initialization

Running:

```bash
python scripts/init_database.py
```

creates a valid `data/chinook.sqlite` from `data/seed/Chinook_Sqlite.sql`.

The initialized database contains the expected Chinook tables and data.

### AC-2: Safe existing-database behavior

The initializer does not silently overwrite an existing runtime database.

An explicit `--force` path is available if replacement is implemented.

A failed initialization does not leave a partial target that appears valid.

### AC-3: Generated database is ignored

`data/chinook.sqlite` is excluded from Git.

`data/seed/Chinook_Sqlite.sql` remains tracked.

### AC-4: SQL tool registration

`sql_query` is represented by a valid `ToolSpec`, registered in `ToolRegistry`, and bound to exactly one handler in `ToolExecutor`.

`python_calculate` remains registered and functional.

### AC-5: Ollama receives both tools

The model request receives registry-generated declarations for:

```text
python_calculate
sql_query
```

No duplicate hand-written schemas exist in `app.py` or `llm.py`.

### AC-6: Model receives Chinook schema guidance

The model receives one authoritative concise schema description covering the supported Chinook tables, columns, and key relationships.

The schema is not independently duplicated across multiple modules.

### AC-7: Model-selected SQL execution

For a database-backed question:

- the model requests `sql_query`;
- arguments contain one SQL string;
- the harness executes the registered handler;
- structured rows return to the model;
- the model produces a final grounded answer.

### AC-8: Local-only execution

The SQL handler executes through Python's local `sqlite3` module against the configured local file.

No external database or network service is used.

### AC-9: Fixed connection target

The model cannot choose or override the database path.

The handler uses the configured Chinook path only.

Additional model arguments are rejected.

### AC-10: Read-only connection

The runtime database is opened in SQLite read-only mode.

A missing database is not automatically created.

### AC-11: Defense-in-depth authorization

A SQLite authorizer or equivalent connection-level protection blocks writes, schema changes, attach/detach, pragmas, and other forbidden operations.

A simple SQL-prefix check is not the sole safety mechanism.

### AC-12: One statement only

Only one SQL statement is executed.

`executescript` is never used for model-generated SQL.

Multiple-statement input does not execute partially or fully.

### AC-13: Valid SELECT support

Normal read-only SQLite queries work, including:

- filtering;
- ordering;
- grouping;
- aggregates;
- joins;
- aliases;
- `WITH ... SELECT`;
- built-in SQLite functions needed for common analytics.

### AC-14: Structured result

Success returns:

```text
ok
columns
rows
row_count
truncated
```

Column and row order are preserved.

`row_count` equals the number of returned rows.

### AC-15: Empty-result behavior

A valid query returning zero rows succeeds with:

```text
rows = []
row_count = 0
truncated = false
```

### AC-16: Deterministic limits

The handler enforces documented limits for:

- query length;
- returned rows;
- returned columns;
- SQLite execution work;
- result size or an equivalent conservative bound.

No unbounded result fetch is used.

### AC-17: Truncation metadata

When more rows exist than the configured output limit:

- only bounded complete rows are returned;
- `truncated` is true;
- the model is instructed to disclose truncation when relevant.

### AC-18: Stable errors

Expected failures return stable JSON-compatible envelopes.

At minimum:

```text
invalid_arguments
database_not_initialized
invalid_query
read_only_violation
resource_limit
unsupported_result
database_error
internal_error
```

No traceback or absolute path is returned to the model.

### AC-19: Database immutability

After every successful or failed tool call, the Chinook schema and data remain unchanged.

Forbidden write tests confirm this behavior.

### AC-20: Connection cleanup

Every SQL execution closes its connection on success, failure, interruption, and resource-limit termination.

No global mutable SQLite connection persists across turns.

### AC-21: CLI transparency

The CLI displays:

- selected tool;
- arguments as JSON;
- structured result as JSON.

It does not display:

- hidden reasoning;
- raw Ollama objects;
- raw cursor objects;
- tracebacks;
- absolute database path.

### AC-22: One bounded tool round

At most one tool executes per user turn.

If the post-tool model response requests another tool, the harness stops without executing it.

### AC-23: Semantic persistence

Successful SQL-assisted turns persist only the user message and final assistant answer.

Generated SQL, tool protocol messages, and raw result rows are not persisted.

### AC-24: Failed-turn rollback

If a complete final answer is not produced, the current turn is rolled back and previous history remains unchanged.

### AC-25: Existing behavior regression

These behaviors remain functional:

- text streaming;
- `python_calculate`;
- `/reset`;
- `/bye`;
- empty-input handling;
- conversation context;
- JSON history loading and saving;
- interruption handling.

### AC-26: Documentation

README documents initialization, usage, read-only limitations, database location, and the one-tool-per-turn constraint.

### AC-27: Automated tests

Focused tests cover initialization, successful queries, joins, CTEs, empty results, invalid SQL, missing database, argument validation, write rejection, multi-statement rejection, truncation, resource limits, and existing-tool regression.

### AC-28: Live local-model verification

The configured local Ollama model is tested end-to-end with database, arithmetic, and normal conversational prompts.

Observed behavior and any prompt refinements are recorded in the implementation journal.

---

## Suggested implementation order

1. Add project-relative seed and runtime database paths.
2. Add `.gitignore` rule for generated SQLite files.
3. Implement and test `scripts/init_database.py`.
4. Initialize the real local Chinook database.
5. Implement `SQL_QUERY_SPEC`.
6. Implement a testable read-only SQL handler.
7. Add authorizer and execution limits.
8. Add unit/integration tests for the handler.
9. Export and register the tool.
10. Add authoritative schema/tool guidance to the prompt.
11. Run existing tests and regressions.
12. Run live Ollama end-to-end checks.
13. Update README.
14. Create the SPEC-008 journal.
15. Commit on a dedicated feature branch and merge according to the existing project workflow.

---

## Suggested branch and commit structure

Branch:

```text
feature/SPEC-008-sql-query-tool
```

Possible commits:

```text
Add reproducible Chinook SQLite initialization
Add read-only SQLite query tool
Wire SQL tool into local chat harness
Add SQL tool tests and documentation
```

A smaller commit set is acceptable if each commit remains coherent.

---

## Definition of done

SPEC-008 is complete when a clean checkout can:

```bash
python scripts/init_database.py
python app.py
```

and successfully perform:

```text
User asks a factual question about Chinook
→ model selects sql_query
→ harness executes one safe read-only SQLite query
→ structured rows are returned
→ model streams a grounded final answer
```

while:

- write attempts are blocked;
- the database cannot be redirected by model arguments;
- result size and execution work are bounded;
- the existing Python calculation tool still works;
- normal text responses still work;
- persistent history remains semantic;
- no general multi-step agent loop is introduced.
