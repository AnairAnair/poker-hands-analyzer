"""
Opponent preflop range assignment.

Approach (decided with the user over the Chen-formula-vs-static-chart-vs-flat-heuristic
question): score every one of the 169 canonical starting hands with the Chen formula,
then represent "an opponent's range at this decision point" as a percentile band over
those hands, ordered strongest to weakest. A percentile band of (0, 10) means "the top
10% of starting hands by combo-weighted strength"; (12, 42) means "the slice between the
12th and 42nd percentile" (used for a cold-call band, which excludes both the very best
hands - which would reraise instead - and the weakest ones - which would fold).

Why the Chen formula over a curated per-position range chart: this needs a range for
every (position, action-type) combination that shows up in real logged hands - opens,
cold calls, 3-bets, limps, isolations - and a static chart realistically only covers
opening ranges well. The Chen formula gives a single continuous strength ordering that
a percentile cut can be applied to for *any* action type, so one small table of
percentile thresholds (see `PERCENTILE_BANDS` in engine.py) covers every spot instead of
needing a separate hand-curated chart per action type. It's also self-contained: no
external range data to source, license, or keep in sync.

Known limitations, on purpose (this is v1, preflop-only):
- The Chen formula is a simple heuristic, not a solved-game range. It gets the relative
  ordering of hands roughly right (pairs and big aces on top, weak offsuit hands on the
  bottom) but isn't a substitute for a real solver output.
- We do NOT round the raw score the way the traditional "how many points to play"
  advice does. Rounding is meant for a human at the table; here the raw fractional score
  gives finer-grained, more consistent percentile cuts.
- Percentile bands are combo-weighted (a pair is 6 combos, a suited hand 4, an offsuit
  hand 12) so "top 10%" means 10% of the 1,326 actual two-card combos, not 10% of the
  169 canonical hands.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

RANK_ORDER = "23456789TJQKA"  # low to high
SUITS = "shdc"

RANK_VALUE = {
    "A": 10.0,
    "K": 8.0,
    "Q": 7.0,
    "J": 6.0,
    "T": 5.0,
    "9": 4.5,
    "8": 4.0,
    "7": 3.5,
    "6": 3.0,
    "5": 2.5,
    "4": 2.0,
    "3": 1.5,
    "2": 1.0,
}

TOTAL_COMBOS = 1326  # C(52, 2)


@dataclass(frozen=True)
class CanonicalHand:
    label: str  # e.g. "AKs", "AKo", "AA"
    high: str
    low: str
    suited: bool | None  # None for a pocket pair
    combo_count: int
    score: float


def chen_score(high_rank: str, low_rank: str, suited: bool | None) -> float:
    """
    Score a starting hand with the Chen formula. `suited` is None for a pocket pair
    (high_rank == low_rank), True/False otherwise. Higher score = stronger hand.
    """
    if high_rank == low_rank:
        return max(RANK_VALUE[high_rank] * 2, 5.0)

    score = RANK_VALUE[high_rank]
    if suited:
        score += 2

    hi_idx = RANK_ORDER.index(high_rank)
    lo_idx = RANK_ORDER.index(low_rank)
    gap = hi_idx - lo_idx - 1
    if gap == 0:
        pass
    elif gap == 1:
        score -= 1
    elif gap == 2:
        score -= 2
    elif gap == 3:
        score -= 4
    else:
        score -= 5

    # Straight-potential bonus: small gap, and the top card isn't so high that it
    # can only make straights on one end.
    if gap <= 1 and hi_idx < RANK_ORDER.index("Q"):
        score += 1

    return score


def _build_canonical_hands() -> list[CanonicalHand]:
    hands = []
    for hi in range(13):
        for lo in range(hi + 1):
            rank_hi, rank_lo = RANK_ORDER[hi], RANK_ORDER[lo]
            if hi == lo:
                hands.append(
                    CanonicalHand(
                        label=rank_hi + rank_hi,
                        high=rank_hi,
                        low=rank_hi,
                        suited=None,
                        combo_count=6,
                        score=chen_score(rank_hi, rank_hi, None),
                    )
                )
            else:
                hands.append(
                    CanonicalHand(
                        label=rank_hi + rank_lo + "s",
                        high=rank_hi,
                        low=rank_lo,
                        suited=True,
                        combo_count=4,
                        score=chen_score(rank_hi, rank_lo, True),
                    )
                )
                hands.append(
                    CanonicalHand(
                        label=rank_hi + rank_lo + "o",
                        high=rank_hi,
                        low=rank_lo,
                        suited=False,
                        combo_count=12,
                        score=chen_score(rank_hi, rank_lo, False),
                    )
                )
    return hands


_CANONICAL_HANDS = _build_canonical_hands()
assert len(_CANONICAL_HANDS) == 169
assert sum(h.combo_count for h in _CANONICAL_HANDS) == TOTAL_COMBOS

# Sorted strongest to weakest, tie-broken alphabetically for determinism, with each
# hand's [start_pct, end_pct) window over the combo-weighted percentile scale.
_RANKED_HANDS: list[tuple[CanonicalHand, float, float]] = []
_cumulative = 0
for _hand in sorted(_CANONICAL_HANDS, key=lambda h: (-h.score, h.label)):
    start_pct = _cumulative / TOTAL_COMBOS * 100
    _cumulative += _hand.combo_count
    end_pct = _cumulative / TOTAL_COMBOS * 100
    _RANKED_HANDS.append((_hand, start_pct, end_pct))


def expand_canonical_hand(label: str) -> list[str]:
    """'AKs' -> ['As Ks', 'Ah Kh', 'Ad Kd', 'Ac Kc']; 'AA' -> the 6 AA combos."""
    if len(label) == 2:
        rank = label[0]
        return [f"{rank}{s1} {rank}{s2}" for s1, s2 in combinations(SUITS, 2)]

    rank_hi, rank_lo, suited_flag = label[0], label[1], label[2]
    if suited_flag == "s":
        return [f"{rank_hi}{s} {rank_lo}{s}" for s in SUITS]
    return [f"{rank_hi}{s1} {rank_lo}{s2}" for s1 in SUITS for s2 in SUITS if s1 != s2]


def range_combos_for_band(low_pct: float, high_pct: float, dead_cards: str | list[str] = "") -> list[str]:
    """
    All two-card combos whose canonical hand's percentile window overlaps
    [low_pct, high_pct), excluding any combo that shares a card with `dead_cards`
    (e.g. the hero's own hole cards - a villain can't hold a card hero is holding).
    """
    if isinstance(dead_cards, str):
        dead = set(dead_cards.split())
    else:
        dead = set(dead_cards)

    combos: list[str] = []
    for hand, start, end in _RANKED_HANDS:
        if start < high_pct and end > low_pct:
            for combo in expand_canonical_hand(hand.label):
                if not dead.intersection(combo.split()):
                    combos.append(combo)
    return combos
