from pathlib import Path

import pytest

from poker_analyzer.validation.validator import validate_hand_log_csv


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


def test_template_csv_passes_validation():
    template_path = Path("data/templates/hand_log_template.csv")

    errors = validate_hand_log_csv(template_path)

    assert errors == []


def test_rejects_bad_card_format(tmp_path):
    path = tmp_path / "bad_cards.csv"
    path.write_text(
        ",".join(EXPECTED_COLUMNS) + "\n"
        + "2026-08-05,The Brook,1/3,1,6,CO,Ah 10d,100,UTG:fold,\n",
        encoding="utf-8",
    )

    errors = validate_hand_log_csv(path)

    assert any("hero_hole_cards" in error and "card" in error.lower() for error in errors)


def test_rejects_missing_column(tmp_path):
    path = tmp_path / "missing_column.csv"
    columns = EXPECTED_COLUMNS.copy()
    columns.remove("hero_position")
    path.write_text(
        ",".join(columns) + "\n"
        + "2026-08-05,The Brook,1/3,1,6,Ah Kd,100,UTG:fold,\n",
        encoding="utf-8",
    )

    errors = validate_hand_log_csv(path)

    assert any("missing required column" in error.lower() for error in errors)


def test_rejects_malformed_action_string(tmp_path):
    path = tmp_path / "bad_action.csv"
    path.write_text(
        ",".join(EXPECTED_COLUMNS) + "\n"
        + "2026-08-05,The Brook,1/3,1,6,CO,Ah Kd,100,UTG:fold>MP:raiseX,\n",
        encoding="utf-8",
    )

    errors = validate_hand_log_csv(path)

    assert any("preflop_actions" in error and "action" in error.lower() for error in errors)
