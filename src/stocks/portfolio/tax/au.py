"""Australia — capital gains tax on shares, with the 50% discount.

Scope: CGT events A1 (disposals) on listed shares held by an Australian
resident individual outside super. NOT tax advice — a planning aid. Rules
encoded:

* **The income year runs 1 July to 30 June.** "2025" here means 2025-26, so a
  disposal in May 2026 belongs to it — see `base.covers`/`tax_year_of`.
* **50% CGT discount** on assets held more than 12 months (ITAA 1997 Div. 115).
  Not a lower rate: half the gain simply drops out, and the rest is ordinary
  taxable income.
* **Order of operations.** Capital losses come off the *gross* gains before the
  discount is applied (s.102-5), and you may choose which gains they hit
  first. Applying them to the non-discounted gains first leaves more of the
  discountable gain intact, which is the choice this module makes — it is the
  taxpayer's to make, and it is always the better one.
* **Marginal rates**, since the gain is ordinary income: the resident scale
  below plus the 2% Medicare levy, stacked on the other income you set in
  Profile.
* **FIFO** matching. Australia lets you identify the exact shares you sold, and
  the ATO accepts FIFO when your records don't; that is what positions.py
  does.
* **Losses** offset capital gains only — never salary — and carry forward
  indefinitely.

Not modelled: choosing specific parcels to sell (a real planning lever here),
the Medicare levy's low-income shading-in and the levy surcharge, the
non-resident and temporary-resident rules, the small-business and
main-residence concessions, super contributions, and franking credits on the
dividends this tab does not cover. Rates are the 2024-25 / 2025-26 scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    Kpi,
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    open_period,
    progressive_tax,
    sales_in,
)

CODE = "AU"
CURRENCY = "AUD"
# 1 July: the second non-calendar tax year in this app, after the UK's.
YEAR_START = (7, 1)

CGT_DISCOUNT = 0.50  # Div. 115, individuals
MEDICARE_LEVY = 0.02

# Resident marginal rates (upper bound AUD, rate), by income year.
RATES: dict[int, list[tuple[float, float]]] = {
    2024: [
        (18_200.0, 0.0),
        (45_000.0, 0.16),
        (135_000.0, 0.30),
        (190_000.0, 0.37),
        (float("inf"), 0.45),
    ],
    2025: [
        (18_200.0, 0.0),
        (45_000.0, 0.16),
        (135_000.0, 0.30),
        (190_000.0, 0.37),
        (float("inf"), 0.45),
    ],
}
RATE_YEARS = sorted(RATES)


def rates_for(year: int) -> list[tuple[float, float]]:
    """`year`'s scale, falling back to the closest earlier year on file."""
    if year in RATES:
        return RATES[year]
    earlier = [y for y in RATES if y <= year]
    return RATES[max(earlier) if earlier else min(RATES)]


def income_tax(taxable_income: float, year: int) -> float:
    return progressive_tax(taxable_income, rates_for(year))


def year_label(year: int) -> str:
    """"2025-26" — the way an Australian income year is written."""
    return f"{year}-{(year + 1) % 100:02d}"


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + years, day=28)


def is_long_term(buy_date: str, sell_date: str) -> bool:
    """True when the shares were held more than 12 months (discount eligible).

    The 12 months excludes both the acquisition and the disposal day, so a sale
    exactly a year later is *not* discounted — one day more and half the gain
    disappears, which makes this the most expensive off-by-one in the file.
    """
    buy = date.fromisoformat(buy_date)
    sell = date.fromisoformat(sell_date)
    return sell > _add_years(buy, 1)


def _is_long(sale: RealizedSale) -> bool:
    return is_long_term(sale.buy_date, sale.sell_date)


@dataclass
class AuTaxPeriod(TaxPeriod):
    """One income year (1 July – 30 June) of CGT events."""

    discount_gain: float = 0.0  # gross gains on assets held > 12 months
    other_gain: float = 0.0  # gross gains on the rest
    settings: TaxSettings = field(default_factory=TaxSettings)

    # ------------------------------------------------------------- netting
    def _after_losses(self) -> tuple[float, float]:
        """(non-discounted, discountable) gains left after this year's losses.

        Losses hit the non-discounted gains first: a dollar of loss saves a
        whole dollar there and only half a dollar against a discounted gain.
        """
        losses = self.deductible_loss + self.recovered_loss
        other = self.other_gain
        used = min(losses, other)
        other -= used
        losses -= used
        discount = max(0.0, self.discount_gain - losses)
        return other, discount

    @property
    def discount(self) -> float:
        """What the 50% discount takes off — the half that is never taxed."""
        return self._after_losses()[1] * CGT_DISCOUNT

    @property
    def net_taxable(self) -> float:
        """Net capital gain: gross gains less losses, then the discount."""
        other, discount = self._after_losses()
        return other + discount * (1 - CGT_DISCOUNT)

    @property
    def carryforward_loss(self) -> float:
        """Unapplied capital losses. Indefinite, capital gains only."""
        return max(
            0.0,
            (self.deductible_loss + self.recovered_loss)
            - (self.other_gain + self.discount_gain),
        )

    @property
    def marginal_tax(self) -> float:
        """Income tax on the net capital gain, stacked on the other income."""
        base = max(0.0, self.settings.other_income)
        return income_tax(base + self.net_taxable, self.year) - income_tax(
            base, self.year
        )

    @property
    def medicare(self) -> float:
        return self.net_taxable * MEDICARE_LEVY

    @property
    def estimated_tax(self) -> float:
        return self.marginal_tax + self.medicare

    # ---------------------------------------------------------- UI surface
    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("discount", self.discount, "discount_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = []
        if self.discount:
            out.append(Note("discount_note", {"discount": f"{self.discount:,.0f}"}))
        if self.deductible_loss and self.discount_gain:
            out.append(Note("loss_order_note"))
        out.append(Note("medicare_note"))
        if self.carryforward_loss:
            out.append(Note("carryforward_note"))
        if self.year not in RATES:
            out.append(Note("rate_year_note", {"year": f"{max(RATE_YEARS)}"}))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> AuTaxPeriod:
    """Summarize an income year ("YYYY" = 1 July YYYY to 30 June) or a month.

    `buy_dates` is unused: Australia has no wash-sale provision of its own —
    the ATO attacks the practice through the general anti-avoidance rules
    instead, which no ledger can compute.
    """
    cfg = settings or TaxSettings()
    out = open_period(AuTaxPeriod, CODE, CURRENCY, period, settings=cfg)
    for s in sales_in(period, realized, YEAR_START):
        out.sales.append(s)
        gain = s.gain
        if gain >= 0:
            out.realized_gain += gain
            if _is_long(s):
                out.discount_gain += gain
            else:
                out.other_gain += gain
            continue
        # A loss is a loss whatever the holding period — the discount applies
        # to gains only, which is why there is no long/short loss split here.
        out.realized_loss += -gain
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """None: Australia has no foreign-asset statement for individuals.

    Worldwide income goes on the ordinary return and there is no FBAR, T1135
    or Modelo 720 analogue with a value threshold to cross. What matters
    instead is that foreign amounts are converted at the ATO's rates, which is
    a conversion question rather than a reporting one.
    """
    return []
