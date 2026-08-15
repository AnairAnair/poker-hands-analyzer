"""
Tests for leak detection (poker_analyzer.leaks.detector).

Two kinds of coverage, per the task:
- Unit tests against hand-constructed PreflopDecision/PostflopDecision objects,
  which pin down the grouping key, the min-sample-size threshold, the
  leak/marginal/fine verdict cutoffs, fold exclusion, and hand_id
  dedup/ordering independent of any real equity simulation.
- Integration tests running the real EV engine against the actual 15 hands in
  data/templates/real_hands.csv, checking that a pattern with enough real
  repetitions (UTG preflop raises, 6 occurrences) gets a real verdict while
  every thinner pattern is correctly excluded from being called a leak and
  labeled "insufficient data" instead - the core requirement from the task.
"""

from pathlib import Path

import pytest

from poker_analyzer.db.init_db import init_db
from poker_analyzer.ev.engine import (
    MARGINAL_THRESHOLD_BB,
    PostflopDecision,
    PreflopDecision,
    analyze_all_postflop_decisions,
    analyze_all_preflop_decisions,
)
from poker_analyzer.ingestion.loader import load_hand_log_csv
from poker_analyzer.leaks.detector import MIN_SAMPLE_SIZE, detect_leaks

REAL_HANDS_CSV = Path("data/templates/real_hands.csv")

# Modest and seeded, same rationale as test_ev_engine.py: fast + deterministic.
# Only pattern *counts* are asserted against real hands here (deterministic
# regardless of trial count) - avg_ev_diff_bb precision isn't under test.
TEST_TRIALS_PER_COMBO = 30
TEST_SEED = 7


def _load_real_hands_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "poker.db"
    init_db(db_path)
    load_hand_log_csv(REAL_HANDS_CSV, db_path)
    return db_path


def _make_preflop(**overrides) -> PreflopDecision:
    fields = dict(
        hand_id=1,
        session_id=1,
        hero_position="BB",
        action_type="call",
        amount_bb=None,
        pot_before_bb=3.0,
        cost_bb=2.0,
        opponent_range_band=(0, 30),
        opponent_range_combo_count=42,
        hero_equity_pct=50.0,
        ev_action_bb=0.0,
        ev_fold_bb=0.0,
        ev_diff_bb=0.0,
        flag="marginal",
    )
    fields.update(overrides)
    return PreflopDecision(**fields)


def _make_postflop(**overrides) -> PostflopDecision:
    fields = dict(
        hand_id=1,
        session_id=1,
        hero_position="UTG",
        street="river",
        action_type="bet",
        amount_bb=4.0,
        pot_before_bb=6.5,
        cost_bb=4.0,
        board="Jh 8h 3c 2d 9s",
        opponent_range_band=(0, 12),
        opponent_range_combo_count=18,
        hero_equity_pct=71.0,
        ev_action_bb=0.0,
        ev_fold_bb=0.0,
        ev_diff_bb=0.0,
        flag="marginal",
    )
    fields.update(overrides)
    return PostflopDecision(**fields)


def _fold(hand_id: int, position: str = "BB") -> PreflopDecision:
    return _make_preflop(
        hand_id=hand_id,
        hero_position=position,
        action_type="fold",
        amount_bb=None,
        opponent_range_band=None,
        opponent_range_combo_count=None,
        hero_equity_pct=None,
        ev_action_bb=0.0,
        ev_fold_bb=0.0,
        ev_diff_bb=0.0,
        flag="baseline",
    )


# --- grouping key / labels -----------------------------------------------------------


def test_preflop_pattern_label_omits_street():
    decisions = [_make_preflop(hand_id=i, hero_position="BB", action_type="call") for i in range(1, 4)]

    patterns = detect_leaks(decisions, [])

    assert len(patterns) == 1
    assert patterns[0].label == "BB calls"
    assert patterns[0].position == "BB"
    assert patterns[0].street == "preflop"
    assert patterns[0].action_type == "call"


def test_postflop_pattern_label_includes_street():
    decisions = [
        _make_postflop(hand_id=i, hero_position="UTG", street="river", action_type="bet") for i in range(1, 4)
    ]

    patterns = detect_leaks([], decisions)

    assert len(patterns) == 1
    assert patterns[0].label == "UTG river bets"
    assert patterns[0].street == "river"


def test_different_streets_are_different_patterns_even_with_same_position_and_action():
    decisions = [
        _make_postflop(hand_id=1, hero_position="UTG", street="flop", action_type="bet"),
        _make_postflop(hand_id=2, hero_position="UTG", street="turn", action_type="bet"),
        _make_postflop(hand_id=3, hero_position="UTG", street="river", action_type="bet"),
    ]

    patterns = detect_leaks([], decisions)

    assert {p.street for p in patterns} == {"flop", "turn", "river"}
    assert all(p.occurrences == 1 for p in patterns)


def test_preflop_and_postflop_decisions_are_grouped_independently():
    decisions_pre = [_make_preflop(hand_id=i, hero_position="UTG", action_type="raise") for i in range(1, 4)]
    decisions_post = [
        _make_postflop(hand_id=i, hero_position="UTG", street="flop", action_type="bet") for i in range(4, 7)
    ]

    patterns = detect_leaks(decisions_pre, decisions_post)

    labels = {p.label for p in patterns}
    assert labels == {"UTG raises", "UTG flop bets"}


# --- folds excluded -------------------------------------------------------------------


def test_folds_never_appear_as_a_pattern():
    decisions = [_fold(hand_id=i) for i in range(1, 5)]

    patterns = detect_leaks(decisions, [])

    assert patterns == []


def test_folds_do_not_pollute_a_real_pattern():
    decisions = [
        _make_preflop(hand_id=1, hero_position="BB", action_type="call", ev_diff_bb=2.0, flag="+EV"),
        _make_preflop(hand_id=2, hero_position="BB", action_type="call", ev_diff_bb=2.0, flag="+EV"),
        _make_preflop(hand_id=3, hero_position="BB", action_type="call", ev_diff_bb=2.0, flag="+EV"),
        _fold(hand_id=4, position="BB"),
    ]

    patterns = detect_leaks(decisions, [])

    assert len(patterns) == 1
    assert patterns[0].occurrences == 3
    assert patterns[0].avg_ev_diff_bb == pytest.approx(2.0)


# --- min sample size / insufficient_data ----------------------------------------------


def test_pattern_below_default_min_sample_is_insufficient_data_but_still_reported():
    decisions = [_make_preflop(hand_id=i, hero_position="SB", action_type="raise") for i in range(1, 3)]

    patterns = detect_leaks(decisions, [])

    assert len(patterns) == 1
    assert patterns[0].occurrences == 2
    assert patterns[0].verdict == "insufficient_data"


def test_pattern_at_default_min_sample_gets_a_real_verdict():
    decisions = [
        _make_preflop(hand_id=i, hero_position="SB", action_type="raise", ev_diff_bb=2.0, flag="+EV")
        for i in range(1, 4)
    ]

    patterns = detect_leaks(decisions, [])

    assert len(patterns) == 1
    assert patterns[0].occurrences == 3
    assert patterns[0].verdict != "insufficient_data"


def test_default_min_sample_size_constant_is_three():
    assert MIN_SAMPLE_SIZE == 3


def test_custom_min_sample_size_lowers_the_bar():
    decisions = [_make_preflop(hand_id=i, hero_position="SB", action_type="raise") for i in range(1, 3)]

    patterns = detect_leaks(decisions, [], min_sample_size=2)

    assert patterns[0].verdict != "insufficient_data"


def test_custom_min_sample_size_raises_the_bar():
    decisions = [
        _make_preflop(hand_id=i, hero_position="SB", action_type="raise", ev_diff_bb=2.0, flag="+EV")
        for i in range(1, 4)
    ]

    patterns = detect_leaks(decisions, [], min_sample_size=5)

    assert patterns[0].verdict == "insufficient_data"


# --- verdict classification (reuses MARGINAL_THRESHOLD_BB) ----------------------------


def test_avg_diff_at_or_below_negative_threshold_is_a_leak():
    diffs = [-2.0, -1.5, -3.0]
    decisions = [
        _make_preflop(hand_id=i, hero_position="UTG", action_type="call", ev_diff_bb=diff, flag="-EV")
        for i, diff in enumerate(diffs, start=1)
    ]

    patterns = detect_leaks(decisions, [])

    assert patterns[0].verdict == "leak"
    assert patterns[0].avg_ev_diff_bb == pytest.approx(sum(diffs) / 3)
    assert patterns[0].avg_ev_diff_bb <= -MARGINAL_THRESHOLD_BB


def test_avg_diff_inside_marginal_band_is_marginal():
    diffs = [0.5, -0.5, 0.9]
    decisions = [
        _make_preflop(hand_id=i, hero_position="UTG", action_type="call", ev_diff_bb=diff, flag="marginal")
        for i, diff in enumerate(diffs, start=1)
    ]

    patterns = detect_leaks(decisions, [])

    assert patterns[0].verdict == "marginal"
    assert -MARGINAL_THRESHOLD_BB < patterns[0].avg_ev_diff_bb < MARGINAL_THRESHOLD_BB


def test_avg_diff_at_or_above_positive_threshold_is_fine():
    diffs = [2.0, 3.0, 1.5]
    decisions = [
        _make_preflop(hand_id=i, hero_position="UTG", action_type="call", ev_diff_bb=diff, flag="+EV")
        for i, diff in enumerate(diffs, start=1)
    ]

    patterns = detect_leaks(decisions, [])

    assert patterns[0].verdict == "fine"
    assert patterns[0].avg_ev_diff_bb >= MARGINAL_THRESHOLD_BB


def test_per_decision_flag_counts_are_tallied_independent_of_verdict():
    decisions = [
        _make_preflop(hand_id=1, hero_position="UTG", action_type="call", ev_diff_bb=-3.0, flag="-EV"),
        _make_preflop(hand_id=2, hero_position="UTG", action_type="call", ev_diff_bb=-3.0, flag="-EV"),
        _make_preflop(hand_id=3, hero_position="UTG", action_type="call", ev_diff_bb=0.2, flag="marginal"),
    ]

    patterns = detect_leaks(decisions, [])

    assert patterns[0].negative_ev_count == 2
    assert patterns[0].marginal_count == 1
    assert patterns[0].positive_ev_count == 0


# --- hand_ids -------------------------------------------------------------------------


def test_hand_ids_are_deduplicated_and_sorted():
    decisions = [
        _make_preflop(hand_id=5, hero_position="UTG", action_type="raise"),
        _make_preflop(hand_id=1, hero_position="UTG", action_type="raise"),
        _make_preflop(hand_id=5, hero_position="UTG", action_type="raise"),  # duplicate hand_id
        _make_preflop(hand_id=3, hero_position="UTG", action_type="raise"),
    ]

    patterns = detect_leaks(decisions, [])

    assert patterns[0].hand_ids == (1, 3, 5)
    assert patterns[0].occurrences == 4  # dedup applies to hand_ids, not occurrence count


# --- output ordering --------------------------------------------------------------------


def test_patterns_are_ordered_leak_then_marginal_then_fine_then_insufficient_data():
    leak = [
        _make_preflop(hand_id=i, hero_position="A", action_type="raise", ev_diff_bb=-2.0, flag="-EV")
        for i in range(1, 4)
    ]
    marginal = [
        _make_preflop(hand_id=i, hero_position="B", action_type="raise", ev_diff_bb=0.1, flag="marginal")
        for i in range(4, 7)
    ]
    fine = [
        _make_preflop(hand_id=i, hero_position="C", action_type="raise", ev_diff_bb=3.0, flag="+EV")
        for i in range(7, 10)
    ]
    thin = [_make_preflop(hand_id=10, hero_position="D", action_type="raise")]

    patterns = detect_leaks(thin + fine + marginal + leak, [])

    assert [p.verdict for p in patterns] == ["leak", "marginal", "fine", "insufficient_data"]


def test_leak_patterns_are_ordered_worst_average_diff_first():
    worse = [
        _make_preflop(hand_id=i, hero_position="A", action_type="raise", ev_diff_bb=-5.0, flag="-EV")
        for i in range(1, 4)
    ]
    less_bad = [
        _make_preflop(hand_id=i, hero_position="B", action_type="raise", ev_diff_bb=-1.5, flag="-EV")
        for i in range(4, 7)
    ]

    patterns = detect_leaks(less_bad + worse, [])

    assert [p.position for p in patterns] == ["A", "B"]


def test_insufficient_data_patterns_are_ordered_most_occurrences_first():
    two_occ = [_make_preflop(hand_id=i, hero_position="A", action_type="raise") for i in range(1, 3)]
    one_occ = [_make_preflop(hand_id=3, hero_position="B", action_type="raise")]

    patterns = detect_leaks(one_occ + two_occ, [])

    assert [p.position for p in patterns] == ["A", "B"]


# --- real hands from the database ------------------------------------------------------


def test_real_hands_utg_preflop_raise_pattern_clears_min_sample(tmp_path):
    """
    UTG opens 6 of the 15 real logged hands - the one pattern with enough
    real-world repetition to clear the default min sample size of 3.
    """
    db_path = _load_real_hands_db(tmp_path)
    preflop = analyze_all_preflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    postflop = analyze_all_postflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)

    patterns = detect_leaks(preflop, postflop)

    utg_raises = next(p for p in patterns if p.label == "UTG raises")
    assert utg_raises.occurrences == 6
    assert utg_raises.verdict != "insufficient_data"


def test_real_hands_thin_patterns_are_excluded_from_leak_verdicts(tmp_path):
    """
    Every pattern with fewer than 3 occurrences in the real 15-hand database
    must be labeled insufficient_data, never leak/marginal/fine - the core
    "don't call it a leak on 1-2 hands" requirement.
    """
    db_path = _load_real_hands_db(tmp_path)
    preflop = analyze_all_preflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    postflop = analyze_all_postflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)

    patterns = detect_leaks(preflop, postflop)

    for pattern in patterns:
        if pattern.occurrences < MIN_SAMPLE_SIZE:
            assert pattern.verdict == "insufficient_data"
        else:
            assert pattern.verdict != "insufficient_data"


def test_real_hands_no_fold_pattern_is_ever_produced(tmp_path):
    db_path = _load_real_hands_db(tmp_path)
    preflop = analyze_all_preflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    postflop = analyze_all_postflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)

    # sanity: the real data does contain at least one hero fold, so this is a
    # real exclusion being exercised, not a vacuously true assertion.
    assert any(d.flag == "baseline" for d in preflop + postflop)

    patterns = detect_leaks(preflop, postflop)

    assert all(p.action_type != "fold" for p in patterns)


def test_real_hands_pattern_occurrences_sum_matches_non_fold_decision_count(tmp_path):
    db_path = _load_real_hands_db(tmp_path)
    preflop = analyze_all_preflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    postflop = analyze_all_postflop_decisions(str(db_path), trials_per_combo=TEST_TRIALS_PER_COMBO, seed=TEST_SEED)
    non_fold_count = sum(1 for d in preflop + postflop if d.flag != "baseline")

    patterns = detect_leaks(preflop, postflop)

    assert sum(p.occurrences for p in patterns) == non_fold_count
