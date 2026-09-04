"""The Profile page's Preferences tab, through the real script.

The tab was rebuilt from the "Aguait Perfil Refactor" canvas: settings became
label-plus-control rows inside three cards, the currency chips lost six of
their eleven options to a popover, the tax card grew a strip of the rules the
jurisdiction actually applies, and CSV export plus account deletion moved onto
the page. AppTest runs the page exactly as Streamlit does, which is the only
way to cover a page that IS a script.
"""

from __future__ import annotations

import json

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from stocks.portfolio import ledger
from stocks.portfolio.ledger import Transaction
from stocks.web import auth, exports

PAGE = "src/stocks/web/app_pages/profile.py"


def _stylesheet(at) -> str:
    """The block `css.inject` emits for this page."""
    bodies = [str(getattr(el.proto, "body", "")) for el in at.get("html")]
    return next(b for b in bodies if "st-key-prow_" in b)


def _html(at) -> str:
    """The page's markup, without the stylesheet `css.inject` puts alongside it."""
    bodies = (str(getattr(el.proto, "body", "")) for el in at.get("html"))
    return "\n".join(b for b in bodies if "ts-inline-css" not in b)


@pytest.fixture
def paths(tmp_path):
    return auth.UserPaths(
        root=tmp_path,
        watchlist=tmp_path / "watchlist.yaml",
        db=tmp_path / "portfolio.db",
        last_import=tmp_path / "last_import.json",
        prefs=tmp_path / "prefs.json",
        chat=tmp_path / "chat.json",
        bank=tmp_path / "bank.json",
        action=tmp_path / "daily_action.json",
    )


@pytest.fixture
def page(monkeypatch, paths):
    monkeypatch.setattr(auth, "require_login", lambda: paths)
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(auth, "is_logged_in", lambda: True)
    monkeypatch.setattr(auth, "current_email", lambda: "me@example.com")
    st.cache_data.clear()  # the CSV export caches on the ledger's mtime
    return AppTest.from_file(PAGE, default_timeout=60)


def test_the_page_renders_for_an_account_with_nothing_set(page):
    page.run()
    assert not page.exception
    # Identity, the three setting cards and both rail cards.
    body = _html(page)
    assert "ag-ident" in body
    # Interface, Tax residence, Data and account.
    assert body.count("ag-cardtitle") == 3
    assert "ag-prog-fill" in body and "ag-sum" in body


def test_the_watchlist_tab_carries_its_own_count(page, paths):
    paths.watchlist.write_text("watchlist:\n  - ticker: AAPL\n  - ticker: MSFT\n")
    page.run()
    labels = [t.label for t in page.get("tab")]
    assert any("Watchlist :gray-badge[2]" in label for label in labels)


def test_only_the_five_common_currencies_are_chips(page):
    page.run()
    chips = page.get("button_group")[0]
    assert chips.options == ["€ EUR", "$ USD", "£ GBP", "\u20a3 CHF", "kr SEK"]
    # The other six are named under the row rather than hidden entirely.
    assert "NOK · DKK · PLN · CZK · CAD · AUD" in _html(page)


def test_a_currency_from_the_popover_becomes_the_saved_one(page, paths):
    page.run()
    page.get("button_group")[1].set_value("CA$ CAD").run()
    assert not page.exception
    assert json.loads(paths.prefs.read_text())["currency"] == "CAD"
    # ...and it takes over the chip row, so the row still shows what is set.
    assert "CA$ CAD" in page.get("button_group")[0].options
    # The rail summary follows on the same run, not one interaction later.
    assert "<b>CAD</b>" in _html(page)


def test_the_tax_card_states_the_rules_the_jurisdiction_applies(page, paths):
    paths.prefs.write_text(json.dumps({"tax_residence": "UK"}))
    page.run()
    body = _html(page)
    assert "s.104 pool" in body  # UK matches into a s.104 pool, not FIFO
    assert "GBP" in body
    assert "Starts 6/4" in body  # the UK tax year opens on 6 April


def test_spain_reads_none_of_the_bracket_knobs(page, paths):
    paths.prefs.write_text(json.dumps({"tax_residence": "ES"}))
    page.run()
    assert [s.label for s in page.selectbox] == ["Language", "Where you file"]
    assert not page.number_input and not page.toggle


def test_the_us_gets_the_three_inputs_its_brackets_read(page, paths):
    paths.prefs.write_text(json.dumps({"tax_residence": "US"}))
    page.run()
    assert "Filing status" in [s.label for s in page.selectbox]
    assert page.number_input  # other taxable income
    assert page.toggle  # NIIT


def test_the_export_only_appears_once_there_is_a_ledger(page, paths):
    page.run()
    assert not page.get("download_button")  # nothing imported yet

    ledger.add_many(
        [Transaction("2024-02-10", "SAN.MC", "buy", 5, 4.0, "EUR")], paths.db
    )
    st.cache_data.clear()
    page.run()
    assert page.get("download_button")


def test_the_exported_csv_converts_every_row_at_its_own_date(paths, monkeypatch):
    """The conversion is what makes the file the book the user sees."""
    from stocks.data import fx

    monkeypatch.setattr(fx, "prefetch", lambda pairs, quote="EUR": None)
    monkeypatch.setattr(fx, "rate_on", lambda day, base, quote: 0.5)
    ledger.add_many(
        [
            Transaction("2024-01-10", "AAPL", "buy", 10, 100.0, "USD", 1.0),
            Transaction("2024-02-10", "SAN.MC", "buy", 5, 4.0, "EUR"),
        ],
        paths.db,
    )
    rows = exports.ledger_csv(paths.db, "EUR").decode().splitlines()
    assert rows[0].endswith("fx_rate,amount_eur")
    assert rows[1].endswith("0.5,500.0")  # 10 x 100 USD at 0.5
    assert exports.ledger_csv(paths.db.with_name("nothing.db"), "EUR") == b""


def test_deleting_data_asks_before_it_does_anything(page, monkeypatch):
    called = []
    monkeypatch.setattr(auth, "delete_account", lambda p: called.append(p))
    page.run()
    delete = next(b for b in page.button if b.proto.label == "Delete…")
    delete.click().run()
    assert not called  # the button opens the dialog, it does not delete
    assert page.get("dialog")


def test_the_row_dividers_are_not_borders(page):
    """app.py's card tagger stamps .topstocks-card on ANY main-area block whose
    computed border-top is thicker than 0 — a bordered row therefore renders as
    a 16px-radius card of its own, which is what the rounded dividers were. The
    divider has to stay a pseudo-element."""
    page.run()
    sheet = _stylesheet(page)
    assert '[class*="st-key-prow_"]::before' in sheet
    rules = sheet.split('[class*="st-key-prow_"]')
    assert not any(r.split("}")[0].strip().startswith("{") and "border" in r.split("}")[0]
                   for r in rules[1:])


def test_the_tab_strip_says_the_page_saves_itself(page):
    page.run()
    assert "ag-savehint" in _html(page)
    assert "Changes save instantly" in _html(page)


def test_the_folder_chip_shows_the_tail_and_keeps_the_full_path(page, paths):
    """The identifying part of a per-account data dir is its last segment, and
    the whole path is long enough to push Log out onto a second line."""
    page.run()
    body = _html(page)
    assert f'title="{paths.root}"' in body
    assert f">{paths.root.parent.name}/{paths.root.name}<" in body
