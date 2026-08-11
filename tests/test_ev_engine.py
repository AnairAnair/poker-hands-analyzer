"""
Tests for the preflop EV engine (poker_analyzer.ev.engine) and the Chen-formula range
assignment it's built on (poker_analyzer.ev.ranges).

Two kinds of coverage, per the task:
- A few of the real hands now loaded from data/templates/real_hands.csv, to sanity-check
  the engine against actual logged play.
- A couple of constructed "textbook" preflop spots with an obvious right answer (a
  premium pair opening, and the worst starting hand cold-calling a big raise), to
  sanity-check the equity-vs-range math independent of any real hand's messiness.
"""

from pathlib import Path

import pytest

from poker_analyzer.db.init_db import init_db
from poker_analyzer.ev.engine import analyze_hand_preflop, equity_vs_range
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


def test_textbook_aa_utg_open_has_huge_equity_but_only_modest_bb_edge(tmp_path):
    """
    Textbook spot: UTG opens 3bb with pocket aces, the best starting hand in hold'em,
    against a generic field range (hero is first to act, so there's no specific
    opponent yet - see DEFAULT_FIELD_BAND in ev/engine.py). Equity should be
    overwhelming (AA crushes a wide field), but note this deliberately does NOT
    assert flag == "+EV": a small raise into a small pot only nets a modest
    absolute-bb edge over folding even from a huge equity edge, so under this
    engine's static, no-fold-equity model it can legitimately land as "marginal"
    against the fixed MARGINAL_THRESHOLD_BB. That's the model being honest about
    what it does and doesn't credit (see ev/engine.py's docstring) - the important
    invariant to check here is that the edge is never negative.
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
    assert decision.hero_equity_pct > 75.0, decision
    assert decision.flag in ("+EV", "marginal"), decision
    assert decision.ev_action_bb > 0, decision


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
