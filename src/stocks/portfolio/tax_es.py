"""Spanish personal income-tax helpers for the savings base (base del ahorro).

Scope: capital gains/losses on securities (IRPF ahorro). NOT tax advice — a
planning aid. Key rules encoded:

* Progressive savings-base brackets (2023-2025 scale).
* FIFO matching lives in positions.py (art. 37 LIRPF).
* Art. 33.5.f "regla de los dos meses": a loss on listed securities is NOT
  deductible in the year if identical securities were (re)bought within the two
  months before or after the sale. The loss is deferred, not lost — it becomes
  computable when the replacement shares are later sold. We flag & defer it.
* Losses offset gains within the savings base; any excess carries forward 4
  years (surfaced as a note, not auto-applied across years here).
* Modelo 720 / foreign-asset reporting flag (> 50.000 EUR held abroad).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from stocks.portfolio.positions import RealizedSale

# Base del ahorro — marginal brackets (upper bound EUR, rate). 2023-2025 scale.
SAVINGS_BRACKETS: list[tuple[float, float]] = [
    (6_000, 0.19),
    (50_000, 0.21),
    (200_000, 0.23),
    (300_000, 0.27),
    (float("inf"), 0.28),
]

MODELO_720_THRESHOLD_EUR = 50_000.0
TWO_MONTH_DAYS = 61  # ~2 calendar months; see _within_two_months for exact rule


def tax_on_savings_base(base: float) -> float:
    """Progressive IRPF tax on a positive savings-base amount (EUR)."""
    if base <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in SAVINGS_BRACKETS:
        if base <= lower:
            break
        taxed = min(base, upper) - lower
        tax += taxed * rate
        lower = upper
    return tax


def _shift_months(d: date, months: int) -> date:
    """d shifted by ±months, clamped to the target month's last valid day."""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    # clamp day (e.g. 31 Jan -2mo has no 31 Nov)
    last = [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last))


def _within_two_months(sell: date, buy: date) -> bool:
    """True if `buy` falls in the two calendar months before or after `sell`."""
    return _shift_months(sell, -2) <= buy <= _shift_months(sell, 2)


@dataclass
class TaxYear:
    year: int
    realized_gain_eur: float = 0.0  # sum of gains from winning sales
    realized_loss_eur: float = 0.0  # sum of |losses| from losing sales
    deferred_loss_eur: float = 0.0  # losses disallowed this year (2-month rule)
    sales: list[RealizedSale] = field(default_factory=list)

    @property
    def deductible_loss_eur(self) -> float:
        return self.realized_loss_eur - self.deferred_loss_eur

    @property
    def net_taxable_eur(self) -> float:
        """Net savings base from securities (may be negative -> carryforward)."""
        return self.realized_gain_eur - self.deductible_loss_eur

    @property
    def estimated_tax_eur(self) -> float:
        return tax_on_savings_base(self.net_taxable_eur)

    @property
    def carryforward_loss_eur(self) -> float:
        """Unused net loss to carry forward (4 years). 0 if net is a gain."""
        return max(0.0, -self.net_taxable_eur)


def fiscal_year(
    realized: list[RealizedSale], year: int, buy_dates: dict[str, list[str]]
) -> TaxYear:
    """Summarize one calendar year's realized sales for the savings base.

    `buy_dates` maps ticker -> all buy dates in the ledger (ISO), used to apply
    the 2-month rule against replacement purchases in any year.
    """
    out = TaxYear(year=year)
    for s in realized:
        if int(s.sell_date[:4]) != year:
            continue
        out.sales.append(s)
        gain = s.gain_eur
        if gain >= 0:
            out.realized_gain_eur += gain
            continue
        loss = -gain
        out.realized_loss_eur += loss
        if _has_replacement(s, buy_dates.get(s.ticker, [])):
            out.deferred_loss_eur += loss
    return out


def _has_replacement(sale: RealizedSale, ticker_buy_dates: list[str]) -> bool:
    """A loss is deferred only if homogeneous shares acquired *after* the sold
    lot exist within the 2-month window — i.e. a genuine replacement position,
    not the sold lot's own purchase nor an older parcel."""
    sell = date.fromisoformat(sale.sell_date)
    lot_buy = date.fromisoformat(sale.buy_date)
    for d in ticker_buy_dates:
        b = date.fromisoformat(d)
        if b <= lot_buy:  # the sold lot itself, or an older one: not a replacement
            continue
        if _within_two_months(sell, b):
            return True
    return False


@dataclass
class ForeignAssetFlag:
    total_value_eur: float
    reportable: bool
    message: str


def modelo_720_flag(total_foreign_value_eur: float) -> ForeignAssetFlag:
    """Flag the >50k EUR foreign-asset reporting threshold (Modelo 720).

    Applies to assets *held abroad* (e.g. via a non-Spanish broker/custodian).
    If your broker is Spanish-domiciled, securities are reported by the broker
    and 720 generally does not apply — hence a flag, not a verdict.
    """
    reportable = total_foreign_value_eur >= MODELO_720_THRESHOLD_EUR
    if reportable:
        msg = (
            f"foreign holdings ~{total_foreign_value_eur:,.0f} EUR ≥ 50.000 EUR: "
            "Modelo 720 may apply if held via a non-Spanish broker."
        )
    else:
        msg = (
            f"foreign holdings ~{total_foreign_value_eur:,.0f} EUR "
            "< 50.000 EUR threshold."
        )
    return ForeignAssetFlag(total_foreign_value_eur, reportable, msg)
