"""
Create the sessions/hands/actions tables - in Postgres by default, or in a
local SQLite file when given an explicit path.

    python3 src/poker_analyzer/db/init_db.py                 # Postgres (SUPABASE_DB_URL)
    python3 src/poker_analyzer/db/init_db.py --db poker.db   # local SQLite file

The SQLite mode only exists for the dual-mode test fixtures (stats/ev/leaks/
dashboard's tests), which build their own throwaway local SQLite database
rather than touching Postgres - see connection.py's module docstring.
Production code (this project's CLI) always uses the Postgres mode.

Safe to re-run either mode: every statement in both schema files uses
CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

from poker_analyzer.db.connection import get_connection

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SQLITE_SCHEMA_PATH = Path(__file__).parent / "schema_sqlite.sql"


def _init_postgres() -> None:
    schema_sql = SCHEMA_PATH.read_text()
    # Strip "--" comments (full-line and trailing) first, so a semicolon
    # inside a comment can't be mistaken for a statement boundary below.
    # schema.sql has no "--" inside string literals, so a plain per-line
    # regex is safe here.
    sql_only = "\n".join(re.sub(r"--.*", "", line) for line in schema_sql.splitlines())

    conn = get_connection()
    try:
        # psycopg's extended query protocol only allows one statement per
        # execute() call (unlike sqlite3's executescript), so each ;-terminated
        # statement is sent separately.
        for statement in sql_only.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _init_sqlite(db_path: str | Path) -> None:
    schema_sql = SQLITE_SCHEMA_PATH.read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()


def init_db(sqlite_db_path: str | Path | None = None) -> None:
    if sqlite_db_path is not None:
        _init_sqlite(sqlite_db_path)
    else:
        _init_postgres()


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="Path to a local SQLite database file to create/update. Omit to "
        "initialize the Postgres database (SUPABASE_DB_URL) instead.",
    )
    args = parser.parse_args()

    init_db(args.db)
    if args.db:
        print(f"Initialized schema in {args.db}")
    else:
        print("Initialized schema in Postgres (SUPABASE_DB_URL)")


if __name__ == "__main__":
    _main()
