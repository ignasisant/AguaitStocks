"""Spain — IRPF savings base (base del ahorro) on securities.

Scope: capital gains/losses on securities. NOT tax advice — a planning aid.
Key rules encoded:

* Progressive savings-base brackets (2023-2025 scale).
* FIFO matching lives in positions.py (art. 37 LIRPF).
* Art. 33.5.f "regla de los dos meses": a loss on listed securities is NOT
  deductible in the year if identical securities were (re)bought within the two
  months before or after the sale. The loss is deferred, not lost — it becomes
  computable as the replacement shares are later sold, which the shared
  `recovered_losses` machinery re-integrates.
* Losses offset gains within the savings base; any excess carries forward 4
  years (surfaced as a note, not auto-applied across years here).
* Modelo 720 / foreign-asset reporting flag (> 50.000 EUR held abroad).
"""

from __future__ import annotations

from dataclasses import dataclass

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    covers,
    flag,
    months_window,
    progressive_tax,
    recovered_losses,
    replacement_dates,
)

CODE = "ES"
CURRENCY = "EUR"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

# Base del ahorro — marginal brackets (upper bound EUR, rate). 2023-2025 scale.
SAVINGS_BRACKETS: list[tuple[float, float]] = [
    (6_000, 0.19),
    (50_000, 0.21),
    (200_000, 0.23),
    (300_000, 0.27),
    (float("inf"), 0.28),
]

MODELO_720_THRESHOLD_EUR = 50_000.0
CARRYFORWARD_YEARS = 4

# Two calendar months either side of the sale (art. 33.5.f).
WINDOW = months_window(2)


def tax_on_savings_base(base: float) -> float:
    """Progressive IRPF tax on a positive savings-base amount (EUR)."""
    return progressive_tax(base, SAVINGS_BRACKETS)


@dataclass
class EsTaxPeriod(TaxPeriod):
    @property
    def estimated_tax(self) -> float:
        return tax_on_savings_base(self.net_taxable)

    def notes(self) -> list[Note]:
        out: list[Note] = []
        if self.disallowed_loss:
            out.append(
                Note("deferred_note", {"deferred": f"{self.disallowed_loss:,.0f}"})
            )
        if self.recovered_loss:
            out.append(
                Note("recovered_note", {"recovered": f"{self.recovered_loss:,.0f}"})
            )
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> EsTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for the savings base.

    `buy_dates` maps ticker -> every buy date in the ledger (ISO), used to apply
    the 2-month rule against replacement purchases in any year. The monthly
    slice is a breakdown, not a taxable base — IRPF nets the savings base over
    the whole ejercicio.
    """
    out = EsTaxPeriod(
        jurisdiction=CODE, currency=CURRENCY, year=int(period[:4]), period=period
    )
    for s in realized:
        if not covers(period, s.sell_date, YEAR_START):
            continue
        out.sales.append(s)
        gain = s.gain
        if gain >= 0:
            out.realized_gain += gain
            continue
        loss = -gain
        out.realized_loss += loss
        if replacement_dates(s, buy_dates.get(s.ticker, []), WINDOW):
            out.disallowed_loss += loss
    out.recovered_loss = recovered_losses(realized, period, buy_dates, WINDOW)
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """Modelo 720: the >50.000 EUR foreign-asset informative declaration.

    Applies to assets *held abroad* (e.g. via a non-Spanish broker/custodian).
    If your broker is Spanish-domiciled, securities are reported by the broker
    and 720 generally does not apply — hence a flag, not a verdict.
    """
    msg = (
        f"foreign holdings ~{total_foreign_value:,.0f} EUR "
        + (
            "≥ 50.000 EUR: Modelo 720 may apply if held via a non-Spanish broker."
            if total_foreign_value >= MODELO_720_THRESHOLD_EUR
            else "< 50.000 EUR threshold."
        )
    )
    return [
        flag("modelo_720", total_foreign_value, MODELO_720_THRESHOLD_EUR, msg)
    ]
