"""Jurisdiction resolution and wording for the web layer."""

import pytest

from stocks.portfolio import tax
from stocks.web import tax_ui


@pytest.fixture
def locale(monkeypatch):
    """Pretend the browser reports `code` as its locale."""

    class _Ctx:
        locale = None

    ctx = _Ctx()
    monkeypatch.setattr(tax_ui.st, "context", ctx)

    def _set(code):
        ctx.locale = code

    return _set


# --- resolution ---

def test_explicit_preference_wins(locale):
    locale("es-ES")
    assert tax_ui.resolve_code({"tax_residence": "US"}) == "US"


def test_auto_reads_the_browser_region(locale):
    locale("en-US")
    assert tax_ui.resolve_code({}) == "US"
    assert tax_ui.resolve_code({"tax_residence": "auto"}) == "US"


def test_a_supported_region_resolves_to_its_jurisdiction(locale):
    locale("de-DE")
    assert tax_ui.resolve_code({}) == "DE"


def test_unknown_region_falls_back_to_spain(locale):
    """A book has to be taxed under some rules; this app's home is Spain."""
    locale("fr-FR")
    assert tax_ui.resolve_code({}) == tax.DEFAULT_CODE == "ES"


def test_a_language_without_a_region_falls_back(locale):
    locale("en")
    assert tax_ui.resolve_code({}) == "ES"


def test_region_of_needs_a_two_letter_subtag():
    assert tax_ui.region_of("en_US") == "US"
    assert tax_ui.region_of("es-419") is None
    assert tax_ui.region_of(None) is None


# --- settings ---

def test_settings_come_from_prefs():
    s = tax_ui.settings(
        {"tax_filing_status": "mfj", "tax_other_income": "90000", "tax_niit": True}
    )
    assert (s.filing_status, s.other_income, s.include_niit) == ("mfj", 90_000.0, True)


def test_a_junk_income_preference_does_not_break_the_tab():
    assert tax_ui.settings({"tax_other_income": "lots"}).other_income == 0.0


# --- wording ---

def test_key_prefers_the_jurisdictions_own_copy():
    assert tax_ui.key("US", "estimated_tax_help") == "portfolio.us_estimated_tax_help"
    # No US override for the shared label: the neutral key answers.
    assert tax_ui.key("US", "net_taxable") == "portfolio.net_taxable"
    assert tax_ui.key("ES", "tax_header") == "portfolio.es_tax_header"


def test_money_puts_the_right_symbol_on():
    assert tax_ui.money(1_240, "EUR") == "€1,240"
    assert tax_ui.money(-3_000, "USD") == "$-3,000"
    assert tax_ui.money(1_240, "USD", signed=True) == "$+1,240"


def test_flag_caption_is_localized_per_flag(monkeypatch):
    monkeypatch.setattr(tax_ui.i18n, "active_language", lambda: "en")
    es_flag = tax.get("ES").reporting_flags(60_000)[0]
    caption = tax_ui.flag_caption("ES", es_flag, "EUR")
    assert "Modelo 720" in caption and "€60,000" in caption
    fbar = tax.get("US").reporting_flags(5_000)[0]
    assert "FBAR" in tax_ui.flag_caption("US", fbar, "USD")


def test_an_unworded_flag_still_renders(monkeypatch):
    monkeypatch.setattr(tax_ui.i18n, "active_language", lambda: "en")
    odd = tax.ReportingFlag("form_9999", 12_000, 10_000, True)
    caption = tax_ui.flag_caption("US", odd, "USD")
    assert "$12,000" in caption and "portfolio." not in caption
