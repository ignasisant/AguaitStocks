"""Focus-driven watchlist examples (auth.focus_suggestions).

The investor profile is answered *after* the account is seeded, so it cannot
drive the initial watchlist. What it can do is offer the areas the user said
they follow as examples they do not have yet — which is what this is: a
shortcut for typing symbols, keyed on stated interest and never on risk
tolerance, and additive so an edited watchlist keeps every row it has.
"""

from __future__ import annotations

import pytest

from stocks.config import load_watchlist
from stocks.web import auth


@pytest.fixture
def watchlist(tmp_path):
    p = tmp_path / "watchlist.yaml"
    p.write_text(auth.STARTER_WATCHLIST)
    return p


def test_no_declared_focus_means_no_offer(watchlist):
    """An account that skipped the profile is left alone."""
    assert auth.focus_suggestions({"focus": []}, watchlist) == []
    assert auth.focus_suggestions({}, watchlist) == []


def test_risk_alone_never_produces_an_offer(watchlist):
    """Keyed on interest, not on risk: a list assembled from someone's risk
    tolerance reads as a recommendation however it is worded."""
    assert auth.focus_suggestions({"risk": "very_aggressive"}, watchlist) == []


def test_each_area_offers_its_examples_in_registry_order(watchlist):
    got = auth.focus_suggestions({"focus": ["em", "crypto"]}, watchlist)
    assert [e["ticker"] for e in got] == [
        "BABA", "INFY", "NU", "ETH-EUR", "SOL-EUR", "COIN",
    ]
    assert all(e["name"] and e["tags"] for e in got)


def test_tickers_already_on_the_watchlist_are_not_offered_again(watchlist):
    watchlist.write_text(auth.STARTER_WATCHLIST + "  - ticker: GOOGL\n")
    got = auth.focus_suggestions({"focus": ["tech"]}, watchlist)
    assert "GOOGL" not in [e["ticker"] for e in got]
    assert [e["ticker"] for e in got] == ["AMD", "NOW"]


def test_the_offer_empties_out_once_it_has_been_taken_up(watchlist):
    focus = {"focus": ["dividends_value"]}
    first = auth.focus_suggestions(focus, watchlist)
    assert first
    kept = [
        {"ticker": h.ticker, "name": h.name, "favorite": h.favorite,
         "shares": h.shares or None, "cost": h.cost, "tags": h.tags}
        for h in load_watchlist(watchlist)
    ]
    auth.save_watchlist_entries(kept + first, watchlist)
    assert auth.focus_suggestions(focus, watchlist) == []


def test_adding_them_keeps_every_row_the_watchlist_already_had(watchlist):
    before = load_watchlist(watchlist)
    kept = [
        {"ticker": h.ticker, "name": h.name, "favorite": h.favorite,
         "shares": h.shares or None, "cost": h.cost, "tags": h.tags}
        for h in before
    ]
    suggested = auth.focus_suggestions({"focus": ["tech"]}, watchlist)
    auth.save_watchlist_entries(kept + suggested, watchlist)

    after = {h.ticker: h for h in load_watchlist(watchlist)}
    for h in before:
        assert h.ticker in after
        assert after[h.ticker].favorite == h.favorite  # favorites survive
        assert after[h.ticker].tags == h.tags


def test_the_examples_reuse_the_seeds_own_tag_labels(watchlist):
    """An appended row must land in an existing dashboard group, not start a
    near-duplicate one beside it."""
    seed_tags = {t for h in load_watchlist(watchlist) for t in h.tags}
    tech = auth.focus_suggestions({"focus": ["tech"]}, watchlist)
    assert {t for e in tech for t in e["tags"]} <= seed_tags


@pytest.mark.parametrize("area", sorted(auth.FOCUS_EXAMPLES))
def test_every_focus_area_the_profile_offers_has_examples(area, watchlist):
    """A focus the form can select but the registry does not know is a silent
    dead end — the user declares an interest and nothing ever happens."""
    assert area in auth.PROFILE_FOCUS
    rows = auth.FOCUS_EXAMPLES[area]
    assert rows
    assert all(len(r) == 3 and all(r) for r in rows)


def test_the_registry_covers_every_area_the_form_can_select():
    assert set(auth.PROFILE_FOCUS) == set(auth.FOCUS_EXAMPLES)


def test_no_example_is_offered_under_two_areas(watchlist):
    """Ticker in two areas would be added twice, and save_watchlist_entries
    would silently drop the second — better not to promise it."""
    everything = auth.focus_suggestions({"focus": list(auth.PROFILE_FOCUS)}, watchlist)
    tickers = [e["ticker"] for e in everything]
    assert len(tickers) == len(set(tickers))
