"""The Realized & tax tab, rendered per jurisdiction through the real script.

Unit tests cover the rules (test_tax_es / test_tax_us) and the wording
(test_tax_ui); this covers what neither can — that the page reads the tax
residence, replays the ledger at that jurisdiction's currency and renders its
own KPIs, columns and reporting flags. No network: FX and the priced positions
table are stubbed.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from stocks.data import fx
from stocks.portfolio import ledger
from stocks.portfolio.ledger import Transaction
from stocks.web import auth, portfolio_data, widgets

PAGE = "src/stocks/web/app_pages/portfolio.py"

# One long-term winner and one short-term loser, both in USD, so the US split
# has something in each bucket and Spain still sees one net figure.
TXS = [
    Transaction("2023-01-10", "MSFT", "buy", 10, 200.0, "USD", 1.0),
    Transaction("2026-02-02", "MSFT", "sell", 10, 300.0, "USD", 1.0),
    Transaction("2026-01-05", "AAPL", "buy", 10, 150.0, "USD", 1.0),
    Transaction("2026-03-05", "AAPL", "sell", 10, 120.0, "USD", 1.0),
]


# Two years of quarterly round-trips: the monthly chart then spans ~25
# categories, enough that the phone thinning has something to thin.
LONG_RUN = [
    t
    for i, (buy, sell) in enumerate(
        [
            ("2024-01-10", "2024-03-11"),
            ("2024-04-10", "2024-06-11"),
            ("2024-07-10", "2024-09-11"),
            ("2024-10-10", "2024-12-11"),
            ("2025-04-10", "2025-06-11"),
            ("2025-10-10", "2025-12-11"),
            ("2026-01-10", "2026-01-20"),
        ]
    )
    for t in (
        Transaction(buy, "NVDA", "buy", 1, 100.0 + i, "USD", 1.0),
        Transaction(sell, "NVDA", "sell", 1, 110.0 + i, "USD", 1.0),
    )
]


@pytest.fixture
def paths(tmp_path):
    p = auth.UserPaths(
        root=tmp_path,
        watchlist=tmp_path / "watchlist.yaml",
        db=tmp_path / "portfolio.db",
        last_import=tmp_path / "last_import.json",
        prefs=tmp_path / "prefs.json",
        chat=tmp_path / "chat.json",
        bank=tmp_path / "bank.json",
        action=tmp_path / "daily_action.json",
    )
    ledger.add_many(TXS, p.db)
    return p


@pytest.fixture
def page(monkeypatch, paths):
    """The page signed in, with FX and live prices stubbed out."""
    monkeypatch.setattr(auth, "require_login", lambda: paths)
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(auth, "db_path", lambda: paths.db)
    monkeypatch.setattr(auth, "current_email", lambda: "me@example.com")
    # 1 USD = 0.90 EUR flat: enough for a total, and no request either way.
    monkeypatch.setattr(fx, "prefetch", lambda *a, **k: None)
    monkeypatch.setattr(
        fx, "rate_on",
        lambda day, base, quote: {("USD", "EUR"): 0.9, ("EUR", "USD"): 1 / 0.9}.get(
            (base.upper(), quote.upper()), 1.0
        ),
    )
    # Shaped like the real positions frame (analysis.portfolio.positions_frame)
    # so the Positions tab renders from it, not just the tax tab's total.
    priced = pd.DataFrame(
        {
            "shares": [10.0],
            "ccy": ["USD"],
            "cost": [1_800.0],
            "value": [90_000.0],
            "pnl": [88_200.0],
            "pnl_pct": [49.0],
        },
        index=["MSFT"],
    )
    priced.index.name = "ticker"
    monkeypatch.setattr(portfolio_data, "positions_table", lambda *a: priced.copy())
    monkeypatch.setattr(
        portfolio_data, "eur_spot", lambda quote, base="EUR": 1 / 0.9
    )
    portfolio_data.ledger_state.clear()

    def _run(residence: str | None, currency: str = "EUR", tab: str = "tax"):
        prefs = dict(auth.DEFAULT_PREFS)
        prefs["language"] = "en"
        prefs["currency"] = currency
        prefs["tax_residence"] = residence
        prefs["tax_other_income"] = 100_000.0
        paths.prefs.write_text(json.dumps(prefs))
        at = AppTest.from_file(PAGE, default_timeout=60)
        at.query_params["tab"] = tab
        at.run()
        assert not at.exception, at.exception
        return at

    return _run


def _text(at) -> str:
    """Everything the page rendered, markup included."""
    parts = [str(e.value) for e in at.markdown] + [str(e.value) for e in at.caption]
    parts += [str(e.value) for e in at.subheader]
    parts += [str(getattr(e, "body", "")) for e in at.get("html")]
    return "\n".join(parts)


def test_a_spanish_filer_gets_the_savings_base_in_euros(page):
    body = _text(page("ES"))
    assert "IRPF savings base" in body
    assert "Modelo 720" in body
    assert "€" in body and "FBAR" not in body


def test_a_us_filer_gets_short_and_long_term_in_dollars(page):
    body = _text(page("US"))
    assert "Capital gains" in body
    assert "Short-term net" in body and "Long-term net" in body
    assert "FBAR" in body and "Form 8938" in body
    assert "$" in body and "Modelo 720" not in body


def test_the_us_basis_is_usd_not_a_converted_euro_figure(page):
    """The USD replay must not go through the EUR one (0.9 rate apart)."""
    body = _text(page("US"))
    # 10 shares 200 -> 300 with a 1.00 fee each leg: +$998 long-term.
    assert "$998" in body


def test_the_spanish_basis_stays_in_euros(page):
    body = _text(page("ES"))
    assert "€898" in body  # the same sale at 0.90 EUR/USD


def test_the_holding_period_column_is_us_only(page):
    assert "Term" in _text(page("US"))
    assert ">Term<" not in _text(page("ES"))


def test_a_german_filer_gets_the_flat_rate_and_the_loss_circles(page):
    body = _text(page("DE"))
    assert "Capital income" in body
    assert "Shares net" in body and "Funds &amp; other net" in body
    assert "Anlage KAP" in body
    assert "€" in body and "Modelo 720" not in body and "FBAR" not in body


def test_the_german_page_says_the_vorabpauschale_is_not_modelled(page):
    """A gap the filer must know about is worse silent than stated."""
    assert "Vorabpauschale" in _text(page("DE"))


def test_a_uk_filer_gets_pooled_costs_and_an_april_tax_year(page):
    body = _text(page("UK"))
    assert "2025/26" in body  # the sale is 2 Feb 2026 -> the 2025/26 year
    assert "Allowance used" in body and "Matched" in body
    assert "s.104 pool" in body
    assert "£" in body and "IRC 1091" not in body


def test_the_uk_replay_pools_instead_of_matching_the_oldest_lot(page):
    """One lot here, so the pooled cost equals it — what matters is the label."""
    body = _text(page("UK"))
    assert "s.104 pool" in body and "FBAR" not in body


def test_a_french_filer_gets_the_pfu_split_and_an_averaged_basis(page):
    body = _text(page("FR"))
    assert "Plus-values" in body
    assert "Income tax (12.8%)" in body and "Social charges (17.2%)" in body
    assert "Average cost" in body  # the prix moyen pondéré, labelled as such
    assert "3916" in body
    assert "€" in body and "FBAR" not in body


def test_an_italian_filer_gets_the_flat_rate_and_no_matching_column(page):
    """LIFO names a real purchase, so there is nothing to disclose there."""
    body = _text(page("IT"))
    assert "Plusvalenze" in body and "Quadro RW" in body
    assert "LIFO" in body
    assert "Average cost" not in body and "s.104 pool" not in body


def test_an_irish_filer_gets_the_exemption_and_the_december_deadline(page):
    body = _text(page("IE"))
    assert "Exemption used" in body and "Fund result" in body
    assert "15 December" in body
    assert "€" in body and "Modelo 720" not in body


def test_a_portuguese_filer_gets_the_365_day_split(page):
    body = _text(page("PT"))
    assert "Mais-valias" in body
    assert "Under 365 days" in body and "365 days or more" in body
    assert "Anexo J" in body


def test_a_canadian_filer_gets_the_taxable_half_in_dollars(page):
    body = _text(page("CA"))
    assert "Taxable half" in body and "T1135" in body
    assert "Average cost" in body  # the ACB is not one lot's cost
    assert "CA$" in body


def test_an_australian_filer_gets_the_discount_and_a_july_year(page):
    body = _text(page("AU"))
    assert "2025-26" in body  # the sales are Feb/Mar 2026 -> the 2025-26 year
    assert "Discount" in body and "Income year" in body
    assert "A$" in body and "FBAR" not in body


# ------------------------------------------------- reporting currency (Profile)
# The tax tab follows the tax residence; everything else follows the account's
# reporting currency, and the ledger is replayed in it either way.


def test_the_positions_tab_reckons_in_the_reporting_currency(page):
    body = _text(page("ES", currency="USD", tab="positions"))
    assert "Open positions & P/L (USD)" in body
    assert "$" in body


def test_the_reporting_currency_does_not_move_the_tax_figures(page):
    """A Spanish filer's base is EUR even when the app reports in dollars."""
    body = _text(page("ES", currency="USD"))
    assert "IRPF savings base" in body
    assert "€898" in body  # the sale at 0.90 EUR/USD, not its dollar figure


# ------------------------------------------------------ the period chart, phone
# The card is the same figure on both screens; what changes is what a ~390px
# canvas can print. No AppTest accessor exposes a plotly figure, so these read
# the element's own spec.


def _figure(at) -> dict:
    return json.loads(at.get("plotly_chart")[0].proto.spec)


def _monthly(at):
    """Flip the granularity control to the monthly view and rerun."""
    at.session_state["tax_granularity"] = "month"
    at.run()
    assert not at.exception, at.exception
    return at


def test_the_monthly_chart_thins_its_labels_on_a_phone(page, paths, monkeypatch):
    """~25 slanted "2025-01" labels overlap into a smear at 390px."""
    ledger.add_many(LONG_RUN, paths.db)
    monkeypatch.setattr(widgets, "is_mobile", lambda: True)
    portfolio_data.ledger_state.clear()
    axis = _figure(_monthly(page("ES")))["layout"]["xaxis"]
    assert axis["tickangle"] == -45
    assert axis["dtick"] >= 4  # ~5 labels, not 25
    assert axis["automargin"] is True  # margin b=0 would clip the slant


def test_the_period_chart_is_taller_on_a_phone(page, paths, monkeypatch):
    """Four legend entries wrap to three rows there, plus the label band."""
    ledger.add_many(LONG_RUN, paths.db)
    monkeypatch.setattr(widgets, "is_mobile", lambda: True)
    portfolio_data.ledger_state.clear()
    tall = _figure(_monthly(page("ES")))["layout"]["height"]
    monkeypatch.setattr(widgets, "is_mobile", lambda: False)
    portfolio_data.ledger_state.clear()
    assert tall > _figure(_monthly(page("ES")))["layout"]["height"]


def test_the_desktop_chart_keeps_every_other_month(page, paths, monkeypatch):
    ledger.add_many(LONG_RUN, paths.db)
    portfolio_data.ledger_state.clear()
    axis = _figure(_monthly(page("ES")))["layout"]["xaxis"]
    assert axis["dtick"] == 2
