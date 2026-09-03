"""Tax jurisdictions: pick one, get a period summarizer and reporting flags.

    from stocks.portfolio import tax
    j = tax.get("US")
    period = j.fiscal_year(realized, 2025, buy_dates, tax.TaxSettings(...))
    period.estimated_tax, period.kpis(), j.reporting_flags(foreign_value)

The ledger must be replayed in the jurisdiction's own currency *and* under its
own share-identification rule — `j.currency` and `j.matching` feed
`positions.build(txs, base=…, matching=…)`, so a US filer's basis is USD at the
trade date, a Canadian's is an averaged CAD cost base and an Italian's is LIFO.
`j.year_start` moves the year boundary where it is not 1 January (6 April in
the UK, 1 July in Australia).

Adding a country means one module here plus its `portfolio.<code>_*` catalog
keys; nothing in the web or CLI layer branches on the code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from stocks.portfolio.positions import POOLED_MODES, RealizedSale
from stocks.portfolio.tax import au, ca, de, es, fr, ie, it, pt, uk, us
from stocks.portfolio.tax.base import (
    Kpi,
    Note,
    ReportingFlag,
    TaxPeriod,
    TaxSettings,
    month_range,
    tax_year_of,
)

__all__ = [
    "DEFAULT_CODE",
    "JURISDICTIONS",
    "Jurisdiction",
    "Kpi",
    "Note",
    "ReportingFlag",
    "TaxPeriod",
    "TaxSettings",
    "codes",
    "get",
    "month_range",
    "normalize",
]

PeriodFn = Callable[
    [list[RealizedSale], str, dict[str, list[str]], TaxSettings | None], TaxPeriod
]
FlagsFn = Callable[[float, TaxSettings | None], list[ReportingFlag]]


@dataclass(frozen=True)
class Jurisdiction:
    """One country's tax treatment of realized securities gains."""

    code: str
    currency: str
    _period: PeriodFn
    _flags: FlagsFn
    # Years a net loss may be carried forward; None means indefinitely.
    carryforward_years: int | None
    # (month, day) the tax year opens on: 6 April in the UK, 1 July in
    # Australia, 1 January everywhere else.
    year_start: tuple[int, int] = (1, 1)
    # Share-identification rule for the replay (positions.build(matching=…)).
    matching: str = "fifo"
    # How long a repurchase blocks the loss on a sale, as a bare token — "2m"
    # (Spain's two months), "30d" (the US wash sale, Canada's superficial
    # loss), "28d" (Ireland). Empty where no such rule exists, or where the
    # matching mode already absorbs it (the UK's 30-day rule lives in the s.104
    # replay). Language-free on purpose: the callers that show it — the daily
    # action card's harvest line — localize it themselves. The window the
    # replay actually applies is each module's WINDOW; this is its label.
    repurchase_window: str = ""
    # Filing statuses the brackets distinguish; empty when the country's rate
    # scale doesn't care (Spain's savings base doesn't).
    filing_statuses: tuple[str, ...] = ()
    # Which TaxSettings fields this jurisdiction actually reads, in the order
    # the Profile page should offer them. Spain's savings base reads none of
    # them, so that account sees no bracket inputs at all.
    settings_fields: tuple[str, ...] = ()
    # How the jurisdiction writes a tax year, when "2025" is not how.
    _year_label: Callable[[int], str] | None = None
    # Holding-period test, when the rate depends on one. Spain taxes a gain
    # the same after a week or a decade and leaves this unset; the US splits
    # short from long term at a year, Australia discounts gains past twelve
    # months and Portugal aggregates anything under 365 days.
    _long_term: Callable[[str, str], bool] | None = None

    @property
    def splits_holding_period(self) -> bool:
        """True when short- and long-term results are taxed differently."""
        return self._long_term is not None

    def is_long_term(self, buy_date: str, sell_date: str) -> bool:
        """Whether that lot's gain is long-term here. False where it can't be."""
        return bool(self._long_term and self._long_term(buy_date, sell_date))

    @property
    def pools_shares(self) -> bool:
        """True when a sale's cost can be an average rather than a lot's own."""
        return self.matching in POOLED_MODES

    def tax_year_of(self, sell_date: str) -> int:
        """The tax year a disposal belongs to (6 April boundaries included)."""
        return tax_year_of(sell_date, self.year_start)

    def year_label(self, year: int) -> str:
        """How this jurisdiction writes that year — "2025", or "2025/26"."""
        return self._year_label(year) if self._year_label else str(year)

    def fiscal_period(
        self,
        realized: list[RealizedSale],
        period: str,
        buy_dates: dict[str, list[str]],
        settings: TaxSettings | None = None,
    ) -> TaxPeriod:
        """Summarize an ISO period prefix: "YYYY" (a real base) or "YYYY-MM"."""
        return self._period(realized, period, buy_dates, settings)

    def fiscal_year(
        self,
        realized: list[RealizedSale],
        year: int,
        buy_dates: dict[str, list[str]],
        settings: TaxSettings | None = None,
    ) -> TaxPeriod:
        return self.fiscal_period(realized, f"{year:04d}", buy_dates, settings)

    def reporting_flags(
        self, total_foreign_value: float, settings: TaxSettings | None = None
    ) -> list[ReportingFlag]:
        """Foreign-asset reporting thresholds crossed by `total_foreign_value`."""
        return self._flags(total_foreign_value, settings)


JURISDICTIONS: dict[str, Jurisdiction] = {
    es.CODE: Jurisdiction(
        code=es.CODE,
        currency=es.CURRENCY,
        _period=es.fiscal_period,
        _flags=es.reporting_flags,
        carryforward_years=es.CARRYFORWARD_YEARS,
        repurchase_window="2m",
    ),
    us.CODE: Jurisdiction(
        code=us.CODE,
        currency=us.CURRENCY,
        _period=us.fiscal_period,
        _flags=us.reporting_flags,
        carryforward_years=None,  # indefinite (IRC 1212(b))
        filing_statuses=us.FILING_STATUSES,
        settings_fields=("filing_status", "other_income", "include_niit"),
        _long_term=us.is_long_term,
        repurchase_window="30d",
    ),
    uk.CODE: Jurisdiction(
        code=uk.CODE,
        currency=uk.CURRENCY,
        _period=uk.fiscal_period,
        _flags=uk.reporting_flags,
        carryforward_years=None,  # indefinite, once claimed
        year_start=uk.YEAR_START,
        matching="s104",
        settings_fields=("other_income",),
        _year_label=uk.year_label,
    ),
    de.CODE: Jurisdiction(
        code=de.CODE,
        currency=de.CURRENCY,
        _period=de.fiscal_period,
        _flags=de.reporting_flags,
        carryforward_years=None,  # indefinite, but the two circles stay apart
        filing_statuses=de.FILING_STATUSES,
        settings_fields=("filing_status", "church_tax_rate"),
    ),
    fr.CODE: Jurisdiction(
        code=fr.CODE,
        currency=fr.CURRENCY,
        _period=fr.fiscal_period,
        _flags=fr.reporting_flags,
        carryforward_years=fr.CARRYFORWARD_YEARS,
        matching="average",  # prix moyen pondéré (art. 150-0 D, 3)
    ),
    it.CODE: Jurisdiction(
        code=it.CODE,
        currency=it.CURRENCY,
        _period=it.fiscal_period,
        _flags=it.reporting_flags,
        carryforward_years=it.CARRYFORWARD_YEARS,
        matching="lifo",  # art. 67 c. 1-bis TUIR
    ),
    ie.CODE: Jurisdiction(
        code=ie.CODE,
        currency=ie.CURRENCY,
        _period=ie.fiscal_period,
        _flags=ie.reporting_flags,
        carryforward_years=None,  # indefinite, against chargeable gains
        repurchase_window="28d",
    ),
    pt.CODE: Jurisdiction(
        code=pt.CODE,
        currency=pt.CURRENCY,
        _period=pt.fiscal_period,
        _flags=pt.reporting_flags,
        carryforward_years=pt.CARRYFORWARD_YEARS,
        settings_fields=("other_income",),
        _long_term=pt.is_long_term,
    ),
    ca.CODE: Jurisdiction(
        code=ca.CODE,
        currency=ca.CURRENCY,
        _period=ca.fiscal_period,
        _flags=ca.reporting_flags,
        carryforward_years=None,  # indefinite, capital gains only
        matching="average",  # adjusted cost base (ITA s.47)
        settings_fields=("other_income", "subnational_rate"),
        repurchase_window="30d",
    ),
    au.CODE: Jurisdiction(
        code=au.CODE,
        currency=au.CURRENCY,
        _period=au.fiscal_period,
        _flags=au.reporting_flags,
        carryforward_years=None,  # indefinite, capital gains only
        year_start=au.YEAR_START,
        settings_fields=("other_income",),
        _year_label=au.year_label,
        _long_term=au.is_long_term,
    ),
}

# Spain stays the default: this app was built for a Spanish filer, and an
# unset preference must not silently re-tax an existing book under other rules.
DEFAULT_CODE = es.CODE


def codes() -> tuple[str, ...]:
    return tuple(JURISDICTIONS)


def normalize(code: str | None) -> str:
    """A supported jurisdiction code, or the default. Accepts 'es', 'US-CA'."""
    if not code:
        return DEFAULT_CODE
    head = str(code).replace("_", "-").split("-")[0].upper()
    return head if head in JURISDICTIONS else DEFAULT_CODE


def get(code: str | None = None) -> Jurisdiction:
    return JURISDICTIONS[normalize(code)]
