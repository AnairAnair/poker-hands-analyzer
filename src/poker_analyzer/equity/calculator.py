"""
Heads-up equity calculator built on top of the `treys` hand-evaluation library.

`treys` (https://github.com/ihendley/treys) evaluates the strength of a 5-7 card
poker hand very quickly, but it doesn't compute equity by itself - equity requires
running that evaluator across every possible way the remaining board cards could
come out. This module does that:

- When the number of possible runouts is small (any flop, turn, or complete/river
  board - a river board has exactly one "runout", the empty one, so it's a direct
  hand comparison), it enumerates every remaining combination exhaustively and
  returns an exact answer.
- When it's large (a 0-card preflop board needs a full 5-card runout, ~1.7M
  combinations), exhaustive enumeration is too slow to be practical, so it falls
  back to Monte Carlo sampling instead.

This only handles a single hero hand vs. a single villain hand (heads-up), which
matches the spec's v1 scope (no multiway equity, no opponent ranges yet).
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from math import comb
from typing import Sequence, Union

from treys import Card, Deck, Evaluator

_evaluator = Evaluator()

CardsArg = Union[str, Sequence[str]]

# Above this many possible board runouts, exhaustive enumeration is abandoned in
# favor of Monte Carlo sampling. A flop board (2 cards to come, 4 hole cards known)
# has at most C(46, 2) = 1,035 runouts and a turn board (1 card to come) has at
# most 45 - well under this limit, so both are always evaluated exactly. A preflop
# board (5 cards to come, only 4 hole cards known) has C(48, 5) = 1,712,304
# runouts, which is over the limit, so preflop equity always falls back to Monte
# Carlo.
EXACT_ENUMERATION_LIMIT = 50_000

DEFAULT_MONTE_CARLO_TRIALS = 50_000


@dataclass(frozen=True)
class EquityResult:
    """Result of a heads-up equity calculation. Percentages sum to ~100."""

    hero_equity: float
    villain_equity: float
    tie_equity: float
    trials: int
    method: str  # "exact" or "monte_carlo"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"hero={self.hero_equity:.2f}% villain={self.villain_equity:.2f}% "
            f"tie={self.tie_equity:.2f}% ({self.method}, {self.trials:,} runouts)"
        )


def parse_cards(cards: CardsArg) -> list[int]:
    """
    Parse card notation into treys card ints.

    Accepts a space-separated string ("As Kd") or a list of card strings
    (["As", "Kd"]). Card format is <rank><suit>, e.g. "Ah", "Td", "2c" - the
    same notation used in the hand log CSV template.
    """
    tokens = cards.split() if isinstance(cards, str) else list(cards)
    return [Card.new(token) for token in tokens]


def calculate_equity(
    hero: CardsArg,
    villain: CardsArg,
    board: CardsArg = "",
    trials: int = DEFAULT_MONTE_CARLO_TRIALS,
    seed: int | None = None,
) -> EquityResult:
    """
    Calculate heads-up equity between two starting hands.

    hero / villain: exactly two hole cards each, e.g. "As Ad" or ["As", "Ad"].
    board: 0 (preflop), 3 (flop), 4 (turn), or 5 (river/complete board) known cards.
        A 5-card board leaves nothing left to run out, so it's a single direct hand
        comparison rather than an equity distribution - hero_equity/villain_equity
        come back as 0 or 100 (or split 50/50 on a tie), same shape as any other
        board size, just with only one possible "runout" (the empty one).
    trials: Monte Carlo sample size, only used when exhaustive enumeration isn't
        feasible (i.e. only for a 0-card/preflop board). Ignored otherwise.
    seed: RNG seed for Monte Carlo sampling, for reproducible results in tests.
        Ignored when exact enumeration is used.
    """
    hero_cards = parse_cards(hero)
    villain_cards = parse_cards(villain)
    board_cards = parse_cards(board) if board else []

    if len(hero_cards) != 2:
        raise ValueError(f"hero must have exactly 2 hole cards, got {len(hero_cards)}")
    if len(villain_cards) != 2:
        raise ValueError(f"villain must have exactly 2 hole cards, got {len(villain_cards)}")
    if len(board_cards) not in (0, 3, 4, 5):
        raise ValueError(
            f"board must have 0 (preflop), 3 (flop), 4 (turn), or 5 (river) cards, "
            f"got {len(board_cards)}"
        )

    known = set(hero_cards) | set(villain_cards) | set(board_cards)
    if len(known) != len(hero_cards) + len(villain_cards) + len(board_cards):
        raise ValueError("hero, villain, and board must not share any cards")

    remaining = [c for c in Deck.GetFullDeck() if c not in known]
    cards_needed = 5 - len(board_cards)
    num_combos = comb(len(remaining), cards_needed)

    if num_combos <= EXACT_ENUMERATION_LIMIT:
        return _exact_equity(hero_cards, villain_cards, board_cards, remaining, cards_needed)
    return _monte_carlo_equity(
        hero_cards, villain_cards, board_cards, remaining, cards_needed, trials, seed
    )


def _exact_equity(
    hero: list[int],
    villain: list[int],
    board: list[int],
    remaining: list[int],
    cards_needed: int,
) -> EquityResult:
    hero_wins = villain_wins = ties = 0
    total = 0
    for extra in itertools.combinations(remaining, cards_needed):
        full_board = board + list(extra)
        hero_rank = _evaluator.evaluate(full_board, hero)
        villain_rank = _evaluator.evaluate(full_board, villain)
        total += 1
        if hero_rank < villain_rank:  # treys: lower rank int = stronger hand
            hero_wins += 1
        elif villain_rank < hero_rank:
            villain_wins += 1
        else:
            ties += 1

    return EquityResult(
        hero_equity=hero_wins / total * 100,
        villain_equity=villain_wins / total * 100,
        tie_equity=ties / total * 100,
        trials=total,
        method="exact",
    )


def _monte_carlo_equity(
    hero: list[int],
    villain: list[int],
    board: list[int],
    remaining: list[int],
    cards_needed: int,
    trials: int,
    seed: int | None,
) -> EquityResult:
    rng = random.Random(seed)
    hero_wins = villain_wins = ties = 0
    for _ in range(trials):
        extra = rng.sample(remaining, cards_needed)
        full_board = board + extra
        hero_rank = _evaluator.evaluate(full_board, hero)
        villain_rank = _evaluator.evaluate(full_board, villain)
        if hero_rank < villain_rank:
            hero_wins += 1
        elif villain_rank < hero_rank:
            villain_wins += 1
        else:
            ties += 1

    return EquityResult(
        hero_equity=hero_wins / trials * 100,
        villain_equity=villain_wins / trials * 100,
        tie_equity=ties / trials * 100,
        trials=trials,
        method="monte_carlo",
    )
