"""
Ingestion pipeline: hand-log CSV -> validated -> loaded into the database.

Chains: parse -> validate -> load. Each row is one hand. A session is a
(session_date, location, stakes) triple; the first hand for a new session
creates the session row, later hands for the same triple reuse it.

Writes to Postgres (SUPABASE_DB_URL, via db/connection.py) by default. Pass
an explicit sqlite_db_path to write to a local SQLite file instead - that mode
only exists for the dual-mode test fixtures (stats/ev/leaks/dashboard's tests),
which build their own throwaway local SQLite database rather than touching
Postgres - see db/connection.py's module docstring. Production code (this
project's CLI) always uses the Postgres mode.

Two things the CSV format does not capture, by design of the current
template (see data/templates/hand_log_template_GUIDE.md), and how this
loader handles them:

- `sessions.buy_in_cents` is NOT NULL in the schema, but buy-in is a
  session-level fact the hand-log CSV never records (it's a per-hand log,
  not a session log). This loader takes an optional --buy-in-cents flag
  applied to any *new* session created during a run, defaulting to 0 with
  a printed warning. Real buy-in should be corrected later, session by
  session (e.g. a small `update sessions set buy_in_cents = ... where
  session_id = ...` once that data is tracked somewhere).
- `actions.pot_before_bb` is nullable, so it's left NULL. Actions are
  logged as fold/check/call/bet/raise with a bet/raise size, but NOT a
  call size or a running pot, so pot-before-action can't be reconstructed
  reliably from this format without guessing at call amounts. Computing
  it properly is really an EV-engine-phase job (full hand replay), not
  ingestion's job.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Union

import psycopg

from poker_analyzer.db.connection import get_connection
from poker_analyzer.validation.validator import validate_hand_log_csv

Connection = Union[sqlite3.Connection, psycopg.Connection]

ACTION_TOKEN_RE = re.compile(
    r"^(?P<position>[A-Za-z0-9+]+):(?P<type>fold|check|call|bet|raise|allin)(?P<amount>\d+(?:\.\d+)?)?$"
)

STREET_COLUMNS = {
    "preflop": "preflop_actions",
    "flop": "flop_actions",
    "turn": "turn_actions",
    "river": "river_actions",
}

# Preflop's first bet-facing amount is the big blind itself (no "bet" action is ever
# logged for it); postflop streets start with no bet in front of anyone. Matches
# ev/engine.py's own SB_POST_BB/BB_POST_BB-seeded reconstruction.
_PREFLOP_STARTING_BET_BB = 1.0


class IngestionError(Exception):
    pass


def _connect(sqlite_db_path: str | Path | None) -> Connection:
    if sqlite_db_path is not None:
        conn = sqlite3.connect(sqlite_db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    return get_connection()


def _placeholder(conn: Connection) -> str:
    return "?" if isinstance(conn, sqlite3.Connection) else "%s"


def _big_blind_cents_from_stakes(stakes: str) -> int:
    """'1/3' -> 300 cents. Takes the big blind (second number) in dollars."""
    parts = stakes.split("/")
    if len(parts) != 2:
        raise IngestionError(f"Can't parse big blind from stakes '{stakes}'")
    try:
        big_blind_dollars = float(parts[1])
    except ValueError as exc:
        raise IngestionError(f"Can't parse big blind from stakes '{stakes}'") from exc
    return round(big_blind_dollars * 100)


def _parse_action_string(action_str: str, street: str) -> list[dict]:
    """
    'allin' isn't stored as its own action_type - it's a stack-size modifier on
    whichever of bet/raise/call it actually is, and the schema/EV engine only know
    those five canonical action types (see db/schema.sql's CHECK constraint and
    ev/engine.py's action_type dispatch). So an 'allin<amount>' token is classified
    here, at parse time, by comparing its (always-total, same convention as bet/raise
    tokens) amount against the street's running current_bet: exceeding it is
    aggression (a bet if nothing's been bet yet this street, else a raise - same
    "bet vs. raise" distinction the postflop range-narrowing in ev/engine.py relies
    on); not exceeding it is a call, including a short-stacked call-for-less that
    creates a side pot (this loader's cost/pot reconstruction doesn't model side
    pots - see the module docstring - so a covered call-for-less is still recorded
    as a plain 'call', same simplification a literal 'call' token already gets).
    """
    if not action_str:
        return []
    actions = []
    current_bet = _PREFLOP_STARTING_BET_BB if street == "preflop" else 0.0
    for order, token in enumerate(action_str.split(">"), start=1):
        match = ACTION_TOKEN_RE.match(token)
        if not match:
            raise IngestionError(f"Malformed action token '{token}' in {street} actions")
        raw_type = match.group("type")
        amount = match.group("amount")
        amount_bb = float(amount) if amount else None

        if raw_type == "allin":
            if amount_bb is None:
                raise IngestionError(f"'allin' action token '{token}' in {street} actions is missing an amount")
            if amount_bb > current_bet:
                action_type = "bet" if current_bet == 0.0 else "raise"
            else:
                action_type = "call"
        else:
            action_type = raw_type

        if action_type in ("bet", "raise"):
            current_bet = amount_bb

        actions.append(
            {
                "street": street,
                "action_order": order,
                "actor_position": match.group("position"),
                "action_type": action_type,
                "amount_bb": amount_bb,
                "pot_before_bb": None,  # see module docstring
            }
        )
    return actions


def _get_or_create_session(
    conn: Connection,
    session_date: str,
    location: str,
    stakes: str,
    buy_in_cents_default: int,
) -> int:
    p = _placeholder(conn)
    cur = conn.execute(
        f"SELECT session_id FROM sessions WHERE session_date = {p} AND location = {p} AND stakes = {p}",
        (session_date, location, stakes),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    big_blind_cents = _big_blind_cents_from_stakes(stakes)
    if buy_in_cents_default == 0:
        print(
            f"  [warning] No --buy-in-cents given, creating session "
            f"{session_date} {location} {stakes} with buy_in_cents=0. "
            f"Update this later once real buy-in is tracked."
        )
    insert_sql = (
        f"INSERT INTO sessions (session_date, location, stakes, big_blind_cents, buy_in_cents) "
        f"VALUES ({p}, {p}, {p}, {p}, {p})"
    )
    params = (session_date, location, stakes, big_blind_cents, buy_in_cents_default)
    if isinstance(conn, sqlite3.Connection):
        cur = conn.execute(insert_sql, params)
        return cur.lastrowid
    cur = conn.execute(insert_sql + " RETURNING session_id", params)
    return cur.fetchone()[0]


def _insert_hand(conn: Connection, session_id: int, row: dict) -> int | None:
    """Returns the new hand_id, or None if this hand was already loaded (idempotent)."""
    p = _placeholder(conn)
    existing = conn.execute(
        f"SELECT hand_id FROM hands WHERE session_id = {p} AND hand_number = {p}",
        (session_id, int(row["hand_number"])),
    ).fetchone()
    if existing:
        return None

    insert_sql = (
        "INSERT INTO hands ("
        "session_id, hand_number, hero_position, hero_hole_cards, num_players, "
        "effective_stack_bb, board_flop, board_turn, board_river, "
        "pot_size_bb, result_bb, went_to_showdown, notes"
        f") VALUES ({', '.join([p] * 13)})"
    )
    params = (
        session_id,
        int(row["hand_number"]),
        row["hero_position"],
        row["hero_hole_cards"],
        int(row["num_players"]),
        float(row["effective_stack_bb"]) if row["effective_stack_bb"] else None,
        row["flop_cards"] or None,
        row["turn_card"] or None,
        row["river_card"] or None,
        float(row["pot_size_bb"]) if row["pot_size_bb"] else None,
        float(row["result_bb"]) if row["result_bb"] else None,
        int(row["went_to_showdown"]),
        row["notes"] or None,
    )
    if isinstance(conn, sqlite3.Connection):
        cur = conn.execute(insert_sql, params)
        return cur.lastrowid
    cur = conn.execute(insert_sql + " RETURNING hand_id", params)
    return cur.fetchone()[0]


def _insert_actions(conn: Connection, hand_id: int, row: dict) -> None:
    p = _placeholder(conn)
    insert_sql = (
        "INSERT INTO actions ("
        "hand_id, street, action_order, actor_position, "
        "action_type, amount_bb, pot_before_bb"
        f") VALUES ({', '.join([p] * 7)})"
    )
    for street, column in STREET_COLUMNS.items():
        for action in _parse_action_string(row[column], street):
            conn.execute(
                insert_sql,
                (
                    hand_id,
                    action["street"],
                    action["action_order"],
                    action["actor_position"],
                    action["action_type"],
                    action["amount_bb"],
                    action["pot_before_bb"],
                ),
            )


def load_hand_log_csv(
    csv_path: str | Path,
    sqlite_db_path: str | Path | None = None,
    buy_in_cents_default: int = 0,
) -> dict:
    """
    Parse -> validate -> load a hand-log CSV into the database.

    Writes to Postgres (SUPABASE_DB_URL) unless sqlite_db_path is given, in
    which case it writes to that local SQLite file instead (see module
    docstring for why that mode exists).

    Returns a summary dict: {"hands_loaded": int, "hands_skipped": int,
    "sessions_created": int}. Skipped hands are ones already present
    (same session + hand_number), so re-running on the same file is safe.
    """
    csv_path = Path(csv_path)

    errors = validate_hand_log_csv(csv_path)
    if errors:
        raise IngestionError(
            "CSV failed validation, fix these before ingesting:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    conn = _connect(sqlite_db_path)

    hands_loaded = 0
    hands_skipped = 0
    sessions_before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                session_id = _get_or_create_session(
                    conn,
                    row["session_date"],
                    row["location"],
                    row["stakes"],
                    buy_in_cents_default,
                )
                hand_id = _insert_hand(conn, session_id, row)
                if hand_id is None:
                    hands_skipped += 1
                    continue
                _insert_actions(conn, hand_id, row)
                hands_loaded += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        sessions_after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()

    return {
        "hands_loaded": hands_loaded,
        "hands_skipped": hands_skipped,
        "sessions_created": sessions_after - sessions_before,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a validated hand-log CSV into the database")
    parser.add_argument("csv_path", type=Path, help="Path to the hand log CSV to ingest")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to a local SQLite database file. Omit to ingest into Postgres "
        "(SUPABASE_DB_URL) instead.",
    )
    parser.add_argument(
        "--buy-in-cents",
        type=int,
        default=0,
        help="Buy-in in cents to use for any NEW session created during this run",
    )
    args = parser.parse_args()

    try:
        summary = load_hand_log_csv(args.csv_path, args.db, args.buy_in_cents)
    except IngestionError as exc:
        print(f"Ingestion failed:\n{exc}")
        raise SystemExit(1)

    print(
        f"Loaded {summary['hands_loaded']} hand(s), "
        f"skipped {summary['hands_skipped']} already-loaded hand(s), "
        f"created {summary['sessions_created']} new session(s)."
    )


if __name__ == "__main__":
    main()
