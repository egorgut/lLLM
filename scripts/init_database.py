"""Reproducibly build the local Chinook SQLite database from the trusted seed.

Usage:

    python scripts/init_database.py           # create data/chinook.sqlite
    python scripts/init_database.py --force    # replace an existing database

The seed script (data/seed/Chinook_Sqlite.sql) is a developer-controlled,
version-controlled input, so it is executed with ``executescript`` — the only
place in this project where that is allowed. The runtime database is built into a
temporary sibling file, validated, and only then atomically moved into place, so a
failed run never leaves a partial database that looks valid.

This module never calls Ollama, touches chat history, or accesses the network.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/init_database.py) by making the
# project root importable for `config`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CHINOOK_SEED_PATH, PROJECT_ROOT, SQLITE_DATABASE_PATH  # noqa: E402


# Tables that must exist for the database to be considered a valid Chinook build.
_EXPECTED_TABLES = (
    "Artist",
    "Album",
    "Track",
    "Genre",
    "MediaType",
    "Playlist",
    "PlaylistTrack",
    "Employee",
    "Customer",
    "Invoice",
    "InvoiceLine",
)

# A subset that must contain at least one row (no exact counts are pinned).
_NON_EMPTY_TABLES = ("Artist", "Track", "Invoice")


class InitError(Exception):
    """A clear, user-facing initialization failure."""


def _relative(path: Path) -> str:
    """Render a path relative to the project root for tidy CLI output."""

    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def initialize_database(
    seed_path: Path, database_path: Path, *, force: bool
) -> Path:
    """Build ``database_path`` from ``seed_path``. Returns the created path.

    Raises ``InitError`` for a missing seed, an existing target without ``force``,
    or a seed/validation failure. On any failure no partial target remains.
    """

    seed_path = Path(seed_path)
    database_path = Path(database_path)

    if not seed_path.is_file():
        raise InitError(f"Seed script not found: {_relative(seed_path)}")

    if database_path.exists() and not force:
        raise InitError(
            f"Database already exists: {_relative(database_path)}\n"
            "Use --force to recreate it."
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)

    seed_sql = seed_path.read_text(encoding="utf-8")

    # Build into a temporary sibling, then atomically replace the target.
    temp_path = database_path.with_name(f".{database_path.name}.{os.getpid()}.tmp")
    if temp_path.exists():
        temp_path.unlink()

    try:
        connection = sqlite3.connect(temp_path)
        try:
            connection.executescript(seed_sql)
            connection.commit()
            _validate(connection)
        finally:
            connection.close()
        os.replace(temp_path, database_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    return database_path


def _validate(connection: sqlite3.Connection) -> None:
    existing = {
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = [table for table in _EXPECTED_TABLES if table not in existing]
    if missing:
        raise InitError(f"Seed did not create expected tables: {', '.join(missing)}")

    for table in _NON_EMPTY_TABLES:
        (count,) = connection.execute(
            f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed identifier list
        ).fetchone()
        if count < 1:
            raise InitError(f"Table {table} is unexpectedly empty after seeding.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the local Chinook SQLite database from the seed script."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing database instead of failing.",
    )
    args = parser.parse_args(argv)

    try:
        created = initialize_database(
            CHINOOK_SEED_PATH, SQLITE_DATABASE_PATH, force=args.force
        )
    except InitError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"Created Chinook database: {_relative(created)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
