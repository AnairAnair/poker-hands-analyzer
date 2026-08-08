"""
Create a SQLite database from schema.sql.

Structure only - this does not load any data. Run it to (re)create an empty
database with the sessions/hands/actions tables:

    python3 src/poker_analyzer/db/init_db.py --db poker_hands.db

Safe to re-run: every statement in schema.sql uses CREATE TABLE IF NOT EXISTS.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: str | Path) -> None:
    schema_sql = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="poker_hands.db",
        help="Path to the SQLite database file to create/update (default: poker_hands.db)",
    )
    args = parser.parse_args()

    init_db(args.db)
    print(f"Initialized schema in {args.db}")


if __name__ == "__main__":
    _main()
