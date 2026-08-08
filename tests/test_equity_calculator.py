"""
Validate the equity calculator (src/poker_analyzer/equity/calculator.py) against
known textbook spots and mathematical invariants.

These tests exist to confirm the treys integration produces correct numbers, not
to test treys itself.
"""

from poker_analyzer.equity.calculator import calculate_equity


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


def test_parse_cards_accepts_string_and_list():
    from poker_analyzer.equity.calculator import parse_cards

    assert parse_cards("As Kd") == parse_cards(["As", "Kd"])
    assert len(parse_cards("As Kd")) == 2


def test_rejects_overlapping_cards():
    import pytest

    with pytest.raises(ValueError):
        calculate_equity(hero="As Kd", villain="As Qc", board="")
