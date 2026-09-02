"""Tax jurisdictions: pick one, get a period summarizer and reporting flags.

    from stocks.portfolio import tax
    j = tax.get("US")
    period = j.fiscal_year(realized, 2025, buy_dates, tax.TaxSettings(...))
    period.estimated_tax, period.kpis(), j.reporting_flags(foreign_value)

The ledger must be replayed in the jurisdiction's own currency — `j.currency`
feeds `positions.build(txs, to_base=fx.converter(j.currency))`, so a US filer's
basis is USD at the trade date and a Spanish filer's is EUR at the ECB rate for
that date. Adding a country means one module here plus its `portfolio.<code>_*`
catalog keys; nothing in the web or CLI layer branches on the code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import de, es, uk, us
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
    # (month, day) the tax year opens on. (1, 1) everywhere but the UK.
    year_start: tuple[int, int] = (1, 1)
    # Share-identification rule for the replay (positions.build(matching=…)).
    matching: str = "fifo"
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
    # short from long term at the one-year mark.
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
        return self.matching == "s104"

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
