"""
Tests for session/combined aggregation (poker_analyzer.stats.aggregator).

Two kinds of coverage, per the task:
- Sanity checks against the actual 15 hands loaded from data/templates/real_hands.csv
  (total hand count, sums reconciling between per-session and combined, and the
  variance/std-dev/swing math cross-checked against independent reference
  computations rather than hardcoded numbers).
- A small hand-constructed "textbook" sequence for the swing metric, where the
  peak/trough math can be verified by hand.
"""

import sqlite3
import statistics
from pathlib import Path

import pytest

from poker_analyzer.db.init_db import init_db
from poker_analyzer.ingestion.loader import load_hand_log_csv
from poker_analyzer.stats.aggregator import (
    AggregateStats,
    _compute_stats,
    aggregate_all,
    aggregate_combined_stats,
    aggregate_session_stats,
)

REAL_HANDS_CSV = Path("data/templates/real_hands.csv")


def _load_real_hands_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(REAL_HANDS_CSV, db_path)
    return sqlite3.connect(db_path)


def _brute_force_swings(results: list[float]) -> tuple[float, float]:
    """
    Independent O(n^2) reference for the peak-to-trough / trough-to-peak swing
    metric, used to cross-check the O(n) running-peak/trough implementation in
    aggregator._compute_stats against a much more obviously-correct definition:
    the max/min difference between any earlier cumulative point and any later one.
    Returns (biggest_upswing_bb, biggest_downswing_bb).
    """
    cumulative = [0.0]
    for result in results:
        cumulative.append(cumulative[-1] + result)

    max_upswing = 0.0
    max_downswing = 0.0
    for i in range(len(cumulative)):
        for j in range(i, len(cumulative)):
            max_upswing = max(max_upswing, cumulative[j] - cumulative[i])
            max_downswing = max(max_downswing, cumulative[i] - cumulative[j])
    return max_upswing, -max_downswing


# --- Real hands from the database ---------------------------------------------------


def test_total_hands_across_database_is_15(tmp_path):
    conn = _load_real_hands_db(tmp_path)
    combined = aggregate_combined_stats(conn)
    conn.close()

    assert combined.total_hands == 15


def test_per_session_hand_counts_sum_to_combined(tmp_path):
    conn = _load_real_hands_db(tmp_path)
    sessions = aggregate_session_stats(conn)
    combined = aggregate_combined_stats(conn)
    conn.close()

    assert len(sessions) == 2
    assert sum(s.total_hands for s in sessions) == combined.total_hands


def test_per_session_result_sums_reconcile_with_combined_and_raw_query(tmp_path):
    """
    The combined total_result_bb must equal both the sum of the per-session
    totals and a raw SUM(result_bb) query straight from the database - three
    independent ways of arriving at the same number.
    """
    conn = _load_real_hands_db(tmp_path)
    sessions = aggregate_session_stats(conn)
    combined = aggregate_combined_stats(conn)
    raw_total = conn.execute("SELECT SUM(result_bb) FROM hands").fetchone()[0]
    conn.close()

    assert sum(s.total_result_bb for s in sessions) == pytest.approx(combined.total_result_bb)
    assert combined.total_result_bb == pytest.approx(raw_total)


def test_win_rate_bb_per_100_matches_definition(tmp_path):
    """win_rate_bb_per_100 is (total_result_bb / total_hands) * 100 by definition - no more, no less."""
    conn = _load_real_hands_db(tmp_path)
    combined = aggregate_combined_stats(conn)
    conn.close()

    expected = (combined.total_result_bb / combined.total_hands) * 100
    assert combined.win_rate_bb_per_100 == pytest.approx(expected)


def test_variance_and_std_dev_match_independent_statistics_module_call(tmp_path):
    """
    Cross-check against Python's stdlib `statistics.variance` computed directly
    on the raw result_bb values pulled from the database, independent of
    aggregator's own internals.
    """
    conn = _load_real_hands_db(tmp_path)
    raw_results = [
        row[0] for row in conn.execute("SELECT result_bb FROM hands WHERE result_bb IS NOT NULL ORDER BY hand_id")
    ]
    combined = aggregate_combined_stats(conn)
    conn.close()

    expected_variance = statistics.variance(raw_results)
    assert combined.variance_bb == pytest.approx(expected_variance)
    assert combined.std_dev_bb == pytest.approx(expected_variance**0.5)


def test_swings_match_brute_force_reference_for_real_hands(tmp_path):
    """Cross-check the O(n) running-peak/trough swing calc against an O(n^2) brute-force reference."""
    conn = _load_real_hands_db(tmp_path)
    raw_results = [
        row[0] for row in conn.execute("SELECT result_bb FROM hands WHERE result_bb IS NOT NULL ORDER BY hand_id")
    ]
    combined = aggregate_combined_stats(conn)
    conn.close()

    expected_upswing, expected_downswing = _brute_force_swings(raw_results)
    assert combined.biggest_upswing_bb == pytest.approx(expected_upswing)
    assert combined.biggest_downswing_bb == pytest.approx(expected_downswing)


def test_aggregate_all_returns_sessions_and_combined(tmp_path):
    conn = _load_real_hands_db(tmp_path)
    conn.close()
    db_path = tmp_path / "poker.db"

    result = aggregate_all(str(db_path))

    assert len(result["sessions"]) == 2
    assert isinstance(result["combined"], AggregateStats)
    assert result["combined"].session_id is None
    assert {s.session_id for s in result["sessions"]} == {1, 2}


# --- Hand-constructed swing scenario --------------------------------------------------


def test_swing_tracking_textbook_sequence():
    """
    Hand-verifiable sequence: results = [10, -5, -8, 20, -30, 15].
    Cumulative running total: 0, 10, 5, -3, 17, -13, 2.
    The running peak hits 17 (after the +20 hand) before dropping to a running
    trough of -13 (after the -30 hand) - a 30bb peak-to-trough downswing.
    The running trough hits -3 (after the -8 hand) before climbing to the
    peak of 17 - a 20bb trough-to-peak upswing. Verified by hand in the module
    docstring's worked example and cross-checked here against the brute-force
    reference too.
    """
    results = [10.0, -5.0, -8.0, 20.0, -30.0, 15.0]
    stats = _compute_stats("textbook", None, results)

    assert stats.biggest_downswing_bb == pytest.approx(-30.0)
    assert stats.biggest_upswing_bb == pytest.approx(20.0)

    expected_upswing, expected_downswing = _brute_force_swings(results)
    assert stats.biggest_upswing_bb == pytest.approx(expected_upswing)
    assert stats.biggest_downswing_bb == pytest.approx(expected_downswing)


def test_single_hand_session_has_zero_variance_and_matching_swing():
    """
    With n=1, sample variance (n-1 denominator) is mathematically undefined -
    aggregator defines it as 0.0 rather than raising. A single winning hand's
    entire result is the upswing; a single losing hand's is the downswing.
    """
    win_stats = _compute_stats("single win", None, [25.0])
    assert win_stats.total_hands == 1
    assert win_stats.variance_bb == 0.0
    assert win_stats.std_dev_bb == 0.0
    assert win_stats.biggest_upswing_bb == pytest.approx(25.0)
    assert win_stats.biggest_downswing_bb == pytest.approx(0.0)

    loss_stats = _compute_stats("single loss", None, [-25.0])
    assert loss_stats.biggest_downswing_bb == pytest.approx(-25.0)
    assert loss_stats.biggest_upswing_bb == pytest.approx(0.0)


def test_empty_results_do_not_error():
    stats = _compute_stats("empty", None, [])
    assert stats.total_hands == 0
    assert stats.total_result_bb == 0.0
    assert stats.win_rate_bb_per_100 == 0.0
    assert stats.variance_bb == 0.0
    assert stats.biggest_upswing_bb == 0.0
    assert stats.biggest_downswing_bb == 0.0


def test_hands_with_null_result_bb_are_excluded(tmp_path):
    """
    A hand with no result_bb logged yet must not count toward total_hands or
    pollute any stat - construct one directly (not through the CSV loader,
    since the loader always writes a value from a required-looking column) to
    exercise the NULL-exclusion path documented in the module docstring.
    """
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (session_id, session_date, location, stakes, big_blind_cents, buy_in_cents) "
        "VALUES (1, '2026-08-01', 'Test Room', '1/2', 200, 0)"
    )
    conn.execute(
        "INSERT INTO hands (session_id, hand_number, hero_position, hero_hole_cards, num_players, result_bb) "
        "VALUES (1, 1, 'BTN', 'As Kd', 6, 10.0)"
    )
    conn.execute(
        "INSERT INTO hands (session_id, hand_number, hero_position, hero_hole_cards, num_players, result_bb) "
        "VALUES (1, 2, 'BTN', '2c 3d', 6, NULL)"
    )
    conn.commit()

    combined = aggregate_combined_stats(conn)
    conn.close()

    assert combined.total_hands == 1
    assert combined.total_result_bb == pytest.approx(10.0)
