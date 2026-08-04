"""Fundamentals tests — synthetic data, no network."""

import pandas as pd

from stocks.analysis.fundamentals import (
    KPI_SOURCES,
    METRIC_ORDER,
    annual_financials,
    cagr,
    comp_medals,
    comp_scores,
    comparables_table,
    compute_metrics,
    format_value,
    quarterly_eps,
    sources_table,
    verdict,
    verdict_md,
)
from stocks.data.fundamentals import RawFundamentals

YEARS = ["2025", "2024", "2023", "2022", "2021"]  # newest-first like yfinance


def sample_raw() -> RawFundamentals:
    income = pd.DataFrame(
        {
            "Total Revenue": [400e9, 380e9, 365e9, 350e9, 300e9],
            "Net Income": [100e9, 95e9, 90e9, 85e9, 70e9],
            "EBIT": [120e9, 115e9, 110e9, 105e9, 90e9],
            "EBITDA": [130e9, 125e9, 120e9, 115e9, 100e9],
            "Tax Rate For Calcs": [0.15, 0.15, 0.16, 0.16, 0.14],
            "Diluted Average Shares": [15e9, 15.3e9, 15.6e9, 16e9, 16.5e9],
        },
        index=YEARS,
    ).T
    balance = pd.DataFrame(
        {"Invested Capital": [200e9] * 5, "Net Debt": [50e9] * 5}, index=YEARS
    ).T
    cashflow = pd.DataFrame(
        {"Free Cash Flow": [95e9, 90e9, 88e9, 82e9, 70e9]}, index=YEARS
    ).T
    info = {
        "currency": "USD",
        "currentPrice": 250.0,
        "marketCap": 3.8e12,
        "enterpriseValue": 3.85e12,
        "trailingPE": 38.0,
        "forwardPE": 33.0,
        "trailingPegRatio": 2.6,
        "priceToBook": 44.0,
        "enterpriseToEbitda": 29.6,
        "enterpriseToRevenue": 10.5,
        "returnOnEquity": 1.41,
        "grossMargins": 0.478,
        "operatingMargins": 0.322,
        "profitMargins": 0.271,
    }
    return RawFundamentals("TEST", info, income, balance, cashflow)


def test_cagr_basic():
    assert abs(cagr(100, 200, 4) - (2**0.25 - 1)) < 1e-9
    assert cagr(0, 100, 4) is None
    assert cagr(-5, 100, 4) is None
    assert cagr(100, 100, 0) is None


def test_compute_metrics_derived():
    m = compute_metrics(sample_raw())
    # ROIC = 120e9 * (1 - 0.15) / 200e9
    assert abs(m["roic"] - 0.51) < 1e-9
    # FCF yield = 95e9 / 3.8e12
    assert abs(m["fcf_yield"] - 0.025) < 1e-9
    # Net debt / EBITDA = 50 / 130
    assert abs(m["net_debt_ebitda"] - 50 / 130) < 1e-9
    # Cash conversion = 95 / 100
    assert abs(m["cash_conversion"] - 0.95) < 1e-9
    # Dilution: shares shrink 16.5e9 -> 15e9 over 4y => negative CAGR (buybacks)
    assert m["share_dilution"] < 0
    assert m["revenue_cagr"] > 0


def test_compute_metrics_missing_data():
    raw = RawFundamentals("EMPTY")
    m = compute_metrics(raw)
    for key in METRIC_ORDER:
        assert m.get(key) is None, key


def test_kpi_sources_cover_metric_order():
    assert set(METRIC_ORDER) == set(KPI_SOURCES)
    m = compute_metrics(sample_raw())
    for key in METRIC_ORDER:
        assert key in m, f"compute_metrics missing {key}"


def test_format_value():
    assert format_value("pe_ttm", None) == "n/a"
    assert format_value("pe_ttm", 38.0) == "38.0x"
    assert format_value("roic", 0.51) == "51.0%"
    assert format_value("market_cap", 3.8e12) == "3.80T"
    assert format_value("fcf", 95e9) == "95.00B"


def test_comparables_table_shape():
    m = compute_metrics(sample_raw())
    peer = dict(m, ticker="PEER")
    table = comparables_table([m, peer])
    assert list(table.columns) == ["TEST", "PEER"]
    assert len(table) == len(METRIC_ORDER)


def test_annual_financials_shape_and_order():
    cols = pd.to_datetime([f"{y}-12-31" for y in YEARS])  # yfinance: Timestamp cols
    income = pd.DataFrame(
        {
            "Total Revenue": [400e9, 380e9, 365e9, 350e9, 300e9],
            "Net Income": [100e9, 95e9, 90e9, 85e9, 70e9],
            "Diluted EPS": [6.6, 6.2, 5.8, 5.3, 4.2],
        },
        index=cols,
    ).T
    fin = annual_financials(RawFundamentals("TEST", income=income))
    assert list(fin.columns) == ["Revenue", "Net Income", "EPS"]
    assert list(fin.index) == [2021, 2022, 2023, 2024, 2025]  # oldest-first
    assert fin.loc[2021, "Revenue"] == 300e9
    assert fin.loc[2025, "EPS"] == 6.6


def test_annual_financials_empty():
    assert annual_financials(RawFundamentals("EMPTY")).empty


def test_quarterly_eps_shape_and_order():
    cols = pd.to_datetime(
        ["2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30"]
    )  # yfinance: newest-first Timestamp cols
    income_q = pd.DataFrame(
        {
            "Total Revenue": [110e9, 105e9, 100e9, 95e9],
            "Diluted EPS": [1.8, 1.7, 1.6, 1.5],
        },
        index=cols,
    ).T
    eps = quarterly_eps(RawFundamentals("TEST", income_q=income_q))
    assert list(eps.columns) == ["EPS"]
    assert list(eps.index) == ["2024Q3", "2024Q4", "2025Q1", "2025Q2"]  # oldest-first
    assert eps.loc["2025Q2", "EPS"] == 1.8


def test_quarterly_eps_empty():
    assert quarterly_eps(RawFundamentals("EMPTY")).empty


def test_sources_table_levels():
    df = sources_table()
    assert set(df["Level"]) <= {"fact", "consensus", "derived"}
    assert len(df) == len(KPI_SOURCES)


def test_verdict_bands():
    # valuation multiples — lower is cheaper
    assert verdict("pe_ttm", 12) == ("cheap", "green")
    assert verdict("pe_ttm", 55) == ("very expensive", "red")
    assert verdict("peg", 0.8) == ("cheap", "green")
    # momentum — RSI oversold / neutral / overbought
    assert verdict("rsi", 22.0) == ("oversold", "green")
    assert verdict("rsi", 52.3) == ("neutral", "gray")
    assert verdict("rsi", 80) == ("overbought", "red")
    # quality (fractions) — higher is better
    assert verdict("roic", 0.30) == ("strong", "green")
    # leverage — negative is net cash
    assert verdict("net_debt_ebitda", -1.2) == ("net cash", "green")


def test_verdict_missing_and_nonnumeric():
    assert verdict("peg", None) is None
    assert verdict("rsi", float("nan")) is None  # NaN falls through every band
    assert verdict("roic", True) is None  # bools rejected
    assert verdict("unknown_key", 1.0) is None
    assert verdict_md("peg", None) == ""
    assert verdict_md("pe_ttm", 12) == ":green[cheap]"


def _peer(ticker: str, better: float) -> dict:
    """Synthetic metrics dict; `better` in [0,1] scales every KPI so higher
    `better` wins on all fronts (cheaper multiples, stronger quality/growth)."""
    return {
        "ticker": ticker,
        # lower-better: shrink as `better` grows
        "pe_ttm": 40 - 25 * better,
        "peg": 3 - 2 * better,
        "pb": 10 - 8 * better,
        "ev_ebitda": 30 - 20 * better,
        "net_debt_ebitda": 3 - 4 * better,
        # higher-better: grow with `better`
        "roic": 0.05 + 0.25 * better,
        "fcf_yield": 0.01 + 0.05 * better,
        "revenue_cagr": 0.02 + 0.2 * better,
    }


def test_comp_scores_orders_by_dominance():
    rows = [_peer("BEST", 1.0), _peer("MID", 0.5), _peer("WORST", 0.0)]
    scores = comp_scores(rows)
    assert scores["BEST"] > scores["MID"] > scores["WORST"]
    assert scores["BEST"] == 1.0 and scores["WORST"] == 0.0


def test_comp_scores_skips_sparse_and_nonnumeric():
    rows = [
        _peer("A", 1.0),
        _peer("B", 0.0),
        # only two rankable KPIs -> below _RANK_MIN_METRICS, no score
        {"ticker": "SPARSE", "pe_ttm": 10, "roic": 0.4},
        # non-numeric / NaN / bool values never rank
        {"ticker": "JUNK", "pe_ttm": "n/a", "roic": float("nan"), "peg": True},
    ]
    scores = comp_scores(rows)
    assert set(scores) == {"A", "B"}


def test_comp_medals_podium():
    rows = [_peer(t, x) for t, x in
            [("G", 1.0), ("S", 0.75), ("B", 0.5), ("D", 0.0)]]
    assert comp_medals(rows) == {"G": "🥇", "S": "🥈", "B": "🥉"}
    # fewer than 3 qualifying tickers -> no podium at all
    assert comp_medals(rows[:2]) == {}
