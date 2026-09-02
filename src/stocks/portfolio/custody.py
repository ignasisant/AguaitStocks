"""Where the open shares actually sit: FIFO lots split by broker.

`positions.build` replays the ledger per *security*: Spanish law matches
homogeneous securities across custodians (art. 37 LIRPF), so its lots carry no
broker and its numbers are the tax numbers. Custody is a different question —
in a multi-broker book the same ticker can be half at Revolut and half at
ClickTrade, and only that broker's own buys and sells move its side. This
module replays FIFO per (ticker, broker) pair, the broker taken from the
ledger note prefix exactly as the Fees tab reads it (`fees.broker_of`), so the
share counts say where the shares are while each broker's slice keeps a
FIFO-consistent cost basis in the reporting currency.

A sell stamped with a broker that never held the shares (shares transferred in
kind between brokers, a hand-edited note) would otherwise drive that pair
negative: the remainder falls back to the oldest lots of any broker, so the
totals always reconcile with `positions.build`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from stocks.data.fx import ToBase, converter, prefetch
from stocks.portfolio.fees import broker_of
from stocks.portfolio.ledger import Transaction

# Bucket for an open position no ledger row attributes to a broker. Can only
# happen if the caller mixes frames from different books; kept so allocation
# weights still sum to the whole portfolio instead of quietly dropping a name.
UNKNOWN = "unknown"


@dataclass
class Custody:
    """One ticker's open shares at one broker; cost in the reporting ccy."""

    ticker: str
    broker: str
    quantity: float = 0.0
    cost: float = 0.0


@dataclass
class _Lot:
    broker: str
    quantity: float
    cost: float


def by_position(
    transactions: list[Transaction],
    to_base: ToBase | None = None,
    base: str = "EUR",
) -> dict[str, dict[str, Custody]]:
    """ticker -> broker -> open `Custody`, brokers ordered by share count.

    With no injected converter the ledger's whole FX span is prefetched first,
    the same as `positions.build` (one request per currency, then cache hits).
    """
    if to_base is None:
        prefetch((t.date, t.currency) for t in transactions)
        to_base = converter(base)
    lots: dict[str, list[_Lot]] = defaultdict(list)
    for tx in sorted(transactions, key=lambda t: (t.date, t.id or 0)):
        if tx.action == "buy":
            cost_native = tx.quantity * tx.price + tx.fee
            lots[tx.ticker].append(
                _Lot(
                    broker=broker_of(tx),
                    quantity=tx.quantity,
                    cost=to_base(cost_native, tx.currency, tx.date),
                )
            )
        elif tx.action == "sell":
            _sell(lots[tx.ticker], broker_of(tx), tx.quantity)
        elif tx.action == "split" and tx.quantity > 0:
            # N:1 forward split: every open lot of the ticker scales, at every
            # broker — total cost basis unchanged (same as positions._split).
            for lot in lots[tx.ticker]:
                lot.quantity *= tx.quantity

    out: dict[str, dict[str, Custody]] = {}
    for ticker, queue in lots.items():
        agg: dict[str, Custody] = {}
        for lot in queue:
            if lot.quantity <= 1e-9:
                continue
            row = agg.setdefault(lot.broker, Custody(ticker, lot.broker))
            row.quantity += lot.quantity
            row.cost += lot.cost
        if agg:
            out[ticker] = dict(
                sorted(agg.items(), key=lambda kv: -kv[1].quantity)
            )
    return out


def _sell(queue: list[_Lot], broker: str, quantity: float) -> None:
    """FIFO inside the selling broker's own lots; whatever it can't cover
    (a transfer in kind, a mislabelled note) comes off the oldest lots of any
    broker so the ticker's total still matches the tax replay."""
    remaining = quantity
    for own_only in (True, False):
        for lot in queue:
            if remaining <= 1e-9:
                return
            if lot.quantity <= 1e-9 or (own_only and lot.broker != broker):
                continue
            take = min(lot.quantity, remaining)
            lot.cost -= lot.cost * (take / lot.quantity)
            lot.quantity -= take
            remaining -= take


def mix(row: dict[str, Custody]) -> list[tuple[str, float]]:
    """One position's brokers as (broker, share of shares), largest first."""
    total = sum(c.quantity for c in row.values())
    if total <= 0:
        return []
    return [
        (broker, c.quantity / total)
        for broker, c in sorted(row.items(), key=lambda kv: -kv[1].quantity)
    ]


def broker_weights(
    custody: dict[str, dict[str, Custody]], weights: dict[str, float]
) -> dict[str, float]:
    """Portfolio weights regrouped by broker.

    `weights` is per-ticker (market value, see
    `analysis.portfolio.market_value_weights`); each one is split across
    that ticker's brokers in proportion to the shares they hold, because a
    broker's share of the *value* of one holding is its share of its shares.
    """
    out: dict[str, float] = defaultdict(float)
    for ticker, w in weights.items():
        parts = mix(custody.get(ticker, {}))
        if not parts:
            out[UNKNOWN] += w
            continue
        for broker, share in parts:
            out[broker] += w * share
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
