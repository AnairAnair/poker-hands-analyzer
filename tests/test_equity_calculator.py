"""
Validate the equity calculator (src/poker_analyzer/equity/calculator.py) against
known textbook spots and mathematical invariants.

These tests exist to confirm the treys integration produces correct numbers, not
to test treys itself.
"""

from poker_analyzer.equity.calculator import EXACT_ENUMERATION_LIMIT, calculate_equity


def test_aa_vs_kk_preflop():
    """
    AA vs KK preflop is one of the most commonly cited textbook equity spots:
    AA is roughly an 80/20 favorite. This uses Monte Carlo (preflop board is too
    large to enumerate exactly - see EXACT_ENUMERATION_LIMIT), so allow a few
    points of tolerance around the well-known ~82% figure.
    """
    result = calculate_equity(
        hero="As Ad",
        villain="Ks Kc",
        trials=50_000,
        seed=42,
    )

    assert result.method == "monte_carlo"
    assert 78.0 <= result.hero_equity <= 86.0, result
    assert 14.0 <= result.villain_equity <= 22.0, result
    assert result.tie_equity < 2.0, result
    # Sanity: the three outcomes should account for essentially all trials.
    total = result.hero_equity + result.villain_equity + result.tie_equity
    assert abs(total - 100.0) < 0.01, result


def test_flush_draw_and_overcards_vs_overpair_on_flop():
    """
    A well-known "textbook" flop spot: a nut flush draw with two live overcards
    is a slight *favorite* against an overpair, not an underdog, despite the
    overpair currently having the best hand. This is a common poker-training
    example precisely because it's counterintuitive.

    Hero: Ah Kh (nut flush draw + two overcards to the villain's pair)
    Villain: Qs Qd (overpair)
    Board: Jh 8h 3c

    Only 2 board cards remain to come, so this is small enough to enumerate
    exhaustively (990 combinations) - the result below is mathematically exact,
    not an estimate.
    """
    result = calculate_equity(
        hero="Ah Kh",
        villain="Qs Qd",
        board="Jh 8h 3c",
    )

    assert result.method == "exact"
    assert result.trials == 990
    # Exact known values for this spot.
    assert abs(result.hero_equity - 54.44) < 0.1, result
    assert abs(result.villain_equity - 45.56) < 0.1, result
    assert result.tie_equity == 0.0, result
    # The flush-draw hand should be the favorite here - the whole point of the spot.
    assert result.hero_equity > result.villain_equity


def test_identical_ranks_different_suits_tie_when_no_flush_is_possible():
    """
    Mathematical invariant, not a memorized number: if hero and villain hold the
    same two ranks in different suits (Ah Kh vs As Ks), and the board can't
    complete a flush for either of them (no hearts or spades on board, and only
    2 cards are left to come - not enough to make a 5-card flush from scratch),
    then every possible runout must tie exactly, because hand strength for both
    players reduces to the same set of ranks. This exercises tie-handling in the
    evaluator wrapper independent of any external equity numbers.
    """
    result = calculate_equity(
        hero="Ah Kh",
        villain="As Ks",
        board="Td 9c 2d",
    )

    assert result.method == "exact"
    assert result.hero_equity == 0.0, result
    assert result.villain_equity == 0.0, result
    assert result.tie_equity == 100.0, result


def test_complete_five_card_river_board_is_a_direct_exact_comparison():
    """
    A 5-card (river/complete) board leaves nothing left to run out, so equity
    collapses to a single direct hand comparison - added to support the postflop EV
    engine's river decisions (poker_analyzer.ev.engine.analyze_hand_postflop), which
    always has a fully known board by the time hero acts on the river.

    Ah Kh vs Qs Qd on Jh 8h 3c is the flush-draw-vs-overpair textbook spot above -
    hero already has 4 hearts after the flop (2 hole + Jh/8h), so one more heart on
    either the turn or river completes the nut flush. Run it out with a heart turn
    (2h) and a blank river (5c): hero's rivered flush beats villain's pair of queens
    outright, so this should be 100% equity, exactly one "runout" (the empty one),
    not a percentage.
    """
    result = calculate_equity(hero="Ah Kh", villain="Qs Qd", board="Jh 8h 3c 2h 5c")

    assert result.method == "exact"
    assert result.trials == 1
    assert result.hero_equity == 100.0, result
    assert result.villain_equity == 0.0, result
    assert result.tie_equity == 0.0, result


def test_parse_cards_accepts_string_and_list():
    from poker_analyzer.equity.calculator import parse_cards

    assert parse_cards("As Kd") == parse_cards(["As", "Kd"])
    assert len(parse_cards("As Kd")) == 2


def test_rejects_overlapping_cards():
    import pytest

    with pytest.raises(ValueError):
        calculate_equity(hero="As Kd", villain="As Qc", board="")


# --- range_width: the postflop Monte Carlo fallback ---------------------------------
#
# range_width lets a caller that's about to make many calculate_equity calls
# back-to-back (one per combo in an opponent's range - see ev/engine.py's
# equity_vs_range) inform the exact-vs-Monte-Carlo decision with "runouts *
# range_width" instead of just this call's own runout count. Preflop already always
# falls back on its own (a 0-card board's runout count alone is ~1.7M, over
# EXACT_ENUMERATION_LIMIT regardless of range_width) - these tests cover the new
# postflop case, where a single call's own runout count (<=990) is small but a wide
# range multiplies it past the limit.


def test_range_width_of_one_leaves_a_flop_board_exact():
    """
    range_width defaults to 1 - a single hand-pair comparison on a flop board
    (990 runouts, well under EXACT_ENUMERATION_LIMIT on its own) must stay exact,
    unchanged from before range_width existed.
    """
    result = calculate_equity(hero="Ah Kh", villain="Qs Qd", board="Jh 8h 3c")

    assert result.method == "exact"
    assert result.trials == 990


def test_range_width_wide_enough_to_cross_the_limit_falls_back_to_monte_carlo():
    """
    990 runouts * 100 combos = 99,000, over EXACT_ENUMERATION_LIMIT - a wide enough
    range must fall back to Monte Carlo even though this single hand-pair comparison
    would be cheap to enumerate exactly on its own (the same board, same hands, same
    runout count as test_flush_draw_and_overcards_vs_overpair_on_flop above, which
    gets an exact answer at range_width=1).
    """
    result = calculate_equity(
        hero="Ah Kh", villain="Qs Qd", board="Jh 8h 3c", range_width=100, trials=20_000, seed=42
    )

    assert result.method == "monte_carlo"
    assert result.trials == 20_000


def test_range_width_monte_carlo_fallback_lands_close_to_the_known_exact_value():
    """
    Same flush-draw-vs-overpair spot as test_flush_draw_and_overcards_vs_overpair_on_flop
    (exact answer: 54.44% / 45.56%), forced into the Monte Carlo fallback via a wide
    range_width - the whole point of this fallback is speed *without* moving the
    answer enough to matter, so it should still land close to the exact value at a
    reasonably large trial count.
    """
    result = calculate_equity(
        hero="Ah Kh", villain="Qs Qd", board="Jh 8h 3c", range_width=100, trials=20_000, seed=42
    )

    assert result.method == "monte_carlo"
    assert abs(result.hero_equity - 54.44) < 2.0, result
    assert abs(result.villain_equity - 45.56) < 2.0, result


def test_range_width_does_not_affect_a_board_already_over_the_limit_alone():
    """
    A preflop (0-card) board is already over EXACT_ENUMERATION_LIMIT on its own
    (~1.7M runouts) - range_width=1 (the default, matching a single equity_vs_range
    combo call) must still fall back to Monte Carlo, same as before range_width
    existed.
    """
    result = calculate_equity(hero="As Ad", villain="Ks Kc", trials=1000, seed=1, range_width=1)

    assert result.method == "monte_carlo"


def test_range_width_boundary_matches_exact_enumeration_limit_constant():
    """
    The boundary itself: runouts * range_width == EXACT_ENUMERATION_LIMIT exactly
    should still take the exact path (<=, not <), and one combo over the boundary
    should fall back - pins down the off-by-one behavior directly against the real
    constant rather than a hardcoded number.
    """
    runouts = 990
    boundary_width = EXACT_ENUMERATION_LIMIT // runouts  # 50 (990 * 50 = 49,500 <= 50,000)
    at_boundary = calculate_equity(
        hero="Ah Kh", villain="Qs Qd", board="Jh 8h 3c", range_width=boundary_width
    )
    assert at_boundary.method == "exact"

    over_boundary_width = (EXACT_ENUMERATION_LIMIT // runouts) + 1
    if runouts * over_boundary_width > EXACT_ENUMERATION_LIMIT:
        over_boundary = calculate_equity(
            hero="Ah Kh",
            villain="Qs Qd",
            board="Jh 8h 3c",
            range_width=over_boundary_width,
            trials=1000,
            seed=1,
        )
        assert over_boundary.method == "monte_carlo"
