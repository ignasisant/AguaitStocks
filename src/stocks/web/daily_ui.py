"""The Home page's "Daily action" card — Streamlit over stocks.chat.daily.

One briefing per account per day: a headline, three or four schematic lines,
and the tickers they are about. The reasoning, the prompt and the fallback all
live in the headless module; this file owns the session, the stored copy and
the paint.

The card is rendered into a deferred slot (web/skeletons.py). Home reserves it
high on the page and calls `render()` at the very bottom of the script, so a
day whose briefing still has to be generated shimmers a card-shaped
placeholder in the right place while the rest of the dashboard paints — and
the LLM call, which happens at most once a day, never sits in front of the
numbers.

Generation is attempted at most once per session per action day: a provider
that is down stays down for the next few seconds, and retrying on every rerun
would spend the account's free allowance on the same failure. The computed
fallback fills the card meanwhile, so the section is never empty.

And it runs in a thread. A provider that answers inside GRACE_S still lands in
the first paint; a slower one is handed to the background, the computed card
goes up immediately with a line saying a briefing is still being written, and
a timed fragment swaps the real one in when it arrives. The script run
therefore ends in seconds instead of holding the whole page open — and an open
run is a page nothing on it can be clicked on — for daily.TIMEOUT_S.
"""

from __future__ import annotations

import threading
from datetime import date, datetime

import streamlit as st

from stocks import obs
from stocks.chat import daily, engine, signals
from stocks.web import auth, css, skeletons
from stocks.web.i18n import active_language
from stocks.web.i18n import t as tr
from stocks.web.widgets import is_mobile, ticker_cell, viewer_tz

# The card's own chrome: the AI badge, the headline, a tighter bullet list than
# st.markdown's default, and the ticker chip row. Colors are DS tokens (the
# same variables widgets.py defines), so the card sits on the page like the
# other bordered cards rather than beside them.
#
# NOTE: never write a left angle bracket inside this block, comments included —
# DOMPurify drops the WHOLE style block when its text contains one (see
# skeletons.py and app.py for the same trap).
_CSS = r"""
<style>
  .ag-daily-head {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    margin-bottom: 2px;
  }
  .ag-daily-badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 2px 9px; border-radius: var(--ag-radius-pill);
    background: var(--ag-cta-tint); border: 1px solid var(--ag-cta-tint-edge);
    color: var(--ag-brand-accent);
    font-size: var(--ag-fs-2xs); font-weight: 600; letter-spacing: .04em;
    text-transform: uppercase;
  }
  .ag-daily-when {color: var(--ag-text-muted); font-size: var(--ag-fs-xs);}
  .ag-daily-headline {
    font-family: 'Epilogue', 'Instrument Sans', sans-serif;
    font-size: var(--ag-fs-lg); font-weight: 600; line-height: 1.35;
    color: var(--ag-text-primary); margin: 6px 0 8px;
  }
  .ag-daily-list {margin: 0; padding-left: 1.05rem;}
  .ag-daily-list li {
    color: var(--ag-text-secondary); line-height: 1.45; margin-bottom: 5px;
  }
  .ag-daily-list li::marker {color: var(--ag-brand-accent);}
  .ag-daily-chips {
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;
  }
  .ag-daily-chips a {
    display: inline-flex; align-items: center;
    padding: 3px 10px 3px 8px; border-radius: var(--ag-radius-pill);
    background: var(--ag-surface-hover); border: 1px solid var(--ag-border);
    font-size: var(--ag-fs-sm);
  }
  /* The placeholder the slot holds while a briefing is being written, and
     the line under a computed card whose model answer is still coming. A bare
     shimmer over a multi-second call reads as a broken page; naming who is
     working and that it takes a moment buys the patience it needs. */
  .ag-daily-wait {
    color: var(--ag-text-secondary); font-size: var(--ag-fs-sm);
    line-height: 1.5; margin: 4px 0 14px;
  }
  .ag-daily-wait b {color: var(--ag-text-primary); font-weight: 600;}
  .ag-daily-note {
    display: flex; align-items: flex-start; gap: 7px; margin-top: 12px;
    color: var(--ag-text-muted); font-size: var(--ag-fs-xs); line-height: 1.45;
    min-width: 0; overflow-wrap: anywhere;
  }
  .ag-daily-dot {
    flex: none; width: 7px; height: 7px; border-radius: 50%; margin-top: 6px;
    background: var(--ag-brand-accent);
    animation: ag-daily-pulse 1.3s ease-in-out infinite;
  }
  @keyframes ag-daily-pulse {
    0%, 100% {opacity: .25;}
    50% {opacity: 1;}
  }
  /* Vestibular safety, same rule as the skeleton sheen. */
  @media (prefers-reduced-motion: reduce) {
    .ag-daily-dot {animation: none; opacity: .6;}
  }

  /* ---- phones (DS mobile spec) ---- Last block in the sheet, so it
     overrides the rules above. The card is the first thing on a phone
     dashboard and its lines are the longest text on the page, so the type
     tightens a step and the bullet glyph moves inline: a hanging indent
     wastes 17px of a 390px screen on every wrapped line. Chips grow to a
     thumb-sized target; the 44px button minimum comes from app.py. */
  @media (max-width: 640px) {
    .ag-daily-head {gap: 6px;}
    .ag-daily-headline {
      font-size: var(--ag-fs-base); line-height: 1.3; margin: 8px 0 10px;
    }
    .ag-daily-list {padding-left: 0; list-style: none;}
    .ag-daily-list li {
      position: relative; padding-left: 15px;
      font-size: var(--ag-fs-md); line-height: 1.4; margin-bottom: 9px;
    }
    /* The glyph is written literally, never as a CSS escape: "\25B8" inside
       a Python string is the OCTAL escape \25 followed by "B8", so the
       marker rendered as a purple "B8" on every phone. */
    .ag-daily-list li::before {
      content: "▸"; position: absolute; left: 0;
      color: var(--ag-brand-accent);
    }
    .ag-daily-chips {gap: 8px; margin-top: 12px;}
    .ag-daily-chips a {min-height: 36px; padding: 6px 12px 6px 10px;}
    .ag-daily-wait {font-size: var(--ag-fs-md); line-height: 1.45;}
    .ag-daily-note {font-size: var(--ag-fs-sm); margin-top: 14px;}
    .ag-daily-dot {margin-top: 7px;}
    /* The two actions are the only tappable things in the card, and a
       tertiary button is a label with no surface: stacked full-width on a
       phone the pair reads as two stray captions floating in whitespace, not
       as controls. Give them an edge, a fill and the DS 44px target, and pull
       them together so they read as one block. Keyed selectors (Streamlit
       stamps an "st-key-" plus the widget key class on a widget's container)
       rather than testids, which is the same hook chat_core.py's FAB uses;
       !important because the theme's own button rules are more specific than
       a class. */
    .st-key-daily_action_row {gap: 8px !important;}
    .st-key-daily_action_ask button,
    .st-key-daily_action_refresh button {
      width: 100%; min-height: 44px; justify-content: center;
      border: 1px solid var(--ag-border) !important;
      border-radius: var(--ag-radius-sm);
      background: var(--ag-surface-hover) !important;
      color: var(--ag-text-primary) !important;
    }
    .st-key-daily_action_ask button * ,
    .st-key-daily_action_refresh button * {color: var(--ag-text-primary);}
    .st-key-daily_action_refresh button:disabled {opacity: .55;}
  }
</style>
"""

# How long the page waits for a briefing before painting the computed card and
# leaving the model to the background. Short on purpose: the free chain usually
# answers well inside it, and everything past it is a script run held open over
# a dashboard the reader can already see but not touch.
GRACE_S = 2.5
# How often the card then looks for the thread's answer.
POLL_S = 1.5

_TRIED = "daily_action_tried"   # session key: this action day already called out
_FORCE = "daily_action_force"   # set by the refresh button, read on the next run
_JOB = "daily_action_job"       # the generation running in a background thread
_CARD = "daily_action_card"     # the card already resolved, so a rerun re-uses it


def _card_html(
    action: daily.DailyAction, when: str, pending: bool = False
) -> str:
    """The whole card body as one HTML block.

    One block rather than a stack of st.markdown calls because the pieces are
    typographically related (badge, headline, tight list, chips) and Streamlit
    would put a paragraph gap between each of them. Model text is escaped —
    it is the least trusted string on the dashboard.
    """
    from html import escape

    bullets = "".join(f"<li>{escape(b)}</li>" for b in action.bullets)
    chips = "".join(
        f'<span>{ticker_cell(t, name=False)}</span>' for t in action.focus
    )
    # `pending` means the reader is looking at the computed card while a model
    # writes the real one. Saying so is the difference between a card that
    # looks final and one the reader knows will improve on its own.
    note = (
        '<div class="ag-daily-note"><span class="ag-daily-dot"></span>'
        f'{escape(tr("home.daily_pending"))}</div>'
        if pending
        else ""
    )
    return (
        '<div class="ag-daily-head">'
        f'<span class="ag-daily-badge">{escape(tr("home.daily_badge"))}</span>'
        f'<span class="ag-daily-when">{escape(when)}</span>'
        "</div>"
        f'<div class="ag-daily-headline">{escape(action.headline)}</div>'
        f'<ul class="ag-daily-list">{bullets}</ul>'
        + (f'<div class="ag-daily-chips">{chips}</div>' if chips else "")
        + note
    )


def _when_label(action: daily.DailyAction, day: date) -> str:
    """"Today · 09:00" / "Mon 2 Sep" — the stamp under the badge.

    Days before the cutoff show *yesterday's* card (see daily.action_day), so
    the date is never decoration: it is how the reader knows the briefing
    predates this morning.
    """
    try:
        stamped = date.fromisoformat(action.day)
    except ValueError:
        stamped = day
    today = datetime.now(viewer_tz()).date()
    if stamped == today:
        clock = (
            datetime.fromtimestamp(action.generated, tz=viewer_tz()).strftime("%H:%M")
            if action.generated
            else ""
        )
        return (
            tr("home.daily_today", time=clock)
            if clock
            else tr("home.daily_today_no_time")
        )
    # Weekday keys only cover Mon-Fri (the mini calendar drops the weekend),
    # so the stale stamp is day + month — unambiguous inside a 24h-old card.
    return f"{stamped.day} {tr(f'home.mon_{stamped.month}')}"


def _jurisdiction(prefs: dict):
    """The account's tax jurisdiction, or None.

    Only the harvest action reads it — for the tax year's boundaries and the
    repurchase window — so an unresolvable residence degrades to a
    calendar-year offset figure without the rule note, not to a missing card.
    """
    try:
        from stocks.portfolio import tax
        from stocks.web import tax_ui

        return tax.get(tax_ui.resolve_code(prefs))
    except Exception:
        return None


def _start(
    prefs: dict,
    facts: dict,
    lang: str,
    day: date,
    stored: daily.DailyAction | None,
    *,
    forced: bool = False,
) -> dict:
    """Kick off one generation in a background thread; return its job dict.

    The thread touches neither Streamlit nor the account's files: it calls the
    headless generator and drops the answer in the dict. Everything with a
    side effect — the spent free unit, the stored card — is applied by
    `_collect()` back on the script thread, which is the one that owns those
    files and the toast auth._persist raises when the bucket is down.

    `spend` still mutates the prefs dict from the thread, and that is fine: it
    is this session's own copy, and nothing writes it until _collect does.
    """
    profile = auth.load_profile(prefs)
    job: dict = {
        "key": (day.isoformat(), lang),
        "forced": forced,
        "prefs": prefs,
        "stored": stored,
        "action": None,
        "spent": False,
        "done": False,
    }

    def spend(p: dict) -> bool:
        ok = engine.spend_free_quota(p)
        job["spent"] = job["spent"] or ok
        return ok

    def work() -> None:
        try:
            job["action"] = daily.generate(
                prefs,
                profile,
                facts,
                lang,
                day,
                recent=stored.recent if stored else [],
                spend_free=spend,
            )
        except Exception as exc:  # daily.generate swallows its own, but a
            # thread that dies silently would leave the card polling forever.
            obs.warn("daily_action.generate_failed",
                     error_type=type(exc).__name__, error=str(exc)[:200])
        finally:
            job["done"] = True

    thread = threading.Thread(target=work, name="daily-action", daemon=True)
    job["thread"] = thread
    thread.start()
    return job


def _collect(job: dict) -> daily.DailyAction | None:
    """A finished job's card, after its side effects are applied.

    A free-chain attempt spends the account's daily counter, which lives in
    prefs.json — so prefs are saved only when a unit was actually taken, and
    the card itself only when a model produced it (a computed card is free to
    rebuild on the next run, and storing it would block the upgrade to a real
    briefing once the allowance resets).
    """
    if job.get("spent"):
        auth.save_prefs(job["prefs"])
    action = job.get("action")
    if action:
        card = action.to_dict()
        card["recent"] = daily.remembered(job.get("stored"), action.headline)
        auth.save_action(card)
    return action


def _wait_html(title_key: str) -> str:
    """The card chrome with a line saying a briefing is being written.

    Used twice: by the slot Home reserves before the page has loaded, and by
    the card itself while a regeneration is out — the two moments when there
    is nothing to show yet and an anonymous shimmer would read as a page that
    broke rather than one that is writing.
    """
    from html import escape

    return (
        _CSS
        + '<div class="ag-daily-head">'
        f'<span class="ag-daily-badge">{escape(tr("home.daily_badge"))}</span>'
        "</div>"
        f'<div class="ag-daily-wait"><b>{escape(tr(title_key))}</b> '
        f'{escape(tr("home.daily_wait_body"))}</div>'
        + skeletons.html("text", lines=3)
    )


def reserve(container=None):
    """Reserve the card's place near the top of Home, naming what is coming.

    The fill happens at the very bottom of the script (skeletons' deferred-slot
    pattern), so this placeholder is on screen for the whole page load. On most
    days that is one paint of a stored card and a plain shimmer is right; on
    the day's first visit it covers a model call, and an anonymous shimmer over
    a multi-second wait reads as a page that broke rather than one that is
    writing. Cheap to decide: the stored card is a small JSON read.
    """
    try:
        day = daily.action_day(datetime.now(viewer_tz()))
        stored = daily.DailyAction.from_dict(auth.load_action())
        waiting = not daily.is_fresh(stored, day, active_language())
    except Exception:
        waiting = False  # a placeholder is never worth taking the page down
    if not waiting:
        return skeletons.reserve(
            "text", container=container, border=True, title=True, lines=4
        )
    # The sheet travels inside the slot rather than through css.inject, so
    # filling or clearing the slot takes it with it — a style block left
    # stranded on the page is an element the caller cannot clear. Safe as one
    # block because _CSS contains no left angle bracket (see the NOTE on it):
    # DOMPurify would otherwise drop the whole thing.
    return skeletons.reserve_html(
        _wait_html("home.daily_wait_title"), container=container, border=True
    )


def render(
    slot,
    *,
    tbl=None,
    hist=None,
    currency: str = "EUR",
    day_change: tuple[float, float] | None = None,
    earnings=(),
    extremes=(),
    holdings=(),
    closes=None,
    realized=(),
) -> None:
    """Fill Home's reserved slot with today's card (or clear it).

    Args mirror what the page already loaded, so the card costs no fetch of
    its own: `tbl` is enriched_positions, `hist` the basket history,
    `day_change` the (amount, pct) the KPI row is showing (the card must not
    quote a different number from the tiles above it), `holdings` the watchlist
    entries — which is where the user's own price alerts live — `closes` the
    last two native closes per ticker those alerts are compared against, and
    `realized` the matched sales the harvest arithmetic needs.

    Everything is optional: a watchlist-only account still gets alert and
    52-week actions, a ledger-only one still gets drawdown and harvest.

    Never raises. The dashboard is the first thing a user sees, and no
    assistant feature is worth taking it down — any failure clears the slot.
    """
    try:
        _render(slot, tbl, hist, currency, day_change, earnings, extremes,
                holdings, closes or {}, realized)
    except Exception as exc:
        # Logged, not swallowed: a card that quietly stops appearing is a
        # bug nobody can see, and this is the one path that removes it from
        # the page.
        obs.warn("daily_action.render_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        if not slot.resolved:
            slot.clear()


def _remember(action: daily.DailyAction, key: tuple) -> daily.DailyAction:
    """Keep the resolved card in the session, so a fragment rerun (the refresh
    click, the poll tick, the Ask button) redraws it instead of re-deciding
    what today's card is from a `stored` argument the parent run captured
    before the generation that replaced it."""
    st.session_state[_CARD] = {"key": key, "action": action}
    return action


def _resolve(
    prefs: dict, facts: dict, lang: str, day, stored
) -> tuple[daily.DailyAction | None, bool]:
    """Today's card, and whether a briefing is still being written.

    The card is None in exactly one case: a regeneration the reader asked for
    is out. The old card is theirs and they just replaced it, so it goes the
    moment they click — leaving it up, or substituting the computed one for
    it, would have them reading a briefing they already dismissed. Every other
    path ends in a card.

    In order: a forced refresh outranks everything, then a generation already
    in flight, then the card this session resolved earlier, then the stored
    one, and only after all of those does the day's single generation start —
    waiting GRACE_S for it before going to background. Never returns None:
    every path ends in a card, because an empty slot is the one outcome the
    dashboard cannot show.

    Both the page run and the card's fragment call this, and it is written to
    be called repeatedly: only the first call of an action day starts work.
    """
    def waiting(forced: bool):
        return (None if forced else daily.computed(facts, lang, day)), True

    key = (day.isoformat(), lang)
    force = bool(st.session_state.pop(_FORCE, False))
    job = st.session_state.get(_JOB)
    if force:
        # The click wins: a slow job in flight is abandoned rather than waited
        # on (its free unit is lost, which is the cheaper of the two prices).
        st.session_state.pop(_JOB, None)
        st.session_state.pop(_CARD, None)
    elif job is not None and job.get("key") == key:
        if not job["done"]:
            return waiting(job.get("forced", False))
        st.session_state.pop(_JOB, None)
        return _finish(job, facts, lang, day, key)
    else:
        cached = st.session_state.get(_CARD)
        if cached is not None and cached.get("key") == key:
            return cached["action"], False
        if daily.is_fresh(stored, day, lang):
            return _remember(stored, key), False
        if st.session_state.get(_TRIED) == key:
            # A generation already ran today and gave nothing back: a provider
            # that is down stays down for the next few seconds, and retrying on
            # every rerun would spend the allowance on the same failure.
            return daily.computed(facts, lang, day), False
    st.session_state[_TRIED] = key
    job = _start(prefs, facts, lang, day, stored, forced=force)
    job["thread"].join(GRACE_S)
    if job["done"]:
        return _finish(job, facts, lang, day, key)
    st.session_state[_JOB] = job
    return waiting(force)


def _finish(job, facts, lang, day, key) -> tuple[daily.DailyAction, bool]:
    """Collect a done job: its card, or the computed one when it came back
    empty. Yesterday's stored card is deliberately not a candidate — today's
    triggers plainly stated beat a stale briefing."""
    action = _collect(job)
    if action:
        return _remember(action, key), False
    return daily.computed(facts, lang, day), False


def _force_refresh() -> None:
    """Ask _resolve for a new briefing on the next (fragment) rerun."""
    st.session_state[_FORCE] = True


def _body(prefs: dict, facts: dict, lang: str, day, stored, polling: bool) -> None:
    """The card's markup, its two actions and its footnote."""
    action, pending = _resolve(prefs, facts, lang, day, stored)
    mobile = is_mobile()
    regenerating = action is None
    with st.container(border=True):
        css.inject(_CSS)
        st.html(
            _wait_html("home.daily_regen_title") if regenerating
            else _card_html(action, _when_label(action, day), pending)
        )
        # Phones stack the two actions full-width — side by side, "Ask the
        # assistant" and "Regenerate" wrap to two lines each inside a 390px
        # card and the pair reads as one smudge. Full-width also puts both
        # targets under the thumb, and the labels shorten to match.
        row = st.container(
            key="daily_action_row",
            horizontal=not mobile, vertical_alignment="center", gap="small",
        )
        width = "stretch" if mobile else "content"
        if row.button(
            tr("home.daily_ask_short") if mobile else tr("home.daily_ask"),
            icon=":material/auto_awesome:", width=width,
            key="daily_action_ask", type="tertiary",
        ):
            # The assistant panel is drawn by app.py, outside this fragment,
            # so opening it needs the whole app to rerun — a fragment rerun
            # would set the flag and redraw nothing.
            st.session_state["chat_panel_open"] = True
            st.rerun(scope="app")
        # Refresh via on_click, deliberately: a widget inside a fragment
        # already reruns that fragment on its own, and the callback fires
        # before the rerun, so the flag is set in time for _resolve to see it.
        # Calling st.rerun() here instead is a trap — scope="fragment" is
        # rejected outside a fragment rerun (the first pass runs as part of
        # the page), and that exception would take the card off the page.
        # Disabled while its own regeneration is out: a second click would
        # start a second thread and spend a second unit of the allowance on a
        # card the first one is already writing.
        row.button(
            tr("home.daily_refresh"), icon=":material/refresh:", width=width,
            key="daily_action_refresh", type="tertiary",
            on_click=_force_refresh, disabled=regenerating,
        )
        # The full disclaimer is three lines on a phone, under a card whose
        # whole point is to be skimmed in one; the short form says the same
        # two things (a model wrote it, it is not advice). While a briefing is
        # still coming, neither is true yet — the figures are the reader's own
        # and nothing has been written, which is what the third line says.
        if regenerating:
            note = ""  # nothing on screen to disclaim yet
        elif pending:
            note = tr("home.daily_computed_wait")
        elif action.from_model:
            note = (tr("home.daily_disclaimer_short") if mobile
                    else tr("home.daily_disclaimer"))
        else:
            note = tr("home.daily_computed_note")
        if note:
            st.caption(note)
    if polling != pending:
        # The card and its timer disagree, in one of two ways: the thread
        # answered (stop polling — a run_every cannot be stopped from inside
        # itself, and one left running is a websocket round trip every POLL_S
        # all day for an answer already in hand), or a regenerate clicked in
        # the untimed fragment went to background (start polling, or the new
        # briefing would sit in the thread until the reader touched something).
        # Only the page run can pick the other fragment, so: one app rerun.
        st.rerun(scope="app")


@st.fragment
def _card(prefs: dict, facts: dict, lang: str, day, stored) -> None:
    """The card itself, as a fragment.

    A fragment because of the refresh button: a plain st.rerun() would re-run
    the whole Home script — every price fetch, every card — to redraw three
    lines, and the reader would watch the dashboard blink and the card vanish
    behind its own skeleton while a provider is called. `st.rerun(scope=
    "fragment")` re-enters this function only. Same pattern as Home's
    value-vs-injected sparkline.

    The arguments are the parent run's data: a fragment rerun does not
    recompute them, which is exactly right — a regeneration should re-ask the
    model about the same book, not refetch the book.
    """
    _body(prefs, facts, lang, day, stored, polling=False)


@st.fragment(run_every=POLL_S)
def _card_waiting(prefs: dict, facts: dict, lang: str, day, stored) -> None:
    """The same card on a timer, while a briefing is still being written.

    Two fragments rather than one, because `run_every` is fixed when the
    fragment is declared: the page picks the timed one only for the seconds a
    thread is actually out, and _body drops back to the plain one — via a
    single app rerun — as soon as the answer lands.
    """
    _body(prefs, facts, lang, day, stored, polling=True)


def _render(slot, tbl, hist, currency, day_change, earnings, extremes,
            holdings, closes, realized) -> None:
    if not auth.is_logged_in():
        slot.clear()  # guests share a read-only data dir — no card to store
        return
    lang = active_language()
    prefs = auth.load_prefs()
    day = daily.action_day(datetime.now(viewer_tz()))
    stored = daily.DailyAction.from_dict(auth.load_action())
    # Built even when a stored card will win: it is cheap frame arithmetic
    # over data the page already holds, and the fragment needs it in hand to
    # regenerate without re-running the page.
    facts = daily.build_facts(
        tbl,
        hist,
        currency=currency,
        day=day_change,
        earnings=earnings,
        extremes=extremes,
        signals=signals.candidates(
            holdings=holdings,
            tbl=tbl,
            closes=closes,
            realized=realized,
            earnings=earnings,
            extremes=extremes,
            jurisdiction=_jurisdiction(prefs),
            currency=currency,
        ),
    )
    # Resolved before the card is drawn, not inside it: which fragment paints
    # the card depends on whether a generation went to background, and a
    # fragment cannot choose its own run_every. _resolve is idempotent, so the
    # fragment calling it again a moment later reads the same answer.
    _, pending = _resolve(prefs, facts, lang, day, stored)
    # The fragment must own a real container, not the reserved st.empty():
    # a fragment rerun redraws its own block, and an element it does not own
    # is outside its scope.
    with slot.container():
        paint = _card_waiting if pending else _card
        paint(prefs, facts, lang, day, stored)
