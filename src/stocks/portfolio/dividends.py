"""Dividend income + foreign withholding, valued in EUR at the pay date.

Ledger convention for a dividend row: action='dividend', price = GROSS dividend
total in native ccy, fee = tax withheld at source in native ccy. Net = price-fee.

For a Spanish resident, dividends join the savings base (taxed with capital
gains). Foreign withholding is relieved via the double-taxation credit, capped
at the treaty rate (~15% for most Spain treaties); withholding above the cap is
reclaimable from the source country, not creditable in Spain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stocks.data.fx import ToEur, prefetch
from stocks.data.fx import to_eur as _fx_to_eur
from stocks.portfolio.ledger import Transaction

# Spain double-taxation treaty cap on dividend withholding (creditable ceiling).
TREATY_WHT_CAP = 0.15


@dataclass
class DividendYear:
    year: int
    gross_eur: float = 0.0
    withheld_eur: float = 0.0
    records: list[Transaction] = field(default_factory=list)

    @property
    def net_eur(self) -> float:
        return self.gross_eur - self.withheld_eur

    @property
    def creditable_eur(self) -> float:
        """Foreign tax creditable in Spain (capped at the treaty rate)."""
        return min(self.withheld_eur, TREATY_WHT_CAP * self.gross_eur)

    @property
    def reclaimable_eur(self) -> float:
        """Withholding above the treaty cap — reclaim from the source country."""
        return max(0.0, self.withheld_eur - self.creditable_eur)


def by_year(
    transactions: list[Transaction], to_eur: ToEur | None = None
) -> dict[int, DividendYear]:
    """Aggregate dividend transactions into per-calendar-year EUR summaries."""
    dividends = [t for t in transactions if t.action == "dividend"]
    if to_eur is None:
        prefetch((t.date, t.currency) for t in dividends)
        to_eur = _fx_to_eur
    years: dict[int, DividendYear] = {}
    for tx in dividends:
        yr = int(tx.date[:4])
        dy = years.setdefault(yr, DividendYear(year=yr))
        dy.gross_eur += to_eur(tx.price, tx.currency, tx.date)
        dy.withheld_eur += to_eur(tx.fee, tx.currency, tx.date)
        dy.records.append(tx)
    return years
