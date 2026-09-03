"""Canada — taxable capital gains on securities.

Scope: dispositions of listed securities in a non-registered account by a
Canadian-resident individual. NOT tax advice — a planning aid. Rules encoded:

* **Adjusted cost base.** Identical properties are one holding at one average
  cost (ITA s.47), recomputed on every purchase — `build(matching="average")`.
  A Canadian basis is therefore never a specific lot's price, which is exactly
  what people get wrong when they reuse a FIFO report.
* **50% inclusion rate.** Half the net capital gain is taxable income (ITA
  s.38); the other half is never taxed. The 2024 proposal to raise the rate to
  two thirds above $250,000 was cancelled in March 2025, so one rate applies.
* **Marginal rates.** The taxable half stacks on the other income you set in
  Profile, at the federal brackets below, plus a flat provincial rate you give
  (`TaxSettings.subnational_rate`) — provincial tax is roughly half the bill
  and no federal-only estimate is worth reading without it.
* **Superficial loss** (s.40(2)(g)(i)): a loss is denied when identical
  property is bought within 30 days before or after the disposition and still
  held at the end of that window. The denied loss is not lost — it is added to
  the replacement's ACB, so it comes back when *those* shares are sold, which
  is how this module re-integrates it.
* **Losses** offset capital gains only — never ordinary income — and carry
  forward indefinitely (a three-year carryback exists; it is a note here).

Not modelled: the "still held at day 30" leg of the superficial-loss test (a
repurchase inside the window is treated as denying the loss), provincial
surtaxes and progressivity behind the single rate you supply, the alternative
minimum tax, RRSP/TFSA/FHSA accounts (nothing in them is taxable), and
foreign-exchange gains on the cash itself. Federal rates are the 2025 tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Kpi,
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    covers,
    days_window,
    flag,
    progressive_tax,
    recovered_losses,
    replacement_dates,
)

CODE = "CA"
CURRENCY = "CAD"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

INCLUSION_RATE = 0.50  # ITA s.38(a)

# Federal brackets (upper bound CAD, marginal rate). 2025's lowest rate is the
# 14.5% blend the CRA publishes for the mid-year cut from 15% to 14%.
FEDERAL_BRACKETS: dict[int, list[tuple[float, float]]] = {
    2025: [
        (57_375.0, 0.145),
        (114_750.0, 0.205),
        (177_882.0, 0.26),
        (253_414.0, 0.29),
        (float("inf"), 0.33),
    ],
}
BRACKET_YEARS = sorted(FEDERAL_BRACKETS)

# Superficial loss: 30 days either side of the disposition.
WINDOW = days_window(30)

# T1135: total *cost* of specified foreign property above this needs the form.
T1135_THRESHOLD = 100_000.0


def brackets_for(year: int) -> list[tuple[float, float]]:
    """`year`'s federal brackets, falling back to the closest year on file."""
    if year in FEDERAL_BRACKETS:
        return FEDERAL_BRACKETS[year]
    earlier = [y for y in FEDERAL_BRACKETS if y <= year]
    return FEDERAL_BRACKETS[max(earlier) if earlier else min(FEDERAL_BRACKETS)]


def federal_tax(taxable_income: float, year: int) -> float:
    return progressive_tax(taxable_income, brackets_for(year))


@dataclass
class CaTaxPeriod(TaxPeriod):
    """One calendar year of dispositions, for Schedule 3."""

    settings: TaxSettings = field(default_factory=TaxSettings)

    @property
    def net_gain(self) -> float:
        """Net capital gain after allowable losses. Negative = net loss."""
        return self.net_taxable

    @property
    def taxable_gain(self) -> float:
        """The half that reaches taxable income (line 12700)."""
        return max(0.0, self.net_taxable) * INCLUSION_RATE

    @property
    def federal_tax(self) -> float:
        """Marginal federal tax on the taxable half, stacked on other income."""
        base = max(0.0, self.settings.other_income)
        return federal_tax(base + self.taxable_gain, self.year) - federal_tax(
            base, self.year
        )

    @property
    def provincial_tax(self) -> float:
        """The flat provincial rate you supplied, on the taxable half."""
        return self.taxable_gain * max(0.0, self.settings.subnational_rate)

    @property
    def estimated_tax(self) -> float:
        return self.federal_tax + self.provincial_tax

    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("taxable_gain", self.taxable_gain, "taxable_gain_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = [Note("inclusion_note")]
        if self.disallowed_loss:
            out.append(
                Note("superficial_loss_note",
                     {"denied": f"{self.disallowed_loss:,.0f}"})
            )
        if self.recovered_loss:
            out.append(
                Note("recovered_note", {"recovered": f"{self.recovered_loss:,.0f}"})
            )
        rate = max(0.0, self.settings.subnational_rate)
        out.append(
            Note("provincial_note", {"rate": f"{rate * 100:.1f}"})
            if rate
            else Note("no_provincial_note")
        )
        if self.carryforward_loss:
            out.append(Note("carryback_note"))
        if self.year not in FEDERAL_BRACKETS:
            out.append(Note("bracket_year_note", {"year": f"{max(BRACKET_YEARS)}"}))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> CaTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for Schedule 3.

    `buy_dates` drives the superficial-loss test against repurchases in any
    year. The monthly slice is a breakdown of when a result was booked, not a
    taxable base — the losses net over the whole year.
    """
    cfg = settings or TaxSettings()
    out = CaTaxPeriod(
        jurisdiction=CODE,
        currency=CURRENCY,
        year=int(period[:4]),
        period=period,
        settings=cfg,
    )
    for s in realized:
        if not covers(period, s.sell_date, YEAR_START):
            continue
        out.sales.append(s)
        if s.gain >= 0:
            out.realized_gain += s.gain
            continue
        loss = -s.gain
        out.realized_loss += loss
        if replacement_dates(s, buy_dates.get(s.ticker, []), WINDOW):
            out.disallowed_loss += loss
    out.recovered_loss = recovered_losses(realized, period, buy_dates, WINDOW)
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """T1135, the foreign income verification statement.

    The test is the total **cost** of specified foreign property, above
    CAD 100,000 at any point in the year; the figure here is market value, so
    it can flag a book whose cost is below the line (and, after a bad year,
    miss one whose cost is above it). Shares of a foreign company held in a
    Canadian brokerage account still count — the account's location does not
    save you — which is the opposite of the US rules.
    """
    held = f"foreign property ~CA${total_foreign_value:,.0f} (market value)"
    msg = (
        f"{held} ≥ CA$100,000: T1135 may apply — the real test is total cost, "
        "and the $2,500 late-filing penalty makes it worth checking."
        if total_foreign_value >= T1135_THRESHOLD
        else f"{held} < CA$100,000 T1135 threshold (measured on cost)."
    )
    return [flag("t1135", total_foreign_value, T1135_THRESHOLD, msg)]
