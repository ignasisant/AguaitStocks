"""The demo book (stocks.portfolio.demo) and the two promises it makes.

An account that has not imported anything finds every ledger-derived surface
blank, and not everyone wants to hand over a real statement to find out
whether the app is worth one. The demo book is the answer to that, and it is
only safe while both of its promises hold:

* it is **recognisable as fake** — every row carries the `demo` origin the
  Fees and Custody views read, and the page it fills says so;
* it is **gone the moment anything real arrives** — the first import wipes it,
  from the Import page and from the assistant alike, so an invented cost basis
  can never end up mixed into a real one and reported as tax.

The rest of these tests keep the book worth showing: prices near the real ones
(a +900% demo teaches nothing about the app), and enough shape in it that
every tab has something to draw.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from streamlit.testing.v1 import AppTest

from stocks.portfolio import demo, platforms
from stocks.portfolio.fees import broker_of
from stocks.portfolio.ledger import Transaction, add_many, all_transactions
from stocks.web import auth

IMPORT_PAGE = "src/stocks/web/app_pages/import_transactions.py"
PORTFOLIO_PAGE = "src/stocks/web/app_pages/portfolio.py"


# ---------------------------------------------------------------- the book


def test_it_holds_the_names_it_says_it_holds():
    assert set(demo.TICKERS) == {t.ticker for t in demo.transactions()}


def test_every_row_is_stamped_as_demo_and_reads_back_as_demo():
    """The note's first word is the whole marking scheme — `fees.broker_of`
    reads it, the SQL wipe matches on it, and the Fees view labels rows by it.
    A row that lost it would survive the first real import unnoticed."""
    txs = demo.transactions()
    assert {broker_of(t) for t in txs} == {demo.BROKER}
    assert all(demo.is_demo(t) for t in txs)
    # And it must not be a real broker's key, or demo rows would be wiped as
    # part of that broker's book (or vice versa).
    assert demo.BROKER not in {p.key for p in platforms.PLATFORMS}
    assert demo.BROKER not in platforms.BROKER_NAMES


def test_a_real_row_is_never_mistaken_for_a_demo_one():
    assert not demo.is_demo(Transaction("2026-01-05", "AAPL", "buy", 1, 150.0))
    # Prefix match, not substring: only the first *word*.
    assert not demo.is_demo(
        Transaction("2026-01-05", "AAPL", "buy", 1, 150.0, note="demonstration acct")
    )


def test_it_fills_every_surface_the_portfolio_page_has_a_tab_for():
    txs = demo.transactions()
    kinds = Counter(t.action for t in txs)
    assert kinds["buy"] and kinds["sell"] and kinds["dividend"] and kinds["fee"]

    held: Counter[str] = Counter()
    for t in txs:
        if t.action == "buy":
            held[t.ticker] += t.quantity
        elif t.action == "sell":
            held[t.ticker] -= t.quantity
    assert min(held.values()) > 0, "an oversell would break the position replay"
    assert sum(1 for q in held.values() if q > 0) >= 5  # a basket to weigh
    assert len({t.date[:4] for t in txs}) >= 3  # shape in the return chart


def test_the_numbers_are_plausible_enough_to_learn_from():
    """Prices are roughly the real closes on those dates. This does not pin
    them to a source — it catches the fabrication that would make the demo's
    P/L nonsense: a zero price, a decimal slip, a trade dated in the future."""
    for t in demo.transactions():
        assert t.currency == "USD"
        assert t.date <= "2026-06-30", f"{t.ticker} {t.date} must be in the past"
        if t.action in ("buy", "sell"):
            assert 50 <= t.price <= 2000, f"{t.ticker} at {t.price}"
            assert t.quantity > 0
        if t.action == "dividend":
            assert 0 < t.price < 100  # a total amount, not a per-share price


# ------------------------------------------------------------ seed and wipe


def test_seeding_an_empty_ledger_writes_the_whole_book(tmp_path):
    db = tmp_path / "portfolio.db"
    ids = demo.seed(db)
    assert len(ids) == len(demo.transactions())
    assert demo.active(db)


def test_seeding_twice_does_not_stack_a_second_copy(tmp_path):
    """The offer only shows on the empty path, but a double click and a stale
    rerun both reach here — and a doubled book is a doubled cost basis."""
    db = tmp_path / "portfolio.db"
    demo.seed(db)
    assert demo.seed(db) == []
    assert len(all_transactions(db)) == len(demo.transactions())


def test_seeding_never_touches_a_ledger_that_holds_anything_real(tmp_path):
    db = tmp_path / "portfolio.db"
    add_many([Transaction("2026-01-05", "AAPL", "buy", 1, 150.0, note="revolut")], db)
    assert demo.seed(db) == []
    assert len(all_transactions(db)) == 1


def test_clearing_removes_the_demo_rows_and_only_those(tmp_path):
    db = tmp_path / "portfolio.db"
    demo.seed(db)
    add_many([Transaction("2026-01-05", "AAPL", "buy", 1, 150.0, note="revolut Apple")],
             db)
    assert demo.clear(db) == len(demo.transactions())
    assert not demo.active(db)
    assert [t.note for t in all_transactions(db)] == ["revolut Apple"]


def test_clearing_an_untouched_ledger_is_a_no_op(tmp_path):
    db = tmp_path / "portfolio.db"
    add_many([Transaction("2026-01-05", "AAPL", "buy", 1, 150.0, note="revolut")], db)
    assert demo.clear(db) == 0
    assert len(all_transactions(db)) == 1


def test_without_drops_the_demo_rows_from_a_validation_baseline():
    """The incoming batch is checked against the ledger *as it will be*: demo
    rows are about to go, so duplicates against them are not duplicates, and
    demo buys must not be what makes a real sell pass the oversell replay."""
    real = Transaction("2026-01-05", "AAPL", "buy", 1, 150.0, note="revolut")
    assert demo.without([*demo.transactions(), real]) == [real]


# -------------------------------------------------------------- the pages


@pytest.fixture
def paths(tmp_path):
    p = auth.paths_for("newbie@example.com", users_dir=tmp_path)
    p.root.mkdir(parents=True, exist_ok=True)
    p.prefs.write_text(json.dumps(dict(auth.DEFAULT_PREFS) | {"language": "en"}))
    return p


@pytest.fixture
def import_page(monkeypatch, paths):
    monkeypatch.setattr(auth, "require_login", lambda: paths)
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(auth, "db_path", lambda: paths.db)
    monkeypatch.setattr(auth, "watchlist_path", lambda: paths.watchlist)
    return AppTest.from_file(IMPORT_PAGE, default_timeout=120)


def test_the_portfolio_empty_card_offers_the_demo_book_under_its_cta():
    """Source-level: running the page for real would fetch prices for whatever
    the click just seeded. What matters here is that the offer is on the empty
    path and that it is the card's `extra`, not a second way out."""
    src = open(PORTFOLIO_PAGE).read()
    assert "extra=_demo_offer" in src
    assert "demo.seed(auth.db_path())" in src
    assert 'tr("portfolio.demo_banner")' in src  # and the page says whose


def test_a_demo_only_ledger_is_still_offered_the_example_statement(
    import_page, paths
):
    """A demo book is nothing to lose — the sample offer is gated on having no
    real rows, not on the table being empty."""
    demo.seed(paths.db)
    import_page.run()
    assert not import_page.exception
    assert [b for b in import_page.button if b.label == "Load an example statement"]


def test_the_first_real_import_wipes_the_demo_book(import_page, paths):
    """The promise the whole thing rests on: what lands in the ledger after an
    import is the imported statement and nothing invented."""
    demo.seed(paths.db)
    import_page.run()
    offer = [b for b in import_page.button if b.label == "Load an example statement"]
    offer[0].click().run()
    [b for b in import_page.button if b.label == "Commit to ledger"][0].click().run()

    assert not import_page.exception
    rows = all_transactions(paths.db)
    assert rows and not any(demo.is_demo(t) for t in rows)
    assert {t.note.split()[0] for t in rows} == {"revolut"}
