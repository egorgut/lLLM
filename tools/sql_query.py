"""The second executable tool: a read-only SQLite query runner (SPEC-008).

`sql_query` executes exactly one model-generated read-only SQL statement against
a fixed local Chinook SQLite database. Model-generated SQL is *untrusted*, so the
handler defends the database at the connection boundary rather than by parsing the
SQL itself:

  * the connection is opened in SQLite URI read-only mode (``mode=ro``);
  * a ``set_authorizer`` callback rejects every action outside plain SELECT reads;
  * only one statement runs, through ``connection.execute`` (never ``executescript``);
  * a progress handler interrupts runaway queries;
  * rows, columns, and total result size are bounded before anything is returned.

The database path is harness configuration — never a tool argument. A missing
database is a setup error, not an empty database.

The handler always returns a stable, JSON-compatible envelope:

    success:  {"ok": True, "columns": [...], "rows": [[...]],
               "row_count": <int>, "truncated": <bool>}
    failure:  {"ok": False, "error": {"type": <category>, "message": <str>}}

No traceback, file path, connection URI, or SQLite internal ever reaches the
caller.
"""

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

from tools.registry import ToolSpec


SQL_QUERY_SPEC = ToolSpec(
    name="sql_query",
    description=(
        "Run one read-only SQLite SELECT query against the local Chinook "
        "music-store database. Use the supplied schema. Return database rows for "
        "factual and analytical questions. Never use it for writes or schema "
        "changes."
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
            "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "array"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "error": {"type": "object"},
        },
        "required": ["ok"],
    },
)


# --- Deterministic resource limits (see SPEC-008 §Resource limits) -------------

MAX_QUERY_LENGTH = 10_000
MAX_RESULT_ROWS = 100
MAX_RESULT_COLUMNS = 50
MAX_RESULT_BYTES = 100_000
PROGRESS_HANDLER_INTERVAL = 1_000  # SQLite virtual-machine instructions per tick
MAX_PROGRESS_CALLBACKS = 10_000


# --- Internal control-flow exceptions (mapped to error categories) ------------


class _SqlToolError(Exception):
    """Base for errors that map to a stable envelope category."""

    category = "internal_error"


class _InvalidArguments(_SqlToolError):
    category = "invalid_arguments"


class _DatabaseNotInitialized(_SqlToolError):
    category = "database_not_initialized"


class _InvalidQuery(_SqlToolError):
    category = "invalid_query"


class _ReadOnlyViolation(_SqlToolError):
    category = "read_only_violation"


class _ResourceLimit(_SqlToolError):
    category = "resource_limit"


class _UnsupportedResult(_SqlToolError):
    category = "unsupported_result"


class _DatabaseError(_SqlToolError):
    category = "database_error"


# A raised marker the authorizer/progress handler cannot use directly (SQLite
# swallows exceptions from those callbacks), so we signal through connection
# state and translate the resulting sqlite3 error afterwards.
_READ_ONLY_MESSAGE = "Only read-only SELECT queries are allowed."


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


def create_sql_query_handler(database_path: Path) -> ToolHandler:
    """Build a ``sql_query`` handler bound to one fixed database path.

    The path is captured here, never taken from tool arguments, so the model can
    never redirect the connection. Returning a closure keeps the module import-time
    free of any filesystem or configuration dependency and lets tests inject a
    temporary database.
    """

    database_path = Path(database_path).resolve()

    def sql_query(arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute one bounded read-only query against the local Chinook database.

        Returns the stable success/failure envelope described in the module
        docstring.
        """

        try:
            query = _validate_arguments(arguments)
            return _run_query(database_path, query)
        except _SqlToolError as error:
            return _error(error.category, str(error))
        except Exception:
            # Never leak an unexpected traceback to the model or the CLI.
            return _error("internal_error", "The query could not be completed.")

    return sql_query


# --- Argument validation ------------------------------------------------------


def _validate_arguments(arguments: dict[str, Any]) -> str:
    """Return the cleaned query string, or raise ``_InvalidArguments``."""

    if not isinstance(arguments, dict):
        raise _InvalidArguments("Arguments must be an object.")
    if set(arguments) != {"query"}:
        raise _InvalidArguments("Arguments must contain exactly 'query'.")

    query = arguments["query"]
    if not isinstance(query, str):
        raise _InvalidArguments("'query' must be a string.")
    if "\x00" in query:
        raise _InvalidArguments("'query' must not contain NUL bytes.")

    query = query.strip()
    if not query:
        raise _InvalidArguments("'query' must not be empty.")
    if len(query) > MAX_QUERY_LENGTH:
        raise _ResourceLimit(f"The query exceeds {MAX_QUERY_LENGTH} characters.")

    return query


# --- Read-only connection + authorizer ----------------------------------------

# The SQLite action codes we allow. Everything a plain SELECT over the base
# tables needs — and nothing that can mutate data, change schema, reach another
# file, run a pragma, or control a transaction.
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    if not database_path.exists():
        raise _DatabaseNotInitialized("The local Chinook database is not initialized.")

    # as_uri() percent-encodes the (resolved, absolute) path safely for a URI.
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise _DatabaseNotInitialized("The local Chinook database is not initialized.")


def _make_authorizer(state: dict[str, bool]):
    """Return an authorizer callback that denies anything outside a read-only SELECT.

    ``state["denied"]`` records that a forbidden action was seen, so the caller can
    report a stable ``read_only_violation`` even though SQLite only surfaces a
    generic authorization error.
    """

    def authorizer(action: int, *_ignored: Any) -> int:
        if action in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK
        state["denied"] = True
        return sqlite3.SQLITE_DENY

    return authorizer


def _make_progress_handler(state: dict[str, int]):
    def progress_handler() -> int:
        state["calls"] += 1
        if state["calls"] > MAX_PROGRESS_CALLBACKS:
            state["interrupted"] = True
            return 1  # non-zero aborts the running statement
        return 0

    return progress_handler


def _run_query(database_path: Path, query: str) -> dict[str, Any]:
    connection = _open_read_only(database_path)
    authorizer_state = {"denied": False}
    progress_state = {"calls": 0, "interrupted": False}
    try:
        connection.set_authorizer(_make_authorizer(authorizer_state))
        connection.set_progress_handler(
            _make_progress_handler(progress_state), PROGRESS_HANDLER_INTERVAL
        )

        try:
            cursor = connection.execute(query)
        except sqlite3.Warning:
            # Raised for "You can only execute one statement at a time."
            raise _InvalidQuery("Only one SQL statement may be executed.")
        except sqlite3.OperationalError as error:
            _reraise_operational(error, authorizer_state, progress_state)
        except sqlite3.DatabaseError:
            raise _InvalidQuery("The SQL query is invalid.")

        if cursor.description is None:
            # A statement with no result set is not a valid read-only query.
            raise _InvalidQuery("The query did not produce a result set.")

        columns = [str(column[0]) for column in cursor.description]
        if len(columns) > MAX_RESULT_COLUMNS:
            raise _ResourceLimit(
                f"The query returns more than {MAX_RESULT_COLUMNS} columns."
            )

        try:
            fetched = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        except sqlite3.OperationalError as error:
            _reraise_operational(error, authorizer_state, progress_state)

        truncated = len(fetched) > MAX_RESULT_ROWS
        fetched = fetched[:MAX_RESULT_ROWS]
        rows = [_serialize_row(row) for row in fetched]

        result = {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
        return _enforce_result_size(result)
    finally:
        connection.close()


def _reraise_operational(
    error: sqlite3.OperationalError,
    authorizer_state: dict[str, bool],
    progress_state: dict[str, int],
) -> "None":
    """Classify a sqlite3 OperationalError into a stable tool error and raise it."""

    if progress_state.get("interrupted"):
        raise _ResourceLimit("The query exceeded the execution limit.")
    if authorizer_state.get("denied"):
        raise _ReadOnlyViolation(_READ_ONLY_MESSAGE)

    message = str(error).lower()
    if "not authorized" in message:
        raise _ReadOnlyViolation(_READ_ONLY_MESSAGE)
    if "no such table" in message or "no such column" in message:
        raise _InvalidQuery("The query references an unknown table or column.")
    if "interrupted" in message:
        raise _ResourceLimit("The query exceeded the execution limit.")
    raise _InvalidQuery("The SQL query is invalid.")


# --- Result serialization -----------------------------------------------------


def _serialize_row(row: tuple[Any, ...]) -> list[Any]:
    return [_serialize_value(value) for value in row]


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        # SQLite has no bool type; guard anyway so it never sneaks through as int.
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _UnsupportedResult("The result contains a non-finite number.")
        return value
    raise _UnsupportedResult("The result contains an unsupported value type.")


def _enforce_result_size(result: dict[str, Any]) -> dict[str, Any]:
    """Trim whole rows until the serialized result fits in ``MAX_RESULT_BYTES``."""

    if _encoded_size(result) <= MAX_RESULT_BYTES:
        return result

    rows = result["rows"]
    while rows and _encoded_size(result) > MAX_RESULT_BYTES:
        rows.pop()
        result["row_count"] = len(rows)
        result["truncated"] = True

    if _encoded_size(result) > MAX_RESULT_BYTES:
        # Even a single row (or the header) is too large to represent safely.
        raise _ResourceLimit("The query result exceeds the size limit.")
    return result


def _encoded_size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def _error(category: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": category, "message": message}}
