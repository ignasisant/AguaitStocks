"""FIFO lot matching: transactions -> open positions + realized sales.

Spanish law (art. 37 LIRPF) mandates FIFO for homogeneous securities and values
every leg in EUR at the transaction-date ECB rate. Acquisition cost includes
buy commissions; sale proceeds are net of sell commissions. This module is pure:
the EUR converter is injected so it can be unit-tested without network.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from stocks.data.fx import ToEur, prefetch
from stocks.data.fx import to_eur as _fx_to_eur
from stocks.portfolio.ledger import Transaction


@dataclass
class Lot:
    """An open (unsold) parcel of shares, cost basis in EUR and native ccy."""

    ticker: str
    date: str
    quantity: float
    cost_eur: float  # total EUR basis for `quantity` shares, incl. buy fee
    cost_native: float
    currency: str


@dataclass
class RealizedSale:
    """One buy-lot matched against a sell, both valued in EUR at their dates."""

    ticker: str
    buy_date: str
    sell_date: str
    quantity: float
    cost_eur: float
    proceeds_eur: float
    currency: str

    @property
    def gain_eur(self) -> float:
        return self.proceeds_eur - self.cost_eur


@dataclass
class Position:
    """Aggregated open holding for one ticker."""

    ticker: str
    quantity: float
    cost_eur: float
    cost_native: float
    currency: str

    @property
    def avg_cost_eur(self) -> float:
        return self.cost_eur / self.quantity if self.quantity else 0.0

    @property
    def avg_cost_native(self) -> float:
        return self.cost_native / self.quantity if self.quantity else 0.0


def build(
    transactions: list[Transaction], to_eur: ToEur | None = None
) -> tuple[list[Position], list[RealizedSale]]:
    """Replay the ledger in date order into open positions and realized sales.

    With no injected converter, the whole ledger's FX span is prefetched first
    (one request per currency) so the replay never fetches rates date by date.
    """
    if to_eur is None:
        prefetch((t.date, t.currency) for t in transactions)
        to_eur = _fx_to_eur
    lots: dict[str, deque[Lot]] = defaultdict(deque)
    realized: list[RealizedSale] = []

    for tx in sorted(transactions, key=lambda t: (t.date, t.id or 0)):
        if tx.action == "buy":
            cost_native = tx.quantity * tx.price + tx.fee
            lots[tx.ticker].append(
                Lot(
                    ticker=tx.ticker,
                    date=tx.date,
                    quantity=tx.quantity,
                    cost_eur=to_eur(cost_native, tx.currency, tx.date),
                    cost_native=cost_native,
                    currency=tx.currency,
                )
            )
        elif tx.action == "sell":
            realized += _sell(lots[tx.ticker], tx, to_eur)
        elif tx.action == "split":
            _split(lots[tx.ticker], tx.quantity)
        # dividend / fee: not position-affecting (handled in dividends/cash)

    positions = [_aggregate(t, q) for t, q in lots.items() if _total_qty(q) > 1e-9]
    positions.sort(key=lambda p: p.ticker)
    return positions, realized


def _sell(queue: deque[Lot], tx: Transaction, to_eur: ToEur) -> list[RealizedSale]:
    remaining = tx.quantity
    held = _total_qty(queue)
    if remaining - held > 1e-9:
        raise ValueError(
            f"{tx.ticker}: sell of {tx.quantity} on {tx.date} exceeds held {held:.4f}"
        )
    # Net proceeds per share = gross price less pro-rata sell commission, in EUR.
    gross_eur = to_eur(tx.price, tx.currency, tx.date)
    fee_per_share_eur = (
        to_eur(tx.fee, tx.currency, tx.date) / tx.quantity if tx.quantity else 0.0
    )
    net_per_share_eur = gross_eur - fee_per_share_eur

    sales: list[RealizedSale] = []
    while remaining > 1e-9:
        lot = queue[0]
        take = min(lot.quantity, remaining)
        frac = take / lot.quantity
        cost_eur = lot.cost_eur * frac
        sales.append(
            RealizedSale(
                ticker=tx.ticker,
                buy_date=lot.date,
                sell_date=tx.date,
                quantity=take,
                cost_eur=cost_eur,
                proceeds_eur=take * net_per_share_eur,
                currency=tx.currency,
            )
        )
        lot.quantity -= take
        lot.cost_eur -= cost_eur
        lot.cost_native -= lot.cost_native * frac
        remaining -= take
        if lot.quantity <= 1e-9:
            queue.popleft()
    return sales


def _split(queue: deque[Lot], ratio: float) -> None:
    """N:1 forward split: each open lot's share count scales by `ratio`,
    per-share cost falls proportionally, total cost basis unchanged."""
    if ratio <= 0:
        return
    for lot in queue:
        lot.quantity *= ratio


def _aggregate(ticker: str, queue: deque[Lot]) -> Position:
    return Position(
        ticker=ticker,
        quantity=sum(lot.quantity for lot in queue),
        cost_eur=sum(lot.cost_eur for lot in queue),
        cost_native=sum(lot.cost_native for lot in queue),
        currency=queue[0].currency if queue else "EUR",
    )


def _total_qty(queue: deque[Lot]) -> float:
    return sum(lot.quantity for lot in queue)
