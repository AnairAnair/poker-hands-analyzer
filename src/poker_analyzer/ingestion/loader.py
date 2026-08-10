"""
Ingestion pipeline: hand-log CSV -> validated -> loaded into SQLite.

Chains: parse -> validate -> load. Each row is one hand. A session is a
(session_date, location, stakes) triple; the first hand for a new session
creates the session row, later hands for the same triple reuse it.

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
from typing import Iterable

from poker_analyzer.validation.validator import validate_hand_log_csv

ACTION_TOKEN_RE = re.compile(r"^(?P<position>[A-Za-z0-9+]+):(?P<type>fold|check|call|bet|raise)(?P<amount>\d+(?:\.\d+)?)?$")

STREET_COLUMNS = {
    "preflop": "preflop_actions",
    "flop": "flop_actions",
    "turn": "turn_actions",
    "river": "river_actions",
}


class IngestionError(Exception):
    pass


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
    if not action_str:
        return []
    actions = []
    for order, token in enumerate(action_str.split(">"), start=1):
        match = ACTION_TOKEN_RE.match(token)
        if not match:
            raise IngestionError(f"Malformed action token '{token}' in {street} actions")
        amount = match.group("amount")
        actions.append(
            {
                "street": street,
                "action_order": order,
                "actor_position": match.group("position"),
                "action_type": match.group("type"),
                "amount_bb": float(amount) if amount else None,
                "pot_before_bb": None,  # see module docstring
            }
        )
    return actions


def _get_or_create_session(
    conn: sqlite3.Connection,
    session_date: str,
    location: str,
    stakes: str,
    buy_in_cents_default: int,
) -> int:
    cur = conn.execute(
        "SELECT session_id FROM sessions WHERE session_date = ? AND location = ? AND stakes = ?",
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
    cur = conn.execute(
        """
        INSERT INTO sessions (session_date, location, stakes, big_blind_cents, buy_in_cents)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_date, location, stakes, big_blind_cents, buy_in_cents_default),
    )
    return cur.lastrowid


def _insert_hand(conn: sqlite3.Connection, session_id: int, row: dict) -> int | None:
    """Returns the new hand_id, or None if this hand was already loaded (idempotent)."""
    existing = conn.execute(
        "SELECT hand_id FROM hands WHERE session_id = ? AND hand_number = ?",
        (session_id, int(row["hand_number"])),
    ).fetchone()
    if existing:
        return None

    cur = conn.execute(
        """
        INSERT INTO hands (
            session_id, hand_number, hero_position, hero_hole_cards, num_players,
            effective_stack_bb, board_flop, board_turn, board_river,
            pot_size_bb, result_bb, went_to_showdown, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
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
        ),
    )
    return cur.lastrowid


def _insert_actions(conn: sqlite3.Connection, hand_id: int, row: dict) -> None:
    for street, column in STREET_COLUMNS.items():
        for action in _parse_action_string(row[column], street):
            conn.execute(
                """
                INSERT INTO actions (
                    hand_id, street, action_order, actor_position,
                    action_type, amount_bb, pot_before_bb
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
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
    db_path: str | Path,
    buy_in_cents_default: int = 0,
) -> dict:
    """
    Parse -> validate -> load a hand-log CSV into the SQLite database.

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

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

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
    parser = argparse.ArgumentParser(description="Ingest a validated hand-log CSV into the SQLite database")
    parser.add_argument("csv_path", type=Path, help="Path to the hand log CSV to ingest")
    parser.add_argument("--db", default="poker_hands.db", help="Path to the SQLite database (default: poker_hands.db)")
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
