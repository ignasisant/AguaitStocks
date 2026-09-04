"""The empty-state card (stocks.web.empty).

What must not break: the card always says what is missing and why, it offers
at most one way out, and a call site that promises a way out actually labels
it. The last one is a source-level check because an unlabelled `st.page_link`
renders as a clickable nothing — a dead end that looks like a bug, which is
exactly what this module exists to remove.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from stocks import obs
from stocks.web import auth

WEB = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"

# st.page_link needs a real multipage run (it resolves the page's URL from the
# app's page list), which a bare AppTest script has no way to provide. Patched
# from the test rather than from inside the script: the AppTest body runs in
# this process against these same module objects, so monkeypatch reaches it
# and — unlike an in-script assignment — puts streamlit back afterwards.
SCRIPT = "import streamlit as st\nfrom stocks.web import empty\n"


@pytest.fixture
def links(monkeypatch):
    """Every st.page_link the card draws, in order."""
    recorded: list[tuple] = []
    monkeypatch.setattr(st, "page_link", lambda page, **kw: recorded.append((page, kw)))
    return recorded


@pytest.fixture
def events(monkeypatch):
    """Every obs.event the card emits, in order."""
    recorded: list[tuple] = []
    monkeypatch.setattr(obs, "event", lambda name, **kw: recorded.append((name, kw)))
    return recorded


def _app(body: str) -> AppTest:
    return AppTest.from_string(SCRIPT + body, default_timeout=30)


def _run(body: str) -> AppTest:
    at = _app(body).run()
    assert not at.exception, at.exception
    return at


def test_card_states_what_is_missing_and_why():
    at = _run('empty.state("No transactions yet", "It derives from a ledger.")')
    # The glyph names the missing thing; heading level is what sizes it.
    assert at.markdown[0].value == "### :gray[:material/inbox:]"
    assert at.markdown[1].value == "**No transactions yet**"
    assert at.caption[0].value == "It derives from a ledger."


def test_a_card_with_no_next_step_offers_no_button(links):
    at = _run('empty.state("No dividends recorded", icon="payments")')
    assert at.markdown[0].value == "### :gray[:material/payments:]"
    assert not at.button
    assert links == []
    assert not at.caption  # body is optional


def test_page_renders_one_labelled_link_and_no_button(links):
    at = _run(
        'empty.state("Nothing to screen", "Add tickers.", icon="filter_alt",'
        ' page="app_pages/profile.py", label="Add tickers", cta_icon="add")'
    )
    assert links == [
        ("app_pages/profile.py", {"label": "Add tickers", "icon": ":material/add:"})
    ]
    assert not at.button  # a link, not a widget — no key spent, rerun-proof


def test_on_click_renders_a_button_when_the_next_step_is_not_a_page(links):
    at = _run(
        'empty.state("No key yet", on_click=lambda: None, label="Open assistant",'
        ' key="empty_ai", cta_icon="auto_awesome")'
    )
    assert [(b.label, b.key) for b in at.button] == [("Open assistant", "empty_ai")]
    assert links == []


def test_a_preview_shows_the_shape_instead_of_an_icon():
    at = _run(
        'empty.state("No transactions yet", "Import a statement.",'
        ' preview="chart", preview_kw={"height": 240})'
    )
    shape = at.get("html")[0].body
    # Faded and still: a sheen would read as a load about to resolve, and
    # nothing arrives here without the reader importing something.
    assert shape.startswith('<div class="topstocks-gh">')
    assert 'class="topstocks-sk sk-chart" style="height:240px"' in shape
    # The icon would only compete with the silhouette.
    assert not [m for m in at.markdown if ":material/" in m.value]
    assert "**No transactions yet**" in [m.value for m in at.markdown]


def test_a_page_wins_over_on_click_so_a_card_has_one_way_out(links):
    at = _run(
        'empty.state("Both", page="app_pages/profile.py", label="Go",'
        ' on_click=lambda: None, key="both")'
    )
    assert len(links) == 1
    assert not at.button


@pytest.mark.parametrize("page", sorted(p.name for p in (WEB / "app_pages").glob("*.py")))
def test_every_call_site_labels_the_way_out_it_offers(page):
    """A `page=`/`on_click=` with no `label=` is a clickable blank."""
    src = (WEB / "app_pages" / page).read_text()
    for call in re.findall(r"empty\.state\((?:[^()]|\([^()]*\))*\)", src):
        if "page=" in call or "on_click=" in call:
            assert "label=" in call, f"{page}: unlabelled CTA in {call!r}"
        if "on_click=" in call:
            assert "key=" in call, f"{page}: on_click needs a widget key in {call!r}"


@pytest.mark.parametrize("page", sorted(p.name for p in (WEB / "app_pages").glob("*.py")))
def test_every_call_site_reports_which_section_came_up_empty(page):
    """Without a slug the card is invisible to the logs, and which sections a
    real account finds empty is the only evidence for keeping or cutting one."""
    src = (WEB / "app_pages" / page).read_text()
    slugs = []
    for call in re.findall(r"empty\.state\((?:[^()]|\([^()]*\))*\)", src):
        found = re.search(r'event="([a-z_]+\.[a-z_]+)"', call)
        assert found, f"{page}: empty.state with no event slug in {call!r}"
        slugs.append(found.group(1))
    assert len(slugs) == len(set(slugs)), f"{page}: duplicate event slugs {slugs}"


def test_the_card_reports_its_section_once_per_session(events, links):
    """Streamlit reruns the script on every interaction; an unguarded event
    would log one blank section dozens of times per visit."""
    at = _app(
        'empty.state("No transactions yet", event="portfolio.ledger",'
        ' page="app_pages/import_transactions.py", label="Import")\n'
        'empty.state("No dividends recorded", event="portfolio.dividends")\n'
    )
    for _ in range(3):  # three runs of the same page, one session
        at.run()
        assert not at.exception, at.exception

    assert events == [
        ("empty_state", {"where": "portfolio.ledger", "cta": True}),
        ("empty_state", {"where": "portfolio.dividends", "cta": False}),
    ]


def test_a_card_with_no_slug_reports_nothing(events):
    _run('empty.state("Nothing here")')
    assert events == []


# ------------------------------------------------------- the pages themselves

# What a brand-new account actually lands on: a watchlist it was seeded with
# and a ledger it has not imported anything into. Each of these pages derives
# its whole body from one of the two, so on a first visit each has genuinely
# nothing to render — and each must say so with a way forward rather than
# stopping on a bare warning.
FIRST_VISIT = [
    ("portfolio.py", True, "No transactions yet", "app_pages/import_transactions.py"),
    ("earnings.py", False, "No stocks to track", "app_pages/profile.py"),
    ("screener.py", False, "Nothing to screen", "app_pages/profile.py"),
]


@pytest.fixture
def account(monkeypatch, tmp_path):
    """A signed-in account with an empty ledger, English, no tour."""
    paths = auth.paths_for("newbie@example.com", users_dir=tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    prefs = dict(auth.DEFAULT_PREFS) | {"language": "en", "tour_done": True}
    paths.prefs.write_text(json.dumps(prefs))
    for name in ("require_login", "user_paths", "resolve_user"):
        monkeypatch.setattr(auth, name, lambda: paths)
    monkeypatch.setattr(auth, "db_path", lambda: paths.db)
    monkeypatch.setattr(auth, "watchlist_path", lambda: paths.watchlist)
    monkeypatch.setattr(auth, "current_email", lambda: "newbie@example.com")
    monkeypatch.setattr(auth, "is_logged_in", lambda: True)
    return paths


@pytest.mark.parametrize(("page", "seed", "title", "target"), FIRST_VISIT)
def test_a_first_visit_gets_a_card_with_a_way_out(
    account, monkeypatch, page, seed, title, target
):
    """The card renders and names the page that fills the section.

    `st.page_link` resolves its href from the app's page list, which only
    `st.navigation` builds — running one page module on its own has no list,
    so the recorder stands in for it here. The link itself is verified against
    the real navigation by driving app.py; this test covers the wiring.
    """
    account.watchlist.write_text(auth.STARTER_WATCHLIST if seed else "watchlist: []\n")
    links: list[tuple] = []
    monkeypatch.setattr(st, "page_link", lambda p, **kw: links.append((p, kw)))

    at = AppTest.from_file(str(WEB / "app_pages" / page), default_timeout=120).run()

    assert not at.exception, at.exception
    rendered = [m.value for m in at.markdown]
    assert f"**{title}**" in rendered
    assert at.caption, "a card with no body does not say why it is blank"
    assert [p for p, _ in links] == [target]
