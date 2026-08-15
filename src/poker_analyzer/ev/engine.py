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

3. When hero's action is a BET or RAISE, splits the EV into two mutually exclusive
   branches instead of assuming a check to showdown: villain folds (hero wins the pot
   that existed before hero's bet, uncontested) or villain continues (hero's existing
   equity-vs-range showdown math applies, but now against only the sub-range that
   would actually continue). See "Fold equity" below for exactly how the fold
   probability and the continuing sub-range are derived. For check/call/fold, hero
   isn't the one betting, so there's no one left to fold to hero's action - these keep
   the plain equity-vs-range showdown math from before, unchanged.

4. Compares the resulting EV against EV(fold) = 0 (the cost-free baseline - whatever's
   already in the pot is sunk) and flags the decision +EV / -EV / marginal using a
   fixed threshold.

Fold equity
-----------
When hero bets or raises, villain's own decision is a function of hero's bet size, not
just a fixed range. We estimate villain's fold probability with minimum defense
frequency (MDF), a standard poker-theory result: facing a bet of size B into a pot of
size P, a defender must continue with at least P / (P + B) of their range, or a bettor
could profitably bet 100% of hands (pure bluffs included). We use MDF's complement,
B / (P + B) (`estimate_fold_pct`), as the fold-probability ESTIMATE.

This is a deliberate choice among more than one reasonable option (decided with the
user): the alternative was a static fold-% lookup table keyed by action type/position,
similar in shape to `OPEN_RAISE_TOP_PCT`. MDF was chosen instead because it's derived
from the bet size and pot size already reconstructed for every decision (`cost_bb`,
`pot_before_bb`), so it responds to hero's actual sizing rather than treating a min-raise
and a pot-sized raise the same way, and because it stays inside the same
percentile-band range representation `ev/ranges.py` already uses rather than adding a
disconnected second model.

Known limitation, explicitly: MDF is an equilibrium/GTO benchmark, not a read on any
real villain. It assumes villain defends *exactly* enough to stay unexploitable - real
opponents, especially at the live-cash/informal-game context this project targets,
very often over-fold relative to MDF (or occasionally under-fold). Treat the resulting
fold_pct as a principled first-pass estimate, not a tendency read - a natural next step
would be letting it vary with logged opponent behavior, once this project tracks that.

The villain combos that "fold" are modeled as the weakest slice of villain's
already-assigned range band (`ev/ranges.py`'s bands are ordered strongest-to-weakest
by percentile), so a bet trims the band's weak end down to a narrower CONTINUING band
(`_continuing_band`) before running the same `equity_vs_range` calculation used
everywhere else in this module - fold equity changes how much of the range hero faces
at showdown, not how that showdown equity itself is computed.

Postflop EV
-----------
`analyze_hand_postflop` extends the same shape of analysis - decision-level EV vs.
folding, using equity against an assigned villain range - to hero's flop/turn/river
actions. Two things differ from the preflop path:

1. Range narrowing carries forward street by street instead of being assigned fresh
   per decision. Villain's range enters the flop as whatever band the full preflop
   action sequence implies (via the same `_opponent_band_for` preflop already uses,
   seeded from the last *opponent* action - hero's own closing action, if any,
   doesn't get mistaken for villain's), then narrows further every time an opponent
   bets, raises, or checks on a later street: a bet or raise narrows the band toward
   its strong (low-percentile) end, a check only slightly caps it. This compounds -
   an opponent betting flop and turn narrows twice, once per street - and is applied
   directly to whatever the band currently is, via `_continuing_band` (the same
   mechanic fold equity uses to shrink a band toward strength, reused here for a
   different purpose: narrowing a *villain's* range from *their own* action, not
   splitting hero's bet into fold/continue branches). See `POSTFLOP_BET_NARROW_PCT` /
   `POSTFLOP_RAISE_NARROW_PCT` / `POSTFLOP_CHECK_NARROW_PCT` below.

   A call - from hero or an opponent - never narrows anything; it inherits whatever
   band the last bet/raise/check already set, rather than being assigned a distinct
   caller's range or resetting to what came before. Hero's own bets, raises, and
   checks never narrow the band either: this machinery estimates *villain's* range
   from *villain's* actions, so only non-hero actions feed it.

   This was a real design decision with more than one reasonable approach (decided
   with the user): the alternative was letting the narrowing amount also depend on
   board texture (e.g. a bet on a wet, coordinated board narrowing more than the same
   bet on a dry one). Fixed per-action-type multipliers were chosen instead, for the
   same reason MDF won out over a static fold-% lookup table above: it reuses the
   existing percentile-band machinery directly with no new model to design, tune, or
   test this session. Reading board texture into the narrowing amount is a reasonable
   follow-up, not something this pass rules out.

2. Hero's equity is computed against the narrowed range using the actual board cards
   known at that decision (3 cards on the flop, 4 on the turn, 5 on the river) via
   `equity_vs_range`'s optional `board` parameter, which passes straight through to
   `calculate_equity` - already validated against partial boards (see
   `tests/test_equity_calculator.py`'s flush-draw-vs-overpair flop spot). Board cards
   are also excluded as dead cards when enumerating villain's range combos, alongside
   hero's own hole cards, since villain can't hold a card that's face-up on the board.

Postflop fold equity
---------------------
When hero bets or raises on the flop, turn, or river, the same fold/continue split
used preflop now applies here too: `estimate_fold_pct(cost_bb, pot_before_bb)` (MDF's
complement) estimates villain's fold probability, and the weakest slice of whatever
band the decision would otherwise face is trimmed off via `_continuing_band` before
running the showdown equity math against the narrower continuing range.

This reuses `estimate_fold_pct` and `_continuing_band` completely unchanged from the
preflop path - same formula, called with that street's own `cost_bb`/`pot_before_bb`
(decided with the user): MDF is derived purely from a bet's size relative to the pot
at the moment it's made, so nothing about it is inherently preflop-specific. The
street-by-street range narrowing this module already does (`_reconstruct_postflop_street`)
happens *before* hero's bet is evaluated - it updates what band villain is assigned as
of this decision, reflecting that villain's own prior actions have already tightened
their range. Fold equity then applies on top of that already-current band, exactly the
way it applies to whatever band preflop's own action sequence has assigned by the time
hero bets or raises. The alternative considered was a per-street dampening factor on
top of raw MDF (villains defend more than equilibrium against a late-street bet, since
by then their own range has already been trimmed toward hands that intend to see
showdown) - set aside as a second, untested, un-empirical knob stacked on top of range
narrowing that already does most of that same tightening.

For check/call, hero isn't the one betting, so as before, no fold equity applies -
these keep the plain equity-vs-range showdown math.
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

# Postflop range-narrowing multipliers, fed into `_continuing_band` to shrink the
# incoming band toward its strong (low-percentile) end after an opponent's postflop
# action - see the module docstring's "Postflop EV" section for why fixed per-
# action-type numbers were chosen over reading board texture. A bet/raise narrows
# hard toward strength; a check leaves the range almost untouched (a small cap, since
# a few of the very weakest hands in the band would rather have bet or given up than
# checked, but most of the band could plausibly check).
POSTFLOP_BET_NARROW_PCT = 0.40
POSTFLOP_RAISE_NARROW_PCT = 0.20
POSTFLOP_CHECK_NARROW_PCT = 0.90

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
    # Fold-equity fields - only populated for bet/raise decisions (None otherwise,
    # including for hero's own folds). fold_pct is the MDF-derived estimate of
    # villain's fold probability; continuing_range_band is opponent_range_band
    # narrowed down to the sub-range that continues instead of folding, and is what
    # hero_equity_pct is actually computed against for these decisions.
    fold_pct: float | None = None
    continuing_range_band: tuple[float, float] | None = None


@dataclass(frozen=True)
class PostflopDecision:
    hand_id: int
    session_id: int
    hero_position: str
    street: str  # "flop" | "turn" | "river"
    action_type: str
    amount_bb: float | None
    pot_before_bb: float
    cost_bb: float
    board: str | None  # known board cards at this decision, e.g. "Jh 8h 3c" (None for a fold)
    opponent_range_band: tuple[float, float] | None
    opponent_range_combo_count: int | None
    hero_equity_pct: float | None
    ev_action_bb: float | None
    ev_fold_bb: float
    ev_diff_bb: float | None
    flag: str  # "+EV" | "-EV" | "marginal" | "baseline" (baseline = hero folded)
    # Fold-equity fields - only populated for bet/raise decisions (None otherwise,
    # including for hero's own folds), same convention as PreflopDecision. fold_pct is
    # the MDF-derived estimate of villain's fold probability; continuing_range_band is
    # opponent_range_band narrowed down to the sub-range that continues instead of
    # folding, and is what hero_equity_pct is actually computed against for these
    # decisions. See the module docstring's "Postflop fold equity" section.
    fold_pct: float | None = None
    continuing_range_band: tuple[float, float] | None = None


def equity_vs_range(
    hero_hole_cards: str,
    villain_range_combos: list[str],
    board: str = "",
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> float:
    """
    Average hero's equity across every combo in an opponent's range. `board` is 0
    (preflop), 3 (flop), or 4 (turn) known cards, passed straight through to
    `calculate_equity` - empty by default, so preflop callers are unaffected.

    Passes `range_width=len(villain_range_combos)` through to `calculate_equity` on
    every combo, so a wide postflop range falls back to Monte Carlo sampling instead
    of exact enumeration per combo, the same way a preflop board (with no known
    cards) already always does - see `calculate_equity`'s `range_width` docstring
    and the README's "Postflop equity: Monte Carlo fallback" section.
    """
    if not villain_range_combos:
        raise EVEngineError("Opponent range is empty - can't compute equity against it")

    range_width = len(villain_range_combos)
    total = 0.0
    for index, combo in enumerate(villain_range_combos):
        combo_seed = seed + index if seed is not None else None
        result = calculate_equity(
            hero_hole_cards,
            combo,
            board=board,
            trials=trials_per_combo,
            seed=combo_seed,
            range_width=range_width,
        )
        total += result.hero_equity
    return total / len(villain_range_combos)


def estimate_fold_pct(cost_bb: float, pot_before_bb: float) -> float:
    """
    MDF-derived estimate of villain's fold probability against a bet of `cost_bb`
    into a pot of `pot_before_bb` (i.e. before hero's bet goes in). See the module
    docstring's "Fold equity" section for why MDF's complement is used here.
    """
    return cost_bb / (pot_before_bb + cost_bb)


def _continuing_band(band: tuple[float, float], continue_pct: float) -> tuple[float, float]:
    """
    Narrow `band` (a strongest-to-weakest percentile window) down to the strongest
    `continue_pct` fraction of its own width - the slice of villain's already-assigned
    range that continues against hero's bet rather than folding. continue_pct == 1.0
    (no fold equity, e.g. cost_bb == 0) returns `band` unchanged.
    """
    low, high = band
    return (low, low + (high - low) * continue_pct)


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


def _reconstruct_preflop_decisions(
    preflop_actions: list[dict], hero_position: str
) -> tuple[list[dict], float, dict | None]:
    """
    Replay a hand's ordered preflop actions, tracking the pot and each seat's
    contribution, and collect one raw decision record per hero action.

    Also returns the final pot (bb) and the last actor once preflop betting is
    complete - the starting point `analyze_hand_postflop` carries into the flop
    (see `_opponent_band_for` and the module docstring's "Postflop EV" section).
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
            if position != hero_position:
                last_actor = {"position": position, "action": "call", "raises_faced": num_raises}
            contributions[position] = current_bet
        else:  # bet / raise
            num_raises += 1
            if position != hero_position:
                last_actor = {"position": position, "action": "raise", "is_reraise": num_raises > 1}
            current_bet = amount
            contributions[position] = amount

    return decisions, sum(contributions.values()), last_actor


def _reconstruct_postflop_street(
    street: str,
    street_actions: list[dict],
    hero_position: str,
    incoming_band: tuple[float, float],
    incoming_pot_bb: float,
) -> tuple[list[dict], float, tuple[float, float]]:
    """
    Replay one postflop street's ordered actions, tracking the pot (continuing from
    `incoming_pot_bb`) and villain's range band (continuing from `incoming_band`),
    and collect one raw decision record per hero action.

    The band narrows every time an OPPONENT bets, raises, or checks - using the fixed
    per-action-type multipliers above, applied to whatever the band currently is (so
    back-to-back opponent aggression within the same street compounds). A call never
    narrows anything, from either hero or an opponent - it inherits whatever the last
    bet/raise/check already set the band to, rather than resetting it. Hero's own
    actions never narrow the band either: this is villain's range being estimated
    from villain's actions, not hero's. See the module docstring's "Postflop EV"
    section for the full reasoning.

    Returns (decisions, pot_after_street, band_after_street) so the next street
    picks up exactly where this one left off.
    """
    contributions: dict[str, float] = {}
    current_bet = 0.0
    band = incoming_band
    decisions = []

    for action in street_actions:
        position = action["actor_position"]
        action_type = action["action_type"]
        amount = action["amount_bb"]
        pot_before = incoming_pot_bb + sum(contributions.values())

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
                    "street": street,
                    "action_type": action_type,
                    "amount_bb": amount,
                    "pot_before_bb": pot_before,
                    "cost_bb": cost,
                    "opponent_band": band,
                }
            )

        is_opponent = position != hero_position
        if action_type == "fold":
            pass
        elif action_type == "call":
            contributions[position] = current_bet
        elif action_type == "check":
            if is_opponent:
                band = _continuing_band(band, POSTFLOP_CHECK_NARROW_PCT)
        else:  # bet / raise
            current_bet = amount
            contributions[position] = amount
            if is_opponent:
                narrow_pct = POSTFLOP_BET_NARROW_PCT if action_type == "bet" else POSTFLOP_RAISE_NARROW_PCT
                band = _continuing_band(band, narrow_pct)

    pot_after = incoming_pot_bb + sum(contributions.values())
    return decisions, pot_after, band


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

    raw_decisions, _final_pot_bb, _final_last_actor = _reconstruct_preflop_decisions(
        preflop_actions, hero_position
    )

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
        cost = raw["cost_bb"]
        pot_before = raw["pot_before_bb"]
        pot_after = pot_before + cost

        has_fold_equity = raw["action_type"] in ("bet", "raise")
        if has_fold_equity:
            fold_pct = estimate_fold_pct(cost, pot_before)
            continue_pct = 1.0 - fold_pct
            equity_band = _continuing_band(band, continue_pct)
        else:
            fold_pct = None
            equity_band = band

        villain_combos = range_combos_for_band(
            equity_band[0], equity_band[1], dead_cards=hero_hole_cards
        )
        hero_equity_pct = equity_vs_range(
            hero_hole_cards, villain_combos, trials_per_combo=trials_per_combo, seed=seed
        )

        if has_fold_equity:
            showdown_ev = (hero_equity_pct / 100.0) * pot_after - cost
            ev_action = fold_pct * pot_before + continue_pct * showdown_ev
        else:
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
                pot_before_bb=pot_before,
                cost_bb=cost,
                opponent_range_band=band,
                opponent_range_combo_count=len(villain_combos),
                hero_equity_pct=hero_equity_pct,
                ev_action_bb=ev_action,
                ev_fold_bb=ev_fold,
                ev_diff_bb=diff,
                flag=flag,
                fold_pct=fold_pct,
                continuing_range_band=equity_band if has_fold_equity else None,
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


def _board_through(
    board_flop: str | None, board_turn: str | None, board_river: str | None, street: str
) -> str | None:
    """
    The known board cards as of `street`, or None if that street was never dealt
    (the hand ended earlier). e.g. street="turn" -> "Jh 8h 3c 2d" if all three of
    board_flop/board_turn are set, else None.
    """
    if not board_flop:
        return None
    if street == "flop":
        return board_flop
    if not board_turn:
        return None
    if street == "turn":
        return f"{board_flop} {board_turn}"
    if not board_river:
        return None
    return f"{board_flop} {board_turn} {board_river}"


def analyze_hand_postflop(
    conn: sqlite3.Connection,
    hand_id: int,
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> list[PostflopDecision]:
    """
    Compute a PostflopDecision for every flop/turn/river action hero took in the
    given hand. Returns an empty list for a hand that ended preflop (no flop dealt).
    """
    hand_row = conn.execute(
        """
        SELECT session_id, hero_position, hero_hole_cards, board_flop, board_turn, board_river
        FROM hands WHERE hand_id = ?
        """,
        (hand_id,),
    ).fetchone()
    if hand_row is None:
        raise EVEngineError(f"No hand with hand_id={hand_id}")
    session_id, hero_position, hero_hole_cards, board_flop, board_turn, board_river = hand_row

    preflop_action_rows = conn.execute(
        """
        SELECT actor_position, action_type, amount_bb
        FROM actions
        WHERE hand_id = ? AND street = 'preflop'
        ORDER BY action_order
        """,
        (hand_id,),
    ).fetchall()
    preflop_actions = [
        {"actor_position": row[0], "action_type": row[1], "amount_bb": row[2]}
        for row in preflop_action_rows
    ]
    _preflop_decisions, pot_bb, last_actor = _reconstruct_preflop_decisions(preflop_actions, hero_position)
    band = _opponent_band_for(last_actor)

    results: list[PostflopDecision] = []
    for street in ("flop", "turn", "river"):
        board = _board_through(board_flop, board_turn, board_river, street)
        if board is None:
            break  # hand ended before this street was dealt

        action_rows = conn.execute(
            """
            SELECT actor_position, action_type, amount_bb
            FROM actions
            WHERE hand_id = ? AND street = ?
            ORDER BY action_order
            """,
            (hand_id, street),
        ).fetchall()
        street_actions = [
            {"actor_position": row[0], "action_type": row[1], "amount_bb": row[2]} for row in action_rows
        ]

        raw_decisions, pot_bb, band = _reconstruct_postflop_street(
            street, street_actions, hero_position, band, pot_bb
        )

        for raw in raw_decisions:
            if raw["action_type"] == "fold":
                results.append(
                    PostflopDecision(
                        hand_id=hand_id,
                        session_id=session_id,
                        hero_position=hero_position,
                        street=street,
                        action_type="fold",
                        amount_bb=None,
                        pot_before_bb=raw["pot_before_bb"],
                        cost_bb=0.0,
                        board=board,
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

            decision_band = raw["opponent_band"]
            cost = raw["cost_bb"]
            pot_before = raw["pot_before_bb"]
            pot_after = pot_before + cost

            has_fold_equity = raw["action_type"] in ("bet", "raise")
            if has_fold_equity:
                fold_pct = estimate_fold_pct(cost, pot_before)
                continue_pct = 1.0 - fold_pct
                equity_band = _continuing_band(decision_band, continue_pct)
            else:
                fold_pct = None
                equity_band = decision_band

            villain_combos = range_combos_for_band(
                equity_band[0], equity_band[1], dead_cards=f"{hero_hole_cards} {board}"
            )
            hero_equity_pct = equity_vs_range(
                hero_hole_cards,
                villain_combos,
                board=board,
                trials_per_combo=trials_per_combo,
                seed=seed,
            )

            if has_fold_equity:
                showdown_ev = (hero_equity_pct / 100.0) * pot_after - cost
                ev_action = fold_pct * pot_before + continue_pct * showdown_ev
            else:
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
                PostflopDecision(
                    hand_id=hand_id,
                    session_id=session_id,
                    hero_position=hero_position,
                    street=street,
                    action_type=raw["action_type"],
                    amount_bb=raw["amount_bb"],
                    pot_before_bb=pot_before,
                    cost_bb=cost,
                    board=board,
                    opponent_range_band=decision_band,
                    opponent_range_combo_count=len(villain_combos),
                    hero_equity_pct=hero_equity_pct,
                    ev_action_bb=ev_action,
                    ev_fold_bb=ev_fold,
                    ev_diff_bb=diff,
                    flag=flag,
                    fold_pct=fold_pct,
                    continuing_range_band=equity_band if has_fold_equity else None,
                )
            )

    return results


def analyze_all_postflop_decisions(
    db_path: str,
    trials_per_combo: int = DEFAULT_TRIALS_PER_COMBO,
    seed: int | None = None,
) -> list[PostflopDecision]:
    """Run analyze_hand_postflop over every hand currently in the database."""
    conn = sqlite3.connect(db_path)
    try:
        hand_ids = [row[0] for row in conn.execute("SELECT hand_id FROM hands ORDER BY hand_id")]
        decisions = []
        for hand_id in hand_ids:
            decisions.extend(
                analyze_hand_postflop(conn, hand_id, trials_per_combo=trials_per_combo, seed=seed)
            )
        return decisions
    finally:
        conn.close()
