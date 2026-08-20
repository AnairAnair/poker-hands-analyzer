"""
Unified CLI entry point for the Poker Hand Analyzer.

Wraps the existing validation / ingestion / stats / EV-engine modules as Typer
subcommands. This module intentionally contains no analysis logic of its own -
each command parses arguments, calls straight into the module that already owns
that logic (validation.validator, ingestion.loader, stats.aggregator, ev.engine),
and formats the result for the terminal. See scripts/poker_cli.py for the
executable wrapper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from poker_analyzer.ev.engine import (
    DEFAULT_TRIALS_PER_COMBO,
    EVEngineError,
    PostflopDecision,
    PreflopDecision,
    analyze_all_postflop_decisions,
    analyze_all_preflop_decisions,
)
from poker_analyzer.ingestion.loader import IngestionError, load_hand_log_csv
from poker_analyzer.leaks.detector import MIN_SAMPLE_SIZE, PatternLeak, detect_leaks
from poker_analyzer.stats.aggregator import print_summary
from poker_analyzer.validation.validator import validate_hand_log_csv

DEFAULT_DB_PATH: str | None = None

app = typer.Typer(
    help="Poker Hand Analyzer: validate hand logs, ingest them, and report stats/EV.",
    no_args_is_help=True,
)


@app.command()
def validate(
    csv_path: Path = typer.Argument(..., help="Path to the hand log CSV to validate"),
) -> None:
    """Validate a hand-log CSV against the project template rules."""
    errors = validate_hand_log_csv(csv_path)
    if errors:
        typer.echo(f"Validation failed for {csv_path}:")
        for error in errors:
            typer.echo(f"- {error}")
        raise typer.Exit(code=1)
    typer.echo(f"Validation passed for {csv_path}")


@app.command()
def ingest(
    csv_path: Path = typer.Argument(..., help="Path to the hand log CSV to ingest"),
    buy_in_cents: int = typer.Option(
        0, "--buy-in-cents", help="Buy-in in cents to use for any NEW session created during this run"
    ),
) -> None:
    """Validate then ingest a hand-log CSV into the Postgres database (SUPABASE_DB_URL)."""
    try:
        summary = load_hand_log_csv(csv_path, buy_in_cents_default=buy_in_cents)
    except IngestionError as exc:
        typer.echo(f"Ingestion failed:\n{exc}")
        raise typer.Exit(code=1)

    typer.echo(
        f"Loaded {summary['hands_loaded']} hand(s), "
        f"skipped {summary['hands_skipped']} already-loaded hand(s), "
        f"created {summary['sessions_created']} new session(s)."
    )


@app.command()
def stats(
    db: Optional[str] = typer.Option(
        DEFAULT_DB_PATH,
        "--db",
        help="Path to a local SQLite database file. Omit to read from Postgres (SUPABASE_DB_URL) instead.",
    ),
) -> None:
    """Print per-session and combined win rate / variance / swing stats."""
    print_summary(db)


def _format_decision(decision: PreflopDecision | PostflopDecision, street: str | None = None) -> str:
    action = decision.action_type
    if decision.amount_bb is not None:
        action = f"{action} {decision.amount_bb:.2f}bb"
    if street is not None:
        action = f"{street}: {action}"

    if decision.flag == "baseline":
        return f"{action:<16} flag: baseline (fold, no EV computed)"

    # fold_pct is only set for a bet/raise (the only actions with fold equity) -
    # surfaced here so a result like "0.0% equity, +0.00bb EV" on a bet is legible
    # as "villain folds fold_pct% of the time, and a bet with 0% continuing equity
    # is exactly breakeven against a defender playing MDF" rather than looking like
    # nothing was computed. See ev/engine.py's "Fold equity" / "Postflop fold
    # equity" docstring sections for why a 0%-equity bet nets exactly 0bb EV.
    fold_pct_str = f"  fold_pct: {decision.fold_pct * 100:4.1f}%" if decision.fold_pct is not None else ""

    return (
        f"{action:<16} flag: {decision.flag:<8} "
        f"equity: {decision.hero_equity_pct:5.1f}%{fold_pct_str}  "
        f"EV(action): {decision.ev_action_bb:+.2f}bb  "
        f"EV(fold): {decision.ev_fold_bb:+.2f}bb  "
        f"diff: {decision.ev_diff_bb:+.2f}bb"
    )


@app.command("ev-report")
def ev_report(
    db: Optional[str] = typer.Option(
        DEFAULT_DB_PATH,
        "--db",
        help="Path to a local SQLite database file. Omit to read from Postgres (SUPABASE_DB_URL) instead.",
    ),
    trials: int = typer.Option(
        DEFAULT_TRIALS_PER_COMBO, "--trials", help="Equity simulation trials per opponent range combo"
    ),
    seed: Optional[int] = typer.Option(
        None, "--seed", help="Random seed for reproducible equity simulation"
    ),
) -> None:
    """Print hero's preflop and postflop decisions: position, action, EV flag, and the equity/EV behind it."""
    try:
        preflop_decisions = analyze_all_preflop_decisions(db, trials_per_combo=trials, seed=seed)
        postflop_decisions = analyze_all_postflop_decisions(db, trials_per_combo=trials, seed=seed)
    except EVEngineError as exc:
        typer.echo(f"EV report failed:\n{exc}")
        raise typer.Exit(code=1)

    if not preflop_decisions:
        typer.echo("No preflop decisions found - is the database empty?")
        return

    typer.echo("Poker Hand Analyzer - EV Report")
    typer.echo("=" * 44)

    # Postflop decisions only exist for hands that reached the flop, so they're
    # bucketed by hand_id and printed after that hand's preflop lines rather than
    # interleaved with analyze_all_preflop_decisions' own ordering.
    postflop_by_hand: dict[int, list[PostflopDecision]] = {}
    for decision in postflop_decisions:
        postflop_by_hand.setdefault(decision.hand_id, []).append(decision)

    current_hand_id: int | None = None
    for decision in preflop_decisions:
        if decision.hand_id != current_hand_id:
            if current_hand_id is not None:
                for postflop_decision in postflop_by_hand.get(current_hand_id, []):
                    typer.echo(f"  {_format_decision(postflop_decision, street=postflop_decision.street)}")
            current_hand_id = decision.hand_id
            typer.echo(f"\nHand {decision.hand_id} (session {decision.session_id}) - hero: {decision.hero_position}")
        typer.echo(f"  {_format_decision(decision)}")

    if current_hand_id is not None:
        for postflop_decision in postflop_by_hand.get(current_hand_id, []):
            typer.echo(f"  {_format_decision(postflop_decision, street=postflop_decision.street)}")


def _format_pattern_leak(pattern: PatternLeak) -> str:
    occurrence_word = "occurrence" if pattern.occurrences == 1 else "occurrences"
    occurrence_str = f"{pattern.occurrences} {occurrence_word}"
    line = f"{pattern.label:<22} {occurrence_str:<13} avg diff: {pattern.avg_ev_diff_bb:+.2f}bb"
    if pattern.verdict != "insufficient_data":
        line += (
            f"   (-EV: {pattern.negative_ev_count}  marginal: {pattern.marginal_count}  "
            f"+EV: {pattern.positive_ev_count})"
        )
    hands = ", ".join(str(hand_id) for hand_id in pattern.hand_ids)
    return f"{line}   hands: {hands}"


_LEAK_SECTIONS = [
    ("leak", "Leaks (-EV across >= {n} occurrences)"),
    ("marginal", "Marginal patterns (>= {n} occurrences)"),
    ("fine", "Fine patterns (+EV, >= {n} occurrences)"),
    ("insufficient_data", "Insufficient data (< {n} occurrences - not a leak call yet)"),
]


@app.command()
def leaks(
    db: Optional[str] = typer.Option(
        DEFAULT_DB_PATH,
        "--db",
        help="Path to a local SQLite database file. Omit to read from Postgres (SUPABASE_DB_URL) instead.",
    ),
    trials: int = typer.Option(
        DEFAULT_TRIALS_PER_COMBO, "--trials", help="Equity simulation trials per opponent range combo"
    ),
    seed: Optional[int] = typer.Option(
        None, "--seed", help="Random seed for reproducible equity simulation"
    ),
    min_sample: int = typer.Option(
        MIN_SAMPLE_SIZE,
        "--min-sample",
        help="Minimum occurrences of a (position, action) pattern before it's reported as a real leak instead of insufficient data",
    ),
) -> None:
    """Group hero's EV-flagged decisions by (position, action) pattern and surface which patterns skew -EV or marginal across multiple hands."""
    try:
        preflop_decisions = analyze_all_preflop_decisions(db, trials_per_combo=trials, seed=seed)
        postflop_decisions = analyze_all_postflop_decisions(db, trials_per_combo=trials, seed=seed)
    except EVEngineError as exc:
        typer.echo(f"Leak detection failed:\n{exc}")
        raise typer.Exit(code=1)

    if not preflop_decisions:
        typer.echo("No decisions found - is the database empty?")
        return

    patterns = detect_leaks(preflop_decisions, postflop_decisions, min_sample_size=min_sample)
    total_hands = len({decision.hand_id for decision in preflop_decisions})

    typer.echo("Poker Hand Analyzer - Leak Report")
    typer.echo("=" * 44)
    typer.echo(
        f"\nNOTE: a pattern (same position + action type, preflop or by street) needs at "
        f"least {min_sample} occurrence(s) before its EV skew is reported as a real leak "
        f"rather than \"insufficient data\" - with only {total_hands} hand(s) logged, most "
        "patterns won't clear that bar yet. Folds are excluded (no EV signal - see "
        "leaks/detector.py).\n"
    )

    for verdict, title_template in _LEAK_SECTIONS:
        title = title_template.format(n=min_sample)
        typer.echo(f"\n{title}:")
        typer.echo("-" * len(title))
        group = [pattern for pattern in patterns if pattern.verdict == verdict]
        if not group:
            typer.echo("  (none)")
            continue
        for pattern in group:
            typer.echo(f"  {_format_pattern_leak(pattern)}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
