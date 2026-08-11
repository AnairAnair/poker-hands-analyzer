"""
Tests for the unified CLI (poker_analyzer.cli).

Two kinds of coverage, per the task:
- Dispatch tests: monkeypatch each subcommand's underlying module function and
  assert it's the one actually invoked, with the arguments the CLI parsed off
  the command line. This is what proves the CLI wraps existing logic instead
  of reimplementing it.
- A couple of end-to-end tests running the real pipeline (init_db -> ingest ->
  validate/stats/ev-report) against a temp DB, to check the wiring holds up
  outside of mocks too.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from poker_analyzer import cli
from poker_analyzer.db.init_db import init_db
from poker_analyzer.ev.engine import PreflopDecision
from poker_analyzer.ingestion.loader import IngestionError

runner = CliRunner()

REAL_HANDS_CSV = Path("data/templates/real_hands.csv")
TEMPLATE_CSV = Path("data/templates/hand_log_template.csv")


# --- argument parsing / subcommand dispatch -----------------------------------------


def test_help_lists_all_subcommands():
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for name in ("validate", "ingest", "stats", "ev-report"):
        assert name in result.stdout


def test_no_args_shows_help_instead_of_erroring():
    result = runner.invoke(cli.app, [])

    assert "Usage" in result.stdout


# --- validate ------------------------------------------------------------------------


def test_validate_calls_validator_with_parsed_path(monkeypatch):
    seen = {}

    def fake_validate(path):
        seen["path"] = path
        return []

    monkeypatch.setattr(cli, "validate_hand_log_csv", fake_validate)

    result = runner.invoke(cli.app, ["validate", "some_file.csv"])

    assert result.exit_code == 0
    assert str(seen["path"]) == "some_file.csv"
    assert "Validation passed" in result.stdout


def test_validate_reports_errors_and_exits_nonzero(monkeypatch):
    monkeypatch.setattr(cli, "validate_hand_log_csv", lambda path: ["bad row 2", "bad row 5"])

    result = runner.invoke(cli.app, ["validate", "some_file.csv"])

    assert result.exit_code == 1
    assert "Validation failed" in result.stdout
    assert "bad row 2" in result.stdout
    assert "bad row 5" in result.stdout


# --- ingest --------------------------------------------------------------------------


def test_ingest_calls_loader_with_parsed_args(monkeypatch):
    seen = {}

    def fake_load(csv_path, db_path, buy_in_cents_default):
        seen["csv_path"] = csv_path
        seen["db_path"] = db_path
        seen["buy_in_cents_default"] = buy_in_cents_default
        return {"hands_loaded": 3, "hands_skipped": 1, "sessions_created": 1}

    monkeypatch.setattr(cli, "load_hand_log_csv", fake_load)

    result = runner.invoke(
        cli.app,
        ["ingest", "hands.csv", "--db", "my.db", "--buy-in-cents", "5000"],
    )

    assert result.exit_code == 0
    assert str(seen["csv_path"]) == "hands.csv"
    assert seen["db_path"] == "my.db"
    assert seen["buy_in_cents_default"] == 5000
    assert "Loaded 3 hand(s)" in result.stdout
    assert "skipped 1" in result.stdout
    assert "created 1 new session(s)" in result.stdout


def test_ingest_defaults_db_and_buy_in(monkeypatch):
    seen = {}

    def fake_load(csv_path, db_path, buy_in_cents_default):
        seen["db_path"] = db_path
        seen["buy_in_cents_default"] = buy_in_cents_default
        return {"hands_loaded": 0, "hands_skipped": 0, "sessions_created": 0}

    monkeypatch.setattr(cli, "load_hand_log_csv", fake_load)

    result = runner.invoke(cli.app, ["ingest", "hands.csv"])

    assert result.exit_code == 0
    assert seen["db_path"] == cli.DEFAULT_DB_PATH
    assert seen["buy_in_cents_default"] == 0


def test_ingest_reports_failure_and_exits_nonzero(monkeypatch):
    def fake_load(csv_path, db_path, buy_in_cents_default):
        raise IngestionError("CSV failed validation")

    monkeypatch.setattr(cli, "load_hand_log_csv", fake_load)

    result = runner.invoke(cli.app, ["ingest", "hands.csv"])

    assert result.exit_code == 1
    assert "Ingestion failed" in result.stdout
    assert "CSV failed validation" in result.stdout


# --- stats ---------------------------------------------------------------------------


def test_stats_calls_print_summary_with_parsed_db(monkeypatch):
    seen = {}

    def fake_print_summary(db_path):
        seen["db_path"] = db_path
        print("fake stats output")

    monkeypatch.setattr(cli, "print_summary", fake_print_summary)

    result = runner.invoke(cli.app, ["stats", "--db", "my.db"])

    assert result.exit_code == 0
    assert seen["db_path"] == "my.db"
    assert "fake stats output" in result.stdout


def test_stats_defaults_db(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "print_summary", lambda db_path: seen.setdefault("db_path", db_path))

    result = runner.invoke(cli.app, ["stats"])

    assert result.exit_code == 0
    assert seen["db_path"] == cli.DEFAULT_DB_PATH


# --- ev-report -----------------------------------------------------------------------


def _make_decision(**overrides):
    fields = dict(
        hand_id=1,
        session_id=1,
        hero_position="BTN",
        action_type="raise",
        amount_bb=3.0,
        pot_before_bb=1.5,
        cost_bb=3.0,
        opponent_range_band=(0, 30),
        opponent_range_combo_count=42,
        hero_equity_pct=62.5,
        ev_action_bb=1.85,
        ev_fold_bb=0.0,
        ev_diff_bb=1.85,
        flag="+EV",
    )
    fields.update(overrides)
    return PreflopDecision(**fields)


def test_ev_report_calls_engine_with_parsed_args(monkeypatch):
    seen = {}

    def fake_analyze(db_path, trials_per_combo, seed):
        seen["db_path"] = db_path
        seen["trials_per_combo"] = trials_per_combo
        seen["seed"] = seed
        return [_make_decision()]

    monkeypatch.setattr(cli, "analyze_all_preflop_decisions", fake_analyze)

    result = runner.invoke(
        cli.app,
        ["ev-report", "--db", "my.db", "--trials", "50", "--seed", "7"],
    )

    assert result.exit_code == 0
    assert seen == {"db_path": "my.db", "trials_per_combo": 50, "seed": 7}


def test_ev_report_prints_position_action_flag_and_ev_numbers(monkeypatch):
    decision = _make_decision(
        hero_position="CO",
        action_type="raise",
        amount_bb=7.5,
        flag="+EV",
        hero_equity_pct=78.3,
        ev_action_bb=7.76,
        ev_diff_bb=7.76,
    )
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [decision]
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "hero: CO" in result.stdout
    assert "raise 7.50bb" in result.stdout
    assert "+EV" in result.stdout
    assert "78.3%" in result.stdout
    assert "+7.76bb" in result.stdout


def test_ev_report_flags_fold_as_baseline_without_ev_numbers(monkeypatch):
    fold_decision = _make_decision(
        action_type="fold",
        amount_bb=None,
        opponent_range_band=None,
        opponent_range_combo_count=None,
        hero_equity_pct=None,
        ev_action_bb=0.0,
        ev_fold_bb=0.0,
        ev_diff_bb=0.0,
        flag="baseline",
    )
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [fold_decision]
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "baseline" in result.stdout
    assert "fold" in result.stdout


def test_ev_report_handles_empty_database(monkeypatch):
    monkeypatch.setattr(cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [])

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "No preflop decisions found" in result.stdout


# --- end-to-end, real pipeline (no mocks) ---------------------------------------------


def test_validate_end_to_end_against_template_csv():
    result = runner.invoke(cli.app, ["validate", str(TEMPLATE_CSV)])

    assert result.exit_code == 0
    assert "Validation passed" in result.stdout


def test_ingest_and_stats_end_to_end(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)

    ingest_result = runner.invoke(cli.app, ["ingest", str(REAL_HANDS_CSV), "--db", str(db_path)])
    assert ingest_result.exit_code == 0
    assert "Loaded 15 hand(s)" in ingest_result.stdout

    stats_result = runner.invoke(cli.app, ["stats", "--db", str(db_path)])
    assert stats_result.exit_code == 0
    assert "Combined (all sessions):" in stats_result.stdout


def test_ev_report_end_to_end(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    runner.invoke(cli.app, ["ingest", str(REAL_HANDS_CSV), "--db", str(db_path)])

    result = runner.invoke(
        cli.app, ["ev-report", "--db", str(db_path), "--trials", "20", "--seed", "1"]
    )

    assert result.exit_code == 0
    assert "Hand 1" in result.stdout
    assert "hero:" in result.stdout
    assert "flag:" in result.stdout
