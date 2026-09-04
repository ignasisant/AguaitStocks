"""United Kingdom — capital gains tax on shares.

Scope: CGT on listed shares held outside an ISA or SIPP by a UK-resident
individual. NOT tax advice — a planning aid. Rules encoded:

* **Share identification** happens in positions.py, not here: same-day, then
  the 30 days after the disposal, then the Section 104 pool at average cost
  (TCGA 1992 s.105/106A, `build(matching="s104")`). It is a matching rule, not
  a disallowance — the 30-day match is *why* selling and buying back a week
  later banks no loss, and the parcel it produces simply has a different cost.
* **The tax year runs 6 April to 5 April.** "2025" here means 2025/26, so a
  disposal in February 2026 belongs to it — see `base.covers`/`tax_year_of`.
* **Annual Exempt Amount**: £3,000 (2024/25 and 2025/26, down from £6,000 and
  £12,300 before that). Current-year losses come off gains *before* the AEA,
  which is how a loss-making year can waste the allowance entirely.
* **Rates**: 18% on gains falling inside what is left of the basic-rate band,
  24% above it (both from 30 October 2024; 10%/20% before that). The gain
  stacks on top of taxable income, so `TaxSettings.other_income` decides where
  the 24% starts.
* **Losses** carry forward indefinitely once claimed. Brought-forward losses
  may only reduce gains down to the AEA — this module computes one year at a
  time, so that restriction is surfaced as a note rather than applied.

Not modelled: the £50,000 proceeds reporting test is reported as a note but
not as an obligation; no ISA/SIPP wrappers (nothing in a wrapper is taxable and
nothing here knows which account a trade sat in), no business asset disposal
relief, no negligible-value claims, and no personal-allowance taper above
£100,000 of income.
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
    open_period,
    sales_in,
)

CODE = "UK"
CURRENCY = "GBP"

# 6 April: the one tax calendar in this app that is not the calendar year.
YEAR_START = (4, 6)

# Annual Exempt Amount by tax year (the year it opens in).
ANNUAL_EXEMPT: dict[int, float] = {
    2022: 12_300.0,
    2023: 6_000.0,
    2024: 3_000.0,
    2025: 3_000.0,
}

# (lower rate, higher rate) on shares, by tax year. The 2024/25 year straddles
# the 30 October 2024 Budget — 10/20 before it, 18/24 after — and this table
# holds the post-Budget pair for that year, which is why the period carries a
# note saying so rather than pretending the year had one rate.
RATES: dict[int, tuple[float, float]] = {
    2022: (0.10, 0.20),
    2023: (0.10, 0.20),
    2024: (0.18, 0.24),
    2025: (0.18, 0.24),
}
STRADDLE_YEARS = frozenset({2024})

# The basic-rate band the gain stacks into, over taxable income.
BASIC_RATE_BAND = 37_700.0

# Self-assessment reporting test: a return is due when disposal proceeds pass
# this, even with no gain at all.
PROCEEDS_REPORTING_TEST = 50_000.0

TAX_YEARS = sorted(ANNUAL_EXEMPT)


def _table(table: dict, year: int):
    """`year`'s entry, falling back to the closest earlier one on file."""
    if year in table:
        return table[year]
    earlier = [y for y in table if y <= year]
    return table[max(earlier) if earlier else min(table)]


def allowance_for(year: int) -> float:
    return _table(ANNUAL_EXEMPT, year)


def rates_for(year: int) -> tuple[float, float]:
    return _table(RATES, year)


def year_label(year: int) -> str:
    """"2025/26" — the way a UK tax year is written."""
    return f"{year}/{(year + 1) % 100:02d}"


def cgt(gain: float, other_income: float, year: int) -> float:
    """CGT on a net gain, stacked above `other_income` (taxable, post-allowance).

    The band left over is what gets the lower rate; everything above it the
    higher one. A gain big enough to fill the band therefore pays both.
    """
    if gain <= 0:
        return 0.0
    lower_rate, higher_rate = rates_for(year)
    band_left = max(0.0, BASIC_RATE_BAND - max(0.0, other_income))
    at_lower = min(gain, band_left)
    return at_lower * lower_rate + (gain - at_lower) * higher_rate


@dataclass
class UkTaxPeriod(TaxPeriod):
    """One tax year (6 April – 5 April) of disposals."""

    proceeds_total: float = 0.0
    settings: TaxSettings = field(default_factory=TaxSettings)

    @property
    def allowance(self) -> float:
        """Annual Exempt Amount actually used — capped by the net gain."""
        net = self.realized_gain - self.deductible_loss
        return min(allowance_for(self.year), max(0.0, net))

    @property
    def net_taxable(self) -> float:
        """Net gain after current-year losses and the AEA."""
        net = self.realized_gain - self.deductible_loss
        return max(0.0, net - self.allowance)

    @property
    def estimated_tax(self) -> float:
        return cgt(self.net_taxable, self.settings.other_income, self.year)

    @property
    def carryforward_loss(self) -> float:
        """Net loss to carry forward (indefinitely, once claimed)."""
        return max(0.0, self.deductible_loss - self.realized_gain)

    @property
    def wasted_allowance(self) -> float:
        """AEA left unused — a loss-making year gets nothing back for it."""
        return max(0.0, allowance_for(self.year) - self.allowance)

    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("allowance", self.allowance, "allowance_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = []
        if self.allowance:
            out.append(Note("allowance_note", {"used": f"{self.allowance:,.0f}"}))
        if self.wasted_allowance and self.realized_gain:
            out.append(
                Note("wasted_allowance_note",
                     {"wasted": f"{self.wasted_allowance:,.0f}"})
            )
        if self.proceeds_total > PROCEEDS_REPORTING_TEST:
            out.append(
                Note("proceeds_test_note", {"proceeds": f"{self.proceeds_total:,.0f}"})
            )
        if self.carryforward_loss:
            out.append(Note("carryforward_note"))
        if self.year in STRADDLE_YEARS:
            out.append(Note("straddle_note"))
        if self.year not in ANNUAL_EXEMPT:
            out.append(Note("rate_year_note", {"year": f"{max(TAX_YEARS)}"}))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> UkTaxPeriod:
    """Summarize a UK tax year ("YYYY" = YYYY/YY+1) or one month of it.

    `buy_dates` is unused: the 30-day rule is a matching rule applied during
    the replay (positions.build), so by the time a parcel reaches here its
    cost already reflects it.
    """
    cfg = settings or TaxSettings()
    out = open_period(UkTaxPeriod, CODE, CURRENCY, period, settings=cfg)
    for s in sales_in(period, realized, YEAR_START):
        out.sales.append(s)
        out.proceeds_total += s.proceeds
        if s.gain >= 0:
            out.realized_gain += s.gain
        else:
            out.realized_loss += -s.gain
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """None: a UK resident's foreign shares carry no extra reporting regime.

    There is no FBAR or Modelo 720 analogue — foreign shares are simply
    chargeable assets like any other, and the remittance basis that used to
    complicate this was replaced for 2025/26. The reporting test that does
    matter here is the £50,000 of disposal proceeds, which is a property of the
    year's disposals rather than of what is held, so it is a period note.
    """
    return []
