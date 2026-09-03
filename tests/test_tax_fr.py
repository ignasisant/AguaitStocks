"""France — the PFU on plus-values. Pure, no network."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.fr import (
    INCOME_TAX_RATE,
    PFU_RATE,
    SOCIAL_RATE,
    fiscal_period,
    pfu,
    reporting_flags,
)


def sale(sell_date, cost, proceeds, ticker="MC.PA", buy_date="2020-01-02"):
    return RealizedSale(ticker, buy_date, sell_date, 1, cost, proceeds, "EUR")


def year(realized, y=2025, **settings):
    return fiscal_period(realized, str(y), {}, TaxSettings(**settings))


def keys(period):
    return {n.key for n in period.notes()}


# --- the flat 30% ---

def test_the_pfu_is_twelve_point_eight_plus_seventeen_point_two():
    assert INCOME_TAX_RATE + SOCIAL_RATE == pytest.approx(PFU_RATE)
    assert PFU_RATE == pytest.approx(0.30)
    assert pfu(10_000) == pytest.approx(3_000)


def test_the_two_halves_are_reported_separately_and_add_up():
    ty = year([sale("2025-03-10", 10_000, 20_000)])
    assert ty.net_taxable == pytest.approx(10_000)
    assert ty.income_tax == pytest.approx(1_280)
    assert ty.social_charges == pytest.approx(1_720)
    assert ty.estimated_tax == pytest.approx(3_000)
    values = {k.key: k.value for k in ty.kpis()}
    assert values["income_tax"] + values["social_charges"] == pytest.approx(
        values["estimated_tax"]
    )


def test_losses_net_against_gains_inside_the_year():
    ty = year([
        sale("2025-03-10", 10_000, 18_000),  # +8,000
        sale("2025-06-10", 10_000, 7_000, ticker="OR.PA"),  # -3,000
    ])
    assert ty.net_taxable == pytest.approx(5_000)
    assert ty.estimated_tax == pytest.approx(1_500)


def test_a_net_loss_taxes_nothing_and_carries_forward_ten_years():
    ty = year([sale("2025-03-10", 10_000, 6_000)])
    assert ty.estimated_tax == 0.0
    assert ty.carryforward_loss == pytest.approx(4_000)
    assert "carryforward_note" in keys(ty)
    assert tax.get("FR").carryforward_years == 10


# --- notes ---

def test_the_split_and_the_pea_are_always_worth_saying():
    ty = year([sale("2025-03-10", 10_000, 12_000)])
    assert {"pfu_note", "pea_note"} <= keys(ty)
    assert "cehr_note" not in keys(ty)


def test_a_big_result_warns_about_the_cehr():
    ty = year([sale("2025-03-10", 10_000, 310_000)])
    assert "cehr_note" in keys(ty)


# --- periods ---

def test_a_month_slice_is_a_breakdown_of_the_same_year():
    realized = [
        sale("2025-03-10", 1_000, 3_000),
        sale("2025-06-10", 1_000, 2_000),
    ]
    march = fiscal_period(realized, "2025-03", {})
    assert march.realized_gain == pytest.approx(2_000)
    assert len(march.sales) == 1


# --- reporting ---

def test_a_foreign_account_is_declarable_at_any_value():
    (flag,) = reporting_flags(1.0)
    assert flag.name == "formulaire_3916"
    assert flag.reportable
    assert "3916" in flag.message


def test_nothing_abroad_needs_no_3916():
    (flag,) = reporting_flags(0.0)
    assert not flag.reportable


# --- registry ---

def test_france_is_registered_with_an_averaged_cost_base():
    j = tax.get("FR")
    assert (j.currency, j.matching, j.year_start) == ("EUR", "average", (1, 1))
    assert j.pools_shares  # the PMP is not any one purchase's price
    assert not j.splits_holding_period  # holding period changes nothing
    assert j.settings_fields == ()  # a flat rate reads no bracket inputs
