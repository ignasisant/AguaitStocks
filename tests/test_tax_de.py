"""German Abgeltungsteuer engine — pure, no network."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.de import (
    fiscal_period,
    reporting_flags,
    tax_on_capital_income,
)

FUNDS = frozenset({"VWCE.DE"})


def sale(ticker, buy_date, sell_date, cost, proceeds, qty=1) -> RealizedSale:
    return RealizedSale(ticker, buy_date, sell_date, qty, cost, proceeds, "EUR")


def year(realized, y=2025, **settings):
    settings.setdefault("fund_tickers", FUNDS)
    return fiscal_period(realized, str(y), {}, TaxSettings(**settings))


# --- the rate itself ---

def test_flat_rate_carries_the_solidarity_surcharge():
    # 25% plus 5.5% *of the tax* — the 26.375% everyone quotes.
    assert tax_on_capital_income(10_000) == pytest.approx(2_637.5)


@pytest.mark.parametrize(
    ("rate", "all_in"),
    [(0.0, 0.26375), (0.08, 0.278186), (0.09, 0.279951)],
)
def test_church_tax_reduces_the_base_it_is_charged_on(rate, all_in):
    """§32d(1): the tax is e/(4+k), because church tax is deductible.

    Charging 25% and adding the church rate on top would overstate the bill —
    the published all-in rates are 27.82% at 8% and 27.99% at 9%.
    """
    tax = tax_on_capital_income(10_000, rate)
    assert tax / 10_000 == pytest.approx(all_in, abs=5e-5)


def test_the_nine_percent_all_in_rate_is_the_published_2799():
    assert tax_on_capital_income(10_000, 0.09) / 10_000 == pytest.approx(
        0.279951, abs=1e-6
    )


def test_no_tax_on_a_loss():
    assert tax_on_capital_income(-5_000) == 0.0


# --- allowance ---

def test_the_sparer_pauschbetrag_shelters_the_first_1000():
    ty = year([sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 1_800)])
    assert ty.allowance == pytest.approx(800)  # gain is smaller than the cap
    assert ty.net_taxable == 0.0
    assert ty.estimated_tax == 0.0


def test_a_joint_return_doubles_the_allowance():
    realized = [sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 2_800)]
    assert year(realized).net_taxable == pytest.approx(800)  # 1,800 - 1,000
    joint = year(realized, filing_status="joint")
    assert joint.allowance == pytest.approx(1_800)
    assert joint.net_taxable == 0.0


def test_tax_applies_to_the_amount_above_the_allowance():
    ty = year([sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 12_000)])
    assert ty.net_taxable == pytest.approx(10_000)
    assert ty.estimated_tax == pytest.approx(2_637.5)


# --- the two loss circles ---

def test_share_losses_cannot_touch_fund_gains():
    ty = year([
        sale("SAP.DE", "2024-01-02", "2025-06-01", 10_000, 4_000),   # -6,000 shares
        sale("VWCE.DE", "2024-01-02", "2025-07-01", 10_000, 30_000),  # fund gain
    ])
    # The fund gain is taxed (less its 30% exemption and the allowance); the
    # share loss only waits for a future share gain.
    assert ty.general_gain == pytest.approx(14_000)  # 20,000 × 0.70
    assert ty.share_net == pytest.approx(-6_000)
    assert ty.net_taxable == pytest.approx(13_000)  # 14,000 - 1,000 allowance
    assert ty.share_carryforward == pytest.approx(6_000)


def test_a_general_loss_may_offset_share_gains():
    ty = year([
        sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 21_000),     # +20,000
        sale("VWCE.DE", "2024-01-02", "2025-07-01", 20_000, 10_000),   # fund loss
    ])
    # The fund loss counts at 70% too: 7,000 against the 20,000 share gain.
    assert ty.general_net == pytest.approx(-7_000)
    assert ty.net_taxable == pytest.approx(12_000)  # 13,000 - 1,000
    assert ty.share_carryforward == 0.0
    assert ty.general_carryforward == 0.0


def test_a_general_loss_bigger_than_the_share_gain_carries_the_remainder():
    ty = year([
        sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 3_000),      # +2,000
        sale("VWCE.DE", "2024-01-02", "2025-07-01", 20_000, 0),        # fund loss
    ])
    assert ty.general_net == pytest.approx(-14_000)
    assert ty.net_taxable == 0.0
    assert ty.general_carryforward == pytest.approx(12_000)  # 14,000 - 2,000


def test_share_losses_net_inside_their_own_circle_first():
    ty = year([
        sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 11_000),   # +10,000
        sale("BMW.DE", "2024-01-02", "2025-06-01", 10_000, 6_000),   # -4,000
    ])
    assert ty.share_net == pytest.approx(6_000)
    assert ty.net_taxable == pytest.approx(5_000)
    assert ty.carryforward_loss == 0.0


# --- Teilfreistellung ---

def test_thirty_percent_of_a_fund_gain_is_exempt():
    ty = year([sale("VWCE.DE", "2024-01-02", "2025-06-01", 10_000, 20_000)])
    assert ty.realized_gain == pytest.approx(7_000)
    assert ty.fund_exempt == pytest.approx(3_000)


def test_thirty_percent_of_a_fund_loss_is_exempt_too():
    """The half people forget: the exemption cuts losses, not just gains."""
    ty = year([sale("VWCE.DE", "2024-01-02", "2025-06-01", 20_000, 10_000)])
    assert ty.general_loss == pytest.approx(7_000)
    assert ty.fund_exempt == pytest.approx(3_000)


def test_without_a_classification_no_exemption_is_applied_and_it_says_so():
    realized = [sale("VWCE.DE", "2024-01-02", "2025-06-01", 10_000, 20_000)]
    ty = fiscal_period(realized, "2025", {}, TaxSettings())
    assert ty.realized_gain == pytest.approx(10_000)  # full gain, no 30% off
    assert ty.fund_exempt == 0.0
    assert "funds_unclassified_note" in {n.key for n in ty.notes()}


def test_an_unclassified_but_checked_book_does_not_warn():
    ty = year([sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 2_000)],
              fund_tickers=frozenset())
    assert "funds_unclassified_note" not in {n.key for n in ty.notes()}


# --- notes and flags ---

def test_notes_cover_the_allowance_the_restriction_and_the_vorabpauschale():
    ty = year([
        sale("SAP.DE", "2024-01-02", "2025-06-01", 1_000, 12_000),
        sale("BMW.DE", "2024-01-02", "2025-06-01", 5_000, 1_000),
    ], church_tax_rate=0.09)
    keys = {n.key for n in ty.notes()}
    assert {"allowance_note", "church_note", "vorabpauschale_note"} <= keys


def test_a_foreign_broker_puts_the_income_in_anlage_kap():
    flags = {f.name: f for f in reporting_flags(20_000)}
    assert flags["anlage_kap"].reportable
    assert not flags["awv"].reportable
    assert not {f.name: f for f in reporting_flags(0)}["anlage_kap"].reportable


def test_the_awv_threshold_is_five_million():
    flags = {f.name: f for f in reporting_flags(5_000_000)}
    assert flags["awv"].reportable


# --- registry ---

def test_germany_is_registered_with_its_own_knobs():
    j = tax.get("DE")
    assert (j.currency, j.filing_statuses) == ("EUR", ("single", "joint"))
    assert j.settings_fields == ("filing_status", "church_tax_rate")
    assert not j.splits_holding_period  # a week or a decade, same rate
    assert j.carryforward_years is None


def test_no_wash_sale_rule_here():
    """A repurchase inside days changes nothing — §33.5.f has no German twin."""
    s = sale("SAP.DE", "2024-01-02", "2025-06-01", 10_000, 4_000)
    ty = fiscal_period(
        [s], "2025", {"SAP.DE": ["2024-01-02", "2025-06-10"]}, TaxSettings()
    )
    assert ty.disallowed_loss == 0.0
    assert ty.share_net == pytest.approx(-6_000)
