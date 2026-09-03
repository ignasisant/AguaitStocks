"""Ireland — 33% CGT on shares, 41% exit tax on funds. Pure, no network."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.ie import (
    CGT_RATE,
    EXIT_TAX_RATE,
    PERSONAL_EXEMPTION,
    fiscal_period,
    reporting_flags,
)

FUNDS = frozenset({"IWDA.L"})


def sale(sell_date, cost, proceeds, ticker="RYA.IR", buy_date="2020-01-02", qty=1):
    return RealizedSale(ticker, buy_date, sell_date, qty, cost, proceeds, "EUR")


def year(realized, y=2025, buys=None, **settings):
    return fiscal_period(realized, str(y), buys or {}, TaxSettings(**settings))


def keys(period):
    return {n.key for n in period.notes()}


# --- the exemption ---

def test_the_first_1270_of_gain_is_exempt():
    ty = year([sale("2025-03-10", 10_000, 20_000)])
    assert PERSONAL_EXEMPTION == 1_270.0
    assert ty.exemption == pytest.approx(1_270)
    assert ty.net_taxable == pytest.approx(8_730)
    assert ty.estimated_tax == pytest.approx(8_730 * CGT_RATE)


def test_a_small_gain_uses_only_part_of_the_exemption():
    ty = year([sale("2025-03-10", 10_000, 10_500)])
    assert ty.exemption == pytest.approx(500)
    assert ty.net_taxable == 0.0
    assert ty.estimated_tax == 0.0
    assert "wasted_exemption_note" in keys(ty)


def test_losses_come_off_before_the_exemption():
    ty = year([
        sale("2025-03-10", 10_000, 12_000),  # +2,000
        sale("2025-06-10", 10_000, 9_000, ticker="KRZ.IR"),  # -1,000
    ])
    assert ty.share_net == pytest.approx(1_000)
    assert ty.exemption == pytest.approx(1_000)  # capped by what is left
    assert ty.net_taxable == 0.0


def test_a_net_loss_carries_forward_and_wastes_the_exemption():
    ty = year([sale("2025-03-10", 10_000, 6_000)])
    assert ty.carryforward_loss == pytest.approx(4_000)
    assert ty.exemption == 0.0
    assert ty.wasted_exemption == pytest.approx(1_270)
    assert "carryforward_note" in keys(ty)
    assert tax.get("IE").carryforward_years is None  # indefinite


# --- the four-week rule ---

def test_a_reacquisition_inside_four_weeks_restricts_the_loss():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    ty = year([losing], buys={"RYA.IR": ["2020-01-02", "2025-03-25"]})
    assert ty.share_disallowed == pytest.approx(2_000)
    assert ty.deductible_loss == 0.0
    assert "four_week_note" in keys(ty)


def test_a_purchase_before_the_sale_restricts_nothing():
    """Forward-only: unlike Spain's two months, buying first is fine."""
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    ty = year([losing], buys={"RYA.IR": ["2020-01-02", "2025-02-25"]})
    assert ty.share_disallowed == 0.0
    assert ty.deductible_loss == pytest.approx(2_000)


def test_a_reacquisition_after_four_weeks_restricts_nothing():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    ty = year([losing], buys={"RYA.IR": ["2020-01-02", "2025-04-08"]})  # day 29
    assert ty.share_disallowed == 0.0


def test_the_restricted_loss_comes_back_when_the_replacement_is_sold():
    losing = sale("2025-03-10", 10_000, 8_000, qty=10)
    replacement = sale(
        "2025-09-10", 8_000, 9_000, buy_date="2025-03-25", qty=10
    )
    buys = {"RYA.IR": ["2020-01-02", "2025-03-25"]}
    ty = year([losing, replacement], buys=buys)
    assert ty.recovered_loss == pytest.approx(2_000)
    assert "recovered_note" in keys(ty)


# --- funds ---

def test_a_fund_gain_pays_41_percent_outside_the_exemption():
    ty = year(
        [sale("2025-03-10", 10_000, 15_000, ticker="IWDA.L")],
        fund_tickers=FUNDS,
    )
    assert ty.fund_net == pytest.approx(5_000)
    assert ty.exemption == 0.0  # the exemption is for CGT assets only
    assert ty.estimated_tax == pytest.approx(5_000 * EXIT_TAX_RATE)
    assert "fund_note" in keys(ty)


def test_a_fund_loss_relieves_nothing_at_all():
    ty = year(
        [
            sale("2025-03-10", 10_000, 20_000),  # +10,000 on shares
            sale("2025-06-10", 10_000, 6_000, ticker="IWDA.L"),  # -4,000 fund
        ],
        fund_tickers=FUNDS,
    )
    assert ty.fund_net == pytest.approx(-4_000)
    assert ty.share_net == pytest.approx(10_000)  # untouched by the fund loss
    assert ty.carryforward_loss == 0.0  # and it does not carry forward either
    assert "fund_loss_note" in keys(ty)


def test_an_unclassified_book_is_all_shares_and_says_so():
    ty = year([sale("2025-03-10", 10_000, 15_000, ticker="IWDA.L")])
    assert ty.share_gain == pytest.approx(5_000)
    assert ty.fund_gain == 0.0
    assert "funds_unclassified_note" in keys(ty)


# --- the calendar ---

def test_the_december_payment_deadline_is_always_shown():
    ty = year([sale("2025-03-10", 1_000, 2_000)])
    assert "payment_note" in keys(ty)


def test_no_foreign_asset_regime_applies():
    assert reporting_flags(5_000_000) == []


# --- registry ---

def test_ireland_is_registered_with_fifo_and_a_calendar_year():
    j = tax.get("IE")
    assert (j.currency, j.matching, j.year_start) == ("EUR", "fifo", (1, 1))
    assert not j.pools_shares and not j.splits_holding_period
    assert j.settings_fields == ()  # both rates are flat
