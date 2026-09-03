"""Italy — the 26% imposta sostitutiva. Pure, no network."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.it import (
    IVAFE_RATE,
    SUBSTITUTE_RATE,
    fiscal_period,
    reporting_flags,
    substitute_tax,
)


def sale(sell_date, cost, proceeds, ticker="ENI.MI", buy_date="2020-01-02"):
    return RealizedSale(ticker, buy_date, sell_date, 1, cost, proceeds, "EUR")


def year(realized, y=2025, **settings):
    return fiscal_period(realized, str(y), {}, TaxSettings(**settings))


def keys(period):
    return {n.key for n in period.notes()}


# --- the flat 26% ---

def test_the_substitute_tax_is_twenty_six_percent():
    assert SUBSTITUTE_RATE == pytest.approx(0.26)
    assert substitute_tax(10_000) == pytest.approx(2_600)
    assert substitute_tax(-5_000) == 0.0


def test_the_saldo_nets_gains_and_losses():
    ty = year([
        sale("2025-03-10", 10_000, 18_000),  # +8,000
        sale("2025-06-10", 10_000, 9_000, ticker="ISP.MI"),  # -1,000
    ])
    assert ty.net_taxable == pytest.approx(7_000)
    assert ty.estimated_tax == pytest.approx(1_820)


def test_a_negative_saldo_carries_forward_four_years():
    ty = year([sale("2025-03-10", 10_000, 4_000)])
    assert ty.estimated_tax == 0.0
    assert ty.carryforward_loss == pytest.approx(6_000)
    assert "carryforward_note" in keys(ty)
    assert tax.get("IT").carryforward_years == 4


# --- notes ---

def test_lifo_and_the_regime_are_always_worth_saying():
    ty = year([sale("2025-03-10", 1_000, 2_000)])
    assert {"lifo_note", "regime_note"} <= keys(ty)


# --- reporting ---

def test_quadro_rw_applies_to_securities_at_any_value():
    (flag,) = reporting_flags(20_000.0)
    assert flag.name == "quadro_rw"
    assert flag.reportable
    # IVAFE is 0.2% of the value, and the message says so in euros.
    assert f"{20_000 * IVAFE_RATE:,.0f}" in flag.message


def test_nothing_abroad_needs_no_quadro_rw():
    (flag,) = reporting_flags(0.0)
    assert not flag.reportable


# --- registry ---

def test_italy_is_registered_with_lifo_matching():
    j = tax.get("IT")
    assert (j.currency, j.matching, j.year_start) == ("EUR", "lifo", (1, 1))
    # LIFO names a real purchase, so no "matched" column is needed.
    assert not j.pools_shares
    assert not j.splits_holding_period
    assert j.settings_fields == ()
