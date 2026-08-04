"""Consensus-estimate normalization tests — synthetic data, no network."""

import pandas as pd

from stocks.data.estimates import (
    Consensus,
    RawEstimates,
    consensus,
    estimate_currency,
    long_term_growth,
    projection,
    rating_from_counts,
)


def sample_raw() -> RawEstimates:
    earnings = pd.DataFrame(
        {
            "avg": [1.89, 2.01, 8.77, 9.71],
            "low": [1.83, 1.88, 8.29, 8.81],
            "high": [1.99, 2.13, 9.04, 10.96],
            "growth": [0.2063, 0.0902, 0.1752, 0.1079],
        },
        index=["0q", "+1q", "0y", "+1y"],
    )
    revenue = pd.DataFrame(
        {
            "avg": [1.08e11, 1.14e11, 4.78e11, 5.23e11],
            "growth": [0.158, 0.120, 0.150, 0.092],
        },
        index=["0q", "+1q", "0y", "+1y"],
    )
    recs = pd.DataFrame(
        {
            "period": ["0m", "-1m", "-2m"],
            "strongBuy": [6, 6, 7],
            "buy": [23, 22, 23],
            "hold": [14, 16, 15],
            "sell": [2, 1, 1],
            "strongSell": [2, 2, 2],
        }
    )
    return RawEstimates(
        ticker="TEST",
        price_targets={"current": 333.63, "high": 400.0, "low": 215.0,
                       "mean": 318.81, "median": 329.0},
        earnings_estimate=earnings,
        revenue_estimate=revenue,
        recommendations=recs,
    )


def test_consensus_extracts_targets_and_estimates():
    c = consensus(sample_raw())
    assert c.price == 333.63
    assert c.target_mean == 318.81
    assert c.eps_next_fy == 9.71
    assert c.eps_growth_next_fy == 0.1079
    assert c.rev_next_fy == 5.23e11
    assert c.rev_growth_next_fy == 0.092


def test_consensus_target_upside():
    c = consensus(sample_raw())
    assert c.target_upside == (318.81 / 333.63 - 1)  # slightly negative


def test_consensus_rating_uses_current_period_row():
    c = consensus(sample_raw())
    # 0m split skews buy: mean well under 2.5 -> "buy".
    assert c.rating == "buy"
    assert 1.0 < c.rating_mean < 3.0
    assert c.rating_counts["buy"] == 23


def test_rating_from_counts_bins():
    assert rating_from_counts({"strongBuy": 10})[0] == "strong buy"
    assert rating_from_counts({"hold": 10})[0] == "hold"
    assert rating_from_counts({"strongSell": 10})[0] == "strong sell"
    assert rating_from_counts({}) == (None, None)


def test_consensus_empty_degrades_to_none():
    c = consensus(RawEstimates("EMPTY"))
    assert isinstance(c, Consensus)
    assert c.price is None
    assert c.eps_next_fy is None
    assert c.rating is None
    assert c.rating_counts == {}
    assert c.target_upside is None


def test_projection_three_years_with_extrapolated_tail():
    p = projection(sample_raw(), last_fy=2024, years=3)
    assert list(p.index) == ["2025E", "2026E", "2027E"]
    # Years 1-2 straight from the 0y/+1y consensus.
    assert p.loc["2025E", "Revenue"] == 4.78e11
    assert p.loc["2026E", "EPS"] == 9.71
    assert p.loc["2025E", "EPSLow"] == 8.29
    assert not p.loc["2026E", "RevenueExt"]
    # Year 3 extrapolated at the +1y consensus growth rate, no low/high range.
    assert p.loc["2027E", "RevenueExt"]
    assert p.loc["2027E", "EPSExt"]
    assert pd.isna(p.loc["2027E", "EPSLow"])
    assert abs(p.loc["2027E", "Revenue"] - 5.23e11 * 1.092) < 1e6
    assert abs(p.loc["2027E", "EPS"] - 9.71 * 1.1079) < 1e-6


def test_projection_uses_ltg_for_eps_when_published():
    raw = sample_raw()
    raw.growth_estimates = pd.DataFrame(
        {"stockTrend": [0.88, 0.43, 0.20], "indexTrend": [0.29, 0.14, 0.12]},
        index=["0y", "+1y", "LTG"],
    )
    p = projection(raw, last_fy=2024, years=3)
    assert abs(p.loc["2027E", "EPS"] - 9.71 * 1.20) < 1e-9
    # Revenue keeps its own consensus growth — LTG is an EPS figure.
    assert abs(p.loc["2027E", "Revenue"] - 5.23e11 * 1.092) < 1e6


def test_projection_no_extrapolation_from_losses():
    earnings = pd.DataFrame(
        {"avg": [-1.0, -0.5]}, index=["0y", "+1y"]
    )
    raw = RawEstimates("LOSS", earnings_estimate=earnings)
    p = projection(raw, last_fy=2024, years=3)
    assert list(p.index) == ["2025E", "2026E"]  # tail never invented
    assert p["EPS"].tolist() == [-1.0, -0.5]


def test_projection_empty_without_consensus():
    assert projection(RawEstimates("EMPTY"), last_fy=2024).empty


def test_estimate_currency():
    df = pd.DataFrame({"avg": [1.0], "currency": ["USD"]}, index=["0y"])
    assert estimate_currency(df) == "USD"
    assert estimate_currency(pd.DataFrame()) is None
    assert estimate_currency(sample_raw().revenue_estimate) is None  # no column


def test_long_term_growth():
    df = pd.DataFrame({"stockTrend": [0.43, 0.12]}, index=["+1y", "LTG"])
    assert long_term_growth(df) == 0.12
    nan = pd.DataFrame({"stockTrend": [0.43, float("nan")]}, index=["+1y", "LTG"])
    assert long_term_growth(nan) is None
    assert long_term_growth(pd.DataFrame()) is None
