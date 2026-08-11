"""
Session and combined win-rate / variance / swing aggregation.

Computes the following, both per-session and combined across every session in the
database:

- total_hands: count of hands with a logged result_bb (see the NULL note below).
- total_result_bb: sum of hero's per-hand result_bb.
- win_rate_bb_per_100: the standard poker win-rate convention, (total_result_bb /
  total_hands) * 100 - normalizes a result to "per 100 hands" so it's comparable
  across different sample sizes. With only 15 hands currently in the database this
  number is extremely noisy and not statistically meaningful - see
  scripts/print_stats_summary.py, which prints that caveat alongside the numbers.
- variance_bb / std_dev_bb: sample variance/standard deviation (n-1 denominator,
  matching Python's stdlib `statistics.variance`) of the per-hand result_bb values.
  Sample rather than population variance, since the hands in the database are a
  sample of hero's play, not the full population of every hand hero will ever play.
  0.0 for a single-hand sample (n-1 = 0, variance is undefined).
- biggest_upswing_bb / biggest_downswing_bb: the classic peak-to-trough / trough-to-
  peak swing metric poker tracking software reports (not just hero's single best or
  worst hand) - computed off the *cumulative* running result across hands, in
  database order (hand_number order within a session; hand_id/insertion order for
  the combined row). biggest_downswing_bb is <= 0 (how far below the running peak
  the running result ever fell); biggest_upswing_bb is >= 0 (how far above the
  running trough it ever climbed). Within a session hand_id order matches
  chronological play order (hand_number is sequential), but combined across
  sessions it's insertion order, not necessarily calendar order - several of the
  currently-loaded hands have "PLACEHOLDER DATE" notes, so true calendar order
  across sessions isn't reliably known yet. Known simplification, not a bug.

Hands with no result_bb logged yet (NULL - e.g. a hand entered before the session
was fully recorded) are excluded from every stat, including the hand count. None of
the 15 hands currently in the database hit this case.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class AggregateStats:
    label: str
    session_id: int | None  # None for the combined, all-sessions row
    total_hands: int
    total_result_bb: float
    win_rate_bb_per_100: float
    variance_bb: float
    std_dev_bb: float
    biggest_upswing_bb: float
    biggest_downswing_bb: float


def _compute_stats(label: str, session_id: int | None, results: list[float]) -> AggregateStats:
    total_hands = len(results)
    total_result_bb = sum(results)
    win_rate_bb_per_100 = (total_result_bb / total_hands) * 100 if total_hands else 0.0
    variance_bb = statistics.variance(results) if total_hands > 1 else 0.0
    std_dev_bb = variance_bb ** 0.5

    cumulative = 0.0
    running_peak = 0.0
    running_trough = 0.0
    max_downswing = 0.0
    max_upswing = 0.0
    for result in results:
        cumulative += result
        running_peak = max(running_peak, cumulative)
        running_trough = min(running_trough, cumulative)
        max_downswing = max(max_downswing, running_peak - cumulative)
        max_upswing = max(max_upswing, cumulative - running_trough)

    return AggregateStats(
        label=label,
        session_id=session_id,
        total_hands=total_hands,
        total_result_bb=total_result_bb,
        win_rate_bb_per_100=win_rate_bb_per_100,
        variance_bb=variance_bb,
        std_dev_bb=std_dev_bb,
        biggest_upswing_bb=max_upswing,
        biggest_downswing_bb=-max_downswing,
    )


def aggregate_session_stats(conn: sqlite3.Connection) -> list[AggregateStats]:
    """One AggregateStats per session, in session_id order (swings use hand_number order)."""
    sessions = conn.execute(
        "SELECT session_id, session_date, location, stakes FROM sessions ORDER BY session_id"
    ).fetchall()

    stats = []
    for session_id, session_date, location, stakes in sessions:
        results = [
            row[0]
            for row in conn.execute(
                """
                SELECT result_bb FROM hands
                WHERE session_id = ? AND result_bb IS NOT NULL
                ORDER BY hand_number
                """,
                (session_id,),
            )
        ]
        label = f"{session_date} {location} {stakes}"
        stats.append(_compute_stats(label, session_id, results))
    return stats


def aggregate_combined_stats(conn: sqlite3.Connection) -> AggregateStats:
    """One AggregateStats across every hand in the database, in hand_id (insertion) order."""
    results = [
        row[0]
        for row in conn.execute("SELECT result_bb FROM hands WHERE result_bb IS NOT NULL ORDER BY hand_id")
    ]
    return _compute_stats("All sessions", None, results)


def aggregate_all(db_path: str) -> dict:
    """Convenience entry point: per-session stats plus the combined row, from a db path."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            "sessions": aggregate_session_stats(conn),
            "combined": aggregate_combined_stats(conn),
        }
    finally:
        conn.close()


def _format_stats_block(stats: AggregateStats) -> str:
    return (
        f"  Hands:              {stats.total_hands}\n"
        f"  Total result:       {stats.total_result_bb:+.2f} bb\n"
        f"  Win rate:           {stats.win_rate_bb_per_100:+.2f} bb/100\n"
        f"  Variance:           {stats.variance_bb:.2f} bb^2\n"
        f"  Std dev:            {stats.std_dev_bb:.2f} bb\n"
        f"  Biggest upswing:    {stats.biggest_upswing_bb:+.2f} bb\n"
        f"  Biggest downswing:  {stats.biggest_downswing_bb:+.2f} bb"
    )


def print_summary(db_path: str = "poker_hands.db") -> None:
    """Print a plain-text summary of per-session and combined stats to stdout."""
    result = aggregate_all(db_path)
    sessions = result["sessions"]
    combined = result["combined"]

    print("Poker Hand Analyzer - Session Stats Summary")
    print("=" * 44)
    print(
        f"\nNOTE: only {combined.total_hands} hand(s) in the database right now. bb/100, "
        "variance, and swing numbers need a much larger sample to mean anything - "
        "these are here to sanity-check the calculation pipeline is correct, not to "
        "make a read on actual win rate yet.\n"
    )

    print("Per session:")
    print("-" * 12)
    for stats in sessions:
        print(f"\n{stats.label}")
        print(_format_stats_block(stats))

    print("\nCombined (all sessions):")
    print("-" * 24)
    print(_format_stats_block(combined))


if __name__ == "__main__":
    print_summary()
