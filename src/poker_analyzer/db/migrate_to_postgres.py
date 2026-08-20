"""
One-time migration: copy the real local SQLite database (poker_hands.db)
into the hosted Supabase Postgres database (SUPABASE_DB_URL), preserving
every row's primary key, hand_number, session linkage, and all column
values exactly.

This moves real, already-logged hand history - it does not regenerate or
reshape any data. Safe to re-run: each row is inserted with ON CONFLICT
(<primary key>) DO NOTHING, so running it again after a partial failure
just fills in whatever wasn't copied yet.

Usage:
    python3 src/poker_analyzer/db/migrate_to_postgres.py [--sqlite-db poker_hands.db]
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from poker_analyzer.db.connection import get_connection
from poker_analyzer.db.init_db import init_db

# Sessions -> hands -> actions, in FK dependency order (each table's primary
# key is always its first column per schema.sql, e.g. session_id/hand_id).
TABLES = ["sessions", "hands", "actions"]


def _table_columns(sqlite_conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in sqlite_conn.execute(f"PRAGMA table_info({table})")]


def _copy_table(sqlite_conn: sqlite3.Connection, pg_conn, table: str) -> int:
    columns = _table_columns(sqlite_conn, table)
    rows = sqlite_conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    if not rows:
        return 0

    pk_column = columns[0]
    placeholders = ", ".join(["%s"] * len(columns))
    column_list = ", ".join(columns)
    insert_sql = (
        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({pk_column}) DO NOTHING"
    )
    for row in rows:
        pg_conn.execute(insert_sql, tuple(row))

    # Move the SERIAL sequence past the explicit ids just inserted, so the
    # next hand ingested through the CLI doesn't collide with migrated ids.
    pg_conn.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{pk_column}'), "
        f"(SELECT COALESCE(MAX({pk_column}), 1) FROM {table}))"
    )
    return len(rows)


def migrate(sqlite_db_path: str | Path) -> dict[str, int]:
    """Copy every row from the local SQLite database into Postgres. Returns
    {table: rows_inserted}."""
    init_db()

    sqlite_conn = sqlite3.connect(sqlite_db_path)
    pg_conn = get_connection()
    try:
        copied = {table: _copy_table(sqlite_conn, pg_conn, table) for table in TABLES}
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()

    return copied


def verify_counts(sqlite_db_path: str | Path) -> dict[str, tuple[int, int]]:
    """Returns {table: (sqlite_count, postgres_count)} for sessions/hands/actions."""
    sqlite_conn = sqlite3.connect(sqlite_db_path)
    pg_conn = get_connection()
    try:
        result = {}
        for table in TABLES:
            sqlite_count = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            pg_count = pg_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            result[table] = (sqlite_count, pg_count)
        return result
    finally:
        sqlite_conn.close()
        pg_conn.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-db",
        default="poker_hands.db",
        help="Path to the local SQLite database to migrate (default: poker_hands.db)",
    )
    args = parser.parse_args()

    print(f"Migrating {args.sqlite_db} -> Postgres (SUPABASE_DB_URL, schema 'poker_analyzer')...")
    copied = migrate(args.sqlite_db)
    for table, count in copied.items():
        print(f"  {table}: inserted {count} row(s)")

    print("\nVerifying row counts (SQLite vs Postgres):")
    counts = verify_counts(args.sqlite_db)
    all_match = True
    for table, (sqlite_count, pg_count) in counts.items():
        status = "OK" if sqlite_count == pg_count else "MISMATCH"
        all_match = all_match and sqlite_count == pg_count
        print(f"  {table:10s} sqlite={sqlite_count:<6d} postgres={pg_count:<6d} {status}")

    if not all_match:
        raise SystemExit(1)
    print("\nRow counts match exactly.")


if __name__ == "__main__":
    _main()
