"""US federal capital-gains engine — pure, no network."""

import pytest

from stocks.portfolio import tax
from stocks.portfolio.positions import RealizedSale
from stocks.portfolio.tax import TaxSettings
from stocks.portfolio.tax.us import (
    fiscal_period,
    is_long_term,
    ltcg_tax,
    ordinary_tax,
    reporting_flags,
)


def sale(ticker, buy_date, sell_date, cost, proceeds, qty=1) -> RealizedSale:
    return RealizedSale(ticker, buy_date, sell_date, qty, cost, proceeds, "USD")


def year(realized, y=2025, buys=None, **settings):
    return fiscal_period(
        realized, str(y), buys or {}, TaxSettings(**settings) if settings else None
    )


# --- holding period ---

def test_one_year_anniversary_is_still_short_term():
    assert not is_long_term("2024-01-10", "2025-01-10")


def test_day_after_anniversary_is_long_term():
    assert is_long_term("2024-01-10", "2025-01-11")


def test_leap_day_lot_clamps_to_28_feb():
    assert not is_long_term("2024-02-29", "2025-02-28")
    assert is_long_term("2024-02-29", "2025-03-01")


# --- brackets ---

def test_ordinary_tax_first_bracket_single():
    assert ordinary_tax(10_000, 2025, "single") == pytest.approx(1_000)


def test_ltcg_zero_bracket_absorbs_a_modest_gain():
    # Single filer, $40k ordinary income: the 0% band runs to $48,350, so the
    # first $8,350 of gain is untaxed and the rest is 15%.
    assert ltcg_tax(20_000, 40_000, 2025, "single") == pytest.approx(
        8_350 * 0.0 + 11_650 * 0.15
    )


def test_ltcg_top_rate_above_the_15_pct_ceiling():
    assert ltcg_tax(10_000, 600_000, 2025, "single") == pytest.approx(10_000 * 0.20)


def test_bracket_table_falls_back_to_the_latest_year_on_file():
    assert ordinary_tax(10_000, 2999, "single") == ordinary_tax(10_000, 2025, "single")


# --- bucketing ---

def test_short_and_long_gains_split_by_holding_period():
    ty = year([
        sale("AAPL", "2025-01-02", "2025-06-01", 1_000, 1_500),   # short +500
        sale("MSFT", "2023-01-02", "2025-06-01", 1_000, 3_000),   # long +2000
    ])
    assert ty.short_net == pytest.approx(500)
    assert ty.long_net == pytest.approx(2_000)
    assert ty.net_taxable == pytest.approx(2_500)


def test_short_term_gain_is_taxed_at_the_marginal_ordinary_rate():
    ty = year(
        [sale("AAPL", "2025-01-02", "2025-06-01", 10_000, 20_000)],
        other_income=100_000,
    )
    # $100k lands in the 22% band (to $103,350); the $10k gain spills into 24%.
    assert ty.short_term_tax == pytest.approx(3_350 * 0.22 + 6_650 * 0.24)
    assert ty.long_term_tax == 0.0
    assert ty.estimated_tax == pytest.approx(ty.short_term_tax)


def test_long_term_gain_uses_preferential_rates():
    ty = year(
        [sale("MSFT", "2023-01-02", "2025-06-01", 10_000, 30_000)],
        other_income=40_000,
    )
    assert ty.short_term_tax == 0.0
    assert ty.long_term_tax == pytest.approx(8_350 * 0.0 + 11_650 * 0.15)


def test_short_term_gain_pushes_the_long_term_gain_up_the_bands():
    ty = year(
        [
            sale("AAPL", "2025-01-02", "2025-06-01", 1_000, 11_000),  # +10k short
            sale("MSFT", "2023-01-02", "2025-06-01", 1_000, 11_000),  # +10k long
        ],
        other_income=40_000,
    )
    # Ordinary income becomes 50k for stacking, above the 48,350 zero band.
    assert ty.long_term_tax == pytest.approx(10_000 * 0.15)


# --- netting ---

def test_short_term_loss_offsets_a_long_term_gain():
    ty = year(
        [
            sale("AAPL", "2025-01-02", "2025-06-01", 5_000, 1_000),   # -4000 short
            sale("MSFT", "2023-01-02", "2025-06-01", 1_000, 11_000),  # +10000 long
        ],
        other_income=40_000,
    )
    assert ty.net_taxable == pytest.approx(6_000)
    assert ty.short_term_tax == 0.0
    # The whole net is long-term: 8,350 at 0%, remainder at 15%.
    assert ty.long_term_tax == pytest.approx(6_000 * 0.0)


def test_net_capital_loss_deducts_3000_and_carries_the_rest_forward():
    ty = year([sale("AAPL", "2025-01-02", "2025-06-01", 20_000, 5_000)])
    assert ty.net_taxable == pytest.approx(-15_000)
    assert ty.ordinary_offset == pytest.approx(3_000)
    assert ty.carryforward_loss == pytest.approx(12_000)
    assert ty.estimated_tax == 0.0


def test_married_filing_separately_offset_is_1500():
    ty = year(
        [sale("AAPL", "2025-01-02", "2025-06-01", 20_000, 5_000)],
        filing_status="mfs",
    )
    assert ty.ordinary_offset == pytest.approx(1_500)
    assert ty.carryforward_loss == pytest.approx(13_500)


def test_ordinary_offset_saving_is_valued_at_the_margin():
    ty = year(
        [sale("AAPL", "2025-01-02", "2025-06-01", 20_000, 5_000)],
        other_income=100_000,
    )
    assert ty.ordinary_offset_saving == pytest.approx(3_000 * 0.22)


# --- wash sale ---

def test_wash_sale_disallows_the_loss():
    s = sale("AAPL", "2025-01-02", "2025-06-01", 5_000, 1_000)
    ty = year([s], buys={"AAPL": ["2025-01-02", "2025-06-20"]})
    assert ty.disallowed_loss == pytest.approx(4_000)
    assert ty.short_net == 0.0
    assert ty.carryforward_loss == 0.0


def test_repurchase_outside_30_days_leaves_the_loss_alone():
    s = sale("AAPL", "2025-01-02", "2025-06-01", 5_000, 1_000)
    ty = year([s], buys={"AAPL": ["2025-01-02", "2025-07-15"]})
    assert ty.disallowed_loss == 0.0
    assert ty.short_net == pytest.approx(-4_000)


def test_disallowed_loss_comes_back_when_the_replacement_is_sold():
    buys = {"AAPL": ["2025-01-02", "2025-06-20"]}
    washed = sale("AAPL", "2025-01-02", "2025-06-01", 5_000, 1_000)
    replacement = sale("AAPL", "2025-06-20", "2026-02-01", 1_000, 1_200)
    realized = [washed, replacement]
    assert year(realized, 2025, buys).recovered_loss == 0.0
    later = year(realized, 2026, buys)
    assert later.recovered_loss == pytest.approx(4_000)
    # +200 gain on the replacement, less the 4,000 loss it released.
    assert later.net_taxable == pytest.approx(-3_800)


def test_recovered_loss_keeps_the_character_of_the_blocked_loss():
    buys = {"MSFT": ["2023-01-02", "2025-06-20"]}
    washed = sale("MSFT", "2023-01-02", "2025-06-01", 5_000, 1_000)  # long
    replacement = sale("MSFT", "2025-06-20", "2026-02-01", 1_000, 1_000)  # short
    later = year([washed, replacement], 2026, buys)
    assert later.long_recovered == pytest.approx(4_000)
    assert later.short_recovered == 0.0
    assert later.long_net == pytest.approx(-4_000)


# --- NIIT ---

def test_niit_is_opt_in():
    kw = dict(other_income=300_000)
    gain = [sale("MSFT", "2023-01-02", "2025-06-01", 10_000, 60_000)]
    assert year(gain, **kw).niit == 0.0
    assert year(gain, include_niit=True, **kw).niit == pytest.approx(
        50_000 * 0.038
    )


def test_niit_only_taxes_the_excess_over_the_threshold():
    ty = year(
        [sale("MSFT", "2023-01-02", "2025-06-01", 10_000, 60_000)],
        other_income=180_000,
        include_niit=True,
    )
    # MAGI 230k, threshold 200k -> only 30k of the 50k gain is exposed.
    assert ty.niit == pytest.approx(30_000 * 0.038)


# --- notes and flags ---

def test_notes_flag_wash_sales_and_the_ordinary_offset():
    ty = year(
        [sale("AAPL", "2025-01-02", "2025-06-01", 20_000, 5_000)],
        other_income=100_000,
    )
    keys = {n.key for n in ty.notes()}
    assert "ordinary_offset_note" in keys


def test_reporting_flags_cover_fbar_and_8938():
    flags = {f.name: f for f in reporting_flags(60_000)}
    assert flags["fbar"].reportable
    assert flags["form_8938"].reportable
    joint = {f.name: f for f in reporting_flags(60_000, TaxSettings(filing_status="mfj"))}
    assert not joint["form_8938"].reportable  # $100k threshold when filing jointly


# --- registry ---

def test_registry_exposes_every_jurisdiction():
    assert tax.codes() == (
        "ES", "US", "UK", "DE", "FR", "IT", "IE", "PT", "CA", "AU",
    )
    assert tax.get("US").currency == "USD"
    assert tax.get("es").code == "ES"


def test_unknown_jurisdiction_falls_back_to_the_default():
    assert tax.normalize("XX") == tax.DEFAULT_CODE
    assert tax.normalize(None) == "ES"
    assert tax.normalize("us-ny") == "US"


def test_jurisdiction_fiscal_year_dispatches():
    realized = [sale("AAPL", "2025-01-02", "2025-06-01", 1_000, 2_000)]
    es_ty = tax.get("ES").fiscal_year(realized, 2025, {})
    us_ty = tax.get("US").fiscal_year(realized, 2025, {})
    assert es_ty.currency == "EUR" and us_ty.currency == "USD"
    assert es_ty.estimated_tax == pytest.approx(1_000 * 0.19)
    assert us_ty.estimated_tax == pytest.approx(1_000 * 0.10)
