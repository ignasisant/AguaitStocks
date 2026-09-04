"""The jurisdiction-neutral scaffolding every country module opens with.

`open_period` and `sales_in` were ten copies of the same eleven lines before
they moved to base.py; these cover them once, so a change to the stamp or the
period filter fails here rather than in ten country suites at once.
"""

import pytest

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.base import TaxPeriod, open_period, sales_in
from stocks.portfolio.tax.us import UsTaxPeriod


def sale(sell_date, ticker="AAPL"):
    return RealizedSale(ticker, "2020-01-02", sell_date, 1, 100.0, 150.0, "USD")


# --- open_period ---

def test_the_period_carries_its_jurisdiction_currency_and_year():
    out = open_period(TaxPeriod, "ES", "EUR", "2025")
    assert (out.jurisdiction, out.currency, out.year) == ("ES", "EUR", 2025)


def test_a_month_slice_keeps_the_month_and_still_reads_the_year_off_it():
    out = open_period(TaxPeriod, "ES", "EUR", "2025-03")
    assert out.period == "2025-03" and out.year == 2025


def test_it_returns_the_subclass_it_was_handed_not_the_base():
    out = open_period(UsTaxPeriod, "US", "USD", "2025", settings=TaxSettings())
    assert isinstance(out, UsTaxPeriod)


def test_extra_fields_reach_the_subclass():
    cfg = TaxSettings()
    out = open_period(UsTaxPeriod, "US", "USD", "2025", settings=cfg)
    assert out.settings is cfg


def test_a_field_the_subclass_does_not_declare_is_an_error():
    # The base TaxPeriod has no `settings`; passing one is a programming
    # mistake in the country module, not something to swallow.
    with pytest.raises(TypeError):
        open_period(TaxPeriod, "ES", "EUR", "2025", settings=TaxSettings())


def test_it_opens_empty():
    out = open_period(TaxPeriod, "ES", "EUR", "2025")
    assert out.sales == [] and out.realized_gain == 0.0


# --- sales_in ---

def test_only_the_period_s_disposals_come_through():
    rows = [sale("2024-12-31"), sale("2025-06-01"), sale("2026-01-01")]
    assert [s.sell_date for s in sales_in("2025", rows)] == ["2025-06-01"]


def test_a_month_slice_narrows_to_that_month():
    rows = [sale("2025-03-01"), sale("2025-04-01")]
    assert [s.sell_date for s in sales_in("2025-03", rows)] == ["2025-03-01"]


def test_ledger_order_is_kept():
    rows = [sale("2025-06-01", "MSFT"), sale("2025-01-02", "AAPL")]
    assert [s.ticker for s in sales_in("2025", rows)] == ["MSFT", "AAPL"]


def test_a_tax_year_that_opens_in_april_spans_the_boundary():
    rows = [sale("2025-04-05"), sale("2025-04-06"), sale("2026-04-05")]
    got = [s.sell_date for s in sales_in("2025", rows, (4, 6))]
    assert got == ["2025-04-06", "2026-04-05"]


def test_nothing_in_the_period_yields_nothing():
    assert list(sales_in("2025", [sale("2024-06-01")])) == []
