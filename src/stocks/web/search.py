"""Ticker search: the live input, the match ranking and the result rows.

The top bar's dropdown is the app's only ticker picker. It ranks the
watchlist by symbol, name and tag group first, then coins, funds, the SEC
company map and a worldwide Yahoo lookup, with an "Analyze <SYMBOL>" escape
hatch for anything it still doesn't know. Selecting a row writes the shared
"picker_selected" session key that every page reads.

The input itself is a CCv2 component because `st.text_input` only reruns on
Enter/blur, which cannot drive an as-you-type dropdown.
"""

from __future__ import annotations

import re

import streamlit as st

from stocks.config import load_watchlist
from stocks.fuzzy import FUZZY_CUTOFF, MIN_QUERY, fuzzy_ratio
from stocks.web import auth
from stocks.web import css as css_util
from stocks.web.ds import (
    is_mobile,
)
from stocks.web.i18n import t as tr
from stocks.web.logos import logo
from stocks.web.markup import slug
from stocks.web.portfolio_data import db_mtime, held_tickers

# Live search input (CCv2). Streamlit's st.text_input only reruns on Enter/blur,
# so it can't drive an as-you-type dropdown. This tiny bidirectional component
# streams the field's value to Python on every keystroke (debounced ~160ms) via
# setStateValue; Python echoes it back through `data` so the cursor is never
# fought. Styled in its own shadow root to match the old field.
#
# Registered on first mount, NOT at import: server.py imports this module at
# ASGI boot (via landing_static) before the Streamlit runtime exists, and a
# registration made then lands in a throwaway local manager — every later
# mount would raise "Component 'topstocks_live_search' is not registered".
# The first mount always happens inside a script run, where the runtime's
# registry is live; cached so the name is registered once per process.
# Magnifier glyph for the collapsed mobile search state (spliced into the
# component CSS below — CSS url() forbids line breaks, hence one long
# line). var() can't reach inside a data URI; the stroke is TEXT_MUTED.
_SEARCH_GLYPH_URI = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23827F8C' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'/%3E%3C/svg%3E"  # noqa: E501
_SEARCH_GLYPH_CSS = f'background-image: url("{_SEARCH_GLYPH_URI}");'
_LIVE_SEARCH = None


def _live_search_component():
    global _LIVE_SEARCH
    if _LIVE_SEARCH is not None:
        return _LIVE_SEARCH
    _LIVE_SEARCH = st.components.v2.component(
        "topstocks_live_search",
        html=(
            '<div class="lsw">'
            '<input id="q" class="lsi" type="text"'
            ' autocomplete="off" spellcheck="false" />'
            '<span id="spin" class="lss"></span>'
            "</div>"
        ),
        css="""
    .lsw { position: relative; display: block; }
    /* Busy dot on the field's right edge, shown from the keystroke until
       Python echoes that exact value back (see the JS ack below). The gutter
       is reserved permanently so the query text never jumps when it appears. */
    .lss {
      position: absolute; right: 10px; top: 50%; width: 14px; height: 14px;
      margin-top: -7px; box-sizing: border-box; border-radius: 50%;
      border: 2px solid var(--ag-border);
      border-top-color: var(--ag-brand-accent);
      opacity: 0; transition: opacity 120ms ease; pointer-events: none;
    }
    .lss.on { opacity: 1; animation: lsspin 0.7s linear infinite; }
    @keyframes lsspin { to { transform: rotate(360deg); } }
    .lsi {
      width: 100%; box-sizing: border-box; height: 36px;
      padding: 0 1.75rem 0 0.75rem;
      /* height set again below for phones — 44px DS touch target */
      background: var(--ag-surface-card); color: var(--ag-text-primary);
      border: 1px solid var(--ag-border);
      border-radius: var(--ag-radius-sm); font-size: var(--ag-fs-sm);
      outline: none;
    }
    .lsi::placeholder { color: var(--ag-text-muted); }
    .lsi:focus { border-color: var(--ag-brand-accent); }
    @media (max-width: 640px) {
      .lsi { height: 44px; }
      /* Collapsed 44px icon state (host width is set by the page CSS):
         magnifier glyph, no visible text until focus expands the field.
         var() can't reach inside a data URI — stroke is TEXT_MUTED. */
      .lsi:not(:focus):placeholder-shown {
        color: transparent;
        /* Drop the spinner gutter while collapsed: background-position centers
           on the PADDING box, so the asymmetric padding would sit the
           magnifier 8px left of the 44px button's middle. The field is empty
           in this state, so there is no text to shift. */
        padding: 0;
        /*SEARCH-GLYPH*/
        background-repeat: no-repeat;
        background-position: center;
        background-size: 18px 18px;
      }
      .lsi:not(:focus):placeholder-shown::placeholder { color: transparent; }
    }
    """.replace("/*SEARCH-GLYPH*/", _SEARCH_GLYPH_CSS),
        js="""
export default function (component) {
  const { parentElement, data, setStateValue } = component
  const input = parentElement.querySelector("#q")
  if (!input) return
  // Busy state = "the field shows a query Python has not answered yet". Set on
  // the keystroke itself (before the debounce even fires) and cleared only by
  // the ack below, so the whole dead window — debounce, a keystroke rerun that
  // died behind a full app run, every 250-800ms retry — is visibly loading
  // instead of looking like the field ate the query.
  const spin = parentElement.querySelector("#spin")
  const busy = (on) => spin && spin.classList.toggle("on", !!on)
  input.placeholder = (data && data.placeholder) || ""
  const nextValue = (data && data.value) ?? ""
  // Only overwrite the field when the user isn't typing in it — a render whose
  // run started before the last keystroke echoes the stale value and would
  // wipe the in-progress query.
  if (input.value !== nextValue && !input.matches(":focus")) input.value = nextValue
  if (input.value === nextValue) {
    // Python echoed exactly what the field shows: the keystroke landed, so
    // stop re-asserting it. The results (or the server-side "searching" row
    // this same run draws under the field) take over the feedback from here.
    clearTimeout(input._retry)
    input._retryN = 0
    busy(false)
  } else {
    busy(input.value.trim().length > 0)
  }
  if (data && data.blur) {
    // A row click navigated. Clearing only the DOM input is not enough: the
    // frontend widget manager re-sends its stored "value" with every rerun,
    // so the old query would resurrect the dropdown. Sync the clear into
    // widget state and drop any pending debounce/retry still holding it.
    clearTimeout(input._timer)
    clearTimeout(input._retry)
    input._retryN = 0
    busy(false)
    input.value = ""
    setStateValue("value", "")
    setStateValue("focused", false)
    input.blur()
  }
  if (!input.dataset.wired) {
    input.dataset.wired = "1"
    // A keystroke's fragment-rerun request dies when a full app run is in
    // flight (the run cleared the fragment ids, or fastReruns replaced the
    // ScriptRunner holding the queued request) — the value never reaches
    // Python and the dropdown stays closed even though the field shows the
    // query. Re-assert until a render echoes it back (the ack above); bumping
    // "nonce" defeats same-value dedup so each retry still forces a rerun.
    // Backing off 250ms → 800ms over 14 tries (~9s total) still outlasts the
    // slowest throttled-Yahoo page run, but recovers in a quarter of a second
    // when the blocking run was short — the flat 800ms made every miss feel
    // like a dead field.
    const send = (v) => {
      setStateValue("value", v)
      clearTimeout(input._retry)
      if ((input._retryN = (input._retryN || 0) + 1) > 14) return
      const wait = Math.min(800, 250 * Math.pow(1.25, input._retryN - 1))
      input._retry = setTimeout(() => {
        setStateValue("nonce", (input._nonce = (input._nonce || 0) + 1))
        send(input.value)
      }, wait)
    }
    input.addEventListener("input", (e) => {
      clearTimeout(input._timer)
      input._retryN = 0
      const v = e.target.value
      busy(v.trim().length > 0)
      input._timer = setTimeout(() => send(v), 160)
    })
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(input._timer); input._retryN = 0
        busy(e.target.value.trim().length > 0); send(e.target.value)
      }
    })
    // Report focus so Python can show recent searches on an empty, focused
    // field. Blur is delayed so a click on a dropdown row (a Streamlit button
    // in the parent document, outside this shadow root) registers before the
    // rerun that hides the list would tear the button out from under it.
    input.addEventListener("focus", () => {
      clearTimeout(input._blurTimer)
      setStateValue("focused", true)
    })
    input.addEventListener("blur", () => {
      clearTimeout(input._blurTimer)
      input._blurTimer = setTimeout(() => setStateValue("focused", false), 200)
    })
  }
  // Close the dropdown the instant a row is clicked. The server-side close
  // only lands after the app rerun + ticker page load (seconds), so the stale
  // list would linger on screen the whole time. Listen in the bubble phase so
  // Streamlit's own button handler dispatches its click event FIRST — hiding
  // before that would race the navigation. Re-attached every render so the
  // closure always points at the live input/setStateValue.
  const doc = input.ownerDocument
  if (doc.__lsRowCloser) doc.removeEventListener("click", doc.__lsRowCloser)
  doc.__lsRowCloser = (e) => {
    // The container's key carries a generation counter (see _go_ticker), so
    // match on the prefix and take whichever one holds the clicked row.
    const results = [...doc.querySelectorAll('[class*="st-key-topbar_results"]')]
      .find((el) => el.contains(e.target))
    if (!results) return
    clearTimeout(input._timer)
    clearTimeout(input._retry)
    input._retryN = 0
    clearTimeout(input._blurTimer)
    results.style.display = "none"
    input.value = ""
    setStateValue("value", "")
    // The row's mousedown already blurred the field, so the blur listener's
    // pending focused=false was the only one — and the clearTimeout above just
    // killed it; input.blur() re-fires nothing. Say it outright, or "focused"
    // stays true and the recents dropdown re-opens on the page we land on.
    setStateValue("focused", false)
    input.blur()
  }
  doc.addEventListener("click", doc.__lsRowCloser)
}
""",
    )
    return _LIVE_SEARCH


def _live_search_input(*, key: str, placeholder: str) -> tuple[str, bool]:
    """Mount the live-search field; return its (stripped value, focused?)."""
    state = st.session_state.get(key)
    value = state.get("value", "") if isinstance(state, dict) else ""
    focused = bool(state.get("focused")) if isinstance(state, dict) else False
    blur = bool(st.session_state.pop(f"{key}_blur", False))
    if blur:
        # The frontend may re-send the old query with this rerun, resurrecting
        # it in session state; echo an empty value so the JS clears the field.
        value = ""
    result = _live_search_component()(
        key=key,
        data={"value": value, "placeholder": placeholder, "blur": blur},
        width="stretch",
        on_value_change=lambda: None,
        on_focused_change=lambda: None,
        # "nonce" exists only to force a rerun: the JS bumps it when it retries
        # a keystroke the server never acked (same "value" would be deduped).
        on_nonce_change=lambda: None,
    )
    if blur:
        # Just navigated from a row click: the browser input still reports focus
        # (blur is debounced), which would re-open the recents dropdown. Force it
        # closed for this render; the JS above drops the real focus to match.
        return "", False
    return (
        (getattr(result, "value", "") or "").strip(),
        bool(getattr(result, "focused", focused)),
    )


def _fuzzy_order(
    q: str,
    tickers: list[str],
    labels: dict[str, str],
    tag_map: dict[str, tuple],
) -> list[str]:
    """Tickers whose symbol, name or tag fuzzy-matches `q`, best score first.

    Typo fallback for the top-bar dropdown search;
    call it only after exact substring matching came up empty.
    """
    if len(q) < MIN_QUERY:
        return []
    scored = []
    for i, t in enumerate(tickers):  # ties keep list order (favorites first)
        score = max(
            fuzzy_ratio(q, t.upper()),
            fuzzy_ratio(q, labels[t].upper()),
            *(fuzzy_ratio(q, tag.upper()) for tag in tag_map.get(t, ())),
        )
        if score >= FUZZY_CUTOFF:
            scored.append((-score, i, t))
    return [t for _, _, t in sorted(scored)]


def _topbar_matches(raw: str):
    """Picker-parity search for the top-bar dropdown.

    The watchlist is matched by symbol, company name OR tag-group (favorites
    first, and open-but-unlisted positions from the ledger folded in), then
    coins, then the SEC ticker map,
    then a worldwide Yahoo lookup for everything the US-only map can't see,
    plus an "Analyze <SYMBOL>" fallback for a symbol none of them know. Returns
    `(watch, crypto, funds, sec, world, analyze)` where watch rows carry their
    star/briefcase mark and world rows carry their exchange.
    """
    q = raw.strip().upper()
    if not q:
        return [], [], [], [], [], None
    holdings = load_watchlist(auth.watchlist_path())
    labels = {h.ticker: (h.name or h.ticker) for h in holdings}
    fav_set = {h.ticker for h in holdings if h.favorite}
    tag_map = {h.ticker: h.tags for h in holdings if h.tags}
    db = str(auth.db_path())
    held_set = set(held_tickers(db, db_mtime(db)))
    for t in sorted(held_set - set(labels)):
        labels[t] = sec_title(t) or t
    order = [t for t in labels if t in fav_set] + [t for t in labels if t not in fav_set]

    watch: list[tuple[str, str, str]] = []
    for t in order:
        if (
            q in t.upper()
            or q in labels[t].upper()
            or any(q in tag.upper() for tag in tag_map.get(t, ()))
        ):
            mark = (
                ":material/star:" if t in fav_set
                else (":material/work:" if t in held_set else "")
            )
            watch.append((t, labels[t], mark))
    if not watch:
        # Typo fallback ("oracel"): fuzzy over the same fields, best first.
        # Only when exact substring found nothing, so it never dilutes results.
        for t in _fuzzy_order(q, order, labels, tag_map):
            mark = (
                ":material/star:" if t in fav_set
                else (":material/work:" if t in held_set else "")
            )
            watch.append((t, labels[t], mark))

    from stocks.data.crypto import search_crypto
    from stocks.data.funds import search_funds

    crypto = [(t, n) for t, n in search_crypto(q) if t not in labels]
    # The fund catalog is local, so this tier is the one that still answers
    # "where is my ETF" while Yahoo has the deploy's egress IP in timeout.
    funds = [(t, n) for t, n in search_funds(q) if t not in labels]
    sec = [(t, n) for t, n in sec_matches(q) if t not in labels]
    # Worldwide runs on every query, not just when the tiers above came up
    # empty: their fuzzy fallbacks always produce SOMETHING, so "nothing found
    # locally" is not a usable trigger — "MIPS" pulls VIPS/CMPS/MVIS out of the
    # SEC map and would have suppressed the one real answer (MIPS.ST). It is
    # deduped against them instead, and `_world_first` decides which of the two
    # groups leads.
    seen = (
        set(labels)
        | {t for t, _ in crypto}
        | {t for t, _ in funds}
        | {t for t, _ in sec}
    )
    world = [(t, n, x) for t, n, x in world_matches(q) if t not in seen]
    known = seen | {t for t, _, _ in world}
    analyze = q if (q not in known and re.fullmatch(r"[A-Z0-9.\-]{1,12}", q)) else None
    return watch[:8], crypto[:4], funds[:4], sec[:6], world[:3], analyze


def _recent_rows() -> list[tuple[str, str, str]]:
    """Recently explored tickers as `(symbol, name, mark)`, newest first.

    Names/marks are resolved from the current watchlist + ledger like the
    live matches, so a recent row looks identical to its search-result twin;
    a symbol no longer in either just shows bare.
    """
    recents = auth.load_recent_searches()
    if not recents:
        return []
    holdings = load_watchlist(auth.watchlist_path())
    labels = {h.ticker: (h.name or h.ticker) for h in holdings}
    fav_set = {h.ticker for h in holdings if h.favorite}
    db = str(auth.db_path())
    held_set = set(held_tickers(db, db_mtime(db)))
    rows = []
    for t in recents:
        name = labels.get(t) or (sec_title(t) if t in held_set else None) or ""
        mark = (
                ":material/star:" if t in fav_set
                else (":material/work:" if t in held_set else "")
            )
        rows.append((t, name if name != t else "", mark))
    return rows


def _go_ticker(ticker: str) -> None:
    """Navigate to a ticker from the top-bar dropdown.

    Reuses the picker's contract — set the shared selection and raise
    "picker_clicked" so app.py switches to the Ticker page on the rerun — then
    clear the query so the dropdown closes.
    """
    st.session_state.pop("topbar_q", None)  # reset the live input (its state is a dict)
    # The closer JS hides the open dropdown with an INLINE display:none (the
    # server-side close lands seconds later). Streamlit reuses a keyed
    # container's DOM node, and React never clears a style it didn't set, so
    # reusing that node for the next query renders the rows invisible. Bump the
    # generation: the next dropdown gets a new key, hence a new node.
    st.session_state["topbar_res_gen"] = st.session_state.get("topbar_res_gen", 0) + 1
    st.session_state["topbar_q_blur"] = True  # blur the field so recents don't re-open
    auth.push_recent_search(ticker)  # remember it for the empty-field dropdown
    st.session_state["picker_selected"] = ticker.strip().upper()
    st.session_state["picker_clicked"] = True


def _results_key() -> str:
    """Key for the dropdown container, carrying the generation counter.

    A row click hides the open dropdown from JS with an inline style; keying the
    container per generation guarantees the next dropdown is a brand-new DOM
    node, never the hidden one. CSS/JS match it with `[class*=...]`.
    """
    return f"topbar_results_{st.session_state.get('topbar_res_gen', 0)}"


def _search_row(t: str, label: str, key: str) -> None:
    """One dropdown row: a full-width button that navigates to ticker `t`."""
    st.button(label, key=key, on_click=_go_ticker, args=(t,), width="stretch")


def inline_style(css: str) -> None:
    """Emit bare CSS in the page flow — a thin alias for `css_util.inject`.

    Kept as its own name because the callers below read better with it: rules
    computed per rerun (the dropdown's logo backgrounds, the collapsed mobile
    field) rather than a page-level stylesheet. See `stocks.web.css` for why
    nothing here may reach `st.html()` as style-only content.
    """
    css_util.inject(css)


def _render_ticker_rows(
    rows: list[tuple[str, str, str]], *, key_prefix: str = "tbres"
) -> None:
    """Render `(symbol, name, mark)` rows as logo'd buttons in the dropdown.

    Each carries its watchlist logo (CSS background, like the picker) and its
    star/briefcase mark. Logo rules are scoped under the results container so
    they beat the base row rule's specificity, and every row rule sets
    `background-color` only — the `background` shorthand resets
    background-image and wiped the logo (base rule, then hover once the base
    was scoped: same specificity as this rule, so source order decided).
    """
    logo_rules = [
        f'[class*="st-key-topbar_results"] .st-key-{key_prefix}_{slug(t)} button {{'
        f'background-image:url("{src}"); background-repeat:no-repeat;'
        " background-position:8px center; background-size:16px 16px;"
        " padding-left:30px;}"
        for t, _, _ in rows
        if (src := logo(t))
    ]
    if logo_rules:
        inline_style("".join(logo_rules))
    for t, name, mark in rows:
        pre = f"{mark} " if mark else ""
        tail = f"  {name}" if name and name != t else ""
        _search_row(t, f"{pre}**{t}**{tail}", f"{key_prefix}_{slug(t)}")


@st.fragment
def topbar_search_panel() -> None:
    """Live ticker search + autocomplete dropdown, isolated in a fragment.

    Typing streams through the CCv2 field and reruns ONLY this fragment, so the
    dropdown updates as-you-type without re-running the whole page (charts and
    all). A dropdown row click sets the picker flags but — being inside the
    fragment — reruns only the fragment; we then escalate with an app-scoped
    rerun so app.py's switch_page runs. On any full rerun app.py has already
    popped the flag before this renders, so the escalation never double-fires.
    Escalate BEFORE rendering: the intermediate fragment run is discarded by the
    app rerun, so drawing (and consuming the blur flag) here would waste both.
    """
    if st.session_state.get("picker_clicked"):
        st.rerun(scope="app")
    with st.container(key="topbar_search"):
        q, focused = _live_search_input(
            key="topbar_q", placeholder=tr("widgets.search_placeholder")
        )
        if not q and is_mobile():
            # DS mobile header: an empty field is a 44px magnifier button.
            # Emitted here (not in the page stylesheet) because the dropdown
            # below is a child of this container — collapsing the host while
            # results are showing would squash them to 44px. The fragment
            # reruns as-you-type, so the rule lifts on the first keystroke.
            inline_style(
                "@media (max-width: 640px) {"
                ".st-key-topbar_search:not(:focus-within)"
                " { width: 44px !important; } }"
            )
        if q:
            # Typed query: live matches. Recents never show here — searching
            # something else replaces them (they only stand in for an empty field).
            #
            # The panel opens BEFORE the matches are known. The last tier is a
            # network round-trip (worldwide symbols, up to a 6s timeout on a
            # cold query), so computing first and rendering after left the
            # field looking inert for seconds — the field's own busy dot is
            # already gone by then, cleared by this run's echo. A "searching"
            # row goes into the open panel and is replaced in place by the
            # rows: Streamlit flushes deltas as they are produced, so it
            # reaches the browser while the tier is still running.
            with st.container(key=_results_key()):
                pending = st.empty()
                with pending.container(key="topbar_pending"):
                    st.caption(tr("widgets.searching"))
                watch, crypto, funds, sec, world, analyze = _topbar_matches(q)
                pending.empty()
                if not (watch or crypto or funds or sec or world or analyze):
                    # Never leave the panel blank: an empty bordered box reads
                    # as "still working", which is what this whole path fixes.
                    st.caption(tr("widgets.no_results"))
                _render_ticker_rows(watch)
                if crypto:
                    st.caption(tr("widgets.crypto"))
                    for t, name in crypto:
                        _search_row(t, f"🪙 **{t}**  {name}", f"tbrescx_{slug(t)}")
                if funds:
                    st.caption(tr("widgets.funds"))
                    for t, name in funds:
                        _search_row(t, f"🧺 **{t}**  {name}", f"tbresfd_{slug(t)}")

                def _world_group() -> None:
                    if world:
                        st.caption(tr("widgets.from_world_search"))
                        for t, name, exch in world:
                            _search_row(
                                t, _world_label(t, name, exch), f"tbresw_{slug(t)}"
                            )

                def _sec_group() -> None:
                    if sec:
                        st.caption(tr("widgets.from_sec_search"))
                        for t, name in sec:
                            _search_row(t, f"🔎 **{t}**  {name}", f"tbressec_{slug(t)}")

                # Whichever of the two searched the query better goes first.
                groups = (_world_group, _sec_group)
                if not _world_first(q.strip().upper(), sec):
                    groups = groups[::-1]
                for group in groups:
                    group()
                if analyze:
                    st.button(
                        tr("widgets.analyze", q=analyze),
                        key="tbres_analyze",
                        on_click=_go_ticker,
                        args=(analyze,),
                        width="stretch",
                        type="primary",
                    )
        elif focused and (recent := _recent_rows()):
            # Empty but focused: offer the last few explored tickers.
            with st.container(key=_results_key()):
                st.caption(tr("widgets.recent"))
                _render_ticker_rows(recent, key_prefix="tbrec")



@st.cache_data(ttl=86400, show_spinner=False)
def sec_title(ticker: str) -> str | None:
    """Company name for a held-but-unlisted ticker, from the SEC map.

    Without it those rows carry the bare symbol, so a name query ("oracle")
    can't find an imported ORCL position — and the `t not in labels` dedup
    then drops the SEC result too, leaving only the Analyze fallback. None
    (cached, so an offline miss isn't retried per rerun) for non-US symbols.
    """
    from stocks.data.edgar import title_for

    try:
        return title_for(ticker)
    except Exception:
        return None


@st.cache_data(ttl=86400, show_spinner=False)
def sec_matches(query: str) -> list[tuple[str, str]]:
    """SEC ticker-map search (symbol or company name) behind the search box.

    Pure in-memory scan of the cached map, but memoised per query anyway so
    reruns while typing don't rescan 10k rows. Empty when the map has never
    been cached and the network is down — search degrades, the app survives.
    """
    from stocks.data.edgar import search_companies

    try:
        return search_companies(query, limit=6)
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def world_matches(query: str) -> list[tuple[str, str, str]]:
    """Worldwide symbol search (Yahoo), the last tier of the search box.

    The tiers above it are all local tables and all partial — the watchlist,
    the coin list, and the SEC map's US filers — so a foreign listing had no
    way to be found by name. This one covers every venue Yahoo quotes and is
    typo-tolerant, which is what makes "mips" reach MIPS.ST instead of falling
    through to an Analyze button for a symbol Yahoo has no data for.

    Unlike its siblings this is a network round-trip, so it is cached for an
    hour per query (listings barely move) and hard-capped inside
    `search_symbols` by a short timeout plus a cooldown after any failure.
    """
    from stocks.data.symbols import search_symbols

    try:
        return search_symbols(query, limit=6)
    except Exception:
        return []


# How close a SEC company name must be to the query to count as "nailed it".
# Above the general FUZZY_CUTOFF: this decides which group leads, so it should
# admit a typo ("SANDISC" vs "SANDISK" .86) but not a near-miss neighbour
# ("MIPS" vs "VIPSHOP" .55, "IWDA" vs "IDEA" .75).
STRONG_MATCH = 0.8


def _norm_name(s: str) -> str:
    return "".join(c for c in s.upper() if c.isalnum())


def _world_first(q: str, sec: list[tuple[str, str]]) -> bool:
    """Whether the worldwide group should render above the SEC group.

    The SEC tier degrades as it goes: after its exact and prefix hits it falls
    back to substrings and then to fuzz, so "MIPS" answers with VIPS, CMPS and
    MVIS — six wrong US tickers that would bury the one real match (MIPS.ST).
    It keeps the top slot only when it actually nailed the query.

    "Nailed it" is an exact symbol, a company name starting with the query, or
    a company name whose OPENING words are a near-match for it. Only the
    opening words, because the query being buried anywhere in a longer name
    proves nothing — "hermes" scores .92 against "Federated Hermes, Inc." and
    would hand the lead to an asset manager over Hermès itself. Matching word
    for word from the start instead keeps "sandisc" on Sandisk Corp and
    "nvidia" on Nvidia Corp (above the leveraged NVDA ETFs Yahoo returns),
    while "bank of amrica" still lands on BANK OF AMERICA CORP.
    """
    key = _norm_name(q)
    for t, n in sec:
        if t == q or (key and _norm_name(n).startswith(key)):
            return False
        words = re.sub(r"[^A-Z0-9 ]", " ", n.upper()).split()
        head = " ".join(words[: len(q.split())])
        if head and fuzzy_ratio(q, head) >= STRONG_MATCH:
            return False
    return True


def _world_label(t: str, name: str, exch: str) -> str:
    """Dropdown label for a worldwide hit: 🌐 SYMBOL  Name · Exchange.

    The exchange is what disambiguates this tier — several rows can be the
    same brand on different venues, and it is also the hint that the symbol
    is foreign (MIPS.ST · Stockholm). Long legal names ("Hermès International
    Société en commandite par actions") are clipped so the row stays one line.
    """
    short = name if len(name) <= 34 else name[:33].rstrip() + "…"
    tail = f"{short} · {exch}" if exch else short
    return f"🌐 **{t}**  {tail}"
