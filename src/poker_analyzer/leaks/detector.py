"""
Leak detection: groups hero's already-flagged EV decisions (preflop and postflop)
by pattern - same position, same action type, and same street (or "preflop") -
and surfaces which patterns skew -EV or marginal *across multiple hands*, rather
than just looking at one hand's flag in isolation.

This module recomputes nothing. It consumes the `PreflopDecision`/`PostflopDecision`
objects `ev/engine.py` already produces (via `analyze_all_preflop_decisions` /
`analyze_all_postflop_decisions`) and only aggregates their existing `ev_diff_bb`
and `flag` fields by pattern - no equity simulation, range assignment, or EV math
happens here.

Minimum sample size
--------------------
With only 15 hands in the database, most (position, street, action) patterns occur
once or twice - nowhere near enough repetition to call a "leak" rather than one
hand's noise. `MIN_SAMPLE_SIZE` (3, decided with the user after checking the real
database: at 3, two patterns already clear the bar - UTG preflop raises x6 and UTG
turn bets x3 - while the rest correctly stay unclassified; at 2 eleven patterns
would qualify, which is barely more reliable than a single hand; at 4+ almost
nothing would qualify yet, which defeats the point of running this at all) is the
minimum occurrence count a pattern needs before its EV skew is reported as a real
`leak` / `marginal` / `fine` verdict instead of `insufficient_data`.

Under-sampled patterns are never dropped from the results - only their verdict is
withheld - so the report stays honest about what it doesn't know yet instead of
silently omitting thin data. Same "won't mean anything yet" caveat already used in
`stats/aggregator.py`'s module docstring, applied here to per-pattern sample size
instead of overall hand count.

Folds are excluded from grouping entirely. A hero fold's `ev_diff_bb` is always
exactly 0.0 by construction - `ev/engine.py` compares EV(fold) against itself for
a fold decision (see its `flag="baseline"` handling) - so a fold carries no EV
signal to aggregate. This isn't a claim that hero never leaks EV by folding too
much, just that this decision-level engine has no way to measure that (a fold's
opportunity cost against what hero could have done instead is out of scope for
`ev/engine.py` itself, so it's out of scope here too).

Verdict thresholds
-------------------
`_classify` reuses `ev/engine.py`'s own `MARGINAL_THRESHOLD_BB` rather than
introducing a second, disconnected cutoff: a pattern's *average* `ev_diff_bb`
across its occurrences is compared against the same +/-1.0bb band a single
decision's diff is compared against to become `+EV`/`-EV`/`marginal` in the first
place. `leak` = average diff at or below -MARGINAL_THRESHOLD_BB, `marginal` =
average diff inside the band, `fine` = average diff at or above
+MARGINAL_THRESHOLD_BB.
"""

from __future__ import annotations

from dataclasses import dataclass

from poker_analyzer.ev.engine import MARGINAL_THRESHOLD_BB, PostflopDecision, PreflopDecision

MIN_SAMPLE_SIZE = 3

_VERDICT_ORDER = {"leak": 0, "marginal": 1, "fine": 2, "insufficient_data": 3}


@dataclass(frozen=True)
class PatternLeak:
    position: str
    street: str  # "preflop" | "flop" | "turn" | "river"
    action_type: str
    label: str  # e.g. "BB calls" (preflop), "UTG river bets" (postflop)
    occurrences: int
    avg_ev_diff_bb: float
    total_ev_diff_bb: float
    negative_ev_count: int  # occurrences individually flagged "-EV" by the EV engine
    marginal_count: int
    positive_ev_count: int
    hand_ids: tuple[int, ...]  # distinct hands this pattern occurred in, ascending
    verdict: str  # "leak" | "marginal" | "fine" | "insufficient_data"


def _pattern_label(position: str, street: str, action_type: str) -> str:
    if street == "preflop":
        return f"{position} {action_type}s"
    return f"{position} {street} {action_type}s"


def _pattern_key(decision: PreflopDecision | PostflopDecision) -> tuple[str, str, str]:
    street = getattr(decision, "street", "preflop")
    return (decision.hero_position, street, decision.action_type)


def _classify(avg_ev_diff_bb: float, occurrences: int, min_sample_size: int) -> str:
    if occurrences < min_sample_size:
        return "insufficient_data"
    if avg_ev_diff_bb <= -MARGINAL_THRESHOLD_BB:
        return "leak"
    if avg_ev_diff_bb < MARGINAL_THRESHOLD_BB:
        return "marginal"
    return "fine"


def detect_leaks(
    preflop_decisions: list[PreflopDecision],
    postflop_decisions: list[PostflopDecision],
    min_sample_size: int = MIN_SAMPLE_SIZE,
) -> list[PatternLeak]:
    """
    Group hero's non-fold decisions by (position, street, action_type) and
    aggregate each group's `ev_diff_bb`/`flag` (both already computed by
    `ev/engine.py`) into one `PatternLeak`. Patterns with fewer than
    `min_sample_size` occurrences are still returned - with `verdict` set to
    "insufficient_data" rather than a real leak classification, per this
    module's "Minimum sample size" docstring section.

    Returned in display order: "leak" patterns first (worst average diff
    first), then "marginal", then "fine", then "insufficient_data" (most
    occurrences first, since those are closest to becoming meaningful).
    """
    grouped: dict[tuple[str, str, str], list[PreflopDecision | PostflopDecision]] = {}
    for decision in [*preflop_decisions, *postflop_decisions]:
        if decision.flag == "baseline":
            continue  # hero folded - ev_diff_bb is always 0.0, no EV signal to group
        grouped.setdefault(_pattern_key(decision), []).append(decision)

    patterns = []
    for (position, street, action_type), decisions in grouped.items():
        occurrences = len(decisions)
        total_diff = sum(d.ev_diff_bb for d in decisions)
        avg_diff = total_diff / occurrences

        patterns.append(
            PatternLeak(
                position=position,
                street=street,
                action_type=action_type,
                label=_pattern_label(position, street, action_type),
                occurrences=occurrences,
                avg_ev_diff_bb=avg_diff,
                total_ev_diff_bb=total_diff,
                negative_ev_count=sum(1 for d in decisions if d.flag == "-EV"),
                marginal_count=sum(1 for d in decisions if d.flag == "marginal"),
                positive_ev_count=sum(1 for d in decisions if d.flag == "+EV"),
                hand_ids=tuple(sorted({d.hand_id for d in decisions})),
                verdict=_classify(avg_diff, occurrences, min_sample_size),
            )
        )

    patterns.sort(
        key=lambda p: (
            _VERDICT_ORDER[p.verdict],
            p.avg_ev_diff_bb if p.verdict != "insufficient_data" else -p.occurrences,
        )
    )
    return patterns
