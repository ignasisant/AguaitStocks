"""Portfolio analytics tests — synthetic price/return frames, no network."""

import numpy as np
import pandas as pd

from stocks.analysis.portfolio import (
    allocation,
    annualized_volatility,
    beta,
    effective_positions,
    equal_weights,
    hhi,
    market_value_weights,
    max_drawdown,
    portfolio_returns,
    position_table,
    returns_frame,
    top_n_weight,
    weights_from_holdings,
)
from stocks.config import Holding


def test_equal_weights_sum_to_one():
    w = equal_weights(["A", "B", "C", "D"])
    assert abs(sum(w.values()) - 1.0) < 1e-12
    assert all(abs(v - 0.25) < 1e-12 for v in w.values())


def test_market_value_weights():
    w = market_value_weights({"A": 10, "B": 10}, {"A": 30.0, "B": 10.0})
    assert abs(w["A"] - 0.75) < 1e-12
    assert abs(w["B"] - 0.25) < 1e-12


def test_weights_prefer_positions_over_equal():
    holds = [
        Holding("A", shares=10),
        Holding("B", shares=0),  # watchlist-only, excluded when positions exist
    ]
    w = weights_from_holdings(holds, {"A": 5.0, "B": 5.0})
    assert set(w) == {"A"}
    assert abs(w["A"] - 1.0) < 1e-12


def test_weights_equal_when_no_positions():
    holds = [Holding("A"), Holding("B")]
    w = weights_from_holdings(holds, {"A": 5.0, "B": 5.0})
    assert set(w) == {"A", "B"}
    assert abs(w["A"] - 0.5) < 1e-12


def _prices() -> dict[str, pd.Series]:
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    return {
        "A": pd.Series([100, 101, 102, 103, 104, 105], index=idx, dtype=float),
        "B": pd.Series([50, 49, 50, 51, 50, 52], index=idx, dtype=float),
    }


def test_returns_frame_shape():
    r = returns_frame(_prices())
    assert list(r.columns) == ["A", "B"]
    assert len(r) == 5  # 6 prices -> 5 returns


def test_portfolio_returns_weighting():
    r = returns_frame(_prices())
    p = portfolio_returns(r, {"A": 0.5, "B": 0.5})
    # first day: A +1%, B -2% -> mean -0.5%
    assert abs(p.iloc[0] - ((0.01 + (-0.02)) / 2)) < 1e-12


def test_annualized_volatility_zero_for_constant():
    r = pd.Series([0.0, 0.0, 0.0, 0.0])
    assert annualized_volatility(r) == 0.0


def test_beta_of_series_with_itself_is_one():
    r = pd.Series([0.01, -0.02, 0.03, -0.01, 0.02])
    assert abs(beta(r, r) - 1.0) < 1e-9


def test_beta_scaled():
    b = pd.Series([0.01, -0.02, 0.03, -0.01, 0.02])
    a = b * 2  # perfectly correlated, twice the moves
    assert abs(beta(a, b) - 2.0) < 1e-9


def test_max_drawdown():
    prices = pd.Series([100, 120, 90, 110, 60], dtype=float)
    # peak 120 -> trough 60 => -50%
    assert abs(max_drawdown(prices) - (-0.5)) < 1e-12


def test_concentration_metrics():
    w = {"A": 0.5, "B": 0.3, "C": 0.2}
    assert abs(hhi(w) - (0.25 + 0.09 + 0.04)) < 1e-12
    assert abs(effective_positions({"A": 0.5, "B": 0.5}) - 2.0) < 1e-12
    assert abs(top_n_weight(w, 2) - 0.8) < 1e-12


def test_allocation_groups_by_meta():
    w = {"A": 0.5, "B": 0.3, "C": 0.2}
    meta = {
        "A": {"sector": "Tech"},
        "B": {"sector": "Tech"},
        "C": {"sector": "Health"},
    }
    alloc = allocation(w, meta, "sector")
    assert abs(alloc["Tech"] - 0.8) < 1e-12
    assert abs(alloc["Health"] - 0.2) < 1e-12


def test_allocation_unknown_bucket():
    alloc = allocation({"A": 1.0}, {"A": {}}, "country")
    assert alloc["Unknown"] == 1.0


def test_position_table_pnl():
    holds = [Holding("A", shares=10, cost=90.0), Holding("B")]  # B has no shares
    tbl = position_table(holds, {"A": 100.0, "B": 20.0})
    assert list(tbl.index) == ["A"]
    assert tbl.loc["A", "value"] == 1000.0
    assert abs(tbl.loc["A", "pnl"] - 100.0) < 1e-9
    assert abs(tbl.loc["A", "pnl_pct"] - (1000 / 900 - 1)) < 1e-9


def test_position_table_empty_without_shares():
    assert position_table([Holding("A"), Holding("B")], {"A": 1.0}).empty


def test_market_value_weights_ignore_missing_price():
    w = market_value_weights({"A": 10, "B": 10}, {"A": 5.0})  # B has no price
    assert set(w) == {"A"}
    assert not np.isnan(w["A"])


# ------------------------------------------------------------ injected vs value
from stocks.analysis.portfolio import (  # noqa: E402
    injected_series,
    injected_vs_value,
    shares_frame,
)
from stocks.portfolio.ledger import Transaction  # noqa: E402


def _eur(amount: float, currency: str, day: str) -> float:
    return amount * (0.5 if currency == "USD" else 1.0)


def test_shares_frame_replays_buys_sells_splits():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=1),
        Transaction("2024-01-03", "A", "buy", quantity=5, price=1),
        Transaction("2024-01-05", "A", "sell", quantity=8, price=1),
        Transaction("2024-01-02", "B", "buy", quantity=2, price=1),
        Transaction("2024-01-04", "B", "split", quantity=4),
    ]
    f = shares_frame(txs, end="2024-01-06")
    assert f.loc["2024-01-01", "A"] == 10
    assert f.loc["2024-01-04", "A"] == 15
    assert f.loc["2024-01-06", "A"] == 7
    assert f.loc["2024-01-01", "B"] == 0  # before first buy
    assert f.loc["2024-01-03", "B"] == 2
    assert f.loc["2024-01-05", "B"] == 8  # 4:1 split


def test_injected_series_buys_minus_net_sell_proceeds():
    txs = [
        Transaction(
            "2024-01-01", "A", "buy", quantity=10, price=10, fee=5, currency="EUR"
        ),
        Transaction(
            "2024-01-03", "A", "sell", quantity=4, price=12, fee=3, currency="EUR"
        ),
        Transaction("2024-01-04", "A", "dividend", price=50, currency="EUR"),
    ]
    s = injected_series(txs, to_eur=_eur)
    assert len(s) == 2  # dividend ignored
    assert abs(s.iloc[0] - 105.0) < 1e-9  # cost incl. fee
    assert abs(s.iloc[-1] - 60.0) < 1e-9  # minus net proceeds (48 - 3)


def test_injected_vs_value_marks_to_market_in_eur():
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=10, currency="USD"),
        Transaction("2024-01-02", "B", "buy", quantity=1, price=99, currency="EUR"),
    ]
    idx = pd.date_range("2024-01-01", "2024-01-04", freq="D")
    closes = {"A": pd.Series([10.0, 12.0, 12.0, 14.0], index=idx)}  # B: no prices
    fx = {"USD": pd.Series(0.5, index=idx)}
    df = injected_vs_value(txs, closes, fx, to_eur=_eur)
    assert abs(df.loc["2024-01-01", "injected_eur"] - 50.0) < 1e-9
    assert abs(df.loc["2024-01-01", "value_eur"] - 50.0) < 1e-9
    assert abs(df.loc["2024-01-01", "pnl_pct"] - 0.0) < 1e-9
    assert abs(df.loc["2024-01-02", "injected_eur"] - 149.0) < 1e-9
    # B has no close series: carried at cost (99) while held.
    assert abs(df.loc["2024-01-02", "value_eur"] - (60.0 + 99.0)) < 1e-9
    assert abs(df.loc["2024-01-04", "value_eur"] - (70.0 + 99.0)) < 1e-9
    assert abs(df.loc["2024-01-04", "pnl_pct"] - (169.0 / 149.0 - 1)) < 1e-9


def test_injected_vs_value_empty_ledger():
    assert injected_vs_value([], {}, {}).empty


def test_injected_vs_value_stray_quote_outside_holding_carried_at_cost():
    """Delisted-ticker regression (ORGN): one stray quote after the position was
    sold must not zero out the holding period — carry at cost instead."""
    txs = [
        Transaction("2024-01-01", "A", "buy", quantity=10, price=10, currency="EUR"),
        Transaction("2024-01-03", "A", "sell", quantity=10, price=11, currency="EUR"),
    ]
    # Only quote is after the sale — useless for the holding window.
    closes = {"A": pd.Series([1.0], index=pd.to_datetime(["2024-06-01"]))}
    df = injected_vs_value(txs, closes, {}, to_eur=_eur)
    assert abs(df.loc["2024-01-01", "value_eur"] - 100.0) < 1e-9  # at cost
    assert abs(df.loc["2024-01-02", "value_eur"] - 100.0) < 1e-9
    assert abs(df.loc["2024-01-03", "value_eur"] - 0.0) < 1e-9  # sold out
    assert df.attrs["carried_at_cost"] == ["A"]


# ------------------------------------------------------- time-weighted returns
from stocks.analysis.portfolio import (  # noqa: E402
    flow_series,
    time_weighted_returns,
)


def test_portfolio_returns_ipo_partial_history_not_truncated():
    """A late-listing ticker (FIG-style IPO) must not truncate the series."""
    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    returns = pd.DataFrame(
        {
            "A": [0.01, 0.02, 0.01, 0.02],
            "B": [np.nan, np.nan, 0.04, 0.02],  # lists on day 3
        },
        index=idx,
    )
    p = portfolio_returns(returns, {"A": 0.5, "B": 0.5})
    assert len(p) == 4  # full window, not just B's two days
    assert abs(p.iloc[0] - 0.01) < 1e-12  # A alone, weight renormalised
    assert abs(p.iloc[2] - 0.025) < 1e-12  # (0.01 + 0.04) / 2


def test_flow_series_signs_and_ticker_filter():
    txs = [
        Transaction(
            "2024-01-02", "A", "buy", quantity=10, price=10, currency="USD", fee=2
        ),
        Transaction(
            "2024-01-03", "A", "sell", quantity=5, price=12, currency="USD", fee=2
        ),
        Transaction("2024-01-04", "A", "dividend", price=50, currency="EUR", fee=5),
        Transaction("2024-01-02", "B", "buy", quantity=1, price=100, currency="EUR"),
    ]
    f = flow_series(txs, to_eur=_eur, tickers={"A"})
    assert abs(f[pd.Timestamp("2024-01-02")] - 51.0) < 1e-9  # (100 + 2) * 0.5, B skipped
    assert abs(f[pd.Timestamp("2024-01-03")] - (-29.0)) < 1e-9  # -(60 - 2) * 0.5
    assert abs(f[pd.Timestamp("2024-01-04")] - (-45.0)) < 1e-9  # net dividend out


def test_time_weighted_returns_flow_neutral():
    """A mid-period top-up must not change the return path (that's the point of TWR)."""
    idx = pd.date_range("2024-01-01", periods=4, freq="D")
    # 10 shares @100, price +10%/flat/+10%; day 3 buys 5 more @110.
    value = pd.Series([1000.0, 1100.0, 1650.0, 1815.0], index=idx)
    flows = pd.Series(
        {pd.Timestamp("2024-01-01"): 1000.0, pd.Timestamp("2024-01-03"): 550.0}
    )
    r = time_weighted_returns(value, flows)
    assert pd.Timestamp("2024-01-01") not in r.index  # no prior value
    assert abs(r[pd.Timestamp("2024-01-02")] - 0.10) < 1e-9
    assert abs(r[pd.Timestamp("2024-01-03")] - 0.0) < 1e-9  # top-up ≠ performance
    assert abs(r[pd.Timestamp("2024-01-04")] - 0.10) < 1e-9
    cum = float((1 + r).prod()) - 1
    assert abs(cum - 0.21) < 1e-9  # pure price move 100 -> 121


def test_time_weighted_returns_credits_dividends():
    idx = pd.date_range("2024-01-01", periods=2, freq="D")
    value = pd.Series([1000.0, 1000.0], index=idx)
    flows = pd.Series({pd.Timestamp("2024-01-02"): -50.0})  # dividend paid out
    r = time_weighted_returns(value, flows)
    assert abs(r[pd.Timestamp("2024-01-02")] - 0.05) < 1e-9


def test_time_weighted_returns_empty():
    assert time_weighted_returns(pd.Series(dtype=float), pd.Series(dtype=float)).empty


from stocks.analysis.portfolio import basket_change  # noqa: E402


def test_basket_change_day_week_month_windows():
    idx = pd.date_range("2024-01-01", periods=40, freq="D")
    vals = pd.DataFrame({"A": [float(100 + i) for i in range(40)]}, index=idx)
    day = basket_change(vals, 1)
    assert abs(day[0] - 1.0) < 1e-9 and abs(day[1] - 1 / 138) < 1e-9
    week = basket_change(vals, 7)  # anchors exactly 7 days back: 132 -> 139
    assert abs(week[0] - 7.0) < 1e-9 and abs(week[1] - 7 / 132) < 1e-9
    month = basket_change(vals, 30)  # 109 -> 139
    assert abs(month[0] - 30.0) < 1e-9 and abs(month[1] - 30 / 109) < 1e-9


def test_basket_change_window_not_covered():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    vals = pd.DataFrame({"A": [1.0] * 5}, index=idx)
    assert basket_change(vals, 30) is None
    assert basket_change(vals.iloc[:1], 1) is None
    assert basket_change(pd.DataFrame(), 1) is None


def test_basket_change_ignores_tickers_missing_at_either_endpoint():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    vals = pd.DataFrame(
        {
            "A": [100.0, 100.0, 110.0],
            "B": [float("nan"), float("nan"), 500.0],  # listed mid-window
        },
        index=idx,
    )
    chg = basket_change(vals, 1)
    assert abs(chg[0] - 10.0) < 1e-9  # B's appearance isn't a +500 "gain"
    assert abs(chg[1] - 0.10) < 1e-9
