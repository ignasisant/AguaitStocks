"""Empty states — the card a section draws when it has nothing to draw.

Most of this app's value is derived: positions, P/L, risk, dividends, fees and
the tax report all come out of an imported ledger, and the screener, the
earnings calendar and the sentiment pass all come out of a watchlist. A fresh
account has one of those and not the other, so on a first visit a good half of
the app has genuinely nothing to render.

Those spots used to render one gray sentence — `st.warning("No transactions
yet — upload a Revolut CSV on the **Import** page.")` — which reads as a
malfunction rather than as a step. An empty state has to answer three
questions in one glance, and a sentence with a page name bolded in it answers
at most one:

1. **what belongs here**, so the blank space is understood as a feature that
   exists rather than one that is broken;
2. **why it is blank**, which is nearly always "this derives from data you
   haven't given it yet" and not "the fetch failed";
3. **the one action that fixes it**, as something clickable — a link to the
   page that fills it, not the page's name in bold.

So this module owns one shape (`state()`) and every section that comes up
empty uses it, which is also what keeps the answer to (3) honest: a section
that cannot name a next step passes no `page`/`on_click` and the card renders
without a button instead of inventing one.

Not for failures. A fetch that died is a `notices.data_toast()` — the section
degrades with a toast saying why and the user's next move is to wait, not to
go and configure something. This card means "nothing here yet", never
"something went wrong".
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from stocks import obs
from stocks.web import skeletons

# Which empty states this session has already reported. Streamlit re-runs the
# whole script on every interaction, so an unguarded event here would log a
# section's emptiness dozens of times per visit and drown the signal it is
# there to give.
_SEEN = "_empty_state_seen"


def state(
    title: str,
    body: str = "",
    *,
    icon: str = "inbox",
    page: str | None = None,
    label: str = "",
    cta_icon: str = "arrow_forward",
    on_click: Callable[[], None] | None = None,
    key: str | None = None,
    extra: Callable[[], None] | None = None,
    preview: str | None = None,
    preview_kw: dict | None = None,
    event: str = "",
    container=None,
    border: bool = True,
) -> None:
    """Draw "nothing here yet" as a centered card with one way out.

    Args:
        title: what belongs here, in a few words ("No transactions yet").
        body: why it is blank and what filling it unlocks. Markdown.
        icon: Material Symbols name for the glyph above the title — the icon
            of the *thing that is missing* (`upload_file` for an unimported
            ledger, `list_alt` for an empty watchlist), so the card is
            recognizable before it is read.
        page: the page module path that fills this section, rendered as an
            `st.page_link`. A link, not a button: it costs no widget key, it
            survives a rerun, and the browser shows the destination on hover.
        label: text for the link or button. Required with `page`/`on_click`.
        cta_icon: Material Symbols name for the call to action.
        on_click: for a next step that is not a page — opening the assistant
            panel, seeding session state. Rendered as a button, which needs a
            `key`. Ignored when `page` is set; a card offers one way out.
        key: widget key for the `on_click` button. Streamlit requires it to be
            unique per script run, so pass the call site's own name.
        extra: rendered inside the card, under the call to action. The card
            still offers *one* way out — this is for the rare section whose
            blankness has a genuine second answer that is not a way out at
            all: the Portfolio page's "or look around with demo transactions"
            fills the same space without giving the app anything real. Draw it
            quieter than the CTA; anything that competes with the CTA belongs
            in the CTA.
        preview: a `skeletons.html` shape name to draw faded above the title —
            the silhouette of the chart, table or calendar that will stand
            here once the section has data. Shows the payoff instead of
            describing it, which is the difference between "this page is
            broken" and "this page is worth filling". Replaces the `icon`,
            which would only compete with it.
        preview_kw: shape arguments for `preview` (height, rows, cols…). Match
            the real content's shape; a three-row ghost above a card that
            fills with a year of history undersells it.
        event: stable slug for the log line this card emits, once per session
            (`portfolio.ledger`, `screener.watchlist`). Which sections a real
            account actually finds empty is the only way to know which of
            these cards is worth its space — and it must be a slug, not the
            title, which is translated and would split the same section across
            two languages. Omit only for a card whose emptiness says nothing.
        container: parent to draw into (a column, a tab). Defaults to wherever
            the call sits.
        border: draw the card outline. Off for a card that is already inside
            one, where a second outline reads as a nested panel.

    A page whose *whole* body is empty calls this and then `st.stop()`; the
    stop stays at the call site rather than hiding in here, because the same
    card is also used for one empty section of an otherwise full page.
    """
    if event and event not in st.session_state.setdefault(_SEEN, set()):
        st.session_state[_SEEN].add(event)
        obs.event("empty_state", where=event, cta=bool(page or on_click))
    host = container if container is not None else st
    with host.container(border=border, horizontal_alignment="center"):
        if preview:
            # Still and faded, not shimmering: a sheen would read as a load in
            # progress, and nothing is going to arrive without the reader.
            st.html(skeletons.html(preview, ghost=True, **(preview_kw or {})))
        else:
            # Material icons in markdown scale with the surrounding text, so
            # the heading level is what makes the glyph card-sized. Muted: the
            # icon is the least important thing in the card once it has been
            # recognized.
            st.markdown(f"### :gray[:material/{icon}:]")
        st.markdown(f"**{title}**")
        if body:
            st.caption(body)
        if page:
            st.page_link(page, label=label, icon=f":material/{cta_icon}:")
        elif on_click is not None:
            st.button(
                label,
                key=key,
                icon=f":material/{cta_icon}:",
                on_click=on_click,
            )
        if extra is not None:
            extra()
