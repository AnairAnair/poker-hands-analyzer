"""
Data-preparation layer for the Streamlit dashboard (dashboard/app.py).

Every function here shapes data for a chart or table but computes nothing that
already has a home elsewhere: win rate, variance, and swing math live in
`stats/aggregator.py`; preflop decision EV and +EV/-EV/marginal flagging live in
`ev/engine.py`. This module only queries, joins, and reshapes what those modules
already produce into pandas DataFrames the dashboard can plot or display directly -
same wrap-don't-duplicate principle as `cli.py`.

The one piece of "new" math is the per-hand running cumulative-result and
running-win-rate series needed for the "over time" line chart - `aggregate_*_stats`
only returns the final swing/win-rate numbers, not the point-by-point series. Rather
than re-deriving that accumulation here, `stats/aggregator.py` exposes the exact
building blocks it uses internally (`cumulative_series`, `win_rate_bb_per_100`,
`combined_hand_results`) and this module just calls them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, fields as dataclass_fields

import pandas as pd

from poker_analyzer.ev.engine import (
    DEFAULT_TRIALS_PER_COMBO,
    PreflopDecision,
    analyze_all_preflop_decisions,
)
from poker_analyzer.stats.aggregator import (
    aggregate_all,
    combined_hand_results,
    cumulative_series,
    session_hand_results,
    win_rate_bb_per_100,
)

RESULTS_OVER_TIME_COLUMNS = [
    "hand_id",
    "session_id",
    "session_date",
    "hand_number",
    "result_bb",
    "cumulative_result_bb",
    "cumulative_win_rate_bb_per_100",
]

# Fixed display order for the preflop decision breakdown - not alphabetical, so the
# chart/table reads best-to-worst-ish rather than shuffling on flag_counts() calls.
PREFLOP_FLAG_ORDER = ["+EV", "-EV", "marginal", "baseline"]


def results_over_time(db_path: str, session_id: int | None = None) -> pd.DataFrame:
    """
    One row per hand with a logged result, carrying:
    - result_bb: that hand's own result
    - cumulative_result_bb: running total through that hand (aggregator.cumulative_series)
    - cumulative_win_rate_bb_per_100: bb/100 computed on the running total through that
      hand (aggregator.win_rate_bb_per_100), i.e. "what would win rate have read at this
      point in the sample"
    session_date/hand_number are joined in for chart labeling/hover text.

    With session_id=None (the default), this is every hand in combined (hand_id) order,
    matching aggregator.aggregate_combined_stats's ordering - the cumulative columns run
    across the whole database. With a specific session_id, this is that session's hands
    in hand_number order and the cumulative columns *restart at that session's first
    hand* - matching aggregator.aggregate_session_stats, which computes each session's
    swings independently. Filtering the combined view down to one session would instead
    show that session's slice of the all-time running total, which is a different (and
    for a single-session chart, wrong) number.
    """
    conn = sqlite3.connect(db_path)
    try:
        if session_id is None:
            rows = [(hand_id, sid, result_bb) for hand_id, sid, result_bb in combined_hand_results(conn)]
        else:
            rows = [
                (hand_id, session_id, result_bb)
                for hand_id, _hand_number, result_bb in session_hand_results(conn, session_id)
            ]
        session_dates = dict(conn.execute("SELECT session_id, session_date FROM sessions"))
        hand_numbers = dict(conn.execute("SELECT hand_id, hand_number FROM hands"))
    finally:
        conn.close()

    if not rows:
        return pd.DataFrame(columns=RESULTS_OVER_TIME_COLUMNS)

    results = [row[2] for row in rows]
    cumulative = cumulative_series(results)

    records = []
    for index, (hand_id, row_session_id, result_bb) in enumerate(rows):
        hands_so_far = index + 1
        records.append(
            {
                "hand_id": hand_id,
                "session_id": row_session_id,
                "session_date": session_dates.get(row_session_id),
                "hand_number": hand_numbers.get(hand_id),
                "result_bb": result_bb,
                "cumulative_result_bb": cumulative[index],
                "cumulative_win_rate_bb_per_100": win_rate_bb_per_100(cumulative[index], hands_so_far),
            }
        )
    return pd.DataFrame.from_records(records, columns=RESULTS_OVER_TIME_COLUMNS)


def session_summary_table(db_path: str) -> pd.DataFrame:
    """
    One row per session plus a final "All sessions" combined row, straight from
    `aggregator.aggregate_all` - every column on AggregateStats, no recomputation.
    """
    result = aggregate_all(db_path)
    rows = [asdict(stats) for stats in result["sessions"]] + [asdict(result["combined"])]
    return pd.DataFrame.from_records(rows)


def preflop_decisions_dataframe(
    db_path: str,
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> pd.DataFrame:
    """One row per preflop decision, straight from `ev.engine.analyze_all_preflop_decisions`."""
    decisions = analyze_all_preflop_decisions(db_path, trials_per_combo=trials_per_combo, seed=seed)
    columns = [field.name for field in dataclass_fields(PreflopDecision)]
    if not decisions:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame.from_records([asdict(decision) for decision in decisions], columns=columns)


def preflop_flag_counts(decisions_df: pd.DataFrame) -> pd.DataFrame:
    """
    Count of preflop decisions per flag (+EV/-EV/marginal/baseline), in a fixed display
    order, with each flag's share of the total. Pure reshape over an already-computed
    decisions DataFrame - takes no db_path, so it's cheap to call and test.
    """
    if decisions_df.empty:
        counts = pd.Series(0, index=PREFLOP_FLAG_ORDER)
    else:
        counts = decisions_df["flag"].value_counts().reindex(PREFLOP_FLAG_ORDER, fill_value=0)

    total = int(counts.sum())
    pct = (counts / total * 100) if total else counts.astype(float)
    return pd.DataFrame({"flag": PREFLOP_FLAG_ORDER, "count": counts.values, "pct": pct.values})
