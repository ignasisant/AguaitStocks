"""Dividend income + foreign withholding, valued at the pay-date rate.

Ledger convention for a dividend row: action='dividend', price = GROSS dividend
total in native ccy, fee = tax withheld at source in native ccy. Net = price-fee.

For a Spanish resident, dividends join the savings base (taxed with capital
gains). Foreign withholding is relieved via the double-taxation credit, capped
at the treaty rate (~15% for most Spain treaties); withholding above the cap is
reclaimable from the source country, not creditable in Spain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stocks.data.fx import ToBase, converter, prefetch
from stocks.portfolio.ledger import Transaction

# Spain double-taxation treaty cap on dividend withholding (creditable ceiling).
TREATY_WHT_CAP = 0.15


@dataclass
class DividendYear:
    year: int
    gross: float = 0.0
    withheld: float = 0.0
    records: list[Transaction] = field(default_factory=list)

    @property
    def net(self) -> float:
        return self.gross - self.withheld

    @property
    def creditable(self) -> float:
        """Foreign tax creditable in Spain (capped at the treaty rate)."""
        return min(self.withheld, TREATY_WHT_CAP * self.gross)

    @property
    def reclaimable(self) -> float:
        """Withholding above the treaty cap — reclaim from the source country."""
        return max(0.0, self.withheld - self.creditable)


def by_year(
    transactions: list[Transaction],
    to_base: ToBase | None = None,
    base: str = "EUR",
) -> dict[int, DividendYear]:
    """Aggregate dividends into per-calendar-year summaries in `base`."""
    dividends = [t for t in transactions if t.action == "dividend"]
    if to_base is None:
        prefetch((t.date, t.currency) for t in dividends)
        to_base = converter(base)
    years: dict[int, DividendYear] = {}
    for tx in dividends:
        yr = int(tx.date[:4])
        dy = years.setdefault(yr, DividendYear(year=yr))
        dy.gross += to_base(tx.price, tx.currency, tx.date)
        dy.withheld += to_base(tx.fee, tx.currency, tx.date)
        dy.records.append(tx)
    return years
