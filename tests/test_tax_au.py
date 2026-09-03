"""Australia — the 50% CGT discount and the 1 July income year."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.au import (
    MEDICARE_LEVY,
    fiscal_period,
    is_long_term,
    reporting_flags,
    year_label,
)
from stocks.portfolio.tax.base import covers, tax_year_of


def sale(sell_date, cost, proceeds, buy_date="2020-01-02", ticker="BHP.AX"):
    return RealizedSale(ticker, buy_date, sell_date, 1, cost, proceeds, "AUD")


def year(realized, y=2024, **settings):
    return fiscal_period(realized, str(y), {}, TaxSettings(**settings))


def keys(period):
    return {n.key for n in period.notes()}


# --- the 1 July income year ---

def test_a_may_disposal_belongs_to_the_year_that_opened_in_july():
    assert tax_year_of("2025-05-10", (7, 1)) == 2024
    assert tax_year_of("2025-06-30", (7, 1)) == 2024  # last day of 2024-25
    assert tax_year_of("2025-07-01", (7, 1)) == 2025  # first day of 2025-26


def test_the_period_filter_spans_the_july_boundary():
    assert covers("2024", "2025-06-30", (7, 1))
    assert covers("2024", "2024-07-01", (7, 1))
    assert not covers("2024", "2024-06-30", (7, 1))


def test_the_year_is_written_the_australian_way():
    assert year_label(2025) == "2025-26"


# --- the discount ---

def test_twelve_months_exactly_is_not_long_enough():
    assert not is_long_term("2023-06-01", "2024-06-01")
    assert is_long_term("2023-06-01", "2024-06-02")


def test_a_discounted_gain_is_halved():
    ty = year([sale("2025-03-10", 10_000, 20_000)])
    assert ty.discount == pytest.approx(5_000)
    assert ty.net_taxable == pytest.approx(5_000)
    assert "discount_note" in keys(ty)


def test_a_gain_held_under_a_year_gets_nothing():
    ty = year([sale("2025-03-10", 10_000, 20_000, buy_date="2024-09-01")])
    assert ty.discount == 0.0
    assert ty.net_taxable == pytest.approx(10_000)


def test_losses_hit_the_undiscounted_gains_first():
    """The ordering is the taxpayer's choice, and this is the better one."""
    ty = year([
        sale("2025-03-10", 10_000, 20_000),  # +10,000, discountable
        sale("2025-03-11", 10_000, 14_000, buy_date="2024-09-01"),  # +4,000, not
        sale("2025-04-10", 10_000, 6_000, ticker="CBA.AX"),  # -4,000
    ])
    # Loss eats the 4,000 non-discounted gain, leaving 10,000 to be halved.
    assert ty.net_taxable == pytest.approx(5_000)
    # Spending the loss on the discounted gain instead would have cost more:
    # 4,000 + (10,000 - 4,000) / 2 = 7,000.
    assert "loss_order_note" in keys(ty)


def test_losses_beyond_the_gains_carry_forward():
    ty = year([sale("2025-03-10", 10_000, 4_000)])
    assert ty.net_taxable == 0.0
    assert ty.estimated_tax == 0.0
    assert ty.carryforward_loss == pytest.approx(6_000)
    assert "carryforward_note" in keys(ty)
    assert tax.get("AU").carryforward_years is None


# --- the tax itself ---

def test_the_gain_is_taxed_at_the_marginal_rate_plus_medicare():
    ty = year([sale("2025-03-10", 10_000, 20_000)], other_income=100_000.0)
    # 5,000 of net gain, entirely inside the 30% bracket.
    assert ty.marginal_tax == pytest.approx(1_500)
    assert ty.medicare == pytest.approx(5_000 * MEDICARE_LEVY)
    assert ty.estimated_tax == pytest.approx(1_600)
    assert "medicare_note" in keys(ty)


def test_the_tax_free_threshold_leaves_a_small_gain_almost_untaxed():
    ty = year([sale("2025-03-10", 10_000, 18_000)])  # 8,000 gain, 4,000 net
    assert ty.marginal_tax == 0.0  # below 18,200
    assert ty.estimated_tax == pytest.approx(80)  # the Medicare levy alone


def test_rates_fall_back_to_the_last_published_year_and_say_so():
    ty = year([sale("2030-03-10", 10_000, 20_000)], 2030)
    assert "rate_year_note" in keys(ty)


def test_no_foreign_asset_regime_applies():
    assert reporting_flags(5_000_000) == []


# --- registry ---

def test_australia_is_registered_with_a_july_year_and_fifo():
    j = tax.get("AU")
    assert (j.currency, j.matching, j.year_start) == ("AUD", "fifo", (7, 1))
    assert j.splits_holding_period  # discounted vs not
    assert not j.pools_shares
    assert j.year_label(2025) == "2025-26"
    assert j.settings_fields == ("other_income",)
