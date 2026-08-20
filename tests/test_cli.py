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

import uuid
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from poker_analyzer import cli
from poker_analyzer.db.connection import get_db_url
from poker_analyzer.db.init_db import init_db
from poker_analyzer.ev.engine import PostflopDecision, PreflopDecision
from poker_analyzer.ingestion.loader import IngestionError
from poker_analyzer.leaks.detector import PatternLeak

runner = CliRunner()

REAL_HANDS_CSV = Path("data/templates/real_hands.csv")
TEMPLATE_CSV = Path("data/templates/hand_log_template.csv")


# --- argument parsing / subcommand dispatch -----------------------------------------


def test_help_lists_all_subcommands():
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for name in ("validate", "ingest", "stats", "ev-report", "leaks"):
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

    def fake_load(csv_path, buy_in_cents_default):
        seen["csv_path"] = csv_path
        seen["buy_in_cents_default"] = buy_in_cents_default
        return {"hands_loaded": 3, "hands_skipped": 1, "sessions_created": 1}

    monkeypatch.setattr(cli, "load_hand_log_csv", fake_load)

    result = runner.invoke(
        cli.app,
        ["ingest", "hands.csv", "--buy-in-cents", "5000"],
    )

    assert result.exit_code == 0
    assert str(seen["csv_path"]) == "hands.csv"
    assert seen["buy_in_cents_default"] == 5000
    assert "Loaded 3 hand(s)" in result.stdout
    assert "skipped 1" in result.stdout
    assert "created 1 new session(s)" in result.stdout


def test_ingest_defaults_db_and_buy_in(monkeypatch):
    seen = {}

    def fake_load(csv_path, buy_in_cents_default):
        seen["buy_in_cents_default"] = buy_in_cents_default
        return {"hands_loaded": 0, "hands_skipped": 0, "sessions_created": 0}

    monkeypatch.setattr(cli, "load_hand_log_csv", fake_load)

    result = runner.invoke(cli.app, ["ingest", "hands.csv"])

    assert result.exit_code == 0
    assert seen["buy_in_cents_default"] == 0


def test_ingest_reports_failure_and_exits_nonzero(monkeypatch):
    def fake_load(csv_path, buy_in_cents_default):
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


def _make_postflop_decision(**overrides):
    fields = dict(
        hand_id=1,
        session_id=1,
        hero_position="BTN",
        street="flop",
        action_type="bet",
        amount_bb=4.0,
        pot_before_bb=6.5,
        cost_bb=4.0,
        board="Jh 8h 3c",
        opponent_range_band=(0, 12),
        opponent_range_combo_count=18,
        hero_equity_pct=71.0,
        ev_action_bb=3.2,
        ev_fold_bb=0.0,
        ev_diff_bb=3.2,
        flag="+EV",
    )
    fields.update(overrides)
    return PostflopDecision(**fields)


def test_ev_report_calls_engine_with_parsed_args(monkeypatch):
    seen = {}

    def fake_analyze_preflop(db_path, trials_per_combo, seed):
        seen["preflop"] = {"db_path": db_path, "trials_per_combo": trials_per_combo, "seed": seed}
        return [_make_decision()]

    def fake_analyze_postflop(db_path, trials_per_combo, seed):
        seen["postflop"] = {"db_path": db_path, "trials_per_combo": trials_per_combo, "seed": seed}
        return []

    monkeypatch.setattr(cli, "analyze_all_preflop_decisions", fake_analyze_preflop)
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", fake_analyze_postflop)

    result = runner.invoke(
        cli.app,
        ["ev-report", "--db", "my.db", "--trials", "50", "--seed", "7"],
    )

    assert result.exit_code == 0
    expected = {"db_path": "my.db", "trials_per_combo": 50, "seed": 7}
    assert seen == {"preflop": expected, "postflop": expected}


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
    monkeypatch.setattr(
        cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: []
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "hero: CO" in result.stdout
    assert "raise 7.50bb" in result.stdout
    assert "+EV" in result.stdout
    assert "78.3%" in result.stdout
    assert "+7.76bb" in result.stdout


def test_ev_report_shows_fold_pct_for_bet_and_raise_but_not_for_call(monkeypatch):
    """
    fold_pct is only meaningful for a bet/raise (the only actions with fold equity -
    see ev/engine.py). It's None on every other decision. The printed line should
    show it when present and omit it entirely otherwise, rather than printing a
    misleading "fold_pct: 0.0%" for a decision hero didn't induce any folds on.
    """
    raise_decision = _make_decision(
        action_type="raise",
        hero_equity_pct=0.0,
        ev_action_bb=0.0,
        ev_diff_bb=0.0,
        flag="marginal",
        fold_pct=0.46875,
        continuing_range_band=(12.0, 14.295),
    )
    call_decision = _make_decision(hand_id=2, action_type="call", fold_pct=None, continuing_range_band=None)
    monkeypatch.setattr(
        cli,
        "analyze_all_preflop_decisions",
        lambda db_path, trials_per_combo, seed: [raise_decision, call_decision],
    )
    monkeypatch.setattr(
        cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: []
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    raise_line = next(line for line in lines if "raise 3.00bb" in line)
    call_line = next(line for line in lines if "call" in line)
    assert "fold_pct: 46.9%" in raise_line
    assert "fold_pct" not in call_line


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
    monkeypatch.setattr(
        cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: []
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "baseline" in result.stdout
    assert "fold" in result.stdout


def test_ev_report_handles_empty_database(monkeypatch):
    monkeypatch.setattr(cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [])
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "No preflop decisions found" in result.stdout


def test_ev_report_prints_postflop_lines_after_hands_preflop_lines(monkeypatch):
    preflop_decision = _make_decision(hand_id=1, hero_position="BTN", action_type="raise")
    flop_decision = _make_postflop_decision(
        hand_id=1, street="flop", action_type="bet", amount_bb=4.0, hero_equity_pct=71.0
    )
    turn_decision = _make_postflop_decision(
        hand_id=1, street="turn", action_type="check", amount_bb=None, hero_equity_pct=65.0
    )
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [preflop_decision]
    )
    monkeypatch.setattr(
        cli,
        "analyze_all_postflop_decisions",
        lambda db_path, trials_per_combo, seed: [flop_decision, turn_decision],
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    assert "flop: bet 4.00bb" in result.stdout
    assert "turn: check" in result.stdout
    preflop_line = result.stdout.index("raise")
    flop_line = result.stdout.index("flop: bet")
    turn_line = result.stdout.index("turn: check")
    assert preflop_line < flop_line < turn_line


def test_ev_report_groups_postflop_lines_under_correct_hand(monkeypatch):
    hand1_preflop = _make_decision(hand_id=1, hero_position="BTN")
    hand2_preflop = _make_decision(hand_id=2, hero_position="CO")
    hand2_flop = _make_postflop_decision(hand_id=2, street="flop")
    monkeypatch.setattr(
        cli,
        "analyze_all_preflop_decisions",
        lambda db_path, trials_per_combo, seed: [hand1_preflop, hand2_preflop],
    )
    monkeypatch.setattr(
        cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [hand2_flop]
    )

    result = runner.invoke(cli.app, ["ev-report"])

    assert result.exit_code == 0
    hand1_header = result.stdout.index("Hand 1")
    hand2_header = result.stdout.index("Hand 2")
    flop_line = result.stdout.index("flop: bet")
    assert hand1_header < hand2_header < flop_line


# --- leaks ---------------------------------------------------------------------------


def _make_pattern_leak(**overrides) -> PatternLeak:
    fields = dict(
        position="UTG",
        street="preflop",
        action_type="raise",
        label="UTG raises",
        occurrences=6,
        avg_ev_diff_bb=2.31,
        total_ev_diff_bb=13.86,
        negative_ev_count=0,
        marginal_count=1,
        positive_ev_count=5,
        hand_ids=(1, 2, 4, 6, 9, 12),
        verdict="fine",
    )
    fields.update(overrides)
    return PatternLeak(**fields)


def test_leaks_calls_engine_with_parsed_args(monkeypatch):
    seen = {}

    def fake_analyze_preflop(db_path, trials_per_combo, seed):
        seen["preflop"] = {"db_path": db_path, "trials_per_combo": trials_per_combo, "seed": seed}
        return [_make_decision()]

    def fake_analyze_postflop(db_path, trials_per_combo, seed):
        seen["postflop"] = {"db_path": db_path, "trials_per_combo": trials_per_combo, "seed": seed}
        return []

    monkeypatch.setattr(cli, "analyze_all_preflop_decisions", fake_analyze_preflop)
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", fake_analyze_postflop)

    result = runner.invoke(
        cli.app,
        ["leaks", "--db", "my.db", "--trials", "50", "--seed", "7"],
    )

    assert result.exit_code == 0
    expected = {"db_path": "my.db", "trials_per_combo": 50, "seed": 7}
    assert seen == {"preflop": expected, "postflop": expected}


def test_leaks_passes_min_sample_through_to_detector(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [_make_decision()]
    )
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])

    def fake_detect_leaks(preflop_decisions, postflop_decisions, min_sample_size):
        seen["min_sample_size"] = min_sample_size
        return []

    monkeypatch.setattr(cli, "detect_leaks", fake_detect_leaks)

    result = runner.invoke(cli.app, ["leaks", "--min-sample", "5"])

    assert result.exit_code == 0
    assert seen["min_sample_size"] == 5


def test_leaks_defaults_min_sample_to_module_constant(monkeypatch):
    from poker_analyzer.leaks.detector import MIN_SAMPLE_SIZE

    seen = {}
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [_make_decision()]
    )
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])

    def fake_detect_leaks(preflop_decisions, postflop_decisions, min_sample_size):
        seen["min_sample_size"] = min_sample_size
        return []

    monkeypatch.setattr(cli, "detect_leaks", fake_detect_leaks)

    result = runner.invoke(cli.app, ["leaks"])

    assert result.exit_code == 0
    assert seen["min_sample_size"] == MIN_SAMPLE_SIZE


def test_leaks_handles_empty_database(monkeypatch):
    monkeypatch.setattr(cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [])
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])

    result = runner.invoke(cli.app, ["leaks"])

    assert result.exit_code == 0
    assert "No decisions found" in result.stdout


def test_leaks_prints_pattern_under_its_verdict_section(monkeypatch):
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [_make_decision()]
    )
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])
    monkeypatch.setattr(
        cli,
        "detect_leaks",
        lambda preflop_decisions, postflop_decisions, min_sample_size: [
            _make_pattern_leak(
                label="UTG turn bets",
                verdict="leak",
                avg_ev_diff_bb=-2.5,
                occurrences=3,
                hand_ids=(3, 7, 11),
            )
        ],
    )

    result = runner.invoke(cli.app, ["leaks"])

    assert result.exit_code == 0
    leak_header = result.stdout.index("Leaks (")
    pattern_line = result.stdout.index("UTG turn bets")
    marginal_header = result.stdout.index("Marginal patterns")
    assert leak_header < pattern_line < marginal_header
    assert "-2.50bb" in result.stdout
    assert "hands: 3, 7, 11" in result.stdout


def test_leaks_shows_insufficient_data_without_flag_breakdown(monkeypatch):
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [_make_decision()]
    )
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])
    monkeypatch.setattr(
        cli,
        "detect_leaks",
        lambda preflop_decisions, postflop_decisions, min_sample_size: [
            _make_pattern_leak(
                label="BB calls",
                verdict="insufficient_data",
                occurrences=1,
                avg_ev_diff_bb=-7.83,
                negative_ev_count=0,
                marginal_count=0,
                positive_ev_count=0,
                hand_ids=(11,),
            )
        ],
    )

    result = runner.invoke(cli.app, ["leaks"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    pattern_line = next(line for line in lines if "BB calls" in line)
    assert "-EV:" not in pattern_line
    assert "hands: 11" in pattern_line
    assert "Insufficient data" in result.stdout


def test_leaks_shows_none_for_empty_verdict_sections(monkeypatch):
    monkeypatch.setattr(
        cli, "analyze_all_preflop_decisions", lambda db_path, trials_per_combo, seed: [_make_decision()]
    )
    monkeypatch.setattr(cli, "analyze_all_postflop_decisions", lambda db_path, trials_per_combo, seed: [])
    monkeypatch.setattr(cli, "detect_leaks", lambda preflop_decisions, postflop_decisions, min_sample_size: [])

    result = runner.invoke(cli.app, ["leaks"])

    assert result.exit_code == 0
    assert result.stdout.count("(none)") == 4


# --- end-to-end, real pipeline (no mocks) ---------------------------------------------


def test_validate_end_to_end_against_template_csv():
    result = runner.invoke(cli.app, ["validate", str(TEMPLATE_CSV)])

    assert result.exit_code == 0
    assert "Validation passed" in result.stdout


@pytest.fixture
def isolated_pg_schema(monkeypatch):
    """
    Point every test using this fixture at a throwaway, uniquely-named Postgres
    schema instead of the real 'poker_analyzer' schema, so the chained
    ingest -> stats/ev-report/leaks tests below never read or write the real
    migrated hand history in Supabase. Dropped again once the test finishes.
    Same pattern as test_ingestion.py's fixture of the same name.
    """
    schema = f"test_cli_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("POKER_ANALYZER_PG_SCHEMA", schema)
    yield
    conn = psycopg.connect(get_db_url())
    conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    conn.commit()
    conn.close()


def test_ingest_and_stats_end_to_end(isolated_pg_schema):
    init_db()

    ingest_result = runner.invoke(cli.app, ["ingest", str(REAL_HANDS_CSV)])
    assert ingest_result.exit_code == 0
    assert "Loaded 15 hand(s)" in ingest_result.stdout

    stats_result = runner.invoke(cli.app, ["stats"])
    assert stats_result.exit_code == 0
    assert "Per stakes level:" in stats_result.stdout
    assert "Combined (all sessions, MIXED STAKES" in stats_result.stdout


def test_ev_report_end_to_end(isolated_pg_schema):
    init_db()
    runner.invoke(cli.app, ["ingest", str(REAL_HANDS_CSV)])

    result = runner.invoke(cli.app, ["ev-report", "--trials", "20", "--seed", "1"])

    assert result.exit_code == 0
    assert "Hand 1" in result.stdout
    assert "hero:" in result.stdout
    assert "flag:" in result.stdout


def test_leaks_end_to_end(isolated_pg_schema):
    init_db()
    runner.invoke(cli.app, ["ingest", str(REAL_HANDS_CSV)])

    result = runner.invoke(cli.app, ["leaks", "--trials", "20", "--seed", "1"])

    assert result.exit_code == 0
    assert "Poker Hand Analyzer - Leak Report" in result.stdout
    assert "UTG raises" in result.stdout  # the one pattern with 6 real occurrences
    assert "Insufficient data" in result.stdout


def test_leaks_end_to_end_respects_custom_min_sample(isolated_pg_schema):
    init_db()
    runner.invoke(cli.app, ["ingest", str(REAL_HANDS_CSV)])

    strict_result = runner.invoke(
        cli.app, ["leaks", "--trials", "20", "--seed", "1", "--min-sample", "10"]
    )

    assert strict_result.exit_code == 0
    # No real pattern in the 15-hand database repeats 10+ times.
    assert "  (none)" in strict_result.stdout.split("Insufficient data")[0]
