"""Canada — half the gain, marginal rates, and the superficial-loss rule."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.ca import (
    INCLUSION_RATE,
    T1135_THRESHOLD,
    federal_tax,
    fiscal_period,
    reporting_flags,
)


def sale(sell_date, cost, proceeds, ticker="SHOP.TO", buy_date="2020-01-02", qty=1):
    return RealizedSale(ticker, buy_date, sell_date, qty, cost, proceeds, "CAD")


def year(realized, y=2025, buys=None, **settings):
    return fiscal_period(realized, str(y), buys or {}, TaxSettings(**settings))


def keys(period):
    return {n.key for n in period.notes()}


# --- the inclusion rate ---

def test_only_half_the_gain_is_taxable():
    ty = year([sale("2025-03-10", 10_000, 20_000)])
    assert INCLUSION_RATE == 0.5
    assert ty.net_taxable == pytest.approx(10_000)
    assert ty.taxable_gain == pytest.approx(5_000)
    assert "inclusion_note" in keys(ty)


def test_the_taxable_half_stacks_on_the_other_income():
    ty = year([sale("2025-03-10", 10_000, 20_000)], other_income=60_000.0)
    # 60,000 sits in the 20.5% bracket, and 5,000 more stays inside it.
    assert ty.federal_tax == pytest.approx(5_000 * 0.205)
    expected = federal_tax(65_000, 2025) - federal_tax(60_000, 2025)
    assert ty.federal_tax == pytest.approx(expected)


def test_the_provincial_rate_is_added_flat():
    ty = year(
        [sale("2025-03-10", 10_000, 20_000)],
        other_income=60_000.0,
        subnational_rate=0.12,
    )
    assert ty.provincial_tax == pytest.approx(600)
    assert ty.estimated_tax == pytest.approx(5_000 * 0.205 + 600)
    assert "provincial_note" in keys(ty)


def test_without_a_provincial_rate_the_estimate_says_it_is_federal_only():
    ty = year([sale("2025-03-10", 10_000, 20_000)], other_income=60_000.0)
    assert ty.provincial_tax == 0.0
    assert "no_provincial_note" in keys(ty)


def test_a_net_loss_carries_forward_indefinitely():
    ty = year([sale("2025-03-10", 10_000, 6_000)])
    assert ty.estimated_tax == 0.0
    assert ty.carryforward_loss == pytest.approx(4_000)
    assert "carryback_note" in keys(ty)  # three years back, not applied here
    assert tax.get("CA").carryforward_years is None


# --- superficial losses ---

def test_a_repurchase_within_thirty_days_denies_the_loss():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    ty = year([losing], buys={"SHOP.TO": ["2020-01-02", "2025-03-25"]})
    assert ty.disallowed_loss == pytest.approx(2_000)
    assert ty.deductible_loss == 0.0
    assert "superficial_loss_note" in keys(ty)


def test_the_window_looks_both_ways():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    before = year([losing], buys={"SHOP.TO": ["2020-01-02", "2025-02-20"]})
    assert before.disallowed_loss == pytest.approx(2_000)


def test_a_repurchase_after_thirty_days_leaves_the_loss_alone():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    ty = year([losing], buys={"SHOP.TO": ["2020-01-02", "2025-04-10"]})
    assert ty.disallowed_loss == 0.0
    assert ty.deductible_loss == pytest.approx(2_000)


def test_the_denied_loss_returns_when_the_replacement_is_sold():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    replacement = sale("2025-09-10", 8_000, 9_000, buy_date="2025-03-25", qty=10)
    buys = {"SHOP.TO": ["2020-01-02", "2025-03-25"]}
    ty = year([losing, replacement], buys=buys)
    assert ty.recovered_loss == pytest.approx(2_000)
    assert "recovered_note" in keys(ty)


# --- brackets ---

def test_rates_fall_back_to_the_last_published_year_and_say_so():
    ty = year([sale("2030-03-10", 10_000, 20_000)], 2030)
    assert ty.taxable_gain == pytest.approx(5_000)
    assert "bracket_year_note" in keys(ty)


# --- reporting ---

def test_t1135_flags_a_hundred_thousand_of_foreign_property():
    (flag,) = reporting_flags(T1135_THRESHOLD)
    assert flag.name == "t1135" and flag.reportable
    assert not reporting_flags(50_000.0)[0].reportable


# --- registry ---

def test_canada_is_registered_with_an_averaged_cost_base():
    j = tax.get("CA")
    assert (j.currency, j.matching, j.year_start) == ("CAD", "average", (1, 1))
    assert j.pools_shares  # the ACB is an average, not a lot's own cost
    assert not j.splits_holding_period  # no holding-period preference here
    assert j.settings_fields == ("other_income", "subnational_rate")
