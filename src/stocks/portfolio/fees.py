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
mid, sells below. `outside_range_eur` is the portion that is definitely
markup — executions beyond the day's exchange high/low (typical of
market-maker markups on FX/crypto legs). Bars must be UNADJUSTED for
dividends (auto_adjust=False); split adjustment is replayed here from the
ledger's own `split` rows, so pre-split executions land on Yahoo's
split-adjusted scale before comparing.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from stocks.data.fx import ToEur, prefetch
from stocks.data.fx import to_eur as _fx_to_eur
from stocks.portfolio.ledger import Transaction


@dataclass
class BrokerFees:
    broker: str
    trades: int = 0
    volume_eur: float = 0.0  # gross executed value, buys + sells
    commission_eur: float = 0.0  # fee field on buy/sell rows
    other_fees_eur: float = 0.0  # standalone action="fee" rows

    @property
    def explicit_eur(self) -> float:
        return self.commission_eur + self.other_fees_eur


@dataclass
class SpreadStats:
    broker: str
    measured: int = 0  # trades with a bar on the trade date
    skipped: int = 0  # trades with no usable bar (not in `bars`, NaN, holiday)
    measured_volume_eur: float = 0.0
    spread_eur: float = 0.0  # signed: + = paid above mid, - = beat the mid
    outside_range_eur: float = 0.0  # executions beyond the day's high/low

    @property
    def spread_bps(self) -> float:
        """Average round-cost of measured executions, in basis points."""
        if not self.measured_volume_eur:
            return 0.0
        return self.spread_eur / self.measured_volume_eur * 1e4


def broker_of(tx: Transaction) -> str:
    """Broker label for a ledger row: first word of the importer-stamped note
    ("revolut crypto BTC" -> "revolut"); hand-entered rows -> "manual"."""
    words = tx.note.split()
    return words[0].lower() if words else "manual"


def by_broker(
    transactions: list[Transaction], to_eur: ToEur | None = None
) -> dict[str, BrokerFees]:
    """Explicit ledger costs per broker, valued in EUR at each row's date."""
    rows = [t for t in transactions if t.action in ("buy", "sell", "fee")]
    if to_eur is None:
        prefetch((t.date, t.currency) for t in rows)
        to_eur = _fx_to_eur
    out: dict[str, BrokerFees] = {}
    for tx in rows:
        bf = out.setdefault(broker_of(tx), BrokerFees(broker=broker_of(tx)))
        if tx.action == "fee":
            # Convention: amount in `fee`; tolerate rows that put it in `price`.
            bf.other_fees_eur += to_eur(tx.fee or tx.price, tx.currency, tx.date)
            continue
        bf.trades += 1
        bf.volume_eur += to_eur(tx.quantity * tx.price, tx.currency, tx.date)
        bf.commission_eur += to_eur(tx.fee, tx.currency, tx.date)
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
    for ts, high, low in zip(df.index, df["High"], df["Low"]):
        if pd.notna(high) and pd.notna(low):
            out[ts.date().isoformat()] = (float(high), float(low))
    return out


def spread_by_broker(
    transactions: list[Transaction],
    bars: dict[str, pd.DataFrame],
    to_eur: ToEur | None = None,
) -> dict[str, SpreadStats]:
    """Execution-vs-midpoint cost per broker from daily unadjusted OHLC bars."""
    trades = [
        t for t in transactions
        if t.action in ("buy", "sell") and t.quantity > 0 and t.price > 0
    ]
    if to_eur is None:
        prefetch((t.date, t.currency) for t in trades)
        to_eur = _fx_to_eur
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
        # price/qty scale inversely, so EUR values are unchanged by `ratio`.
        ratio = 1.0
        for day, r in splits.get(tx.ticker, []):
            if day > tx.date:
                ratio *= r
        price, qty = tx.price / ratio, tx.quantity * ratio
        diff = price - mid if tx.action == "buy" else mid - price
        outside = max(0.0, price - high) if tx.action == "buy" else max(0.0, low - price)
        st.measured += 1
        st.measured_volume_eur += to_eur(tx.quantity * tx.price, tx.currency, tx.date)
        st.spread_eur += to_eur(diff * qty, tx.currency, tx.date)
        st.outside_range_eur += to_eur(outside * qty, tx.currency, tx.date)
    return out
