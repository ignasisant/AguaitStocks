"""Share matching: transactions -> open positions + realized sales.

Which shares a sale actually sold is a legal question, and the answer differs
by country, so `build(matching=...)` takes the rule:

* ``"fifo"`` — oldest lots first. Spain mandates it (art. 37 LIRPF), Germany
  too (§20(4) S.7 EStG), and it is the US broker default when the filer
  identifies no lots. This is the default here.
* ``"s104"`` — the UK's three-step identification (TCGA 1992 s.105/106A):
  acquisitions on the *same day* first, then any made in the 30 days *after*
  the disposal, then the Section 104 pool at its average cost. Only the pool
  step averages, and that is exactly what makes a UK gain different from a
  FIFO one on the same trades.

Acquisition cost includes buy commissions; sale proceeds are net of sell
commissions. This module is pure: the currency converter is injected so it can
be unit-tested without network.

Money fields hold the *reporting* currency picked by `build(base=...)`: the
account's own currency for the app's analytics, the tax jurisdiction's for a
tax replay (see stocks.portfolio.tax). `*_native` stays in the trade currency.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _timedelta

from stocks.data.fx import ToBase, converter, prefetch
from stocks.portfolio.ledger import Transaction


@dataclass
class Lot:
    """An open (unsold) parcel: cost in the reporting ccy and the native one."""

    ticker: str
    date: str
    quantity: float
    cost: float  # total basis for `quantity` shares, incl. buy fee
    cost_native: float
    currency: str


# How a sale's shares were identified. "fifo" covers the oldest-lot rules;
# the other three are the UK's steps, and "pool" is the only one whose cost is
# an average rather than a specific purchase.
MATCH_FIFO = "fifo"
MATCH_SAME_DAY = "same_day"
MATCH_THIRTY_DAY = "thirty_day"
MATCH_POOL = "pool"

MATCHING_MODES = ("fifo", "s104")


@dataclass
class RealizedSale:
    """One matched parcel of a sale, each side valued at its own date.

    `buy_date` is the acquisition the shares came from. For a Section 104 pool
    match there is no single acquisition — the pool is one averaged holding —
    so it carries the earliest acquisition still in the pool, and `matched`
    says "pool" so nothing reads that date as the lot's own.
    """

    ticker: str
    buy_date: str
    sell_date: str
    quantity: float
    cost: float
    proceeds: float
    currency: str
    matched: str = MATCH_FIFO

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost


@dataclass
class Position:
    """Aggregated open holding for one ticker."""

    ticker: str
    quantity: float
    cost: float
    cost_native: float
    currency: str

    @property
    def avg_cost(self) -> float:
        return self.cost / self.quantity if self.quantity else 0.0

    @property
    def avg_cost_native(self) -> float:
        return self.cost_native / self.quantity if self.quantity else 0.0


def build(
    transactions: list[Transaction],
    to_base: ToBase | None = None,
    base: str = "EUR",
    matching: str = "fifo",
) -> tuple[list[Position], list[RealizedSale]]:
    """Replay the ledger in date order into open positions and realized sales.

    With no injected converter, the whole ledger's FX span is prefetched first
    (one request per currency) so the replay never fetches rates date by date.
    `base` is the reporting currency the lots are valued in — EUR for the app's
    own analytics, the tax jurisdiction's currency for a tax replay (a US filer
    needs a USD basis at each trade date, not a converted EUR one). `matching`
    picks the share-identification rule (see the module docstring); the open
    positions come out the same either way, only the realized parcels differ.
    """
    if to_base is None:
        prefetch(((t.date, t.currency) for t in transactions), quote=base)
        to_base = converter(base)
    if matching == "s104":
        return _build_s104(transactions, to_base)
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
                    cost=to_base(cost_native, tx.currency, tx.date),
                    cost_native=cost_native,
                    currency=tx.currency,
                )
            )
        elif tx.action == "sell":
            realized += _sell(lots[tx.ticker], tx, to_base)
        elif tx.action == "split":
            _split(lots[tx.ticker], tx.quantity)
        # dividend / fee: not position-affecting (handled in dividends/cash)

    positions = [_aggregate(t, q) for t, q in lots.items() if _total_qty(q) > 1e-9]
    positions.sort(key=lambda p: p.ticker)
    return positions, realized


def _sell(queue: deque[Lot], tx: Transaction, to_base: ToBase) -> list[RealizedSale]:
    remaining = tx.quantity
    held = _total_qty(queue)
    if remaining - held > 1e-9:
        raise ValueError(
            f"{tx.ticker}: sell of {tx.quantity} on {tx.date} exceeds held {held:.4f}"
        )
    # Net proceeds/share = gross price less the pro-rata sell commission.
    gross = to_base(tx.price, tx.currency, tx.date)
    fee_per_share = (
        to_base(tx.fee, tx.currency, tx.date) / tx.quantity if tx.quantity else 0.0
    )
    net_per_share = gross - fee_per_share

    sales: list[RealizedSale] = []
    while remaining > 1e-9:
        lot = queue[0]
        take = min(lot.quantity, remaining)
        frac = take / lot.quantity
        cost = lot.cost * frac
        sales.append(
            RealizedSale(
                ticker=tx.ticker,
                buy_date=lot.date,
                sell_date=tx.date,
                quantity=take,
                cost=cost,
                proceeds=take * net_per_share,
                currency=tx.currency,
            )
        )
        lot.quantity -= take
        lot.cost -= cost
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
        cost=sum(lot.cost for lot in queue),
        cost_native=sum(lot.cost_native for lot in queue),
        currency=queue[0].currency if queue else "EUR",
    )


def _total_qty(queue: deque[Lot]) -> float:
    return sum(lot.quantity for lot in queue)


# ------------------------------------------------------------ UK: s.104 pool
# Three steps per disposal (TCGA 1992 s.105, s.106A), in this order:
#
#   1. acquisitions on the SAME DAY as the disposal,
#   2. acquisitions in the 30 days AFTER it — the bed-and-breakfast rule, which
#      is why selling and buying back the next week does not bank a loss,
#   3. the Section 104 pool: every other acquisition, held as one holding with
#      one average cost.
#
# Steps 1 and 2 look *forward*, so this cannot be a streaming loop like the
# FIFO path: an acquisition made after a disposal can belong to it, and only
# what such a match leaves behind ever reaches the pool.


@dataclass
class _Acquisition:
    """One buy, with what is left of it after same-day/30-day matching."""

    date: str
    quantity: float
    remaining: float
    cost: float  # reporting currency, incl. the buy commission
    cost_native: float
    currency: str
    pooled: bool = False  # already folded into the s.104 pool

    @property
    def unit_cost(self) -> float:
        return self.cost / self.quantity if self.quantity else 0.0

    @property
    def unit_cost_native(self) -> float:
        return self.cost_native / self.quantity if self.quantity else 0.0

    def take(self, qty: float) -> tuple[float, float]:
        """Consume `qty` shares; returns their (cost, native cost)."""
        qty = min(qty, self.remaining)
        self.remaining -= qty
        return qty * self.unit_cost, qty * self.unit_cost_native


@dataclass
class _Pool:
    """A ticker's s.104 holding: one quantity, one averaged cost."""

    quantity: float = 0.0
    cost: float = 0.0
    cost_native: float = 0.0
    first_date: str = ""  # earliest acquisition still in the pool

    def add(self, acq: _Acquisition, qty: float) -> None:
        if qty <= 0:
            return
        self.cost += qty * acq.unit_cost
        self.cost_native += qty * acq.unit_cost_native
        self.quantity += qty
        if not self.first_date or acq.date < self.first_date:
            self.first_date = acq.date

    def take(self, qty: float) -> tuple[float, float]:
        """Consume `qty` shares at the pool's average cost."""
        if self.quantity <= 0:
            return 0.0, 0.0
        qty = min(qty, self.quantity)
        share = qty / self.quantity
        cost, native = self.cost * share, self.cost_native * share
        self.quantity -= qty
        self.cost -= cost
        self.cost_native -= native
        return cost, native


def _days_between(a: str, b: str) -> int:
    return (_date.fromisoformat(b) - _date.fromisoformat(a)).days


def _next_day(day: str) -> str:
    return (_date.fromisoformat(day) + _timedelta(days=1)).isoformat()


def _build_s104(
    transactions: list[Transaction], to_base: ToBase
) -> tuple[list[Position], list[RealizedSale]]:
    """`build` for the UK rules. Same signature and outputs, other matching."""
    by_ticker: dict[str, list[Transaction]] = defaultdict(list)
    for tx in sorted(transactions, key=lambda t: (t.date, t.id or 0)):
        if tx.action in ("buy", "sell", "split"):
            by_ticker[tx.ticker].append(tx)

    positions: list[Position] = []
    realized: list[RealizedSale] = []
    for ticker, txs in by_ticker.items():
        pos, sales = _replay_s104(ticker, txs, to_base)
        realized += sales
        if pos is not None:
            positions.append(pos)
    positions.sort(key=lambda p: p.ticker)
    return positions, realized


def _replay_s104(
    ticker: str, txs: list[Transaction], to_base: ToBase
) -> tuple[Position | None, list[RealizedSale]]:
    """One ticker's timeline under the UK rules.

    Pooling is *deferred*: a buy joins the s.104 pool only once an event dated
    after it comes along (and, for a same-day disposal, only after that
    disposal has taken what the same-day rule gives it). Pooling on arrival
    would hide a same-day buy inside the average before the sale it belongs to
    could claim it — and a 30-day match has to be able to reach an acquisition
    the timeline has not even got to yet.
    """
    acquisitions: list[_Acquisition] = []
    for tx in txs:
        if tx.action != "buy":
            continue
        cost_native = tx.quantity * tx.price + tx.fee
        acquisitions.append(
            _Acquisition(
                date=tx.date,
                quantity=tx.quantity,
                remaining=tx.quantity,
                cost=to_base(cost_native, tx.currency, tx.date),
                cost_native=cost_native,
                currency=tx.currency,
            )
        )

    pool = _Pool()

    def flush(before: str | None = None) -> None:
        """Fold every acquisition older than `before` into the pool."""
        for acq in acquisitions:
            if acq.pooled or (before is not None and acq.date >= before):
                continue
            acq.pooled = True
            pool.add(acq, acq.remaining)
            acq.remaining = 0.0

    sales: list[RealizedSale] = []
    currency = acquisitions[0].currency if acquisitions else "GBP"
    for tx in txs:
        flush(tx.date)
        if tx.action == "split":
            if tx.quantity > 0:
                pool.quantity *= tx.quantity
                for acq in acquisitions:
                    if not acq.pooled:
                        acq.quantity *= tx.quantity
                        acq.remaining *= tx.quantity
        elif tx.action == "sell":
            currency = tx.currency
            sales += _dispose_s104(ticker, tx, acquisitions, pool, to_base, flush)
    flush()

    position = None
    if pool.quantity > 1e-9:
        position = Position(
            ticker=ticker,
            quantity=pool.quantity,
            cost=pool.cost,
            cost_native=pool.cost_native,
            currency=currency,
        )
    return position, sales


def _dispose_s104(
    ticker: str,
    tx: Transaction,
    acquisitions: list[_Acquisition],
    pool: _Pool,
    to_base: ToBase,
    flush,
) -> list[RealizedSale]:
    """One disposal, matched same-day -> 30-day -> pool.

    `flush` pools everything older than a given date; it is called once the
    same-day rule has had its turn, so a same-day buy's *leftover* is in the
    average this disposal then draws on (HMRC CG51560).
    """
    remaining = tx.quantity
    gross = to_base(tx.price, tx.currency, tx.date)
    fee_per_share = (
        to_base(tx.fee, tx.currency, tx.date) / tx.quantity if tx.quantity else 0.0
    )
    net_per_share = gross - fee_per_share

    def parcel(qty: float, cost: float, buy_date: str, rule: str) -> RealizedSale:
        return RealizedSale(
            ticker=ticker,
            buy_date=buy_date,
            sell_date=tx.date,
            quantity=qty,
            cost=cost,
            proceeds=qty * net_per_share,
            currency=tx.currency,
            matched=rule,
        )

    out: list[RealizedSale] = []
    same_day = [a for a in acquisitions if a.date == tx.date and a.remaining > 1e-9]
    thirty = [
        a for a in acquisitions
        if not a.pooled
        and a.remaining > 1e-9
        and 0 < _days_between(tx.date, a.date) <= 30
    ]
    for rule, candidates in (
        (MATCH_SAME_DAY, same_day),
        (MATCH_THIRTY_DAY, sorted(thirty, key=lambda a: a.date)),
    ):
        for acq in candidates:
            if remaining <= 1e-9:
                break
            take = min(acq.remaining, remaining)
            cost, _ = acq.take(take)
            out.append(parcel(take, cost, acq.date, rule))
            remaining -= take
        if rule == MATCH_SAME_DAY:
            # Whatever the same-day buy did not cover joins the pool now, so
            # the pool step below averages it in.
            flush(_next_day(tx.date))

    if remaining > 1e-9 and pool.quantity > 1e-9:
        take = min(pool.quantity, remaining)
        first = pool.first_date
        cost, _ = pool.take(take)
        out.append(parcel(take, cost, first or tx.date, MATCH_POOL))
        remaining -= take
    if remaining > 1e-9:
        held = tx.quantity - remaining
        raise ValueError(
            f"{ticker}: sell of {tx.quantity} on {tx.date} exceeds held {held:.4f}"
        )
    return out
