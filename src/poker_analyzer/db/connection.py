"""
Shared Postgres connection layer for the Poker Hand Analyzer.

Every module that talks to the database goes through get_connection() here
instead of opening its own connection - this is the only place SUPABASE_DB_URL
is read and the only place a psycopg connection is actually opened.

Tables live in a dedicated "poker_analyzer" Postgres schema, not "public" -
this Supabase project also hosts Voice Hand Logger's own (unrelated) tables
in "public", and a dedicated schema keeps the two projects from colliding on
table names (see e.g. Voice Hand Logger's own stub "hands" table).

POKER_ANALYZER_PG_SCHEMA overrides the target schema (defaults to
"poker_analyzer"). Only the test suite should ever set this - it points each
test run at a throwaway, uniquely-named schema so tests never read or write
the real migrated hand history.
"""

from __future__ import annotations

import os
import re

import psycopg
from dotenv import load_dotenv

load_dotenv()

DEFAULT_SCHEMA = "poker_analyzer"
_VALID_SCHEMA_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def get_db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError(
            "SUPABASE_DB_URL environment variable is not set. Set it to the "
            "Supabase Postgres connection string (see .env.example)."
        )
    return url


def get_schema() -> str:
    schema = os.environ.get("POKER_ANALYZER_PG_SCHEMA", DEFAULT_SCHEMA)
    if not _VALID_SCHEMA_NAME.match(schema):
        raise RuntimeError(f"Invalid Postgres schema name: '{schema}'")
    return schema


def get_connection() -> psycopg.Connection:
    """Open a connection to the Postgres database, with search_path set to the
    target schema (creating that schema first if it doesn't exist yet)."""
    conn = psycopg.connect(get_db_url())
    schema = get_schema()
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    conn.commit()
    return conn
