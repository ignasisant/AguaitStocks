"""Moat scoring tests — synthetic data, no network."""

import pandas as pd

from stocks.analysis.fundamentals import compute_metrics, format_value, verdict
from stocks.analysis.moat import (
    PILLAR_WEIGHTS,
    MoatScore,
    moat_rating,
    moat_score,
)
from stocks.data.fundamentals import RawFundamentals

YEARS = ["2025", "2024", "2023", "2022", "2021"]  # newest-first like yfinance


def wide_moat_raw() -> RawFundamentals:
    """Compounder: high stable ROIC, fat steady margins, buybacks."""
    income = pd.DataFrame(
        {
            "Total Revenue": [400e9, 380e9, 365e9, 350e9, 300e9],
            "Gross Profit": [190e9, 180e9, 172e9, 165e9, 140e9],
            "Net Income": [100e9, 95e9, 90e9, 85e9, 70e9],
            "EBIT": [120e9, 115e9, 110e9, 105e9, 90e9],
            "Tax Rate For Calcs": [0.15, 0.15, 0.16, 0.16, 0.14],
            "Diluted Average Shares": [15e9, 15.3e9, 15.6e9, 16e9, 16.5e9],
        },
        index=YEARS,
    ).T
    balance = pd.DataFrame({"Invested Capital": [200e9] * 5}, index=YEARS).T
    cashflow = pd.DataFrame(
        {"Free Cash Flow": [95e9, 90e9, 88e9, 82e9, 70e9]}, index=YEARS
    ).T
    return RawFundamentals("WIDE", {}, income, balance, cashflow)


def no_moat_raw() -> RawFundamentals:
    """Value trap: thin volatile returns, shrinking revenue, dilution."""
    income = pd.DataFrame(
        {
            "Total Revenue": [80e9, 90e9, 100e9, 110e9, 120e9],
            "Gross Profit": [12e9, 14e9, 15e9, 17e9, 18e9],
            "Net Income": [1e9, -2e9, 2e9, -1e9, 3e9],
            "EBIT": [2e9, -1e9, 3e9, 1e9, 4e9],
            "Tax Rate For Calcs": [0.25] * 5,
            "Diluted Average Shares": [12e9, 11.5e9, 11e9, 10.5e9, 10e9],
        },
        index=YEARS,
    ).T
    balance = pd.DataFrame({"Invested Capital": [150e9] * 5}, index=YEARS).T
    cashflow = pd.DataFrame(
        {"Free Cash Flow": [-2e9, 1e9, -3e9, 0.5e9, -1e9]}, index=YEARS
    ).T
    return RawFundamentals("TRAP", {}, income, balance, cashflow)


def test_weights_sum_to_one():
    assert abs(sum(PILLAR_WEIGHTS.values()) - 1.0) < 1e-9


def test_wide_moat_scores_wide():
    ms = moat_score(wide_moat_raw())
    assert isinstance(ms, MoatScore)
    assert ms.rating == "wide"
    assert ms.score >= 70
    assert ms.years == 5
    # every pillar scored on this fixture
    assert all(p.score is not None for p in ms.pillars)
    # ROIC ~51% every year -> level and persistence both max out
    roic = next(p for p in ms.pillars if p.key == "roic")
    assert roic.score == 100
    # buybacks (shares shrink) -> dilution pillar maxes out
    dilution = next(p for p in ms.pillars if p.key == "dilution")
    assert dilution.score == 100


def test_no_moat_scores_low():
    ms = moat_score(no_moat_raw())
    assert ms.rating == "no moat"
    assert ms.score < 45
    # ROIC ~1-2% -> far below the 10% hurdle every year
    roic = next(p for p in ms.pillars if p.key == "roic")
    assert roic.score < 10
    # revenue shrinks every year
    growth = next(p for p in ms.pillars if p.key == "growth")
    assert growth.score == 0
    # +4.7%/y share growth -> heavy dilution
    dilution = next(p for p in ms.pillars if p.key == "dilution")
    assert dilution.score == 0


def test_missing_statements_yield_none():
    ms = moat_score(RawFundamentals("EMPTY"))
    assert ms.score is None
    assert ms.rating is None
    assert ms.years == 0
    assert all(p.score is None for p in ms.pillars)


def test_snapshot_margin_fallback_alone_is_not_enough():
    # Only info.grossMargins available -> 1 pillar scored -> composite None.
    ms = moat_score(RawFundamentals("SNAP", info={"grossMargins": 0.55}))
    margins = next(p for p in ms.pillars if p.key == "gross_margin")
    assert margins.score is not None
    assert "snapshot" in margins.detail
    assert ms.score is None


def test_moat_rating_bands():
    assert moat_rating(None) is None
    assert moat_rating(85) == "wide"
    assert moat_rating(70) == "wide"
    assert moat_rating(69.9) == "narrow"
    assert moat_rating(45) == "narrow"
    assert moat_rating(44.9) == "no moat"


def test_moat_flows_into_metrics_and_formatting():
    m = compute_metrics(wide_moat_raw())
    assert m["moat"] is not None and m["moat"] >= 70
    assert format_value("moat", m["moat"]).endswith("/100")
    label, color = verdict("moat", m["moat"])
    assert (label, color) == ("wide", "green")
    assert verdict("moat", 30) == ("no moat", "red")
    assert verdict("moat", 55) == ("narrow", "orange")


def test_invested_capital_nonpositive_years_dropped():
    income = pd.DataFrame(
        {
            "Total Revenue": [100e9, 90e9],
            "EBIT": [20e9, 18e9],
            "Tax Rate For Calcs": [0.2, 0.2],
        },
        index=YEARS[:2],
    ).T
    balance = pd.DataFrame({"Invested Capital": [80e9, -5e9]}, index=YEARS[:2]).T
    ms = moat_score(RawFundamentals("NEG", {}, income, balance))
    roic = next(p for p in ms.pillars if p.key == "roic")
    # only the positive-capital year counts: 20*0.8/80 = 20%
    assert roic.score is not None
    assert "1/1 years" in roic.detail
