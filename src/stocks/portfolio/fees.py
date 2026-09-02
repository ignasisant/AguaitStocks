"""Broker running costs: explicit commissions plus an execution-spread estimate.

Explicit costs come straight off the ledger, grouped by broker (the first word
of each row's `note` — importers stamp "revolut", "degiro ...", "ibkr", ...):
the `fee` field on buy/sell rows (commission in native ccy) and standalone
`fee` rows (custody, account charges). Dividend rows are excluded — their
`fee` is withholding tax, handled in stocks.portfolio.dividends.

The spread estimate compares each execution price against the trade day's
session midpoint ((high+low)/2 of that day's bar). One trade against a daily
bar is mostly intraday noise, but summed over a book the noise cancels and the
systematic part that remains is the broker's spread/markup: buys print above
mid, sells below. `outside_range` is the portion that is definitely
markup — executions beyond the day's exchange high/low (typical of
market-maker markups on FX/crypto legs). Bars must be UNADJUSTED for
dividends (auto_adjust=False); split adjustment is replayed here from the
ledger's own `split` rows, so pre-split executions land on Yahoo's
split-adjusted scale before comparing.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stocks.data.fx import ToBase, converter, prefetch
from stocks.portfolio.ledger import Transaction


@dataclass
class BrokerFees:
    broker: str
    trades: int = 0
    volume: float = 0.0  # gross executed value, buys + sells
    commission: float = 0.0  # fee field on buy/sell rows
    other_fees: float = 0.0  # standalone action="fee" rows

    @property
    def explicit(self) -> float:
        return self.commission + self.other_fees


@dataclass
class SpreadStats:
    broker: str
    measured: int = 0  # trades with a bar on the trade date
    skipped: int = 0  # trades with no usable bar (not in `bars`, NaN, holiday)
    measured_volume: float = 0.0
    spread: float = 0.0  # signed: + = paid above mid, - = beat the mid
    outside_range: float = 0.0  # executions beyond the day's high/low

    @property
    def spread_bps(self) -> float:
        """Average round-cost of measured executions, in basis points."""
        if not self.measured_volume:
            return 0.0
        return self.spread / self.measured_volume * 1e4


def broker_of(tx: Transaction) -> str:
    """Broker label for a ledger row: first word of the importer-stamped note
    ("revolut crypto BTC" -> "revolut"); hand-entered rows -> "manual"."""
    words = tx.note.split()
    return words[0].lower() if words else "manual"


def by_broker(
    transactions: list[Transaction],
    to_base: ToBase | None = None,
    base: str = "EUR",
) -> dict[str, BrokerFees]:
    """Explicit ledger costs per broker, valued at each row's own date."""
    rows = [t for t in transactions if t.action in ("buy", "sell", "fee")]
    if to_base is None:
        prefetch((t.date, t.currency) for t in rows)
        to_base = converter(base)
    out: dict[str, BrokerFees] = {}
    for tx in rows:
        bf = out.setdefault(broker_of(tx), BrokerFees(broker=broker_of(tx)))
        if tx.action == "fee":
            # Convention: amount in `fee`; tolerate rows that put it in `price`.
            bf.other_fees += to_base(tx.fee or tx.price, tx.currency, tx.date)
            continue
        bf.trades += 1
        bf.volume += to_base(tx.quantity * tx.price, tx.currency, tx.date)
        bf.commission += to_base(tx.fee, tx.currency, tx.date)
    return out


def _split_factors(transactions: list[Transaction]) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    for t in transactions:
        if t.action == "split" and t.quantity > 0:
            out.setdefault(t.ticker, []).append((t.date, t.quantity))
    return out


def _day_bars(df: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """ISO date -> (high, low) for one ticker's daily OHLC frame."""
    if df is None or df.empty or "High" not in df or "Low" not in df:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for ts, high, low in zip(df.index, df["High"], df["Low"], strict=True):
        if pd.notna(high) and pd.notna(low):
            out[ts.date().isoformat()] = (float(high), float(low))
    return out


def spread_by_broker(
    transactions: list[Transaction],
    bars: dict[str, pd.DataFrame],
    to_base: ToBase | None = None,
    base: str = "EUR",
) -> dict[str, SpreadStats]:
    """Execution-vs-midpoint cost per broker from daily unadjusted OHLC bars."""
    trades = [
        t for t in transactions
        if t.action in ("buy", "sell") and t.quantity > 0 and t.price > 0
    ]
    if to_base is None:
        prefetch(((t.date, t.currency) for t in trades), quote=base)
        to_base = converter(base)
    splits = _split_factors(transactions)
    day_bars = {tk: _day_bars(df) for tk, df in bars.items()}
    out: dict[str, SpreadStats] = {}
    for tx in trades:
        st = out.setdefault(broker_of(tx), SpreadStats(broker=broker_of(tx)))
        bar = day_bars.get(tx.ticker, {}).get(tx.date)
        if bar is None:
            st.skipped += 1
            continue
        high, low = bar
        mid = (high + low) / 2
        if mid <= 0:
            st.skipped += 1
            continue
        # Yahoo bars are split-adjusted; scale pre-split executions to match.
        # price/qty scale inversely, so converted values are unchanged by `ratio`.
        ratio = 1.0
        for day, r in splits.get(tx.ticker, []):
            if day > tx.date:
                ratio *= r
        price, qty = tx.price / ratio, tx.quantity * ratio
        diff = price - mid if tx.action == "buy" else mid - price
        outside = max(0.0, price - high) if tx.action == "buy" else max(0.0, low - price)
        st.measured += 1
        st.measured_volume += to_base(tx.quantity * tx.price, tx.currency, tx.date)
        st.spread += to_base(diff * qty, tx.currency, tx.date)
        st.outside_range += to_base(outside * qty, tx.currency, tx.date)
    return out
