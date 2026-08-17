"""Fuzzy matcher tests — the typo fallback behind every searcher."""

from stocks.fuzzy import FUZZY_CUTOFF, MIN_QUERY, fuzzy_ratio


def test_single_letter_typos_clear_cutoff():
    assert fuzzy_ratio("ORACEL", "ORACLE CORP") >= FUZZY_CUTOFF
    assert fuzzy_ratio("APLE", "APPLE INC.") >= FUZZY_CUTOFF
    assert fuzzy_ratio("MICROSFT", "MICROSOFT CORP") >= FUZZY_CUTOFF


def test_scores_against_best_word_of_multiword_text():
    # Single-word query lands on NVIDIA, not dragged down by CORP.
    assert fuzzy_ratio("NVIDAI", "NVIDIA CORP") >= FUZZY_CUTOFF


def test_multiword_query_needs_every_word():
    assert fuzzy_ratio("BANK OF AMRICA", "BANK OF AMERICA CORP") >= FUZZY_CUTOFF
    # One word landing, one word junk: mean drops below the cutoff.
    assert fuzzy_ratio("BANK QXZWV", "BANK OF AMERICA CORP") < FUZZY_CUTOFF


def test_unrelated_stays_below_cutoff():
    assert fuzzy_ratio("ORACLE", "NVIDIA CORP") < FUZZY_CUTOFF
    assert fuzzy_ratio("QXZWV", "APPLE INC.") < FUZZY_CUTOFF


def test_empty_sides_score_zero():
    assert fuzzy_ratio("", "APPLE") == 0.0
    assert fuzzy_ratio("APPLE", "") == 0.0


def test_min_query_constant_sane():
    assert MIN_QUERY >= 3  # 1-2 letter queries must never hit the fuzzy path
