"""
Session, per-stakes, and combined win-rate / variance / swing aggregation.

Computes the following, per-session, per-stakes-level, and combined across every
session in the database:

- total_hands: count of hands with a logged result_bb (see the NULL note below).
- total_result_bb: sum of hero's per-hand result_bb.
- win_rate_bb_per_100: the standard poker win-rate convention, (total_result_bb /
  total_hands) * 100 - normalizes a result to "per 100 hands" so it's comparable
  across different sample sizes. With only a small number of hands logged so far,
  this number is still noisy and not statistically meaningful yet - see
  print_summary, which prints that caveat alongside the numbers.
- variance_bb / std_dev_bb: sample variance/standard deviation (n-1 denominator,
  matching Python's stdlib `statistics.variance`) of the per-hand result_bb values.
  Sample rather than population variance, since the hands in the database are a
  sample of hero's play, not the full population of every hand hero will ever play.
  0.0 for a single-hand sample (n-1 = 0, variance is undefined).
- biggest_upswing_bb / biggest_downswing_bb: the classic peak-to-trough / trough-to-
  peak swing metric poker tracking software reports (not just hero's single best or
  worst hand) - computed off the *cumulative* running result across hands, in
  database order (hand_number order within a session; hand_id/insertion order for
  the per-stakes and combined rows). biggest_downswing_bb is <= 0 (how far below the
  running peak the running result ever fell); biggest_upswing_bb is >= 0 (how far
  above the running trough it ever climbed). Within a session hand_id order matches
  chronological play order (hand_number is sequential), but per-stakes/combined
  across sessions it's insertion order, not necessarily calendar order - several of
  the currently-loaded hands have "PLACEHOLDER DATE" notes, so true calendar order
  across sessions isn't reliably known yet. Known simplification, not a bug.

Hands with no result_bb logged yet (NULL - e.g. a hand entered before the session
was fully recorded) are excluded from every stat, including the hand count.

Per-stakes aggregation (aggregate_stakes_stats) exists because bb is a *relative*
unit - a bb at 0.10/0.20 is worth 10x a bb at 0.01/0.02 in real dollars, so blending
sessions across different stakes into one bb/100 or variance number treats those
dollars as equivalent when they aren't. Grouping by the sessions.stakes field (not
per-session, not all sessions at once) gives a win-rate/variance number that's
actually comparable within itself. The all-sessions combined row (still computed by
aggregate_combined_stats, used by aggregate_all/print_summary/the dashboard) still
blends across stakes - kept rather than dropped (a real design decision, decided
with the user) because it matches this project's existing style of surfacing
caveats rather than removing data, and because dropping it would require touching
dashboard/data_prep.py's use of aggregate_all()["combined"], out of scope this
session - but print_summary now labels it explicitly as mixing incomparable bb
units instead of letting it read as a real win-rate number.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class AggregateStats:
    label: str
    session_id: int | None  # None for a per-stakes row or the combined, all-sessions row
    stakes: str | None  # the session's stakes; the grouped stakes level; or None for the combined row
    total_hands: int
    total_result_bb: float
    win_rate_bb_per_100: float
    variance_bb: float
    std_dev_bb: float
    biggest_upswing_bb: float
    biggest_downswing_bb: float


def win_rate_bb_per_100(total_result_bb: float, total_hands: int) -> float:
    """The standard bb/100 win-rate convention - see the module docstring."""
    return (total_result_bb / total_hands) * 100 if total_hands else 0.0


def cumulative_series(results: list[float]) -> list[float]:
    """Running cumulative sum of results, in the given order. One entry per result."""
    cumulative = 0.0
    series = []
    for result in results:
        cumulative += result
        series.append(cumulative)
    return series


def _compute_stats(
    label: str, session_id: int | None, results: list[float], stakes: str | None = None
) -> AggregateStats:
    total_hands = len(results)
    total_result_bb = sum(results)
    variance_bb = statistics.variance(results) if total_hands > 1 else 0.0
    std_dev_bb = variance_bb ** 0.5

    running_peak = 0.0
    running_trough = 0.0
    max_downswing = 0.0
    max_upswing = 0.0
    for cumulative in cumulative_series(results):
        running_peak = max(running_peak, cumulative)
        running_trough = min(running_trough, cumulative)
        max_downswing = max(max_downswing, running_peak - cumulative)
        max_upswing = max(max_upswing, cumulative - running_trough)

    return AggregateStats(
        label=label,
        session_id=session_id,
        stakes=stakes,
        total_hands=total_hands,
        total_result_bb=total_result_bb,
        win_rate_bb_per_100=win_rate_bb_per_100(total_result_bb, total_hands),
        variance_bb=variance_bb,
        std_dev_bb=std_dev_bb,
        biggest_upswing_bb=max_upswing,
        biggest_downswing_bb=-max_downswing,
    )


def session_hand_results(conn: sqlite3.Connection, session_id: int) -> list[tuple[int, int, float]]:
    """(hand_id, hand_number, result_bb) for one session's logged-result hands, hand_number order."""
    return conn.execute(
        """
        SELECT hand_id, hand_number, result_bb FROM hands
        WHERE session_id = ? AND result_bb IS NOT NULL
        ORDER BY hand_number
        """,
        (session_id,),
    ).fetchall()


def combined_hand_results(conn: sqlite3.Connection) -> list[tuple[int, int, float]]:
    """(hand_id, session_id, result_bb) for every logged-result hand, in hand_id (insertion) order."""
    return conn.execute(
        """
        SELECT hand_id, session_id, result_bb FROM hands
        WHERE result_bb IS NOT NULL
        ORDER BY hand_id
        """
    ).fetchall()


def stakes_hand_results(conn: sqlite3.Connection, stakes: str) -> list[tuple[int, int, float]]:
    """(hand_id, session_id, result_bb) for every logged-result hand at this stakes level, hand_id order."""
    return conn.execute(
        """
        SELECT h.hand_id, h.session_id, h.result_bb
        FROM hands h
        JOIN sessions s ON s.session_id = h.session_id
        WHERE s.stakes = ? AND h.result_bb IS NOT NULL
        ORDER BY h.hand_id
        """,
        (stakes,),
    ).fetchall()


def aggregate_session_stats(conn: sqlite3.Connection) -> list[AggregateStats]:
    """One AggregateStats per session, in session_id order (swings use hand_number order)."""
    sessions = conn.execute(
        "SELECT session_id, session_date, location, stakes FROM sessions ORDER BY session_id"
    ).fetchall()

    stats = []
    for session_id, session_date, location, stakes in sessions:
        results = [row[2] for row in session_hand_results(conn, session_id)]
        label = f"{session_date} {location} {stakes}"
        stats.append(_compute_stats(label, session_id, results, stakes=stakes))
    return stats


def aggregate_stakes_stats(conn: sqlite3.Connection) -> list[AggregateStats]:
    """
    One AggregateStats per distinct stakes level (grouping sessions by their
    `stakes` field, not per-session and not all sessions blended together),
    ordered by big blind size ascending. See the module docstring for why this
    exists - bb is not a comparable unit across different real-money stakes.
    """
    stakes_levels = conn.execute(
        "SELECT stakes, COUNT(*) FROM sessions GROUP BY stakes ORDER BY MIN(big_blind_cents)"
    ).fetchall()

    stats = []
    for stakes, session_count in stakes_levels:
        results = [row[2] for row in stakes_hand_results(conn, stakes)]
        session_word = "session" if session_count == 1 else "sessions"
        label = f"{stakes} ({session_count} {session_word})"
        stats.append(_compute_stats(label, None, results, stakes=stakes))
    return stats


def aggregate_combined_stats(conn: sqlite3.Connection) -> AggregateStats:
    """
    One AggregateStats across every hand in the database, in hand_id (insertion)
    order - blends every stakes level together. See the module docstring: kept
    for aggregate_all/the dashboard, but print_summary labels it explicitly as
    mixing incomparable bb units rather than a real win-rate number.
    """
    results = [row[2] for row in combined_hand_results(conn)]
    return _compute_stats("All sessions (mixed stakes)", None, results)


def aggregate_all(db_path: str) -> dict:
    """Convenience entry point: per-session, per-stakes, and combined stats, from a db path."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            "sessions": aggregate_session_stats(conn),
            "by_stakes": aggregate_stakes_stats(conn),
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
    """Print a plain-text summary of per-session, per-stakes, and combined stats to stdout."""
    result = aggregate_all(db_path)
    sessions = result["sessions"]
    by_stakes = result["by_stakes"]
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

    print("\nPer stakes level:")
    print("-" * 17)
    for stats in by_stakes:
        print(f"\n{stats.label}")
        print(_format_stats_block(stats))

    print(
        "\nCombined (all sessions, MIXED STAKES - bb is a relative unit, not a fixed dollar "
        "amount, so this number blends stake levels that can be 10x apart in real dollar "
        "value. Use the per-stakes-level numbers above for an actual win-rate read; this "
        "row is here for total-sample sanity-checking only):"
    )
    print("-" * 24)
    print(_format_stats_block(combined))


if __name__ == "__main__":
    print_summary()
