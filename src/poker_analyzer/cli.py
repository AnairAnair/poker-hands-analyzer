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
from poker_analyzer.stats.aggregator import print_summary
from poker_analyzer.validation.validator import validate_hand_log_csv

DEFAULT_DB_PATH = "poker_hands.db"

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
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database"),
    buy_in_cents: int = typer.Option(
        0, "--buy-in-cents", help="Buy-in in cents to use for any NEW session created during this run"
    ),
) -> None:
    """Validate then ingest a hand-log CSV into the SQLite database."""
    try:
        summary = load_hand_log_csv(csv_path, db, buy_in_cents)
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
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database"),
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

    return (
        f"{action:<16} flag: {decision.flag:<8} "
        f"equity: {decision.hero_equity_pct:5.1f}%  "
        f"EV(action): {decision.ev_action_bb:+.2f}bb  "
        f"EV(fold): {decision.ev_fold_bb:+.2f}bb  "
        f"diff: {decision.ev_diff_bb:+.2f}bb"
    )


@app.command("ev-report")
def ev_report(
    db: str = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database"),
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
