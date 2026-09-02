"""United States — federal capital-gains tax on securities.

Scope: realized gains/losses on securities for an individual filer. NOT tax
advice — a planning aid, federal only (no state tax, no AMT, no QSBS/collectible
rates, no Section 1256 or option straddles). Rules encoded:

* **Holding period.** Long-term when the lot was held *more than* one year
  (sold on or before the anniversary is short-term). Lots come from the FIFO
  replay in positions.py; specific-lot identification is not modelled, so this
  is the "no election / broker default" answer.
* **Wash sale (IRC 1091).** A loss is disallowed when substantially identical
  stock is bought within 30 days before or after the sale. The loss is not
  lost: it bumps the basis of the replacement shares, so it comes back when
  those are sold — the shared `recovered_losses` machinery re-integrates it,
  keeping the original loss's short/long character.
* **Netting (IRC 1222).** Short-term and long-term buckets net internally,
  then against each other. A net short-term gain is taxed at ordinary rates
  (stacked on `TaxSettings.other_income`); a net long-term gain at the 0/15/20
  preferential rates, stacked above ordinary taxable income.
* **Net capital loss (IRC 1211(b)).** Deductible against ordinary income up to
  $3,000 a year ($1,500 married filing separately); the rest carries forward
  indefinitely, retaining character.
* **NIIT (IRC 1411).** Optional 3.8% on net investment income above the MAGI
  threshold. Off by default: it needs income we don't hold.
* **Reporting flags.** FBAR (FinCEN 114, $10,000 aggregate foreign accounts)
  and Form 8938 ($50k/$75k single, $100k/$150k joint) — thresholds crossed,
  not filings due: both look at foreign *accounts*, and a US-held brokerage
  account full of foreign stocks is not one.

Rate tables are keyed by tax year (2025 = Rev. Proc. 2024-40 as amended by the
2025 reconciliation act, which kept the seven-bracket structure). A year with
no table falls back to the closest earlier one, so the numbers age visibly
rather than silently: check `BRACKET_YEARS` before trusting a future year.
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
    covers,
    days_window,
    flag,
    progressive_tax,
    recovered_losses,
    replacement_dates,
)

CODE = "US"
CURRENCY = "USD"
# The tax year is the calendar year here.
YEAR_START = (1, 1)

FILING_STATUSES = ("single", "mfj", "mfs", "hoh")
DEFAULT_STATUS = "single"

# Thirty days either side of the sale (IRC 1091) — a 61-day window.
WASH_SALE_DAYS = 30
WINDOW = days_window(WASH_SALE_DAYS)

# Ordinary-income brackets: (upper bound of taxable income, marginal rate).
ORDINARY_BRACKETS: dict[int, dict[str, list[tuple[float, float]]]] = {
    2025: {
        "single": [
            (11_925, 0.10), (48_475, 0.12), (103_350, 0.22), (197_300, 0.24),
            (250_525, 0.32), (626_350, 0.35), (float("inf"), 0.37),
        ],
        "mfj": [
            (23_850, 0.10), (96_950, 0.12), (206_700, 0.22), (394_600, 0.24),
            (501_050, 0.32), (751_600, 0.35), (float("inf"), 0.37),
        ],
        "mfs": [
            (11_925, 0.10), (48_475, 0.12), (103_350, 0.22), (197_300, 0.24),
            (250_525, 0.32), (375_800, 0.35), (float("inf"), 0.37),
        ],
        "hoh": [
            (17_000, 0.10), (64_850, 0.12), (103_350, 0.22), (197_300, 0.24),
            (250_500, 0.32), (626_350, 0.35), (float("inf"), 0.37),
        ],
    },
}

# Long-term capital-gain rates: (upper bound of *total* taxable income, rate).
# The gain stacks on top of ordinary taxable income, so these are income
# thresholds, not gain amounts.
LTCG_BRACKETS: dict[int, dict[str, list[tuple[float, float]]]] = {
    2025: {
        "single": [(48_350, 0.0), (533_400, 0.15), (float("inf"), 0.20)],
        "mfj": [(96_700, 0.0), (600_050, 0.15), (float("inf"), 0.20)],
        "mfs": [(48_350, 0.0), (300_000, 0.15), (float("inf"), 0.20)],
        "hoh": [(64_750, 0.0), (566_700, 0.15), (float("inf"), 0.20)],
    },
}

BRACKET_YEARS = sorted(ORDINARY_BRACKETS)

# Net capital loss deductible against ordinary income, per year.
LOSS_OFFSET_LIMIT = {"single": 3_000.0, "mfj": 3_000.0, "hoh": 3_000.0,
                     "mfs": 1_500.0}

NIIT_RATE = 0.038
NIIT_THRESHOLD = {"single": 200_000.0, "hoh": 200_000.0, "mfj": 250_000.0,
                  "mfs": 125_000.0}

FBAR_THRESHOLD = 10_000.0
# Form 8938, filer living in the US: year-end value (the "any time" test is
# 1.5x and needs intra-year highs we don't keep).
FORM_8938_THRESHOLD = {"single": 50_000.0, "mfs": 50_000.0, "hoh": 50_000.0,
                       "mfj": 100_000.0}


def _status(settings: TaxSettings | None) -> str:
    s = (settings.filing_status if settings else DEFAULT_STATUS) or DEFAULT_STATUS
    s = s.lower()
    return s if s in FILING_STATUSES else DEFAULT_STATUS


def _table(
    table: dict[int, dict[str, list[tuple[float, float]]]], year: int, status: str
) -> list[tuple[float, float]]:
    """Brackets for `year`, falling back to the closest earlier year on file."""
    if year in table:
        return table[year][status]
    earlier = [y for y in table if y <= year]
    return table[max(earlier) if earlier else min(table)][status]


def _add_years(d: date, years: int) -> date:
    """d + years, clamping 29 Feb to 28 Feb in a non-leap target year."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + years, day=28)


def is_long_term(buy_date: str, sell_date: str) -> bool:
    """True when the lot was held more than one year (IRS Pub. 544).

    The holding period starts the day after acquisition, so a sale *on* the
    one-year anniversary is still short-term; one day later is long-term.
    """
    buy = date.fromisoformat(buy_date)
    sell = date.fromisoformat(sell_date)
    return sell > _add_years(buy, 1)


def _is_long(sale: RealizedSale) -> bool:
    return is_long_term(sale.buy_date, sale.sell_date)


def ordinary_tax(taxable_income: float, year: int, status: str) -> float:
    """Federal ordinary-income tax on a taxable-income figure."""
    return progressive_tax(taxable_income, _table(ORDINARY_BRACKETS, year, status))


def ltcg_tax(gain: float, other_taxable_income: float, year: int, status: str) -> float:
    """Preferential-rate tax on a long-term gain stacked above ordinary income.

    The gain fills the 0/15/20 bands from wherever ordinary taxable income
    leaves off, which is why a modest filer's first slice of long-term gain is
    taxed at nothing at all.
    """
    if gain <= 0:
        return 0.0
    brackets = _table(LTCG_BRACKETS, year, status)
    base = max(0.0, other_taxable_income)
    tax = 0.0
    lower = base
    top = base + gain
    for upper, rate in brackets:
        if top <= lower:
            break
        if upper <= lower:
            continue
        tax += (min(top, upper) - lower) * rate
        lower = upper
    return tax


@dataclass
class UsTaxPeriod(TaxPeriod):
    """One tax year (or month slice) of US capital-gain result.

    The base fields hold the combined totals so the shared period chart and the
    ES-shaped call sites keep working; the short/long fields below carry the
    split the brackets actually need.
    """

    short_gain: float = 0.0
    short_loss: float = 0.0
    short_disallowed: float = 0.0
    short_recovered: float = 0.0
    long_gain: float = 0.0
    long_loss: float = 0.0
    long_disallowed: float = 0.0
    long_recovered: float = 0.0
    settings: TaxSettings = field(default_factory=TaxSettings)

    # ------------------------------------------------------------- netting
    @property
    def short_net(self) -> float:
        """Net short-term result: gains less deductible and recovered losses."""
        return (
            self.short_gain
            - (self.short_loss - self.short_disallowed)
            - self.short_recovered
        )

    @property
    def long_net(self) -> float:
        return (
            self.long_gain
            - (self.long_loss - self.long_disallowed)
            - self.long_recovered
        )

    @property
    def net_taxable(self) -> float:
        """Overall net capital gain (or loss) after cross-bucket netting."""
        return self.short_net + self.long_net

    @property
    def _taxable_buckets(self) -> tuple[float, float]:
        """(short, long) amounts actually taxed, after they offset each other."""
        total = self.net_taxable
        if total <= 0:
            return 0.0, 0.0
        if self.short_net <= 0:
            return 0.0, total
        if self.long_net <= 0:
            return total, 0.0
        return self.short_net, self.long_net

    @property
    def ordinary_offset(self) -> float:
        """Net capital loss deductible against ordinary income this year."""
        loss = max(0.0, -self.net_taxable)
        return min(loss, LOSS_OFFSET_LIMIT[_status(self.settings)])

    @property
    def carryforward_loss(self) -> float:
        """Net loss carried forward (indefinitely), after the ordinary offset."""
        return max(0.0, -self.net_taxable) - self.ordinary_offset

    @property
    def short_term_tax(self) -> float:
        """Marginal ordinary tax on the net short-term gain."""
        short, _ = self._taxable_buckets
        if short <= 0:
            return 0.0
        status = _status(self.settings)
        other = max(0.0, self.settings.other_income)
        return (
            ordinary_tax(other + short, self.year, status)
            - ordinary_tax(other, self.year, status)
        )

    @property
    def long_term_tax(self) -> float:
        short, long = self._taxable_buckets
        if long <= 0:
            return 0.0
        status = _status(self.settings)
        other = max(0.0, self.settings.other_income)
        return ltcg_tax(long, other + short, self.year, status)

    @property
    def niit(self) -> float:
        """3.8% net investment income tax, when the filer opted in."""
        if not self.settings.include_niit:
            return 0.0
        short, long = self._taxable_buckets
        nii = short + long
        if nii <= 0:
            return 0.0
        status = _status(self.settings)
        magi = max(0.0, self.settings.other_income) + nii
        over = magi - NIIT_THRESHOLD[status]
        return NIIT_RATE * max(0.0, min(nii, over))

    @property
    def estimated_tax(self) -> float:
        return self.short_term_tax + self.long_term_tax + self.niit

    @property
    def ordinary_offset_saving(self) -> float:
        """Tax the $3,000 ordinary-income deduction saves at the margin."""
        offset = self.ordinary_offset
        if offset <= 0:
            return 0.0
        status = _status(self.settings)
        other = max(0.0, self.settings.other_income)
        return ordinary_tax(other, self.year, status) - ordinary_tax(
            max(0.0, other - offset), self.year, status
        )

    # ------------------------------------------------------------ UI surface
    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi("short_net", self.short_net, "short_net_help"),
            Kpi("long_net", self.long_net, "long_net_help"),
            Kpi("carryforward_loss", self.carryforward_loss,
                "carryforward_loss_help"),
        ]

    def notes(self) -> list[Note]:
        out: list[Note] = []
        if self.disallowed_loss:
            out.append(
                Note("wash_sale_note", {"disallowed": f"{self.disallowed_loss:,.0f}"})
            )
        if self.recovered_loss:
            out.append(
                Note("recovered_note", {"recovered": f"{self.recovered_loss:,.0f}"})
            )
        if self.ordinary_offset:
            out.append(
                Note(
                    "ordinary_offset_note",
                    {
                        "offset": f"{self.ordinary_offset:,.0f}",
                        "saving": f"{self.ordinary_offset_saving:,.0f}",
                    },
                )
            )
        if self.year not in ORDINARY_BRACKETS:
            out.append(Note("bracket_year_note", {"year": f"{max(BRACKET_YEARS)}"}))
        return out


def fiscal_period(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> UsTaxPeriod:
    """Summarize an ISO date prefix ("YYYY" or "YYYY-MM") for Schedule D.

    The monthly slice is a breakdown of when a result was booked, not a taxable
    base: the buckets net over the whole tax year.
    """
    cfg = settings or TaxSettings()
    out = UsTaxPeriod(
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
        long = _is_long(s)
        gain = s.gain  # base-currency gain; USD here (see tax.base)
        if gain >= 0:
            out.realized_gain += gain
            if long:
                out.long_gain += gain
            else:
                out.short_gain += gain
            continue
        loss = -gain
        out.realized_loss += loss
        washed = bool(replacement_dates(s, buy_dates.get(s.ticker, []), WINDOW))
        if washed:
            out.disallowed_loss += loss
        if long:
            out.long_loss += loss
            out.long_disallowed += loss if washed else 0.0
        else:
            out.short_loss += loss
            out.short_disallowed += loss if washed else 0.0
    out.short_recovered = recovered_losses(
        realized, period, buy_dates, WINDOW,
        blocked_filter=lambda s: not _is_long(s),
    )
    out.long_recovered = recovered_losses(
        realized, period, buy_dates, WINDOW, blocked_filter=_is_long,
    )
    out.recovered_loss = out.short_recovered + out.long_recovered
    return out


def reporting_flags(
    total_foreign_value: float, settings: TaxSettings | None = None
) -> list[ReportingFlag]:
    """FBAR and Form 8938 thresholds against the value held abroad.

    Both regimes look at foreign *accounts* / specified foreign financial
    assets. Foreign stocks held in a US brokerage account are outside FBAR and
    outside 8938 — so this is a flag to check, never a filing verdict.
    """
    status = _status(settings)
    eight = FORM_8938_THRESHOLD[status]
    held = f"foreign holdings ~${total_foreign_value:,.0f}"
    fbar_msg = (
        f"{held} ≥ $10,000: FBAR (FinCEN 114) may apply if held in a "
        "foreign account."
        if total_foreign_value >= FBAR_THRESHOLD
        else f"{held} < $10,000 FBAR threshold."
    )
    f8938_msg = (
        f"{held} ≥ ${eight:,.0f}: Form 8938 may apply to specified foreign "
        "financial assets."
        if total_foreign_value >= eight
        else f"{held} < ${eight:,.0f} Form 8938 threshold."
    )
    return [
        flag("fbar", total_foreign_value, FBAR_THRESHOLD, fbar_msg),
        flag("form_8938", total_foreign_value, eight, f8938_msg),
    ]
