"""
Preflop decision-level EV engine.

For every preflop action hero took (fold / check / call / bet / raise), this module:

1. Reconstructs the pot and the amount hero had to put in, from the ordered preflop
   action sequence. The `actions` table intentionally leaves `pot_before_bb` NULL (see
   `ingestion/loader.py`'s module docstring - it calls this out as "an EV-engine-phase
   job"), so this is where that reconstruction actually happens. Blinds themselves
   aren't logged as actions, so the pot starts from the standard SB=0.5bb/BB=1.0bb
   posts implied by the bb-denominated stakes convention this project already uses.

2. Assigns the range of the single most recent *other* player to act before hero (the
   player whose action hero is actually reacting to), using the Chen-formula percentile
   bands from `ev/ranges.py`. If hero is the first to voluntarily act (e.g. an UTG open),
   there's no specific opponent yet to react to, so a generic `DEFAULT_FIELD_BAND` is
   used as a stand-in for "whoever ends up contesting the pot." This is heads-up-vs-range
   throughout, not full multiway equity - the equity calculator itself is heads-up only
   (see its module docstring), and modeling exact multiway range interaction is out of
   scope for this pass.

3. Computes hero's equity against that range (`calculate_equity`, averaged across the
   range's combos) and a *static* EV for the action hero took: assumes the hand simply
   gets shown down at current equity with no further betting. This deliberately ignores
   fold equity (a raise's real value from opponents folding) and any postflop betting -
   both are real simplifications, but postflop EV modeling is explicitly out of scope for
   this session, and fold-equity modeling needs opponent continuance frequencies this
   project doesn't have yet. The number produced is best read as "EV assuming a check
   to showdown from here," which is a reasonable first-pass signal, not a full solver.

4. Compares that EV against EV(fold) = 0 (the cost-free baseline - whatever's already in
   the pot is sunk) and flags the decision +EV / -EV / marginal using a fixed threshold.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from poker_analyzer.equity.calculator import calculate_equity
from poker_analyzer.ev.ranges import range_combos_for_band

# Opponent's range on an OPENING raise (the street's first bet/raise), by their position.
# Tighter in early position, wider on the button/blinds - standard shape. BB's number is
# for isolation-style raises over limpers, since BB can't "open" preflop in the usual
# sense (action reaches BB unopened only as a walk, which isn't a raise).
OPEN_RAISE_TOP_PCT: dict[str, float] = {
    "UTG": 10,
    "UTG+1": 12,
    "MP": 14,
    "HJ": 17,
    "CO": 20,
    "BTN": 30,
    "SB": 35,
    "BB": 20,
}

# A 3-bet-or-later range is narrow regardless of position - simplified to one fixed
# number rather than a position table, since re-raise ranges compress a lot more by
# "did they already show aggression" than by seat.
REREAISE_TOP_PCT = 7

# Limping (calling with no raise yet on the street) can be very wide, especially at
# these stakes/home-game context - modeled as a broad band rather than a tight top-X%.
LIMP_BAND = (0, 55)

# Cold-calling a single raise: excludes both the top end (those hands would reraise
# instead) and the bottom end (those hands would fold), leaving a "flatting" slice.
COLD_CALL_BAND = (12, 42)

# Calling a 3-bet+: narrower than a cold call of an open, since it takes more strength
# to continue against shown aggression.
CALL_VS_REREAISE_BAND = (7, 20)

# Stand-in range for "no specific opponent yet" (e.g. hero is opening first-in). Represents
# a generic composite field rather than any one player's actual range.
DEFAULT_FIELD_BAND = (0, 45)

# Decisions within this many bb of EV(fold) are flagged "marginal" rather than +EV/-EV -
# the model has enough built-in simplification that a razor-thin edge isn't meaningful.
MARGINAL_THRESHOLD_BB = 1.0

SB_POST_BB = 0.5
BB_POST_BB = 1.0

DEFAULT_TRIALS_PER_COMBO = 300


class EVEngineError(Exception):
    pass


@dataclass(frozen=True)
class PreflopDecision:
    hand_id: int
    session_id: int
    hero_position: str
    action_type: str
    amount_bb: float | None
    pot_before_bb: float
    cost_bb: float
    opponent_range_band: tuple[float, float] | None
    opponent_range_combo_count: int | None
    hero_equity_pct: float | None
    ev_action_bb: float | None
    ev_fold_bb: float
    ev_diff_bb: float | None
    flag: str  # "+EV" | "-EV" | "marginal" | "baseline" (baseline = hero folded)


def equity_vs_range(
    hero_hole_cards: str,
    villain_range_combos: list[str],
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> float:
    """Average hero's preflop equity across every combo in an opponent's range."""
    if not villain_range_combos:
        raise EVEngineError("Opponent range is empty - can't compute equity against it")

    total = 0.0
    for index, combo in enumerate(villain_range_combos):
        combo_seed = seed + index if seed is not None else None
        result = calculate_equity(hero_hole_cards, combo, trials=trials_per_combo, seed=combo_seed)
        total += result.hero_equity
    return total / len(villain_range_combos)


def _opponent_band_for(last_actor: dict | None) -> tuple[float, float]:
    if last_actor is None:
        return DEFAULT_FIELD_BAND

    if last_actor["action"] == "raise":
        if last_actor["is_reraise"]:
            return (0, REREAISE_TOP_PCT)
        return (0, OPEN_RAISE_TOP_PCT.get(last_actor["position"], 20))

    # action == "call"
    raises_faced = last_actor["raises_faced"]
    if raises_faced == 0:
        return LIMP_BAND
    if raises_faced == 1:
        return COLD_CALL_BAND
    return CALL_VS_REREAISE_BAND


def _reconstruct_preflop_decisions(preflop_actions: list[dict], hero_position: str) -> list[dict]:
    """
    Replay a hand's ordered preflop actions, tracking the pot and each seat's
    contribution, and collect one raw decision record per hero action.
    """
    contributions: dict[str, float] = {"SB": SB_POST_BB, "BB": BB_POST_BB}
    current_bet = BB_POST_BB
    num_raises = 0
    last_actor: dict | None = None
    decisions = []

    for action in preflop_actions:
        position = action["actor_position"]
        action_type = action["action_type"]
        amount = action["amount_bb"]
        pot_before = sum(contributions.values())

        if position == hero_position:
            hero_already_in = contributions.get(hero_position, 0.0)
            if action_type == "fold" or action_type == "check":
                cost = 0.0
            elif action_type == "call":
                cost = current_bet - hero_already_in
            else:  # bet / raise
                cost = amount - hero_already_in

            decisions.append(
                {
                    "action_type": action_type,
                    "amount_bb": amount,
                    "pot_before_bb": pot_before,
                    "cost_bb": cost,
                    "opponent_band": _opponent_band_for(last_actor),
                }
            )

        if action_type in ("fold", "check"):
            pass
        elif action_type == "call":
            last_actor = {"position": position, "action": "call", "raises_faced": num_raises}
            contributions[position] = current_bet
        else:  # bet / raise
            num_raises += 1
            last_actor = {"position": position, "action": "raise", "is_reraise": num_raises > 1}
            current_bet = amount
            contributions[position] = amount

    return decisions


def analyze_hand_preflop(
    conn: sqlite3.Connection,
    hand_id: int,
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> list[PreflopDecision]:
    """Compute a PreflopDecision for every preflop action hero took in the given hand."""
    hand_row = conn.execute(
        "SELECT session_id, hero_position, hero_hole_cards FROM hands WHERE hand_id = ?",
        (hand_id,),
    ).fetchone()
    if hand_row is None:
        raise EVEngineError(f"No hand with hand_id={hand_id}")
    session_id, hero_position, hero_hole_cards = hand_row

    action_rows = conn.execute(
        """
        SELECT actor_position, action_type, amount_bb
        FROM actions
        WHERE hand_id = ? AND street = 'preflop'
        ORDER BY action_order
        """,
        (hand_id,),
    ).fetchall()
    preflop_actions = [
        {"actor_position": row[0], "action_type": row[1], "amount_bb": row[2]} for row in action_rows
    ]

    raw_decisions = _reconstruct_preflop_decisions(preflop_actions, hero_position)

    results = []
    for raw in raw_decisions:
        if raw["action_type"] == "fold":
            results.append(
                PreflopDecision(
                    hand_id=hand_id,
                    session_id=session_id,
                    hero_position=hero_position,
                    action_type="fold",
                    amount_bb=None,
                    pot_before_bb=raw["pot_before_bb"],
                    cost_bb=0.0,
                    opponent_range_band=None,
                    opponent_range_combo_count=None,
                    hero_equity_pct=None,
                    ev_action_bb=0.0,
                    ev_fold_bb=0.0,
                    ev_diff_bb=0.0,
                    flag="baseline",
                )
            )
            continue

        band = raw["opponent_band"]
        villain_combos = range_combos_for_band(band[0], band[1], dead_cards=hero_hole_cards)
        hero_equity_pct = equity_vs_range(
            hero_hole_cards, villain_combos, trials_per_combo=trials_per_combo, seed=seed
        )

        cost = raw["cost_bb"]
        pot_after = raw["pot_before_bb"] + cost
        ev_action = (hero_equity_pct / 100.0) * pot_after - cost
        ev_fold = 0.0
        diff = ev_action - ev_fold

        if abs(diff) < MARGINAL_THRESHOLD_BB:
            flag = "marginal"
        elif diff > 0:
            flag = "+EV"
        else:
            flag = "-EV"

        results.append(
            PreflopDecision(
                hand_id=hand_id,
                session_id=session_id,
                hero_position=hero_position,
                action_type=raw["action_type"],
                amount_bb=raw["amount_bb"],
                pot_before_bb=raw["pot_before_bb"],
                cost_bb=cost,
                opponent_range_band=band,
                opponent_range_combo_count=len(villain_combos),
                hero_equity_pct=hero_equity_pct,
                ev_action_bb=ev_action,
                ev_fold_bb=ev_fold,
                ev_diff_bb=diff,
                flag=flag,
            )
        )

    return results


def analyze_all_preflop_decisions(
    db_path: str,
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> list[PreflopDecision]:
    """Run analyze_hand_preflop over every hand currently in the database."""
    conn = sqlite3.connect(db_path)
    try:
        hand_ids = [row[0] for row in conn.execute("SELECT hand_id FROM hands ORDER BY hand_id")]
        decisions = []
        for hand_id in hand_ids:
            decisions.extend(
                analyze_hand_preflop(conn, hand_id, trials_per_combo=trials_per_combo, seed=seed)
            )
        return decisions
    finally:
        conn.close()
