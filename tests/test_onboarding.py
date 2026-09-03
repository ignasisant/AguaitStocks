"""The guided tour (stocks.web.onboarding) — registry, gating and open/close.

Three things here are worth a test and the rest is Streamlit chrome:

* the **registry** has to stay consistent with the rest of the repo — every
  step's page module must exist, every release must point at real steps, and
  every step and release item must have copy in every shipped language. Those
  are exactly the mistakes a hand-edited registry (or the update-tutorial
  skill) makes, and they surface as a raw `tour.foo_body` key on screen.
* **`setup_state`** is shared with the Home setup card, so a change in one
  place must not silently move the other.
* **auto-open** decides what a returning account sees, which involves the
  release list, the persisted stamp and the guest rules — easy to get wrong
  and invisible until someone signs in for the first time.

The open/close cases run through AppTest because they need a real session
state; the rest are pure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from stocks.web import auth, i18n, onboarding

LOCALES = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web" / "locales"
SRC = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"


def _catalog(lang: str) -> dict[str, str]:
    return json.loads((LOCALES / lang / "tour.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ registry
def test_step_ids_are_unique():
    ids = [s.id for s in onboarding.STEPS]
    assert len(ids) == len(set(ids))


def test_every_step_page_exists():
    """A typo'd module path only fails when the user clicks "take me there".

    Checked over `visible_steps()`, not `STEPS`: a step whose feature is not
    built into this deploy is filtered out before anyone can click it, and its
    page module is legitimately absent from the tree.
    """
    missing = [
        s.id
        for s in onboarding.visible_steps()
        if s.page and not (SRC / s.page).is_file()
    ]
    assert not missing, f"steps pointing at no page module: {missing}"


def test_home_page_maps_to_the_root_url_path():
    """st.navigation serves the default page at "", so the tour must compare
    against "" or it would switch to Home from Home, forever."""
    assert onboarding._url_path("app_pages/home.py") == ""
    assert onboarding._url_path("app_pages/portfolio.py") == "portfolio"


def test_releases_reference_real_steps():
    unknown = [
        (r.version, sid)
        for r in onboarding.RELEASES
        for sid in r.steps
        if onboarding.by_id(sid) is None
    ]
    assert not unknown, f"release entries naming no step: {unknown}"


def test_current_version_is_the_last_release():
    assert onboarding.CURRENT_VERSION == onboarding.RELEASES[-1].version


@pytest.mark.parametrize("lang", sorted(i18n.LANGUAGES))
def test_every_step_has_copy(lang):
    cat = _catalog(lang)
    missing = [
        key
        for s in onboarding.STEPS
        for key in (f"tour.{s.id}_title", f"tour.{s.id}_body")
        if key not in cat
    ]
    assert not missing, f"{lang}: steps with no copy: {missing}"


@pytest.mark.parametrize("lang", sorted(i18n.LANGUAGES))
def test_every_release_item_has_copy(lang):
    cat = _catalog(lang)
    missing = [
        key for r in onboarding.RELEASES for key in r.items if key not in cat
    ]
    assert not missing, f"{lang}: release items with no copy: {missing}"


def test_catalog_has_no_copy_for_steps_that_are_gone():
    """Orphaned copy is how a renamed step goes unnoticed: the old block stays
    translated, the new one falls back to the raw key."""
    ids = {s.id for s in onboarding.STEPS}
    orphans = sorted(
        key
        for key in _catalog(i18n.DEFAULT_LANG)
        if key.endswith("_body")
        and key.removeprefix("tour.").removesuffix("_body") not in ids
    )
    assert not orphans, f"copy for steps no longer in the registry: {orphans}"


# ---------------------------------------------------------------- setup_state
def test_setup_state_reads_the_four_capabilities(monkeypatch):
    monkeypatch.setattr(auth, "is_logged_in", lambda: True)
    monkeypatch.setattr(onboarding, "_has_ledger", lambda prefs: True)
    state = onboarding.setup_state(
        {"anthropic_key_enc": "…", "telegram_chat_id": 42}
    )
    assert state == {"login": True, "import": True, "ai": True, "telegram": True}


def test_setup_state_for_a_guest_has_nothing_switched_on(monkeypatch):
    monkeypatch.setattr(auth, "is_logged_in", lambda: False)
    monkeypatch.setattr(onboarding, "_has_ledger", lambda prefs: True)
    state = onboarding.setup_state({"telegram_chat_id": 42})
    # The ledger check is skipped for a guest on purpose — the guest data dir
    # is shared, so its starter ledger is nobody's import.
    assert state["login"] is False and state["import"] is False


def test_setup_state_ignores_the_keyless_free_chain(monkeypatch):
    monkeypatch.setattr(auth, "is_logged_in", lambda: True)
    monkeypatch.setattr(onboarding, "_has_ledger", lambda prefs: False)
    assert onboarding.setup_state({})["ai"] is False


def test_unreadable_ledger_reads_as_nothing_imported(monkeypatch):
    def _boom():
        raise OSError("no such database")

    monkeypatch.setattr(auth, "db_path", _boom)
    assert onboarding._has_ledger({}) is False


# ------------------------------------------------------------------- gating
def test_bank_step_is_hidden_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(onboarding, "_bank_available", lambda: False)
    assert "bank" not in {s.id for s in onboarding.visible_steps()}


def test_bank_step_shows_when_the_feature_is_on(monkeypatch):
    monkeypatch.setattr(onboarding, "_bank_available", lambda: True)
    assert "bank" in {s.id for s in onboarding.visible_steps()}


# ----------------------------------------------------------------- releases
def test_unseen_releases_are_everything_for_a_new_account():
    assert onboarding.unseen_releases({}) == onboarding.RELEASES


def test_unseen_releases_are_nothing_once_stamped_current():
    prefs = {onboarding.PREF_SEEN_VERSION: onboarding.CURRENT_VERSION}
    assert onboarding.unseen_releases(prefs) == ()


def test_unseen_releases_are_the_tail_after_the_stamp(monkeypatch):
    releases = (
        onboarding.Release(version="1.0", date="2026-01", items=()),
        onboarding.Release(version="1.1", date="2026-02", items=()),
        onboarding.Release(version="1.2", date="2026-03", items=()),
    )
    monkeypatch.setattr(onboarding, "RELEASES", releases)
    got = onboarding.unseen_releases({onboarding.PREF_SEEN_VERSION: "1.0"})
    assert [r.version for r in got] == ["1.1", "1.2"]


def test_an_unknown_stamp_shows_everything(monkeypatch):
    """A downgrade or a hand-edited prefs.json must not crash the app."""
    prefs = {onboarding.PREF_SEEN_VERSION: "not-a-version"}
    assert onboarding.unseen_releases(prefs) == onboarding.RELEASES


# ----------------------------------------------------------------- auto-open
def _flag(at: AppTest, key: str) -> bool:
    """A session flag, defaulting to False. AppTest's session state proxy has
    no `.get()` — it would read "get" as a widget key."""
    return bool(at.session_state[key]) if key in at.session_state else False


def _button(at: AppTest, key: str):
    """The button with this key, or None. `at.button(key=…)` raises."""
    try:
        return at.button(key=key)
    except KeyError:
        return None


def _script() -> None:
    """What app.py does with the tour, and nothing else.

    Run through AppTest so the module gets a real session state. Defined at
    module level because AppTest re-executes the function's *source* as a
    script — a nested function's closure would not come with it. The stubs it
    needs are monkeypatched onto the modules themselves by `app` below.
    """
    import streamlit as st

    from stocks.web import onboarding as _onb

    class _Page:  # stand-in for the StreamlitPage app.py passes in
        url_path = "portfolio"
        title = "Portfolio"

    st.session_state["claimed"] = _onb.maybe_open()
    _onb.consume_goto(_Page())
    _onb.render(_Page())


@pytest.fixture
def app(monkeypatch):
    """Factory for an AppTest over the tour, backed by a prefs dict.

    Everything the tour persists lands in the dict the test passes in, so
    "did it stamp the version" is a plain assertion. The bank feature and the
    ledger are off unless a test says otherwise.
    """

    def make(prefs: dict, *, logged_in: bool = True) -> AppTest:
        monkeypatch.setattr(auth, "is_logged_in", lambda: logged_in)
        monkeypatch.setattr(auth, "load_prefs", lambda path=None: dict(prefs))
        monkeypatch.setattr(
            auth, "save_prefs", lambda p, path=None: prefs.update(p)
        )
        monkeypatch.setattr(onboarding, "_bank_available", lambda: False)
        monkeypatch.setattr(onboarding, "_has_ledger", lambda p: False)
        return AppTest.from_function(_script, default_timeout=15)

    return make


def test_a_new_account_gets_the_tour(app):
    at = app({}).run()
    assert at.session_state["claimed"] is True
    assert at.session_state[onboarding._MODE] == "tour"
    # The modal renders the first step's title, so the copy is really wired.
    assert any(
        i18n.translate("tour.welcome_title", "en") in m.value for m in at.markdown
    )


def test_a_finished_account_gets_whats_new_after_a_release(app):
    at = app({onboarding.PREF_DONE: True}).run()
    assert at.session_state[onboarding._MODE] == "news"


def test_a_finished_and_up_to_date_account_is_left_alone(app):
    prefs = {
        onboarding.PREF_DONE: True,
        onboarding.PREF_SEEN_VERSION: onboarding.CURRENT_VERSION,
    }
    at = app(prefs).run()
    assert at.session_state["claimed"] is False
    assert not _flag(at, onboarding._OPEN)


def test_a_guest_is_never_auto_opened(app):
    at = app({}, logged_in=False).run()
    assert at.session_state["claimed"] is False


def test_a_guest_progress_is_never_written_to_the_shared_dir(app):
    """The guest data dir belongs to every anonymous visitor at once."""
    prefs: dict = {}
    at = app(prefs, logged_in=False)
    at.query_params["tour"] = "1"
    at.run()
    at.button(key="tour_skip").click().run()
    assert prefs == {}
    assert not _flag(at, onboarding._OPEN)


def test_finishing_the_tour_stamps_prefs_and_stops_the_nagging(app):
    prefs: dict = {}
    at = app(prefs).run()
    while _button(at, "tour_next"):  # walk to the last step
        at.button(key="tour_next").click().run()
    at.button(key="tour_finish").click().run()
    assert prefs[onboarding.PREF_DONE] is True
    assert prefs[onboarding.PREF_SEEN_VERSION] == onboarding.CURRENT_VERSION
    assert not _flag(at, onboarding._OPEN)


def test_skipping_ends_the_tour_without_walking_it(app):
    prefs: dict = {}
    at = app(prefs).run()
    at.button(key="tour_skip").click().run()
    assert prefs[onboarding.PREF_DONE] is True
    assert not _flag(at, onboarding._RESUME)


def test_take_me_there_parks_the_tour_and_seeds_the_target(app):
    """The modal cannot survive the navigation, so the button hands the step
    to the next run and leaves the resume strip behind.

    Driven from the assistant step because its target is the chat panel rather
    than a page — st.switch_page needs a real st.navigation, which a test
    harness of one script does not have.
    """
    at = app({onboarding.PREF_DONE: True})
    at.query_params["tour"] = "assistant"
    at.run()
    # AppTest re-applies its query params on every run, where a browser would
    # have dropped the one the app deleted. Clear it so the tour isn't
    # reopened underneath the click.
    at.query_params.clear()
    at.button(key="tour_goto").click().run()
    assert _flag(at, onboarding._RESUME) is True
    assert not _flag(at, onboarding._OPEN)
    assert at.session_state["chat_panel_open"] is True  # the step's own state
    assert _button(at, "tour_strip_resume") is not None  # the way back in


def test_a_parked_tour_keeps_claiming_the_run(app):
    """While the tour sits in the resume strip it still owns the session, so
    app.py never pops the investor-profile modal over a walkthrough."""
    at = app({onboarding.PREF_DONE: True})
    at.query_params["tour"] = "assistant"
    at.run()
    at.query_params.clear()
    at.button(key="tour_goto").click().run()
    assert _flag(at, onboarding._RESUME) is True
    assert at.session_state["claimed"] is True


def test_the_tour_query_param_opens_at_a_named_step(app):
    at = app({onboarding.PREF_DONE: True})
    at.query_params["tour"] = "notify"
    at.run()
    steps = [s.id for s in onboarding.visible_steps()]
    assert at.session_state[onboarding._MODE] == "tour"
    assert steps[at.session_state[onboarding._STEP]] == "notify"
