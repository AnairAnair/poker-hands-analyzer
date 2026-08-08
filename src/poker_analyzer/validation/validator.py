from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import List

REQUIRED_COLUMNS = [
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

VALID_POSITIONS = {"UTG", "UTG+1", "MP", "HJ", "CO", "BTN", "SB", "BB"}
VALID_RANKS = set("23456789TJQKA")
VALID_SUITS = set("shdc")
ACTION_TOKEN_PATTERN = re.compile(r"^[A-Za-z+]+:(fold|check|call|bet|raise)(?:\d+(?:\.\d+)?)?$")


def _is_valid_date(value: str) -> bool:
    if not value:
        return False
    if len(value) != 10:
        return False
    parts = value.split("-")
    if len(parts) != 3:
        return False
    year, month, day = parts
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return False
    if not (1 <= int(month) <= 12):
        return False
    if not (1 <= int(day) <= 31):
        return False
    return True


def _is_valid_card(card: str) -> bool:
    if not card:
        return False
    if len(card) != 2:
        return False
    rank, suit = card[0], card[1]
    return rank in VALID_RANKS and suit in VALID_SUITS


def _is_valid_card_field(value: str) -> bool:
    if value == "":
        return True
    cards = value.split()
    return len(cards) in {1, 2, 3, 4} and all(_is_valid_card(card) for card in cards)


def _is_valid_action_string(value: str) -> bool:
    if value == "":
        return True
    if value.endswith(">"):
        return False
    tokens = value.split(">")
    for token in tokens:
        if not ACTION_TOKEN_PATTERN.match(token):
            return False
    return True


def validate_hand_log_csv(path: str | Path) -> List[str]:
    path = Path(path)
    errors: List[str] = []

    if not path.exists():
        return [f"File not found: {path}"]

    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        if not rows:
            return ["CSV file is empty."]

        reader = csv.reader(handle)
        handle.seek(0)
        header = next(reader)

        if header != REQUIRED_COLUMNS:
            missing = [column for column in REQUIRED_COLUMNS if column not in header]
            extra = [column for column in header if column not in REQUIRED_COLUMNS]
            if missing:
                errors.append(f"Missing required column(s): {', '.join(missing)}")
            if extra:
                errors.append(f"Unexpected column(s): {', '.join(extra)}")

        for index, row in enumerate(rows, start=2):
            for column in REQUIRED_COLUMNS:
                if column not in row:
                    errors.append(f"Row {index}: missing column '{column}' in parsed row")
                    continue

            if not row.get("session_date", ""):
                errors.append(f"Row {index}, column 'session_date': value is blank")
            elif not _is_valid_date(row["session_date"]):
                errors.append(
                    f"Row {index}, column 'session_date': expected YYYY-MM-DD, got '{row['session_date']}'"
                )

            if row.get("hero_hole_cards", "") and not _is_valid_card_field(row["hero_hole_cards"]):
                errors.append(
                    f"Row {index}, column 'hero_hole_cards': expected 1-2 cards in rank+suit format, got '{row['hero_hole_cards']}'"
                )

            if row.get("flop_cards", "") and not _is_valid_card_field(row["flop_cards"]):
                errors.append(
                    f"Row {index}, column 'flop_cards': expected 3 cards in rank+suit format, got '{row['flop_cards']}'"
                )

            if row.get("turn_card", "") and not _is_valid_card_field(row["turn_card"]):
                errors.append(
                    f"Row {index}, column 'turn_card': expected 1 card in rank+suit format, got '{row['turn_card']}'"
                )

            if row.get("river_card", "") and not _is_valid_card_field(row["river_card"]):
                errors.append(
                    f"Row {index}, column 'river_card': expected 1 card in rank+suit format, got '{row['river_card']}'"
                )

            hero_position = row.get("hero_position", "")
            if hero_position and hero_position not in VALID_POSITIONS:
                errors.append(
                    f"Row {index}, column 'hero_position': invalid position code '{hero_position}'"
                )

            for column_name in ["preflop_actions", "flop_actions", "turn_actions", "river_actions"]:
                value = row.get(column_name, "")
                if value and not _is_valid_action_string(value):
                    errors.append(
                        f"Row {index}, column '{column_name}': malformed action string '{value}'"
                    )

            street_fields = {
                "flop_cards": "flop_actions",
                "turn_card": "turn_actions",
                "river_card": "river_actions",
            }
            for street_card_col, street_action_col in street_fields.items():
                street_card_value = row.get(street_card_col, "")
                street_action_value = row.get(street_action_col, "")
                if not street_card_value and street_action_value:
                    errors.append(
                        f"Row {index}, column '{street_action_col}': action string present but street card field is blank"
                    )
                if street_card_value and not street_action_value and street_card_col != "flop_cards":
                    errors.append(
                        f"Row {index}, column '{street_card_col}': street card present but no actions recorded"
                    )

    return errors


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Validate a hand log CSV against the project template rules")
    parser.add_argument("csv_path", type=Path, help="Path to the hand log CSV to validate")
    args = parser.parse_args()

    errors = validate_hand_log_csv(args.csv_path)
    if errors:
        print(f"Validation failed for {args.csv_path}:")
        for error in errors:
            print(f"- {error}")
    else:
        print(f"Validation passed for {args.csv_path}")
