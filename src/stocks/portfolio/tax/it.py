"""Italy — imposta sostitutiva on plusvalenze from securities.

Scope: realized gains and losses on listed shares and funds held by an
Italian-resident individual outside a business. NOT tax advice — a planning
aid. Rules encoded:

* **Flat 26% substitute tax** on the net capital gain (art. 5 D.Lgs. 461/1997).
  Equities and funds alike; the 12.5% rate for white-list government bonds is
  out of scope here.
* **LIFO matching** (art. 67 c. 1-bis TUIR): the shares disposed of are the
  most recently acquired ones. That is `build(matching="lifo")`, and in a
  rising market it books a *smaller* gain than FIFO on identical trades — the
  reason this app cannot just reuse its default replay for an Italian filer.
* **Losses** (minusvalenze) offset gains of the same nature and carry forward
  **four years** (art. 68 c. 5 TUIR). They never touch other income, and an
  unused fourth-year loss simply expires.
* **No annual allowance**: the first euro of net gain is taxed.
* **Quadro RW** monitors assets held abroad and is where **IVAFE** — 0.2% a
  year of their value — is assessed. A duty at any amount for securities,
  hence a flag with no threshold to cross.

Not modelled: the difference between the three regimes (in *risparmio
amministrato* or *gestito* the broker withholds and nets your losses for you,
so nothing reaches quadro RT; only the *dichiarativo* regime makes you compute
this yourself), the imposta di bollo on Italian custody statements, and the
special rules for qualified holdings and non-white-list jurisdictions.
"""

from __future__ import annotations

from dataclasses import dataclass

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    open_period,
    sales_in,
)

CODE = "IT"
CURRENCY = "EUR"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

SUBSTITUTE_RATE = 0.26  # imposta sostitutiva
CARRYFORWARD_YEARS = 4

# IVAFE on foreign financial assets, assessed through quadro RW.
IVAFE_RATE = 0.002
QUADRO_RW_THRESHOLD = 0.0  # securities abroad: declarable at any value


def substitute_tax(net_gain: float) -> float:
    """26% on a positive net gain. 0 on a net loss."""
    return max(0.0, net_gain) * SUBSTITUTE_RATE


@dataclass
class ItTaxPeriod(TaxPeriod):
    """One calendar year of plusvalenze for quadro RT."""

    @property
    def estimated_tax(self) -> float:
        return substitute_tax(self.net_taxable)

    def notes(self) -> list[Note]:
        out: list[Note] = [Note("lifo_note")]
        if self.carryforward_loss:
            out.append(
                Note("carryforward_note",
                     {"carry": f"{self.carryforward_loss:,.0f}"})
            )
        out.append(Note("regime_note"))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> ItTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for quadro RT.

    `buy_dates` is unused: Italy has no repurchase rule, so buying back after
    a loss changes nothing about that loss (only which lot a later sale takes,
    which the LIFO replay already settled).
    """
    out = open_period(ItTaxPeriod, CODE, CURRENCY, period)
    for s in sales_in(period, realized, YEAR_START):
        out.sales.append(s)
        if s.gain >= 0:
            out.realized_gain += s.gain
        else:
            out.realized_loss += -s.gain
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """Quadro RW: monitoring of assets abroad, and the IVAFE assessed on them.

    The €15,000 exemption people remember covers bank accounts, not securities:
    shares and funds held abroad go in RW at any value. IVAFE then takes 0.2%
    of that value — a wealth tax, not part of the gains estimate above.
    """
    ivafe = total_foreign_value * IVAFE_RATE
    msg = (
        f"securities held abroad ~{total_foreign_value:,.0f} EUR: quadro RW "
        f"applies at any value, and IVAFE at 0.2% is about {ivafe:,.0f} EUR "
        "a year on top of the tax on gains."
        if total_foreign_value > 0
        else "nothing held abroad: no quadro RW, no IVAFE."
    )
    return [
        # Built directly: `flag` reads a threshold as ">=", which at zero would
        # report a duty for a book with nothing abroad.
        ReportingFlag(
            "quadro_rw",
            total_foreign_value,
            QUADRO_RW_THRESHOLD,
            total_foreign_value > 0,
            msg,
        )
    ]
