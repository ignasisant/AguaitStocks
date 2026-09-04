"""The shipped example statement, and the Import page's offer of it.

A brand-new account has nothing to import, so every ledger-derived surface —
positions, P/L, risk, dividends, fees, tax — is empty until it does. The
example statement is how that account can see those surfaces working without
inventing a ledger: it is a real broker statement in the repo, parsed by the
real parser, validated and committed by the real page, and undone by the
ordinary "clear last import".

What must not break: the file parses clean (a sample that errors is worse than
no sample), it fills every one of those surfaces, and the offer only ever
appears to an account with nothing to lose.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from stocks.portfolio import last_import, platforms
from stocks.portfolio.ledger import Transaction, add_many, all_transactions
from stocks.portfolio.validate import validate
from stocks.web import auth

PAGE = "src/stocks/web/app_pages/import_transactions.py"
ASSETS = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web" / "assets"
REVOLUT = platforms.by_key("revolut")


# ------------------------------------------------------------------- the file


def test_exactly_one_platform_ships_a_sample():
    """One sample, on the platform whose columns the parser's tests pin.

    A sample has to track a real broker's export layout; every extra one is
    another layout to keep true, and a sample that has rotted teaches the
    reader that the importer is broken.
    """
    with_sample = [p for p in platforms.PLATFORMS if p.sample]
    assert [p.key for p in with_sample] == ["revolut"]
    assert (ASSETS / REVOLUT.sample).is_file()


@pytest.fixture
def parsed():
    return REVOLUT.parse(REVOLUT.sample, (ASSETS / REVOLUT.sample).read_bytes())


def test_the_sample_parses_and_validates_without_a_single_rejection(parsed):
    checked = validate(parsed, [], known=set(), lookup=lambda t: True)
    assert len(checked.importable) == len(parsed.transactions)
    assert "0 rejected" in checked.summary
    assert "0 with warnings" in checked.summary


def test_the_sample_fills_every_surface_a_new_account_finds_empty(parsed):
    """One statement, and the whole ledger half of the app has content."""
    kinds = Counter(t.action for t in parsed.transactions)
    assert kinds["buy"] and kinds["sell"] and kinds["dividend"]  # positions,
    # realised gains + the tax report, and the dividends tab
    assert any(t.fee for t in parsed.transactions)  # the fees tab

    held: Counter[str] = Counter()
    for t in parsed.transactions:
        if t.action == "buy":
            held[t.ticker] += t.quantity
        elif t.action == "sell":
            held[t.ticker] -= t.quantity
    assert sum(1 for q in held.values() if q > 0) >= 4  # a basket to weigh
    assert any(q == 0 for q in held.values())  # a closed position to realise
    assert min(held.values()) >= 0, "an oversell would be rejected on import"

    years = {t.date[:4] for t in parsed.transactions if t.action == "sell"}
    assert len(years) > 1  # more than one tax year on the Realized tab
    span = {t.date[:4] for t in parsed.transactions}
    assert len(span) >= 3  # enough history for the return chart to have shape


def test_the_sample_keeps_a_skipped_row_to_show_what_is_not_imported(parsed):
    """The preview's honesty is a feature, so the sample demonstrates it."""
    assert parsed.skipped
    assert all(s.get("reason") for s in parsed.skipped)


def test_the_sample_is_attributed_to_the_broker_it_came_from(parsed):
    # fees.broker_of reads the note's first word; an unstamped batch lands
    # under whatever its notes happened to start with.
    assert {t.note.split()[0] for t in parsed.transactions} == {"revolut"}


# --------------------------------------------------------------- the page


@pytest.fixture
def paths(tmp_path):
    p = auth.paths_for("newbie@example.com", users_dir=tmp_path)
    p.root.mkdir(parents=True, exist_ok=True)
    p.prefs.write_text(json.dumps(dict(auth.DEFAULT_PREFS) | {"language": "en"}))
    return p


@pytest.fixture
def page(monkeypatch, paths):
    monkeypatch.setattr(auth, "require_login", lambda: paths)
    monkeypatch.setattr(auth, "user_paths", lambda: paths)
    monkeypatch.setattr(auth, "db_path", lambda: paths.db)
    monkeypatch.setattr(auth, "watchlist_path", lambda: paths.watchlist)
    return AppTest.from_file(PAGE, default_timeout=120)


def _offer(at):
    return [b for b in at.button if b.label == "Load an example statement"]


def test_an_account_with_nothing_imported_is_offered_the_sample(page):
    page.run()
    assert not page.exception
    assert _offer(page)


def test_an_account_with_a_ledger_is_not_offered_someone_elses_trades(page, paths):
    add_many([Transaction("2026-01-05", "AAPL", "buy", 1, 150.0, "USD", 1.0)],
             paths.db)
    page.run()
    assert not page.exception
    assert not _offer(page)


def test_loading_the_sample_previews_it_as_a_real_upload_would(page):
    page.run()
    _offer(page)[0].click().run()

    assert not page.exception
    # Staged for the runs between the click and the commit, exactly as
    # st.file_uploader's own value survives them.
    key, name, data = page.session_state["import_sample"]
    assert (key, name) == ("revolut", REVOLUT.sample)
    assert data
    # The preview is the real one: the parser's own summary, not a canned line.
    assert any("importable" in str(s.value) for s in page.subheader)
    assert [b for b in page.button if b.label == "Commit to ledger"]


def test_committing_the_sample_fills_the_ledger_and_stays_undoable(page, paths):
    """The whole point: it goes in through the ordinary commit, and comes back
    out through the ordinary undo — no separate demo mode to get stuck in."""
    page.run()
    _offer(page)[0].click().run()
    commit = [b for b in page.button if b.label == "Commit to ledger"][0]
    commit.click().run()

    assert not page.exception
    rows = all_transactions(paths.db)
    assert len(rows) == 25
    # Attributed to the broker the statement came from, so Fees and Custody
    # read it as one book rather than as unattributed rows.
    assert {t.note.split()[0] for t in rows} == {"revolut"}

    record = last_import.load(paths.last_import)
    assert record is not None
    assert record.platform == "revolut"
    assert len(record.tx_ids) == 25  # the undo knows exactly what to remove
    # Staging cleared, or the next rerun would offer the same rows again.
    assert "import_sample" not in page.session_state


def test_the_offer_is_gone_once_the_sample_has_been_imported(page):
    page.run()
    _offer(page)[0].click().run()
    [b for b in page.button if b.label == "Commit to ledger"][0].click().run()
    page.run()
    assert not _offer(page)
