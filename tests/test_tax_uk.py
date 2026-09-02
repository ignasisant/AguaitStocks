"""UK capital gains tax — pure, no network."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.base import covers, tax_year_of
from stocks.portfolio.tax.uk import (
    BASIC_RATE_BAND,
    cgt,
    fiscal_period,
    reporting_flags,
    year_label,
)


def sale(sell_date, cost, proceeds, ticker="LLOY.L", buy_date="2020-01-02"):
    qty = 1
    return RealizedSale(ticker, buy_date, sell_date, qty, cost, proceeds, "GBP")


def year(realized, y=2025, **settings):
    return fiscal_period(realized, str(y), {}, TaxSettings(**settings))


# --- the 6 April tax year ---

def test_a_february_disposal_belongs_to_the_year_that_opened_in_april():
    assert tax_year_of("2026-02-02", (4, 6)) == 2025
    assert tax_year_of("2025-04-05", (4, 6)) == 2024  # last day of 2024/25
    assert tax_year_of("2025-04-06", (4, 6)) == 2025  # first day of 2025/26


def test_the_period_filter_spans_the_april_boundary():
    assert covers("2025", "2026-02-02", (4, 6))
    assert covers("2025", "2025-04-06", (4, 6))
    assert not covers("2025", "2025-04-05", (4, 6))
    assert not covers("2025", "2026-04-06", (4, 6))


def test_a_calendar_jurisdiction_is_unaffected():
    assert covers("2025", "2025-02-02")
    assert not covers("2025", "2026-02-02")


def test_the_year_is_written_the_british_way():
    assert year_label(2025) == "2025/26"
    assert year_label(2099) == "2099/00"


def test_a_disposal_after_5_april_lands_in_the_next_year():
    early = sale("2025-04-05", 1_000, 20_000)   # 2024/25
    late = sale("2025-04-06", 1_000, 20_000)    # 2025/26
    assert year([early, late], 2024).realized_gain == pytest.approx(19_000)
    assert year([early, late], 2025).realized_gain == pytest.approx(19_000)
    assert len(year([early, late], 2025).sales) == 1


# --- allowance ---

def test_the_annual_exempt_amount_shelters_the_first_3000():
    ty = year([sale("2025-06-01", 1_000, 3_500)])
    assert ty.allowance == pytest.approx(2_500)  # gain smaller than the AEA
    assert ty.net_taxable == 0.0
    assert ty.estimated_tax == 0.0


def test_gains_above_the_allowance_are_taxable():
    ty = year([sale("2025-06-01", 1_000, 14_000)])
    assert ty.net_taxable == pytest.approx(10_000)  # 13,000 - 3,000


def test_the_allowance_was_bigger_in_earlier_years():
    ty = year([sale("2022-06-01", 1_000, 20_000)], 2022)
    assert ty.allowance == pytest.approx(12_300)


def test_losses_come_off_before_the_allowance_and_can_waste_it():
    """The trap: a loss eats the gain, and the AEA is simply gone."""
    ty = year([
        sale("2025-06-01", 1_000, 6_000),    # +5,000
        sale("2025-07-01", 10_000, 6_000),   # -4,000
    ])
    assert ty.net_taxable == 0.0
    assert ty.allowance == pytest.approx(1_000)
    assert ty.wasted_allowance == pytest.approx(2_000)
    assert "wasted_allowance_note" in {n.key for n in ty.notes()}


def test_a_net_loss_carries_forward():
    ty = year([sale("2025-06-01", 20_000, 5_000)])
    assert ty.net_taxable == 0.0
    assert ty.carryforward_loss == pytest.approx(15_000)
    assert "carryforward_note" in {n.key for n in ty.notes()}


# --- rates ---

def test_gains_inside_the_basic_rate_band_pay_18_percent():
    ty = year([sale("2025-06-01", 1_000, 14_000)], other_income=10_000)
    assert ty.estimated_tax == pytest.approx(10_000 * 0.18)


def test_gains_above_the_band_pay_24_percent():
    ty = year([sale("2025-06-01", 1_000, 14_000)], other_income=50_000)
    assert ty.estimated_tax == pytest.approx(10_000 * 0.24)


def test_a_gain_that_fills_the_band_pays_both_rates():
    band_left = BASIC_RATE_BAND - 30_000
    ty = year([sale("2025-06-01", 1_000, 54_000)], other_income=30_000)
    taxable = 50_000  # 53,000 gain less the 3,000 AEA
    assert ty.estimated_tax == pytest.approx(
        band_left * 0.18 + (taxable - band_left) * 0.24
    )


def test_the_older_rates_applied_before_the_budget():
    assert cgt(10_000, 0, 2023) == pytest.approx(10_000 * 0.10)
    assert cgt(10_000, 60_000, 2023) == pytest.approx(10_000 * 0.20)


def test_the_straddle_year_says_it_used_one_pair_of_rates():
    ty = year([sale("2024-06-01", 1_000, 20_000)], 2024)
    assert "straddle_note" in {n.key for n in ty.notes()}


def test_a_year_with_no_table_falls_back_and_says_so():
    ty = year([sale("2099-06-01", 1_000, 20_000)], 2099)
    assert ty.allowance == pytest.approx(3_000)
    assert "rate_year_note" in {n.key for n in ty.notes()}


# --- reporting ---

def test_big_proceeds_trigger_the_reporting_note_even_without_a_gain():
    ty = year([sale("2025-06-01", 60_000, 60_500)])
    assert ty.proceeds_total == pytest.approx(60_500)
    assert "proceeds_test_note" in {n.key for n in ty.notes()}


def test_no_foreign_asset_regime_applies():
    assert reporting_flags(5_000_000) == []


# --- registry and the replay it asks for ---

def test_the_uk_is_registered_with_pooling_and_an_april_year():
    j = tax.get("UK")
    assert (j.currency, j.year_start, j.matching) == ("GBP", (4, 6), "s104")
    assert j.pools_shares and not j.splits_holding_period
    assert j.year_label(2025) == "2025/26"
    assert j.settings_fields == ("other_income",)  # no joint filing for CGT


def test_the_other_jurisdictions_still_use_fifo_and_calendar_years():
    for code in ("ES", "US", "DE"):
        j = tax.get(code)
        assert j.matching == "fifo" and j.year_start == (1, 1)
        assert not j.pools_shares
