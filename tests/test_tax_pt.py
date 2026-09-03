"""Portugal — 28% on the saldo, with the short-term aggregation rule."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.pt import (
    FLAT_RATE,
    TOP_MARGINAL_RATE,
    fiscal_period,
    is_long_term,
    reporting_flags,
    top_bracket_for,
)


def sale(sell_date, cost, proceeds, buy_date="2020-01-02", ticker="EDP.LS"):
    return RealizedSale(ticker, buy_date, sell_date, 1, cost, proceeds, "EUR")


def year(realized, y=2025, **settings):
    return fiscal_period(realized, str(y), {}, TaxSettings(**settings))


def keys(period):
    return {n.key for n in period.notes()}


# --- the 365-day line ---

def test_a_year_less_a_day_is_short_term():
    assert not is_long_term("2024-01-01", "2024-12-30")  # 364 days
    assert is_long_term("2024-01-01", "2024-12-31")  # 365 days exactly


# --- the flat rate ---

def test_the_saldo_pays_twenty_eight_percent():
    ty = year([sale("2025-03-10", 10_000, 20_000)])
    assert ty.net_taxable == pytest.approx(10_000)
    assert ty.estimated_tax == pytest.approx(10_000 * FLAT_RATE)
    assert "flat_rate_note" in keys(ty)


def test_a_short_term_loss_nets_against_a_long_term_gain():
    ty = year([
        sale("2025-03-10", 10_000, 20_000),  # +10,000 long
        sale("2025-06-10", 10_000, 8_000, buy_date="2025-01-05"),  # -2,000 short
    ])
    assert ty.short_net == pytest.approx(-2_000)
    assert ty.long_net == pytest.approx(10_000)
    assert ty.net_taxable == pytest.approx(8_000)
    assert ty.estimated_tax == pytest.approx(8_000 * FLAT_RATE)


def test_a_negative_saldo_carries_forward_five_years():
    ty = year([sale("2025-03-10", 10_000, 4_000)])
    assert ty.estimated_tax == 0.0
    assert ty.carryforward_loss == pytest.approx(6_000)
    assert "carryforward_note" in keys(ty)
    assert tax.get("PT").carryforward_years == 5


# --- the aggregation rule ---

def test_a_top_bracket_filer_aggregates_short_term_gains():
    short = sale("2025-06-10", 10_000, 15_000, buy_date="2025-01-05")
    ty = year([short], other_income=90_000.0)
    assert ty.aggregated
    assert ty.estimated_tax == pytest.approx(5_000 * TOP_MARGINAL_RATE)
    assert {"aggregation_note", "solidarity_note"} <= keys(ty)


def test_below_the_top_bracket_the_flat_rate_still_applies():
    short = sale("2025-06-10", 10_000, 15_000, buy_date="2025-01-05")
    ty = year([short], other_income=40_000.0)
    assert not ty.aggregated
    assert ty.estimated_tax == pytest.approx(5_000 * FLAT_RATE)
    assert "short_term_note" in keys(ty)  # but it warns where the line is


def test_long_term_gains_are_never_aggregated():
    ty = year([sale("2025-03-10", 10_000, 15_000)], other_income=200_000.0)
    assert not ty.aggregated
    assert ty.estimated_tax == pytest.approx(5_000 * FLAT_RATE)


def test_the_threshold_falls_back_to_the_last_published_year():
    assert top_bracket_for(2025) == pytest.approx(83_696)
    assert top_bracket_for(2030) == pytest.approx(83_696)
    assert "bracket_year_note" in keys(year([sale("2030-03-10", 1_000, 2_000)], 2030))


# --- reporting ---

def test_foreign_gains_go_in_anexo_j_whatever_their_size():
    (flag,) = reporting_flags(1.0)
    assert flag.name == "anexo_j" and flag.reportable
    assert not reporting_flags(0.0)[0].reportable


# --- registry ---

def test_portugal_is_registered_with_fifo_and_a_holding_period_split():
    j = tax.get("PT")
    assert (j.currency, j.matching, j.year_start) == ("EUR", "fifo", (1, 1))
    assert j.splits_holding_period  # the under-365-day column matters here
    assert not j.pools_shares
    assert j.settings_fields == ("other_income",)
