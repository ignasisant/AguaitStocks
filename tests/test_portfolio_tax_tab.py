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
from stocks.web import auth, portfolio_data

PAGE = "src/stocks/web/app_pages/portfolio.py"

# One long-term winner and one short-term loser, both in USD, so the US split
# has something in each bucket and Spain still sees one net figure.
TXS = [
    Transaction("2023-01-10", "MSFT", "buy", 10, 200.0, "USD", 1.0),
    Transaction("2026-02-02", "MSFT", "sell", 10, 300.0, "USD", 1.0),
    Transaction("2026-01-05", "AAPL", "buy", 10, 150.0, "USD", 1.0),
    Transaction("2026-03-05", "AAPL", "sell", 10, 120.0, "USD", 1.0),
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
