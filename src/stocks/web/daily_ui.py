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
"""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from stocks import obs
from stocks.chat import daily, engine, signals
from stocks.web import auth, css
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
_CSS = """
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
    .ag-daily-list li::before {
      content: "\25B8"; position: absolute; left: 0;
      color: var(--ag-brand-accent);
    }
    .ag-daily-chips {gap: 8px; margin-top: 12px;}
    .ag-daily-chips a {min-height: 36px; padding: 6px 12px 6px 10px;}
  }
</style>
"""

_TRIED = "daily_action_tried"   # session flag: this action day already called out
_FORCE = "daily_action_force"   # set by the refresh button, read on the next run


def _card_html(action: daily.DailyAction, when: str) -> str:
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
    return (
        '<div class="ag-daily-head">'
        f'<span class="ag-daily-badge">{escape(tr("home.daily_badge"))}</span>'
        f'<span class="ag-daily-when">{escape(when)}</span>'
        "</div>"
        f'<div class="ag-daily-headline">{escape(action.headline)}</div>'
        f'<ul class="ag-daily-list">{bullets}</ul>'
        + (f'<div class="ag-daily-chips">{chips}</div>' if chips else "")
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


def _generate(
    prefs: dict, facts: dict, lang: str, day: date, stored: daily.DailyAction | None
) -> daily.DailyAction | None:
    """One generation attempt, with the accounting that goes with it.

    A free-chain attempt spends the account's daily counter, which lives in
    prefs.json — so prefs are saved only when a unit was actually taken, and
    the card itself only when a model produced it (a computed card is free to
    rebuild on the next run, and storing it would block the upgrade to a real
    briefing once the allowance resets).
    """
    spent = False

    def spend(p: dict) -> bool:
        nonlocal spent
        ok = engine.spend_free_quota(p)
        spent = spent or ok
        return ok

    action = daily.generate(
        prefs,
        auth.load_profile(),
        facts,
        lang,
        day,
        recent=stored.recent if stored else [],
        spend_free=spend,
    )
    if spent:
        auth.save_prefs(prefs)
    if action:
        stored_card = action.to_dict()
        stored_card["recent"] = daily.remembered(stored, action.headline)
        auth.save_action(stored_card)
    return action


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


def _resolve(prefs: dict, facts: dict, lang: str, day, stored) -> daily.DailyAction:
    """Today's card: the stored one, a fresh generation, or the computed one.

    Consumes the refresh flag, so a forced regeneration happens exactly once
    per click. Never returns None — every path ends in a card, because an
    empty slot is the one outcome the dashboard cannot show.
    """
    force = bool(st.session_state.pop(_FORCE, False))
    if not force and daily.is_fresh(stored, day, lang):
        return stored
    tried = st.session_state.get(_TRIED) == day.isoformat()
    action = None
    if not tried or force:
        st.session_state[_TRIED] = day.isoformat()
        action = _generate(prefs, facts, lang, day, stored)
    # Nothing from the model: yesterday's card is worse than today's triggers
    # plainly stated, so the computed one wins over a stale one.
    return action or daily.computed(facts, lang, day)


def _force_refresh() -> None:
    """Ask _resolve for a new briefing on the next (fragment) rerun."""
    st.session_state[_FORCE] = True


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
    action = _resolve(prefs, facts, lang, day, stored)
    mobile = is_mobile()
    with st.container(border=True):
        css.inject(_CSS)
        st.html(_card_html(action, _when_label(action, day)))
        # Phones stack the two actions full-width — side by side, "Ask the
        # assistant" and "Regenerate" wrap to two lines each inside a 390px
        # card and the pair reads as one smudge. Full-width also puts both
        # targets under the thumb, and the labels shorten to match.
        row = st.container(
            horizontal=not mobile, vertical_alignment="center", gap="small"
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
        row.button(
            tr("home.daily_refresh"), icon=":material/refresh:", width=width,
            key="daily_action_refresh", type="tertiary",
            on_click=_force_refresh,
        )
        # The full disclaimer is three lines on a phone, under a card whose
        # whole point is to be skimmed in one; the short form says the same
        # two things (a model wrote it, it is not advice).
        st.caption(
            (tr("home.daily_disclaimer_short") if mobile
             else tr("home.daily_disclaimer"))
            if action.from_model
            else tr("home.daily_computed_note")
        )


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
    # The fragment must own a real container, not the reserved st.empty():
    # a fragment rerun redraws its own block, and an element it does not own
    # is outside its scope.
    with slot.container():
        _card(prefs, facts, lang, day, stored)
