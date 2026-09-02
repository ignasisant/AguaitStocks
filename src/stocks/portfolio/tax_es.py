"""Back-compatible surface for the Spain-only tax API.

The engine moved to `stocks.portfolio.tax` (one module per jurisdiction) when
US rules were added. Existing importers — the CLI, the chat tools, tests —
keep working through these re-exports; new code should go through
`tax.get(code)` so it isn't hard-wired to one country.
"""

from __future__ import annotations

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax.base import (
    ReportingFlag,
    TaxSettings,
    month_range,
    shift_months,
)
from stocks.portfolio.tax.es import (
    CARRYFORWARD_YEARS,
    MODELO_720_THRESHOLD_EUR,
    SAVINGS_BRACKETS,
    WINDOW,
    EsTaxPeriod,
    fiscal_period,
    reporting_flags,
    tax_on_savings_base,
)

__all__ = [
    "CARRYFORWARD_YEARS",
    "MODELO_720_THRESHOLD_EUR",
    "SAVINGS_BRACKETS",
    "ForeignAssetFlag",
    "TaxYear",
    "fiscal_period",
    "fiscal_year",
    "modelo_720_flag",
    "month_range",
    "tax_on_savings_base",
]

# Old names for the period type and the 720 flag.
TaxYear = EsTaxPeriod
ForeignAssetFlag = ReportingFlag

_shift_months = shift_months


def _within_two_months(sell, buy) -> bool:
    """True if `buy` falls in the two calendar months before or after `sell`."""
    return WINDOW(sell, buy)


def fiscal_year(
    realized: list[RealizedSale],
    year: int,
    buy_dates: dict[str, list[str]],
    settings: TaxSettings | None = None,
) -> EsTaxPeriod:
    """Summarize one calendar year's realized sales for the savings base."""
    return fiscal_period(realized, f"{year:04d}", buy_dates, settings)


def modelo_720_flag(total_foreign_value: float) -> ReportingFlag:
    """The >50.000 EUR foreign-asset flag, with the pre-refactor `.message`."""
    return reporting_flags(total_foreign_value)[0]
