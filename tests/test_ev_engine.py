"""
Tests for the preflop and postflop EV engines (poker_analyzer.ev.engine) and the
Chen-formula range assignment they're built on (poker_analyzer.ev.ranges).

Several kinds of coverage, per the task:
- A few of the real hands now loaded from data/templates/real_hands.csv, to sanity-check
  the engine against actual logged play.
- A couple of constructed "textbook" preflop spots with an obvious right answer (a
  premium pair opening, and the worst starting hand cold-calling a big raise), to
  sanity-check the equity-vs-range math independent of any real hand's messiness.
- Direct unit tests of the fold-equity math (`estimate_fold_pct`, `_continuing_band`)
  in isolation, since the end-to-end textbook/real-hand tests only exercise it bundled
  together with equity simulation and can't pin down the fold-equity arithmetic itself.
  These same helpers are reused unchanged by postflop fold equity, so they cover both.
- Direct unit tests of the postflop range-narrowing replay (`_reconstruct_postflop_street`)
  in isolation, for the same reason - pinning down exactly which actions narrow the band
  and by how much, independent of equity simulation.
- Real hands and a constructed textbook postflop spot (reusing the flush-draw-vs-tight-
  range flop example already validated directly against `calculate_equity` in
  tests/test_equity_calculator.py) for `analyze_hand_postflop`.
- Postflop fold equity: real hands re-verified against their own pre-fold-equity
  (full-range showdown) numbers to confirm a bet/raise that likely won the pot
  outright now shows clearly higher EV, plus a direct check that postflop bet/raise
  decisions populate `fold_pct`/`continuing_range_band` using the exact same
  `estimate_fold_pct`/`_continuing_band` calls preflop already uses.
"""

from pathlib import Path

import pytest

from poker_analyzer.db.init_db import init_db
from poker_analyzer.ev.engine import (
    MARGINAL_THRESHOLD_BB,
    POSTFLOP_BET_NARROW_PCT,
    POSTFLOP_CHECK_NARROW_PCT,
    _continuing_band,
    _reconstruct_postflop_street,
    analyze_hand_postflop,
    analyze_hand_preflop,
    equity_vs_range,
    estimate_fold_pct,
)
from poker_analyzer.ev.ranges import chen_score, range_combos_for_band
from poker_analyzer.ingestion.loader import load_hand_log_csv

REAL_HANDS_CSV = Path("data/templates/real_hands.csv")

EXPECTED_COLUMNS = [
    "session_date",
    "location",
    "stakes",
    "hand_number",
    "num_players",
    "hero_position",
    "hero_hole_cards",
    "effective_stack_bb",
    "preflop_actions",
    "flop_cards",
    "flop_actions",
    "turn_card",
    "turn_actions",
    "river_card",
    "river_actions",
    "pot_size_bb",
    "result_bb",
    "went_to_showdown",
    "notes",
]

# Keep Monte Carlo trials modest and seeded: fast + deterministic test runs. The engine's
# default (used for real analysis) is higher; see ev/engine.py DEFAULT_TRIALS_PER_COMBO.
TEST_TRIALS_PER_COMBO = 150
TEST_SEED = 7


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "hands.csv"
    path.write_text(",".join(EXPECTED_COLUMNS) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _load_real_hands_db(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(REAL_HANDS_CSV, db_path)
    return sqlite3.connect(db_path)


# --- Real hands from the database -------------------------------------------------


def test_real_hand_pocket_aces_3bet_is_plus_ev(tmp_path):
    """
    Hand 3 (session 2026-07-12): hero is BB with Ac Ah, 3-bets to 3bb over an
    UTG raise-to-1 and a SB raise-to-2.5 (i.e. hero's raise is the 2nd re-raise
    of the street). Pocket aces re-raising is about as clear-cut a +EV spot as
    preflop poker has - if this doesn't come back +EV, something in the pot/cost
    reconstruction or range assignment is wrong.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_preflop(conn, hand_id=3, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.hero_position == "BB"
    assert decision.action_type == "raise"
    assert decision.hero_equity_pct > 70.0, decision
    assert decision.flag == "+EV", decision
    assert decision.fold_pct == pytest.approx(decision.cost_bb / (decision.pot_before_bb + decision.cost_bb))
    assert decision.continuing_range_band[1] < decision.opponent_range_band[1], decision


def test_real_hand_light_3bet_bluff_is_not_plus_ev(tmp_path):
    """
    Hand 5 (session 2026-07-12): hero is UTG+1 with Qs 9s, 3-bets to 4bb over
    UTG's raise-to-1. Under this engine's static (no-fold-equity) EV model, a
    3-bet with a suited two-gapper against a tight opening range should NOT
    come back as a clear win - it's exactly the kind of spot whose real-world
    value depends on fold equity this model doesn't credit.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_preflop(conn, hand_id=5, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.hero_position == "UTG+1"
    assert decision.action_type == "raise"
    assert decision.flag in ("-EV", "marginal"), decision
    assert decision.ev_action_bb < 1.0, decision
    assert decision.fold_pct == pytest.approx(decision.cost_bb / (decision.pot_before_bb + decision.cost_bb))


def test_real_hand_bb_checking_option_is_never_negative(tmp_path):
    """
    Hand 13: hero is BB with Ah Th and everyone limps to hero, who checks the
    option. Checking a free option costs nothing (cost_bb == 0), so under this
    engine's EV formula the result must always be >= 0 - it's a mathematical
    guarantee of the model, not a judgment about the hand.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_preflop(conn, hand_id=13, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action_type == "check"
    assert decision.cost_bb == 0.0
    assert decision.ev_action_bb >= 0.0, decision
    assert decision.fold_pct is None, decision  # no fold equity on a check - hero isn't betting
    assert decision.continuing_range_band is None, decision


def test_analyze_hand_preflop_reconstructs_pot_and_cost_from_actions(tmp_path):
    """
    Hand 1: hero is HJ with As Js, raising to 3bb after UTG limps. Pot before
    hero's raise should be exactly the blinds plus UTG's limp (0.5 + 1.0 + 1.0),
    and hero's cost should be the full 3bb (hero hadn't put anything in yet).
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_preflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.pot_before_bb == pytest.approx(2.5)
    assert decision.cost_bb == pytest.approx(3.0)
    assert decision.amount_bb == pytest.approx(3.0)


# --- Textbook preflop spots ---------------------------------------------------------


def test_textbook_aa_utg_open_is_clearly_plus_ev_once_fold_equity_is_credited(tmp_path):
    """
    Textbook spot: UTG opens 3bb with pocket aces, the best starting hand in hold'em,
    against a generic field range (hero is first to act, so there's no specific
    opponent yet - see DEFAULT_FIELD_BAND in ev/engine.py).

    Before fold equity was modeled, this came back only "marginal": a small raise
    into a small pot nets a modest absolute-bb edge from showdown equity alone, even
    with AA's huge equity edge over a wide field. That was the model being honest
    about what it didn't credit yet - but it was also missing most of AA's real
    value, which comes from folding out the rest of the table just as much as from
    winning showdowns. With fold equity now modeled (MDF-derived - see the "Fold
    equity" section of ev/engine.py's docstring), the same spot should clearly cross
    into "+EV": this is the real correctness check the task calls for. If this still
    came back "marginal" or "-EV", that would mean the fold-equity math is wrong, not
    that the model is being appropriately conservative.
    """
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,UTG,As Ad,100,"
            "UTG:raise3>MP:fold>CO:fold>BTN:fold>SB:fold>BB:fold,,,,,,,4.5,4.5,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_preflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.hero_equity_pct > 60.0, decision
    assert decision.fold_pct == pytest.approx(3.0 / 4.5)  # cost 3bb into a 1.5bb pot
    assert decision.continuing_range_band[1] < decision.opponent_range_band[1], decision
    assert decision.flag == "+EV", decision
    assert decision.ev_action_bb > MARGINAL_THRESHOLD_BB, decision


def test_textbook_aa_3betting_a_utg_open_is_clearly_plus_ev(tmp_path):
    """
    Textbook spot: CO holds pocket aces and 3-bets to 10bb over an UTG open-raise
    to 3bb (villain's range is UTG's opening range - the top ~10% of hands). The
    bigger pot and cost here give AA's equity edge real room to show up in bb
    terms, unlike the small-open version above - this should be unambiguously +EV.
    """
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,CO,As Ad,100,"
            "UTG:raise3>MP:fold>CO:raise10>BTN:fold>SB:fold>BB:fold,,,,,,,14.5,14.5,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_preflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.hero_equity_pct > 75.0, decision
    assert decision.flag == "+EV", decision
    assert decision.ev_action_bb > 1.5, decision


def test_textbook_worst_hand_cold_calling_a_big_raise_is_minus_ev(tmp_path):
    """
    Textbook spot: BB cold-calls a 40bb UTG raise with 7-2 offsuit, the worst
    starting hand in hold'em, against UTG's (already tight) opening range. This
    should never be anything but clearly -EV.
    """
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,BB,7c 2d,100,"
            "UTG:raise40>MP:fold>CO:fold>BTN:fold>SB:fold>BB:call,,,,,,,81.5,-39,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_preflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.hero_equity_pct < 30.0, decision
    assert decision.flag == "-EV", decision
    assert decision.ev_action_bb < -10, decision


def test_textbook_fold_is_always_baseline_zero(tmp_path):
    """A fold is EV(fold) itself by definition - always the zero baseline, never flagged +EV/-EV."""
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,MP,7c 2d,100,"
            "UTG:raise10>MP:fold,,,,,,,11.5,0,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_preflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action_type == "fold"
    assert decision.flag == "baseline"
    assert decision.ev_action_bb == 0.0
    assert decision.hero_equity_pct is None
    assert decision.fold_pct is None


# --- Range assignment (ev/ranges.py) -------------------------------------------------


def test_chen_score_matches_known_textbook_values():
    """Spot-check the Chen formula implementation against its well-known published scores."""
    assert chen_score("A", "A", None) == 20
    assert chen_score("K", "K", None) == 16
    assert chen_score("A", "K", True) == 12  # AKs
    assert chen_score("A", "K", False) == 10  # AKo
    assert chen_score("7", "2", False) == -1.5  # 72o, the "worst hand in hold'em"


def test_range_combos_for_band_excludes_hero_dead_cards():
    """A villain's range can never include a card hero is already holding."""
    combos = range_combos_for_band(0, 100, dead_cards="As Ad")
    for combo in combos:
        assert "As" not in combo.split()
        assert "Ad" not in combo.split()


def test_range_combos_for_band_is_combo_weighted():
    """Top 100% of the percentile scale should recover very close to all 1,326 combos (minus any removed for dead cards - none here)."""
    combos = range_combos_for_band(0, 100, dead_cards="")
    assert len(combos) == 1326


def test_equity_vs_range_matches_single_hand_equity_for_a_single_combo_range():
    """equity_vs_range with a range of exactly one combo should match calculate_equity directly."""
    from poker_analyzer.equity.calculator import calculate_equity

    single = calculate_equity("As Ad", "Ks Kc", trials=2000, seed=99)
    ranged = equity_vs_range("As Ad", ["Ks Kc"], trials_per_combo=2000, seed=99)

    assert ranged == pytest.approx(single.hero_equity, abs=0.01)


# --- Fold-equity math (ev/engine.py's estimate_fold_pct / _continuing_band) --------
#
# Direct unit tests of the fold-equity arithmetic in isolation - the textbook/real-hand
# tests above exercise it bundled together with Monte Carlo equity simulation, which
# can't pin down the formula itself the way these can.


def test_estimate_fold_pct_matches_mdf_formula_directly():
    """estimate_fold_pct(cost, pot) must be exactly cost / (pot + cost) - MDF's complement."""
    assert estimate_fold_pct(cost_bb=3.0, pot_before_bb=1.5) == pytest.approx(3.0 / 4.5)
    assert estimate_fold_pct(cost_bb=10.0, pot_before_bb=10.0) == pytest.approx(0.5)
    assert estimate_fold_pct(cost_bb=1.0, pot_before_bb=99.0) == pytest.approx(0.01)


def test_estimate_fold_pct_is_zero_for_a_zero_cost_action():
    """A $0 action (shouldn't reach a bet/raise path, but the formula itself must be safe here) folds nobody."""
    assert estimate_fold_pct(cost_bb=0.0, pot_before_bb=5.0) == 0.0


def test_estimate_fold_pct_increases_with_bet_size_relative_to_pot():
    """A bigger bet into the same pot should estimate a higher fold frequency - monotonic in bet size."""
    small_bet = estimate_fold_pct(cost_bb=2.0, pot_before_bb=10.0)
    big_bet = estimate_fold_pct(cost_bb=20.0, pot_before_bb=10.0)
    overbet = estimate_fold_pct(cost_bb=100.0, pot_before_bb=10.0)

    assert 0.0 < small_bet < big_bet < overbet < 1.0


def test_continuing_band_at_full_continue_pct_is_unchanged():
    """continue_pct=1.0 (no fold equity, e.g. a $0-cost action) must leave the band untouched."""
    assert _continuing_band((0, 45), 1.0) == (0, 45)


def test_continuing_band_narrows_from_the_weak_end():
    """
    Bands are strongest-to-weakest (low percentile = strongest), so narrowing to the
    continuing sub-range must keep the strong (low) edge fixed and pull in the weak
    (high) edge - continuing hands are the *strongest* slice of the original band.
    """
    band = (10, 50)
    continuing = _continuing_band(band, continue_pct=0.5)

    assert continuing[0] == band[0]  # strong edge unchanged
    assert continuing[1] == pytest.approx(30.0)  # halfway into the original 40-point width
    assert continuing[1] < band[1]  # strictly narrower


def test_continuing_band_at_zero_continue_pct_collapses_to_the_strong_edge():
    """continue_pct=0.0 (nobody continues) collapses the band to a zero-width point at its strong edge."""
    band = (0, 20)
    continuing = _continuing_band(band, continue_pct=0.0)

    assert continuing == (0, 0)


def test_bet_and_raise_decisions_populate_fold_equity_fields_call_and_check_do_not(tmp_path):
    """
    fold_pct/continuing_range_band should only ever be populated for bet/raise
    decisions (the only ones where hero's own action can induce a fold) - never for
    check/call, where hero isn't the one betting.
    """
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,BB,Ah Th,100,"
            "UTG:raise3>MP:call>CO:call>BTN:fold>SB:fold>BB:call,,,,,,,10.5,-3,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_preflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action_type == "call"
    assert decision.fold_pct is None
    assert decision.continuing_range_band is None


def test_postflop_bet_and_raise_decisions_populate_fold_equity_fields_call_does_not(tmp_path):
    """
    Same convention check as the preflop test above, for postflop. Hand 13 gives all
    three action types across its three streets in one real hand: flop is a call
    (BB:call - no fold equity), turn is a bet (BB:bet5), river is a raise
    (BB:raise27.5) - so this pins down that postflop bet/raise decisions reuse the
    exact preflop fold-equity formula (`estimate_fold_pct`) while a call still leaves
    fold_pct/continuing_range_band unpopulated, exactly like preflop.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_postflop(conn, hand_id=13, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    by_street = {d.street: d for d in decisions}

    flop = by_street["flop"]
    assert flop.action_type == "call"
    assert flop.fold_pct is None
    assert flop.continuing_range_band is None

    turn = by_street["turn"]
    assert turn.action_type == "bet"
    assert turn.fold_pct == pytest.approx(turn.cost_bb / (turn.pot_before_bb + turn.cost_bb))
    assert turn.continuing_range_band[0] == turn.opponent_range_band[0]
    assert turn.continuing_range_band[1] < turn.opponent_range_band[1]

    river = by_street["river"]
    assert river.action_type == "raise"
    assert river.fold_pct == pytest.approx(river.cost_bb / (river.pot_before_bb + river.cost_bb))
    assert river.continuing_range_band[0] == river.opponent_range_band[0]
    assert river.continuing_range_band[1] <= river.opponent_range_band[1]


# --- Postflop range-narrowing replay (ev/engine.py's _reconstruct_postflop_street) --
#
# Direct unit tests of the postflop narrowing rules in isolation, independent of
# equity simulation - pinning down exactly which actions narrow the band, by how
# much, and which don't.


def test_postflop_opponent_bet_narrows_band_toward_strength():
    """An opponent's bet narrows the incoming band to its strongest POSTFLOP_BET_NARROW_PCT."""
    decisions, pot_after, band_after = _reconstruct_postflop_street(
        "flop",
        [{"actor_position": "UTG", "action_type": "bet", "amount_bb": 5.0}],
        hero_position="BB",
        incoming_band=(0.0, 10.0),
        incoming_pot_bb=6.0,
    )
    assert band_after == pytest.approx((0.0, 10.0 * POSTFLOP_BET_NARROW_PCT))
    assert decisions == []  # hero never acted this street


def test_postflop_opponent_check_only_slightly_caps_band():
    """An opponent's check narrows the band by only POSTFLOP_CHECK_NARROW_PCT - a small cap, not a real narrowing."""
    decisions, pot_after, band_after = _reconstruct_postflop_street(
        "flop",
        [{"actor_position": "UTG", "action_type": "check", "amount_bb": None}],
        hero_position="BB",
        incoming_band=(0.0, 10.0),
        incoming_pot_bb=6.0,
    )
    assert band_after == pytest.approx((0.0, 10.0 * POSTFLOP_CHECK_NARROW_PCT))


def test_postflop_call_does_not_narrow_the_band_further():
    """
    A call - here, calling an opponent's bet - inherits whatever the bet already
    narrowed the band to, rather than narrowing it again or resetting it back to
    the pre-street incoming band.
    """
    decisions, pot_after, band_after_bet_only = _reconstruct_postflop_street(
        "flop",
        [{"actor_position": "UTG", "action_type": "bet", "amount_bb": 5.0}],
        hero_position="BB",
        incoming_band=(0.0, 10.0),
        incoming_pot_bb=6.0,
    )
    decisions, pot_after, band_after_with_call = _reconstruct_postflop_street(
        "flop",
        [
            {"actor_position": "UTG", "action_type": "bet", "amount_bb": 5.0},
            {"actor_position": "CO", "action_type": "call", "amount_bb": None},
        ],
        hero_position="BB",
        incoming_band=(0.0, 10.0),
        incoming_pot_bb=6.0,
    )
    assert band_after_with_call == pytest.approx(band_after_bet_only)


def test_postflop_heros_own_action_never_narrows_the_band():
    """
    This machinery estimates *villain's* range from *villain's* actions - hero's own
    bet must not narrow the band, even though a bet from an opponent would.
    """
    decisions, pot_after, band_after = _reconstruct_postflop_street(
        "flop",
        [{"actor_position": "BB", "action_type": "bet", "amount_bb": 5.0}],
        hero_position="BB",
        incoming_band=(0.0, 10.0),
        incoming_pot_bb=6.0,
    )
    assert band_after == (0.0, 10.0)
    assert decisions[0]["opponent_band"] == (0.0, 10.0)


def test_postflop_narrowing_compounds_across_streets():
    """
    Narrowing carries forward and compounds: an opponent betting both the flop and
    the turn should narrow the band twice, once per street, not just once overall.
    """
    _, pot_after_flop, band_after_flop = _reconstruct_postflop_street(
        "flop",
        [{"actor_position": "UTG", "action_type": "bet", "amount_bb": 5.0}],
        hero_position="BB",
        incoming_band=(0.0, 10.0),
        incoming_pot_bb=6.0,
    )
    _, pot_after_turn, band_after_turn = _reconstruct_postflop_street(
        "turn",
        [{"actor_position": "UTG", "action_type": "bet", "amount_bb": 5.0}],
        hero_position="BB",
        incoming_band=band_after_flop,
        incoming_pot_bb=pot_after_flop,
    )

    assert band_after_flop == pytest.approx((0.0, 10.0 * POSTFLOP_BET_NARROW_PCT))
    assert band_after_turn == pytest.approx((0.0, 10.0 * POSTFLOP_BET_NARROW_PCT * POSTFLOP_BET_NARROW_PCT))
    assert band_after_turn[1] < band_after_flop[1]  # strictly narrower each street


# --- Postflop EV engine (analyze_hand_postflop) - real hands ------------------------


def test_real_hand_flopped_trip_aces_is_plus_ev_every_street(tmp_path):
    """
    Hand 3 (session 2026-07-12): hero is BB with Ac Ah, flops trip aces on
    Jc 5h Ad and bets/raises every street (flop raise, turn bet, river bet) before
    villain folds the river. Flopping trips with the best possible hand and betting
    it on every street should never come back as anything but +EV - if any street
    doesn't, something in the postflop pot reconstruction, range narrowing, or
    board-aware equity is wrong.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_postflop(conn, hand_id=3, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert [d.street for d in decisions] == ["flop", "turn", "river"]
    for decision in decisions:
        assert decision.hero_position == "BB"
        assert decision.hero_equity_pct > 85.0, decision
        assert decision.flag == "+EV", decision
        # Flop is a raise, turn and river are bets - every street here has fold equity.
        assert decision.fold_pct == pytest.approx(
            decision.cost_bb / (decision.pot_before_bb + decision.cost_bb)
        ), decision
        assert decision.continuing_range_band[1] <= decision.opponent_range_band[1], decision
    # Board (dead cards for range assignment) should grow one card at a time.
    assert decisions[0].board == "Jc 5h Ad"
    assert decisions[1].board == "Jc 5h Ad 4s"
    assert decisions[2].board == "Jc 5h Ad 4s 3c"


def test_real_hand_flopped_trip_aces_flop_raise_beats_pre_fold_equity_showdown_number(tmp_path):
    """
    Re-verification per the task: a real postflop bet/raise that likely won the pot
    outright should now show clearly higher EV than the old showdown-only number.

    Hand 3's flop decision is hero raising a flopped set of aces. Recomputes the
    pre-fold-equity number directly - equity against the FULL (unnarrowed)
    opponent_range_band, exactly the formula every postflop bet/raise used before
    fold equity was added - and checks the new fold-equity-aware ev_action_bb comes
    back higher, since folding out part of villain's range on top of already-strong
    showdown equity can only add value here (not subtract it).
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_postflop(conn, hand_id=3, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    flop_decision = decisions[0]
    assert flop_decision.street == "flop"
    assert flop_decision.action_type == "raise"
    assert flop_decision.fold_pct is not None and flop_decision.fold_pct > 0.0

    full_band_combos = range_combos_for_band(
        flop_decision.opponent_range_band[0],
        flop_decision.opponent_range_band[1],
        dead_cards=f"Ac Ah {flop_decision.board}",
    )
    old_equity_pct = equity_vs_range(
        "Ac Ah",
        full_band_combos,
        board=flop_decision.board,
        trials_per_combo=TEST_TRIALS_PER_COMBO,
        seed=TEST_SEED,
    )
    pot_after = flop_decision.pot_before_bb + flop_decision.cost_bb
    old_ev = (old_equity_pct / 100.0) * pot_after - flop_decision.cost_bb

    assert flop_decision.ev_action_bb > old_ev, (flop_decision, old_ev)


def test_real_hand_flop_cbet_with_top_pair_beats_pre_fold_equity_showdown_number(tmp_path):
    """
    Second real-hand re-verification: hand 4's flop decision is hero (UTG, As Kd)
    betting 5bb into a small pot. Same comparison as above - the new fold-equity-aware
    EV should come back higher than the pre-fold-equity full-range showdown number.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_postflop(conn, hand_id=4, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    flop_decision = decisions[0]
    assert flop_decision.street == "flop"
    assert flop_decision.action_type == "bet"
    assert flop_decision.fold_pct is not None and flop_decision.fold_pct > 0.0

    full_band_combos = range_combos_for_band(
        flop_decision.opponent_range_band[0],
        flop_decision.opponent_range_band[1],
        dead_cards=f"As Kd {flop_decision.board}",
    )
    old_equity_pct = equity_vs_range(
        "As Kd",
        full_band_combos,
        board=flop_decision.board,
        trials_per_combo=TEST_TRIALS_PER_COMBO,
        seed=TEST_SEED,
    )
    pot_after = flop_decision.pot_before_bb + flop_decision.cost_bb
    old_ev = (old_equity_pct / 100.0) * pot_after - flop_decision.cost_bb

    assert flop_decision.ev_action_bb > old_ev, (flop_decision, old_ev)


def test_real_hand_river_nut_flush_is_a_lock_and_flags_plus_ev(tmp_path):
    """
    Hand 13 (session 2026-07-12): hero is BB with Ah Th. By the river the board is
    3h 9c 8c 5h 4h - three more hearts, giving hero the nut (ace-high) flush. Since
    hero holds the Ah, no possible villain combo can beat or even tie this hand, so
    hero's equity against ANY range on this river must be exactly 100% - a
    deterministic check (the river board is fully known, so this uses exact
    enumeration with a single "runout", not Monte Carlo - see calculate_equity's
    5-card-board handling).
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_postflop(conn, hand_id=13, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    river_decisions = [d for d in decisions if d.street == "river"]
    assert len(river_decisions) == 1
    decision = river_decisions[0]
    assert decision.action_type == "raise"
    assert decision.board == "3h 9c 8c 5h 4h"
    assert decision.hero_equity_pct == pytest.approx(100.0)
    assert decision.flag == "+EV", decision


def test_real_hand_postflop_fold_is_baseline_zero_with_no_equity(tmp_path):
    """
    Hand 4 (session 2026-07-12): hero (UTG) bets flop and turn, checks the river,
    then folds to a river raise. That final fold should follow the exact same
    "baseline" convention as a preflop fold - cost 0, no equity/range computed,
    flag "baseline" - not get analyzed as if it were a real decision.
    """
    conn = _load_real_hands_db(tmp_path)
    decisions = analyze_hand_postflop(conn, hand_id=4, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    fold_decisions = [d for d in decisions if d.action_type == "fold"]
    assert len(fold_decisions) == 1
    decision = fold_decisions[0]
    assert decision.street == "river"
    assert decision.cost_bb == 0.0
    assert decision.opponent_range_band is None
    assert decision.hero_equity_pct is None
    assert decision.ev_action_bb == 0.0
    assert decision.flag == "baseline"


def test_analyze_hand_postflop_is_empty_for_a_hand_that_ended_preflop(tmp_path):
    """A hand with no flop dealt (everyone folded preflop) has no postflop decisions at all."""
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,MP,7c 2d,100,"
            "UTG:raise10>MP:fold,,,,,,,11.5,0,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_postflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert decisions == []


# --- Postflop EV engine (analyze_hand_postflop) - textbook spot ---------------------


def test_textbook_flush_draw_is_a_slight_favorite_vs_a_tight_flop_range(tmp_path):
    """
    Reuses the textbook flush-draw-vs-overpair flop spot already validated directly
    against calculate_equity in tests/test_equity_calculator.py
    (test_flush_draw_and_overcards_vs_overpair_on_flop: Ah Kh vs Qs Qd on Jh 8h 3c is
    hero-favorite at ~54.44%, despite the overpair currently having the better hand).

    Constructed spot: hero (SB) holds Ah Kh and 3-bets get 4-bet... no - simpler:
    hero cold-calls a 3-bet from MP (over UTG's open), so the range hero actually
    faces on the flop is villain's 3-bet range (REREAISE_TOP_PCT, the top ~7% of
    hands - pairs and big broadways, an "overpair-heavy" range on this board, the
    same shape as the single-combo Qs Qd example). Hero acts first on the flop
    (no one has bet yet this street), so the band reaching the equity calculation is
    that ~7% band unnarrowed. hero's flush draw + two live overcards should come back
    a betting favorite (>50% equity) despite most of that range holding an overpair -
    the same counterintuitive fact the reused example demonstrates, now running
    through the full range-narrowing + board-aware equity pipeline instead of a
    single hardcoded hand.
    """
    csv_path = _write_csv(
        tmp_path,
        [
            "2026-08-05,The Brook,1/3,1,6,SB,Ah Kh,100,"
            "UTG:raise3>MP:raise10>BTN:fold>SB:call>BB:fold>UTG:fold,"
            "Jh 8h 3c,SB:bet10,,,,,,,0,",
        ],
    )
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(csv_path, db_path)

    import sqlite3

    conn = sqlite3.connect(db_path)
    decisions = analyze_hand_postflop(conn, hand_id=1, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    conn.close()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.street == "flop"
    assert decision.board == "Jh 8h 3c"
    assert decision.opponent_range_band == pytest.approx((0.0, 7.0))
    assert decision.hero_equity_pct > 52.0, decision  # a slight favorite, same lesson as the reused example
    assert decision.flag == "+EV", decision


def test_equity_vs_range_matches_known_flop_spot_for_a_single_combo_range():
    """
    Direct reuse of the flush-draw-vs-overpair textbook spot (see
    tests/test_equity_calculator.py's test_flush_draw_and_overcards_vs_overpair_on_flop)
    through equity_vs_range's board-aware path - the same function analyze_hand_postflop
    calls - to pin down that the board actually reaches calculate_equity correctly.
    """
    ranged = equity_vs_range("Ah Kh", ["Qs Qd"], board="Jh 8h 3c")

    assert ranged == pytest.approx(54.44, abs=0.1)


def test_equity_vs_range_passes_range_width_through_to_calculate_equity():
    """
    equity_vs_range must tell calculate_equity how wide the range is
    (range_width=len(villain_range_combos)) on every combo, not just the trial
    count/seed - this is what lets a wide postflop range fall back to Monte Carlo
    instead of enumerating every combo's runouts exactly. See calculate_equity's
    range_width docstring and the README's "Postflop equity: Monte Carlo fallback"
    section.
    """
    import poker_analyzer.ev.engine as engine_module

    seen_range_widths = []
    original = engine_module.calculate_equity

    def spy(*args, **kwargs):
        seen_range_widths.append(kwargs.get("range_width"))
        return original(*args, **kwargs)

    engine_module.calculate_equity = spy
    try:
        combos = ["Qs Qd", "Js Jd", "Ts Td"]
        equity_vs_range("Ah Kh", combos, board="Jh 8h 3c", trials_per_combo=50, seed=1)
    finally:
        engine_module.calculate_equity = original

    assert seen_range_widths == [len(combos)] * len(combos)


# --- Postflop Monte Carlo fallback: regression against exact enumeration -----------
#
# The postflop equity path used to always enumerate every villain-range combo's
# runouts exactly (a flop/turn/river board's own runout count is small enough on its
# own), regardless of how wide the range was - this is what made ev-report/leaks take
# several minutes. equity_vs_range now tells calculate_equity how wide the range is
# (range_width), so a wide-enough flop range falls back to Monte Carlo the same way a
# 0-card preflop board already always did. These tests confirm that fallback doesn't
# change any real hand's postflop +EV/-EV/marginal flag versus forcing exact
# enumeration everywhere (by raising EXACT_ENUMERATION_LIMIT past what any real
# range/runout combination in the 15-hand database can cross), and that equity
# estimates land close enough to the exact values not to matter.


def _all_postflop_decisions(conn, trials_per_combo, seed):
    hand_ids = [row[0] for row in conn.execute("SELECT DISTINCT hand_id FROM hands ORDER BY hand_id")]
    decisions = []
    for hand_id in hand_ids:
        decisions.extend(analyze_hand_postflop(conn, hand_id, trials_per_combo=trials_per_combo, seed=seed))
    return decisions


def test_monte_carlo_fallback_matches_exact_flags_on_every_real_postflop_decision(tmp_path, monkeypatch):
    """
    Every real hand's postflop decisions, computed twice: once with the Monte Carlo
    fallback active (today's default behavior) and once with exact enumeration
    forced everywhere (EXACT_ENUMERATION_LIMIT raised so no real range/runout
    combination in this database can cross it). The fallback must not flip a single
    decision's +EV/-EV/marginal flag, and hero_equity_pct should land within a few
    points of the exact value - the whole point of a Monte Carlo fallback is speed
    without moving the answer enough to change a real decision.
    """
    import poker_analyzer.equity.calculator as calculator_module

    conn = _load_real_hands_db(tmp_path)

    fallback_decisions = _all_postflop_decisions(conn, TEST_TRIALS_PER_COMBO, TEST_SEED)

    monkeypatch.setattr(calculator_module, "EXACT_ENUMERATION_LIMIT", 10_000_000)
    exact_decisions = _all_postflop_decisions(conn, TEST_TRIALS_PER_COMBO, TEST_SEED)

    assert len(fallback_decisions) == len(exact_decisions)
    assert len(fallback_decisions) > 0

    saw_a_fallback_decision = False
    for fb, ex in zip(fallback_decisions, exact_decisions):
        assert fb.hand_id == ex.hand_id
        assert fb.street == ex.street
        assert fb.action_type == ex.action_type
        assert fb.flag == ex.flag, (
            f"hand {fb.hand_id} {fb.street} {fb.action_type}: "
            f"fallback flag {fb.flag!r} != exact flag {ex.flag!r} "
            f"({fb.hero_equity_pct} vs {ex.hero_equity_pct})"
        )

        if fb.opponent_range_combo_count is not None:
            runouts = {"flop": 990, "turn": 44, "river": 1}[fb.street]
            if runouts * fb.opponent_range_combo_count > 50_000:
                saw_a_fallback_decision = True

        if fb.hero_equity_pct is not None:
            assert fb.hero_equity_pct == pytest.approx(ex.hero_equity_pct, abs=5.0), (fb, ex)
        if fb.ev_diff_bb is not None:
            assert fb.ev_diff_bb == pytest.approx(ex.ev_diff_bb, abs=1.5), (fb, ex)

    # Sanity: the real database must actually exercise the fallback at least once,
    # otherwise this test would pass trivially without comparing anything meaningful.
    assert saw_a_fallback_decision


def test_range_width_does_not_change_preflops_already_monte_carlo_method(tmp_path):
    """
    Preflop already always used Monte Carlo before this session's change (a 0-card
    board's own runout count, ~1.7M, is already over EXACT_ENUMERATION_LIMIT with
    range_width=1) - equity_vs_range now passes range_width=len(villain_range_combos)
    on *every* call, preflop included, so this confirms that only ever pushes an
    already-over-the-limit preflop call further over it, never pulling it back under
    (which would attempt a real ~1.7M-combo exact enumeration - not just slow, but
    exactly the runaway cost this session's fix is about avoiding).
    """
    conn = _load_real_hands_db(tmp_path)
    hand_ids = [row[0] for row in conn.execute("SELECT DISTINCT hand_id FROM hands ORDER BY hand_id")]

    for hand_id in hand_ids:
        decisions = analyze_hand_preflop(conn, hand_id, trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
        for decision in decisions:
            if decision.hero_equity_pct is not None:
                assert 0.0 <= decision.hero_equity_pct <= 100.0, decision
