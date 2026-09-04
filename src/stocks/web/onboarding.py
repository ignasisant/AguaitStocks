"""Guided tour and per-release "what's new", both driven by one step registry.

Two surfaces over the same data:

* the **tour** — a modal that walks a new account through the app one feature
  at a time, and for the connectable ones (ledger import, AI key, Telegram,
  tax residence) says whether this account has it switched on and takes the
  user to the page where it is switched on.
* **what's new** — the same modal in a different mode, listing the releases
  the account has not seen yet. `RELEASES` carries the changelog as data and
  each entry names the tour steps its items belong to, so a new feature is
  announced and explained by the same copy.

Three design points worth knowing before editing this:

1. **One dialog per script run.** Streamlit allows exactly one open modal, so
   this module and `auth.maybe_prompt_profile()` cannot both fire on the same
   run. app.py gives the tour priority (`maybe_open()` returns True when it
   claims the run) — the investor profile is a tour step anyway.
2. **Navigating closes the modal.** `st.switch_page` ends the run, and a modal
   cannot survive it. So "take me there" does not try to stay open: it queues
   the step (`_GOTO`), the next full run navigates, and the tour reappears as a
   thin *resume strip* above the page body. That strip — not the modal — is
   what makes "go and look at it" work at all.
3. **A dialog is a fragment.** Back/Next are `on_click` callbacks so they rerun
   only the modal; the buttons that must leave it (take me there, finish, exit)
   call `st.rerun()` for a full-app rerun, which is what lets app.py act on the
   queued step.

Progress lives in prefs.json for signed-in accounts and in session state for
guests: the guest data dir is shared by every anonymous visitor, so writing a
guest's tour progress there would hand it to the next stranger.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import streamlit as st

from stocks import obs
from stocks.portfolio.ledger import has_transactions
from stocks.web import auth, i18n
from stocks.web.i18n import t as tr

# ------------------------------------------------------------- prefs / state
# prefs.json keys. `tour_seen_version` is the newest RELEASES version this
# account has been shown; `tour_done` is set once the tour is finished or
# exited, which is what stops it auto-opening on later sessions.
PREF_SEEN_VERSION = "tour_seen_version"
PREF_DONE = "tour_done"

# Session keys. The modal is kept open by _OPEN rather than by the run that
# opened it: every full rerun in this app (top-bar search, chat panel, page
# nav) would otherwise drop it mid-step.
_OPEN = "_tour_open"  # modal renders this run
_MODE = "_tour_mode"  # "tour" | "news"
_STEP = "_tour_step"  # index into visible_steps()
_GOTO = "_tour_goto"  # step id queued for navigation on the next full run
_RESUME = "_tour_resume"  # tour minimized: show the resume strip
_SEEN = "_tour_auto_seen"  # auto-open evaluated once per session


@dataclass(frozen=True)
class Step:
    """One stop on the tour.

    Copy comes from the catalog by convention — `tour.<id>_title`,
    `tour.<id>_body`, and an optional `tour.<id>_cta` overriding the generic
    "take me there" label — so adding a step means adding a registry entry
    plus its keys in every locale, and nothing else.

    `page` is the module path `st.navigation` knows the page by; `query` are
    query parameters to land with (the Portfolio tabs ride `?tab=`), `session`
    is state to seed before navigating (the Profile tabs and the assistant
    panel are session-driven), and `reset_keys` are widget-state keys to drop
    so a `default=` can take effect again on a page the user is already on.
    """

    id: str
    icon: str
    page: str | None = None
    query: dict[str, str] | None = None
    session: dict[str, object] = field(default_factory=dict)
    reset_keys: tuple[str, ...] = ()
    # Whether the step's target sits behind require_login(). Guests still read
    # the step; its button is disabled with a sign-in hint instead.
    gated: bool = False
    # Whether this account has the feature switched on, given prefs. None for
    # steps that are nothing to switch on (a page is a page).
    done: Callable[[dict], bool] | None = None


def _has_ledger(_prefs: dict) -> bool:
    try:
        return has_transactions(auth.db_path())
    except Exception:
        return False  # unreadable/missing ledger reads as "nothing imported"


def _has_ai_key(prefs: dict) -> bool:
    """A BYOK provider key saved to prefs (encrypted) or entered this session.

    The keyless TopStocks free chain deliberately does not count: the step is
    about connecting your own provider, which is what lifts the daily cap.
    """
    return any(k.endswith("_key_enc") for k in prefs) or any(
        k.startswith("llm_key::") and st.session_state[k] for k in st.session_state
    )


def _has_bank(_prefs: dict) -> bool:
    try:
        from stocks.bank import store

        return bool(store.connections(auth.user_paths().bank))
    except Exception:
        return False


def _bank_available() -> bool:
    """Whether the bank feature exists for this session at all.

    Imported lazily and defensively: bank_ui reaches into st.secrets and the
    Enable Banking client, this module is imported by app.py before either is
    needed, and a deploy built without the bank feature must still get a tour
    rather than an ImportError.
    """
    try:
        from stocks.web import bank_ui
    except ImportError:
        return False
    return bank_ui.available()


# The tour, in order. Sequenced as the work actually flows — get the ledger in,
# read what it derives, then the market tools, then the things that run without
# you (assistant, notifications) — rather than following the nav.
STEPS: tuple[Step, ...] = (
    Step(id="welcome", icon="waving_hand"),
    Step(
        id="import",
        icon="upload_file",
        page="app_pages/import_transactions.py",
        gated=True,
        done=_has_ledger,
    ),
    Step(
        id="bank",
        icon="account_balance",
        page="app_pages/bank.py",
        gated=True,
        done=_has_bank,
    ),
    Step(
        id="positions",
        icon="pie_chart",
        page="app_pages/portfolio.py",
        query={"tab": "positions"},
        reset_keys=("portfolio_tab",),
        gated=True,
    ),
    Step(
        id="risk",
        icon="show_chart",
        page="app_pages/portfolio.py",
        query={"tab": "risk"},
        reset_keys=("portfolio_tab",),
        gated=True,
    ),
    Step(
        id="tax",
        icon="receipt_long",
        page="app_pages/portfolio.py",
        query={"tab": "tax"},
        reset_keys=("portfolio_tab",),
        gated=True,
        done=lambda prefs: bool(prefs.get("tax_residence")),
    ),
    Step(
        id="income",
        icon="payments",
        page="app_pages/portfolio.py",
        query={"tab": "dividends"},
        reset_keys=("portfolio_tab",),
        gated=True,
    ),
    Step(
        id="daily",
        icon="tips_and_updates",
        page="app_pages/home.py",
        gated=True,
    ),
    Step(id="pulse", icon="speed", page="app_pages/sentiment.py"),
    Step(id="market", icon="query_stats", page="app_pages/ticker.py"),
    Step(id="screener", icon="filter_alt", page="app_pages/screener.py"),
    Step(
        id="assistant",
        icon="auto_awesome",
        session={"chat_panel_open": True},
        gated=True,
        done=_has_ai_key,
    ),
    Step(
        id="notify",
        icon="notifications",
        page="app_pages/profile.py",
        session={"profile_tab": "notify"},
        gated=True,
        done=lambda prefs: bool(prefs.get("telegram_chat_id")),
    ),
    Step(
        id="investor",
        icon="person",
        page="app_pages/profile.py",
        session={"profile_tab": "iv"},
        gated=True,
        done=auth.profile_is_set,
    ),
    Step(
        id="prefs",
        icon="tune",
        page="app_pages/profile.py",
        session={"profile_tab": "prefs"},
        gated=True,
    ),
)


# ------------------------------------------------------------------ releases
@dataclass(frozen=True)
class Release:
    """One shipped version, as the "what's new" modal shows it.

    Versions are date-based (`YYYY.MM`) on purpose: the tour's notion of "new"
    is about what the user can see change, which has no relation to the package
    version in pyproject.toml. `items` are catalog keys
    (`tour.news_<version with dots as underscores>_<slug>`); `steps` name the
    tour steps that explain those items, so an announcement can hand the reader
    straight to the walkthrough.
    """

    version: str
    date: str
    items: tuple[str, ...]
    steps: tuple[str, ...] = ()


# Oldest first; the newest entry's version is what an account gets stamped
# with. Add to the end when a release ships — see the update-tutorial skill.
RELEASES: tuple[Release, ...] = (
    Release(
        version="2026.09",
        date="2026-09",
        items=(
            "tour.news_2026_09_tax",
            "tour.news_2026_09_bank",
            "tour.news_2026_09_daily",
            "tour.news_2026_09_chat",
            "tour.news_2026_09_askai",
            "tour.news_2026_09_fees",
            "tour.news_2026_09_demo",
            "tour.news_2026_09_pulse",
            "tour.news_2026_09_profile",
        ),
        steps=("tax", "bank", "daily", "assistant", "market", "income",
               "pulse", "prefs", "import"),
    ),
)

CURRENT_VERSION = RELEASES[-1].version


# --------------------------------------------------------------- shared state
def _has_searched(prefs: dict) -> bool:
    """Whether this account has ever looked a ticker up in the top bar."""
    return bool(prefs.get("recent_searches"))


def _has_asked(_prefs: dict) -> bool:
    """Whether any conversation with the assistant has a turn in it."""
    try:
        book = auth.load_book()
    except Exception:
        return False
    return any(c.get("messages") for c in book.get("conversations", []))


def _watchlist_is_own(_prefs: dict) -> bool:
    """Whether the watchlist has been touched since it was seeded.

    Compared against the seed text rather than tracked with a flag, so it is
    also true for the accounts that predate this and for one restored from the
    bucket. An account that edits its way back to the exact seed reads as
    untouched, which is a shrug, not a bug.
    """
    try:
        return auth.watchlist_path().read_text() != auth.STARTER_WATCHLIST
    except OSError:
        return False


def explore_state(prefs: dict | None = None) -> dict[str, bool]:
    """Which of the no-setup-required things this account has actually tried.

    Separate from `setup_state`: those four are capabilities to switch on, and
    an account with none of them connected can still do all three of these
    right now — the search, the assistant's free chain and the watchlist
    editor need no key, no import and no statement. On a first visit they are
    the shortest path from "signed in" to "this is useful", which is exactly
    what a checklist of things still to connect fails to say.
    """
    p = prefs if prefs is not None else auth.load_prefs()
    return {
        "search": _has_searched(p),
        "ask": _has_asked(p),
        "watchlist": _watchlist_is_own(p),
    }


def setup_state(prefs: dict | None = None) -> dict[str, bool]:
    """Which connectable capabilities this account has switched on.

    One source of truth for the Home setup card and the tour's per-step
    badges — they used to compute this twice and could disagree.
    """
    p = prefs if prefs is not None else auth.load_prefs()
    return {
        "login": auth.is_logged_in(),
        "import": _has_ledger(p) if auth.is_logged_in() else False,
        "ai": _has_ai_key(p),
        "telegram": bool(p.get("telegram_chat_id")),
    }


def visible_steps() -> tuple[Step, ...]:
    """The steps this session may see. The bank step only exists where Enable
    Banking is configured and the account is allowlisted (see bank_ui)."""
    return tuple(s for s in STEPS if s.id != "bank" or _bank_available())


def by_id(step_id: str) -> Step | None:
    return next((s for s in STEPS if s.id == step_id), None)


def unseen_releases(prefs: dict | None = None) -> tuple[Release, ...]:
    """Releases newer than the version this account was last shown.

    Compared by position in RELEASES, not by parsing the version string: the
    list is the ordering, and an unknown stamp (a downgrade, a hand-edited
    prefs.json) reads as "show everything" rather than crashing.
    """
    p = prefs if prefs is not None else auth.load_prefs()
    seen = p.get(PREF_SEEN_VERSION)
    versions = [r.version for r in RELEASES]
    if seen not in versions:
        return RELEASES
    return RELEASES[versions.index(seen) + 1 :]


def _save(prefs: dict) -> None:
    """Persist prefs — for signed-in accounts only.

    A guest's paths point at the shared guest dir, so writing there would give
    the next anonymous visitor this one's tour progress. Guests keep everything
    in session state, which dies with the tab.
    """
    if auth.is_logged_in():
        auth.save_prefs(prefs)


# -------------------------------------------------------------- open / close
def open_tour(step_id: str | None = None) -> None:
    """Open the tour, at `step_id` when given (else where it was left off)."""
    steps = visible_steps()
    if step_id is not None:
        idx = next((i for i, s in enumerate(steps) if s.id == step_id), 0)
        st.session_state[_STEP] = idx
    st.session_state[_OPEN] = True
    st.session_state[_MODE] = "tour"
    st.session_state[_RESUME] = False


def open_news() -> None:
    st.session_state[_OPEN] = True
    st.session_state[_MODE] = "news"
    st.session_state[_RESUME] = False


def _consume_params() -> bool:
    """`?tour=1` (or `?tour=<step id>`) opens the tour, once per URL value.

    The parameter is deleted as it is read so a rerun doesn't reopen the modal
    the user just closed — same one-shot handling as the landing page's CTAs.
    """
    raw = str(st.query_params.get("tour") or "").strip().lower()
    if not raw:
        return False
    del st.query_params["tour"]
    if raw in {"1", "true", "yes"}:
        open_tour(step_id=visible_steps()[0].id)
        return True
    if by_id(raw) is not None:
        open_tour(step_id=raw)
        return True
    return False


def maybe_open() -> bool:
    """Auto-open on the session's first run; True when the tour owns the run.

    A brand-new signed-in account gets the tour; an account that has already
    finished it gets "what's new" when a release shipped since. Returns True
    whenever the tour is active — a modal open, or parked in the resume strip
    — which is what app.py uses to stand the investor-profile prompt down: two
    modals cannot share a run, and a nag over a walkthrough in progress is
    worse than a nag next session.

    Guests are never auto-opened: nothing is persisted for them, so it would
    pop on every new tab. They can still start it by hand.
    """
    # A modal the user asked for, one already open, or a tour parked on a page
    # the user was sent to. Each spends the session's one auto-open, so
    # "what's new" can never pop over the tour.
    if (
        _consume_params()
        or st.session_state.get(_OPEN)
        or st.session_state.get(_RESUME)
    ):
        st.session_state[_SEEN] = True
        return True
    if not auth.is_logged_in() or st.session_state.get(_SEEN):
        return False
    st.session_state[_SEEN] = True
    prefs = auth.load_prefs()
    if not prefs.get(PREF_DONE):
        open_tour(step_id=visible_steps()[0].id)
        return True
    if unseen_releases(prefs):
        open_news()
        return True
    return False


def _minimize() -> None:
    """Dismissing the modal (X, ESC, click-outside) parks the tour in the
    strip instead of ending it — the tour is what the user came for."""
    st.session_state[_OPEN] = False
    st.session_state[_RESUME] = True


def _exit_tour(reason: str = "exit") -> None:
    """Close the tour for good, and record where it was left.

    The step the reader walked out on is the one piece of feedback the tour
    can give about itself: a step that most accounts abandon is either badly
    placed or badly written, and neither is visible from the code. `reason`
    separates the three ways out — finishing it is not the same signal as
    skipping from step two.
    """
    steps = visible_steps()
    idx = min(int(st.session_state.get(_STEP, 0)), len(steps) - 1)
    obs.event("tour.exit", reason=reason, step=steps[idx].id,
              index=idx + 1, of=len(steps))
    prefs = auth.load_prefs()
    prefs[PREF_DONE] = True
    # A first-timer who has just been walked through everything has no
    # "what's new" to catch up on, so stamp the current release too.
    prefs[PREF_SEEN_VERSION] = CURRENT_VERSION
    _save(prefs)
    st.session_state[_OPEN] = False
    st.session_state[_RESUME] = False


def _dismiss_news() -> None:
    prefs = auth.load_prefs()
    prefs[PREF_SEEN_VERSION] = CURRENT_VERSION
    _save(prefs)
    st.session_state[_OPEN] = False
    st.session_state[_RESUME] = False


# ------------------------------------------------------------------ rendering
def render(page) -> None:
    """The tour's visible half: the modal, or the resume strip.

    Call once from app.py after the topbar (which has to be the main column's
    first element to stay sticky) and before `page.run()`. Navigation is *not*
    done here — see `consume_goto`.
    """
    # The toast belongs to the run *after* the modal closed — one emitted
    # inside the dialog dies with the rerun that shuts it.
    if st.session_state.pop("_tour_finished", False):
        st.toast(tr("tour.finished"), icon=":material/check_circle:")
    if st.session_state.get(_OPEN):
        _render_modal()
    elif st.session_state.get(_RESUME):
        _resume_strip()


def consume_goto(page) -> None:
    """Act on a step queued by "take me there" on the previous run.

    Split out of `render` and called early — right after `st.navigation`,
    before the topbar and the assistant panel — for two reasons: a step that
    switches pages ends the run, so anything rendered first is wasted work,
    and a step whose target is the panel itself (`session=` with no `page=`)
    has to seed its state *before* the panel renders or it would only open on
    the following interaction.
    """
    step = by_id(st.session_state.pop(_GOTO, "") or "")
    if step is None:
        return
    for key in step.reset_keys:
        st.session_state.pop(key, None)
    st.session_state.update(step.session)
    if step.page is None:
        return  # in-page target (the assistant panel) — nothing to navigate to
    if _url_path(step.page) != page.url_path:
        st.switch_page(step.page, query_params=step.query or None)
    elif step.query:
        # Already on the target page: the query parameter still has to move,
        # and the tab widget's stored state has to go for `default=` to win.
        st.query_params.update(step.query)


def _url_path(module: str) -> str:
    """The url_path st.navigation derives from a page module's filename."""
    stem = module.rsplit("/", 1)[-1].removesuffix(".py")
    return "" if stem == "home" else stem


def _render_modal() -> None:
    # Built at call time rather than with @st.dialog so the title resolves in
    # the run's active language instead of freezing at import — same as the
    # login, investor-profile and feedback modals.
    if st.session_state.get(_MODE) == "news":
        st.dialog(
            tr("tour.news_title"), width="medium", on_dismiss=_dismiss_news
        )(_news_body)()
        return
    # The title is fixed and the step number lives in the body on purpose: a
    # dialog is a fragment, so Back/Next re-execute only the body — anything
    # computed out here (title included) would still show the previous step.
    st.dialog(tr("tour.dialog_title"), width="medium", on_dismiss=_minimize)(
        _tour_body
    )()


def _cta_label(step: Step) -> str:
    key = f"tour.{step.id}_cta"
    return tr(key) if i18n.has(key) else tr("tour.goto")


def _step_back() -> None:
    st.session_state[_STEP] = max(int(st.session_state.get(_STEP, 0)) - 1, 0)


def _step_next() -> None:
    st.session_state[_STEP] = int(st.session_state.get(_STEP, 0)) + 1


def _current(steps: tuple[Step, ...]) -> int:
    """The step index, clamped and written back. Read inside the dialog body,
    never passed in: a fragment rerun re-executes the body with its *original*
    arguments, so a passed-in index would freeze on the first step shown."""
    idx = min(max(int(st.session_state.get(_STEP, 0)), 0), len(steps) - 1)
    st.session_state[_STEP] = idx
    return idx


def _tour_body() -> None:
    """One step: what it is, whether it is on, and the way to it.

    Back/Next are `on_click` callbacks — a dialog is a fragment, so they rerun
    the modal alone. The buttons that have to leave the modal call st.rerun()
    for a full-app rerun, which is the only thing that can navigate.
    """
    steps = visible_steps()
    idx = _current(steps)
    step = steps[idx]
    prefs = auth.load_prefs()
    signed_in = auth.is_logged_in()

    st.markdown(f":material/{step.icon}: **{tr(f'tour.{step.id}_title')}**")
    if step.done is not None:
        on = step.done(prefs)
        st.markdown(
            f":green-badge[:material/check: {tr('tour.active')}]"
            if on
            else f":gray-badge[{tr('tour.pending')}]"
        )
    st.markdown(tr(f"tour.{step.id}_body"))
    st.progress(
        (idx + 1) / len(steps),
        text=tr("tour.progress", n=idx + 1, total=len(steps)),
    )

    nav = st.container(horizontal=True, vertical_alignment="center")
    nav.button(
        tr("tour.back"),
        key="tour_back",
        icon=":material/chevron_left:",
        disabled=idx == 0,
        on_click=_step_back,
    )
    locked = step.gated and not signed_in
    if (step.page or step.session) and nav.button(
        _cta_label(step),
        key="tour_goto",
        type="secondary",
        icon=":material/open_in_new:",
        disabled=locked,
    ):
        st.session_state[_GOTO] = step.id
        _minimize()
        st.rerun()  # full run: closes the modal, consume_goto() navigates
    last = idx == len(steps) - 1
    if last:
        if nav.button(tr("tour.finish"), key="tour_finish", type="primary",
                      icon=":material/check:"):
            _exit_tour("finished")
            st.session_state["_tour_finished"] = True
            st.rerun()
    else:
        nav.button(
            tr("tour.next"),
            key="tour_next",
            type="primary",
            icon=":material/chevron_right:",
            on_click=_step_next,
        )
    if locked:
        st.caption(tr("tour.locked"))
    if not last and st.button(tr("tour.skip"), key="tour_skip", type="tertiary"):
        _exit_tour("skipped")
        st.rerun()


def _news_body() -> None:
    """Everything that shipped since this account last looked, newest first.

    Each release's items get a jump into the tour steps that explain them, so
    "what changed" and "how it works" are never two different documents.
    """
    releases = unseen_releases()
    if not releases:
        releases = RELEASES[-1:]
    st.caption(tr("tour.news_intro"))
    shown = {s.id for s in visible_steps()}
    for rel in reversed(releases):
        st.markdown(f"**{rel.version}** :gray-badge[{rel.date}]")
        for key in rel.items:
            st.markdown(f"- {tr(key)}")
        jumps = [s for s in (by_id(i) for i in rel.steps if i in shown) if s]
        if jumps:
            row = st.container(horizontal=True)
            for step in jumps:
                if row.button(
                    tr(f"tour.{step.id}_title"),
                    key=f"tour_news_{rel.version}_{step.id}",
                    icon=f":material/{step.icon}:",
                    type="tertiary",
                ):
                    _dismiss_news()  # stamped: the reader has seen this list
                    open_tour(step_id=step.id)
                    st.rerun()
    nav = st.container(horizontal=True)
    if nav.button(tr("tour.full_tour"), key="tour_news_full",
                  icon=":material/play_circle:"):
        _dismiss_news()
        open_tour(step_id=visible_steps()[0].id)
        st.rerun()
    if nav.button(tr("tour.close"), key="tour_news_close", type="primary",
                  icon=":material/check:"):
        _dismiss_news()
        st.rerun()


def _resume_strip() -> None:
    """The minimized tour: one line above the page body, on every page.

    Deliberately not a modal — the point of "take me there" is that the user
    is looking at the real page. The strip says which step they are on and
    holds the two ways out: back into the modal, or done with it.
    """
    steps = visible_steps()
    idx = min(max(int(st.session_state.get(_STEP, 0)), 0), len(steps) - 1)
    step = steps[idx]
    with st.container(border=True):
        row = st.container(horizontal=True, vertical_alignment="center")
        row.markdown(
            f":material/{step.icon}: "
            + tr(
                "tour.strip_progress",
                n=idx + 1,
                total=len(steps),
                title=tr(f"tour.{step.id}_title"),
            )
        )
        if row.button(tr("tour.resume"), key="tour_strip_resume",
                      icon=":material/menu_book:"):
            open_tour()
            st.rerun()
        if idx < len(steps) - 1 and row.button(
            tr("tour.next"), key="tour_strip_next",
            icon=":material/chevron_right:",
        ):
            _step_next()
            open_tour()
            st.rerun()
        if row.button(tr("tour.exit"), key="tour_strip_exit", type="tertiary"):
            _exit_tour("abandoned_strip")
            st.rerun()


def render_launcher(
    key: str,
    container=None,
    *,
    label: str | None = None,
    button_type: Literal["primary", "secondary", "tertiary"] = "secondary",
) -> None:
    """An "open the tutorial" button, for wherever the user looks for it — the
    Profile page, the Home setup card. `container` draws it into a row instead
    of the page flow. Always starts from the top: someone who asks for the
    tutorial wants the tutorial, not the step they abandoned weeks ago.
    """
    host = container if container is not None else st
    if host.button(
        label or tr("tour.launch"),
        key=key,
        # A caller-supplied label carries its own icon inline (the Home setup
        # card builds every pill that way), so don't add a second one.
        icon=None if label else ":material/menu_book:",
        type=button_type,
    ):
        open_tour(step_id=visible_steps()[0].id)
        st.rerun()
