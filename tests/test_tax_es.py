"""Spanish tax engine tests — pure, no network."""

from datetime import date

import pytest

from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax_es import (
    _within_two_months,
    fiscal_year,
    modelo_720_flag,
    tax_on_savings_base,
)


def sale(ticker, buy_date, sell_date, cost, proceeds) -> RealizedSale:
    return RealizedSale(ticker, buy_date, sell_date, 1, cost, proceeds, "EUR")


# --- brackets ---

def test_tax_zero_or_negative_base():
    assert tax_on_savings_base(0) == 0.0
    assert tax_on_savings_base(-1000) == 0.0


def test_tax_first_bracket():
    assert tax_on_savings_base(6_000) == pytest.approx(6_000 * 0.19)


def test_tax_progressive_spans_brackets():
    # 60k: 6000@19% + 44000@21% + 10000@23%
    expected = 6_000 * 0.19 + 44_000 * 0.21 + 10_000 * 0.23
    assert tax_on_savings_base(60_000) == pytest.approx(expected)


def test_tax_top_bracket():
    # 400k reaches the 28% top band
    expected = (
        6_000 * 0.19 + 44_000 * 0.21 + 150_000 * 0.23
        + 100_000 * 0.27 + 100_000 * 0.28
    )
    assert tax_on_savings_base(400_000) == pytest.approx(expected)


# --- 2-month rule ---

def test_within_two_months_window():
    s = date(2025, 3, 15)
    assert _within_two_months(s, date(2025, 4, 1))   # after, inside
    assert _within_two_months(s, date(2025, 1, 20))  # before, inside
    assert not _within_two_months(s, date(2025, 6, 1))  # too late


def test_fiscal_year_nets_gains_and_losses():
    realized = [
        sale("AAPL", "2024-01-01", "2025-02-01", 1000, 1500),  # +500 gain
        sale("MSFT", "2024-01-01", "2025-03-01", 1000, 700),   # -300 loss, no rebuy
    ]
    ty = fiscal_year(realized, 2025, buy_dates={"AAPL": ["2024-01-01"],
                                                "MSFT": ["2024-01-01"]})
    assert ty.realized_gain_eur == pytest.approx(500)
    assert ty.realized_loss_eur == pytest.approx(300)
    assert ty.deferred_loss_eur == pytest.approx(0)
    assert ty.net_taxable_eur == pytest.approx(200)
    assert ty.estimated_tax_eur == pytest.approx(200 * 0.19)


def test_two_month_rule_defers_loss_on_repurchase():
    # sold MSFT at a loss 2025-03-01, rebought within 2 months -> loss deferred
    realized = [sale("MSFT", "2024-01-01", "2025-03-01", 1000, 700)]
    buy_dates = {"MSFT": ["2024-01-01", "2025-04-10"]}  # 2025-04-10 is a replacement
    ty = fiscal_year(realized, 2025, buy_dates)
    assert ty.realized_loss_eur == pytest.approx(300)
    assert ty.deferred_loss_eur == pytest.approx(300)
    assert ty.deductible_loss_eur == pytest.approx(0)
    assert ty.net_taxable_eur == pytest.approx(0)


def test_quick_roundtrip_loss_not_flagged():
    # bought and sold within 2 months, no later replacement -> deductible now
    realized = [sale("MSFT", "2025-02-01", "2025-03-01", 1000, 700)]
    buy_dates = {"MSFT": ["2025-02-01"]}  # only the sold lot's own purchase
    ty = fiscal_year(realized, 2025, buy_dates)
    assert ty.deferred_loss_eur == pytest.approx(0)
    assert ty.deductible_loss_eur == pytest.approx(300)


def test_net_loss_carries_forward_no_tax():
    realized = [sale("MSFT", "2024-01-01", "2025-03-01", 1000, 400)]  # -600
    ty = fiscal_year(realized, 2025, buy_dates={"MSFT": ["2024-01-01"]})
    assert ty.net_taxable_eur == pytest.approx(-600)
    assert ty.estimated_tax_eur == 0.0
    assert ty.carryforward_loss_eur == pytest.approx(600)


def test_fiscal_year_filters_by_year():
    realized = [
        sale("AAPL", "2023-01-01", "2024-06-01", 1000, 1200),  # +200 in 2024
        sale("AAPL", "2024-01-01", "2025-06-01", 1000, 1300),  # +300 in 2025
    ]
    ty = fiscal_year(realized, 2025, buy_dates={"AAPL": ["2023-01-01", "2024-01-01"]})
    assert ty.realized_gain_eur == pytest.approx(300)
    assert len(ty.sales) == 1


# --- foreign-asset flag ---

def test_modelo_720_flag_threshold():
    assert not modelo_720_flag(49_999).reportable
    assert modelo_720_flag(50_000).reportable
    assert modelo_720_flag(120_000).reportable
