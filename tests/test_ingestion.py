from pathlib import Path

import pytest

from poker_analyzer.db.init_db import init_db
from poker_analyzer.ingestion.loader import IngestionError, load_hand_log_csv


EXPECTED_COLUMNS = [
    "session_date",
    "location",
    "stakes",
    "hand_number",
    "num_players",
    "hero_position",
    "hero_hole_cards",
    "effective_stack_bb",
    "preflop_actions",
    "flop_cards",
    "flop_actions",
    "turn_card",
    "turn_actions",
    "river_card",
    "river_actions",
    "pot_size_bb",
    "result_bb",
    "went_to_showdown",
    "notes",
]


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "hands.csv"
    path.write_text(",".join(EXPECTED_COLUMNS) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_loads_template_csv_into_fresh_db(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)

    summary = load_hand_log_csv(Path("data/templates/hand_log_template.csv"), db_path)

    assert summary == {"hands_loaded": 2, "hands_skipped": 0, "sessions_created": 1}


def test_reruns_are_idempotent(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(Path("data/templates/hand_log_template.csv"), db_path)

    summary = load_hand_log_csv(Path("data/templates/hand_log_template.csv"), db_path)

    assert summary == {"hands_loaded": 0, "hands_skipped": 2, "sessions_created": 0}


def test_same_session_triple_reuses_session_row(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,CO,Ah Kd,100,UTG:fold>CO:raise2.5>BB:call,,,,,,,10,5,0,",
            "2026-08-05,The Brook,1/3,2,6,BB,7c 2d,97,UTG:raise3>BB:fold,,,,,,,4.5,-1,0,",
        ],
    )

    summary = load_hand_log_csv(csv_path, db_path)

    assert summary["sessions_created"] == 1
    assert summary["hands_loaded"] == 2


def test_rejects_invalid_csv_without_writing_anything(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    csv_path = _write_csv(
        tmp_path,
        ["2026-08-05,The Brook,1/3,1,6,CO,Ah 10d,100,UTG:fold,,,,,,,10,5,0,"],
    )

    with pytest.raises(IngestionError):
        load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM hands").fetchone()[0] == 0


def test_action_strings_parsed_into_actions_table(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,CO,Ah Kd,100,"
            "UTG:fold>CO:raise2.5>BB:call,Jh 8h 3c,BB:check>CO:bet4>BB:call,,,,,17,8,0,",
        ],
    )

    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    actions = conn.execute(
        "SELECT street, actor_position, action_type, amount_bb FROM actions ORDER BY street, action_order"
    ).fetchall()
    assert ("preflop", "UTG", "fold", None) in actions
    assert ("preflop", "CO", "raise", 2.5) in actions
    assert ("flop", "CO", "bet", 4.0) in actions


def test_allin_tokens_classified_by_amount_vs_current_bet(tmp_path):
    """
    'allin' isn't a stored action_type - it's classified into bet/raise/call by
    comparing its amount to the street's running current_bet: opening from no bet
    is a 'bet', exceeding an existing bet is a 'raise', and shoving for at or below
    the current bet (a covered call-for-less) is a 'call'.
    """
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,CO,Ah Kd,100,"
            "UTG:raise4>CO:allin50,Jh 8h 3c,BB:check>CO:allin20>BTN:raise60>CO:allin20,,,,,17,8,0,",
        ],
    )

    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    actions = conn.execute(
        "SELECT street, actor_position, action_type, amount_bb FROM actions ORDER BY street, action_order"
    ).fetchall()
    # Preflop: CO's allin (50) exceeds UTG's raise (4) -> raise.
    assert ("preflop", "CO", "raise", 50.0) in actions
    # Flop: CO's first allin (20) opens from a check -> bet.
    assert ("flop", "CO", "bet", 20.0) in actions
    # Flop: CO's second allin (20) is covered under BTN's raise (60) -> call.
    assert ("flop", "CO", "call", 20.0) in actions
