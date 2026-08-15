"""
Tests for the dashboard's data-preparation layer (poker_analyzer.dashboard.data_prep).

Per the task, this covers the data-shaping functions that feed the Streamlit charts/
tables, not the Streamlit rendering itself (not practically unit-testable the same way -
see dashboard/app.py). Two kinds of coverage:
- The DB-backed functions (results_over_time, session_summary_table,
  preflop_decisions_dataframe), cross-checked against the same real 15-hand database
  and the aggregator/ev-engine functions they wrap directly - so drift between the
  dashboard's numbers and the underlying modules' numbers would show up as a test
  failure.
- preflop_flag_counts, a pure reshape with no db_path, tested directly against small
  constructed DataFrames.
"""

from pathlib import Path

import pandas as pd
import pytest

from poker_analyzer.dashboard.data_prep import (
    PREFLOP_FLAG_ORDER,
    RESULTS_OVER_TIME_COLUMNS,
    preflop_decisions_dataframe,
    preflop_flag_counts,
    results_over_time,
    session_summary_table,
)
from poker_analyzer.db.init_db import init_db
from poker_analyzer.ev.engine import analyze_all_preflop_decisions
from poker_analyzer.ingestion.loader import load_hand_log_csv
from poker_analyzer.stats.aggregator import aggregate_all

REAL_HANDS_CSV = Path("data/templates/real_hands.csv")

TEST_TRIALS_PER_COMBO = 150
TEST_SEED = 7


def _real_hands_db(tmp_path: Path) -> str:
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(REAL_HANDS_CSV, db_path)
    return str(db_path)


# --- results_over_time ----------------------------------------------------------------


def test_results_over_time_has_one_row_per_hand_with_a_result(tmp_path):
    db_path = _real_hands_db(tmp_path)
    combined = aggregate_all(db_path)["combined"]

    df = results_over_time(db_path)

    assert len(df) == combined.total_hands == 15
    assert list(df.columns) == RESULTS_OVER_TIME_COLUMNS


def test_results_over_time_cumulative_column_matches_a_plain_cumsum(tmp_path):
    """cumulative_result_bb must be exactly the running sum of result_bb, in row order."""
    db_path = _real_hands_db(tmp_path)

    df = results_over_time(db_path)

    expected = df["result_bb"].cumsum()
    assert df["cumulative_result_bb"].tolist() == pytest.approx(expected.tolist())


def test_results_over_time_final_row_matches_combined_aggregate_stats(tmp_path):
    """The last row's running totals must land exactly on aggregator's combined numbers."""
    db_path = _real_hands_db(tmp_path)
    combined = aggregate_all(db_path)["combined"]

    df = results_over_time(db_path)
    last = df.iloc[-1]

    assert last["cumulative_result_bb"] == pytest.approx(combined.total_result_bb)
    assert last["cumulative_win_rate_bb_per_100"] == pytest.approx(combined.win_rate_bb_per_100)


def test_results_over_time_rows_carry_session_date_and_hand_number(tmp_path):
    db_path = _real_hands_db(tmp_path)

    df = results_over_time(db_path)

    assert df["session_date"].notna().all()
    assert df["hand_number"].notna().all()


def test_results_over_time_scoped_to_a_session_restarts_cumulative_at_zero(tmp_path):
    """
    Filtering to one session must NOT just be a slice of the combined running total -
    the cumulative columns should restart at that session's own first hand, matching
    aggregate_session_stats's independent per-session swing calc.
    """
    db_path = _real_hands_db(tmp_path)
    sessions_stats = {s.session_id: s for s in aggregate_all(db_path)["sessions"]}

    for session_id, stats in sessions_stats.items():
        df = results_over_time(db_path, session_id=session_id)

        assert len(df) == stats.total_hands
        assert (df["session_id"] == session_id).all()
        assert df["cumulative_result_bb"].iloc[-1] == pytest.approx(stats.total_result_bb)
        assert df["cumulative_win_rate_bb_per_100"].iloc[-1] == pytest.approx(stats.win_rate_bb_per_100)


def test_results_over_time_session_view_differs_from_combined_view_slice(tmp_path):
    """Sanity guard against the bug this scoping was added to avoid: for the second
    session, the session-scoped cumulative total must differ from whatever the combined
    (all-sessions) running total happened to be at that same row count."""
    db_path = _real_hands_db(tmp_path)
    combined_df = results_over_time(db_path)
    sessions_stats = {s.session_id: s for s in aggregate_all(db_path)["sessions"]}
    second_session_id = sorted(sessions_stats)[1]

    session_df = results_over_time(db_path, session_id=second_session_id)
    combined_slice_final = combined_df[combined_df["session_id"] == second_session_id][
        "cumulative_result_bb"
    ].iloc[-1]

    assert session_df["cumulative_result_bb"].iloc[-1] == pytest.approx(
        sessions_stats[second_session_id].total_result_bb
    )
    assert session_df["cumulative_result_bb"].iloc[-1] != pytest.approx(combined_slice_final)


def test_results_over_time_empty_database_returns_empty_frame_with_columns(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)

    df = results_over_time(str(db_path))

    assert df.empty
    assert list(df.columns) == RESULTS_OVER_TIME_COLUMNS


# --- session_summary_table -------------------------------------------------------------


def test_session_summary_table_has_one_row_per_session_plus_combined(tmp_path):
    db_path = _real_hands_db(tmp_path)

    df = session_summary_table(db_path)

    assert len(df) == 3  # 2 real sessions + 1 combined row
    assert df.iloc[-1]["label"] == "All sessions (mixed stakes)"
    assert pd.isna(df.iloc[-1]["session_id"])


def test_session_summary_table_matches_aggregator_numbers_exactly(tmp_path):
    db_path = _real_hands_db(tmp_path)
    result = aggregate_all(db_path)

    df = session_summary_table(db_path)

    combined_row = df.iloc[-1]
    assert combined_row["total_hands"] == result["combined"].total_hands
    assert combined_row["total_result_bb"] == pytest.approx(result["combined"].total_result_bb)
    assert combined_row["win_rate_bb_per_100"] == pytest.approx(result["combined"].win_rate_bb_per_100)

    session_rows = df.iloc[:-1]
    assert sorted(session_rows["total_hands"].tolist()) == sorted(
        s.total_hands for s in result["sessions"]
    )


# --- preflop_decisions_dataframe --------------------------------------------------------


def test_preflop_decisions_dataframe_matches_engine_output(tmp_path):
    """Row-for-row, this must be exactly what analyze_all_preflop_decisions returns - the
    dashboard must not filter, reorder, or recompute anything on the way in."""
    db_path = _real_hands_db(tmp_path)

    expected = analyze_all_preflop_decisions(
        db_path, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED
    )
    df = preflop_decisions_dataframe(db_path, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)

    assert len(df) == len(expected)
    assert df["hand_id"].tolist() == [d.hand_id for d in expected]
    assert df["flag"].tolist() == [d.flag for d in expected]


def test_preflop_decisions_dataframe_empty_database_returns_empty_frame(tmp_path):
    db_path = tmp_path / "poker.db"
    init_db(db_path)

    df = preflop_decisions_dataframe(str(db_path))

    assert df.empty
    assert "flag" in df.columns


# --- preflop_flag_counts (pure function) ------------------------------------------------


def test_preflop_flag_counts_counts_and_orders_correctly():
    decisions_df = pd.DataFrame(
        {"flag": ["+EV", "+EV", "-EV", "marginal", "baseline", "marginal"]}
    )

    counts_df = preflop_flag_counts(decisions_df)

    assert counts_df["flag"].tolist() == PREFLOP_FLAG_ORDER
    counts_by_flag = dict(zip(counts_df["flag"], counts_df["count"]))
    assert counts_by_flag == {"+EV": 2, "-EV": 1, "marginal": 2, "baseline": 1}


def test_preflop_flag_counts_percentages_sum_to_100():
    decisions_df = pd.DataFrame({"flag": ["+EV", "-EV", "-EV", "marginal"]})

    counts_df = preflop_flag_counts(decisions_df)

    assert counts_df["pct"].sum() == pytest.approx(100.0)
    plus_ev_pct = counts_df.loc[counts_df["flag"] == "+EV", "pct"].iloc[0]
    assert plus_ev_pct == pytest.approx(25.0)


def test_preflop_flag_counts_handles_empty_decisions_without_dividing_by_zero():
    counts_df = preflop_flag_counts(pd.DataFrame({"flag": []}))

    assert counts_df["count"].tolist() == [0, 0, 0, 0]
    assert counts_df["pct"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_preflop_flag_counts_ignores_unknown_flags_gracefully():
    """Defensive: an unexpected flag value shouldn't crash the reindex, just be dropped
    from the fixed-order output (there are only 4 valid flags per ev/engine.py)."""
    decisions_df = pd.DataFrame({"flag": ["+EV", "unexpected"]})

    counts_df = preflop_flag_counts(decisions_df)

    assert counts_df.loc[counts_df["flag"] == "+EV", "count"].iloc[0] == 1
    assert counts_df["count"].sum() == 1
