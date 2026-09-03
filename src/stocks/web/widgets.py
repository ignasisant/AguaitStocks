"""Shared Streamlit widgets — and the facade over the modules they moved to.

This file was 2,700 lines: design tokens, logos, tables, search, the top bar
and the ticker action row, all in one place because they all started as "some
markup a page needed". They now live apart —

* `stocks.web.ds` — tokens, chart chrome, the CSS they emit
* `stocks.web.logos` — logos and display names
* `stocks.web.tables` — HTML tables, KPI grids, cell formatters
* `stocks.web.search` — the ticker picker
* `stocks.web.nav` — top bar and phone tab bar
* `stocks.web.portfolio_data` — the ledger reads the UI caches

— and every name is re-exported here, so the ~40 call sites that say
`from stocks.web.widgets import ...` keep working and the split stays an
implementation detail. New code should import from the module that owns the
thing; this facade exists so that migration never had to be a flag day.

What is still defined here is what has no better home: the per-ticker action
row (favorite, tags, alerts) and the picker's opening selection.
"""

from __future__ import annotations

import streamlit as st

from stocks.config import Alert, load_watchlist
from stocks.web import auth
from stocks.web.ds import (  # noqa: F401 — facade re-export
    ACCENT_AREA,
    ACCENT_BAND,
    BAR_RADIUS,
    BORDER,
    BORDER_BRAND_BAND,
    BORDER_FOCUS,
    BRAND_ACCENT,
    BRAND_CTA,
    BRAND_GOOGLE_TILE,
    CAL_DENSITIES,
    CANDLE_DOWN,
    CANDLE_UP,
    CATEGORICAL_COLORS,
    CHART_MAGENTA,
    CRITICAL_FILL,
    CTA_GLOW,
    CTA_HALO,
    CTA_TINT,
    CTA_TINT_EDGE,
    DIVERGING_SCALE,
    DOWN_COLOR,
    DOWN_FILL,
    EVENT_LINE,
    FS_2XL,
    FS_2XS,
    FS_3XL,
    FS_BASE,
    FS_DISPLAY,
    FS_LG,
    FS_MD,
    FS_SM,
    FS_XL,
    FS_XS,
    HOVER_FONT_DESKTOP,
    HOVER_FONT_MOBILE,
    HOVERLABEL,
    ICON_NAV,
    INFO_COLOR,
    INFO_DEEP,
    LANDING_DOWN,
    LANDING_INFO,
    LANDING_UP,
    LANDING_WARN,
    LOSS_BAND,
    LOSS_COLOR,
    LOSS_COLOR_MUTED,
    ON_BRAND,
    PLOTLY_CONFIG,
    PROFIT_BAND,
    PROFIT_COLOR,
    PROFIT_COLOR_MUTED,
    PURPLE_300,
    PURPLE_400,
    PURPLE_700,
    PURPLE_800,
    PURPLE_900,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_NAV,
    RADIUS_PILL,
    RADIUS_SM,
    RADIUS_XS,
    RULE_SOFT,
    SEQUENTIAL_SCALE,
    SHADOW_CARD,
    SHADOW_COLOR,
    SHADOW_COLOR_STRONG,
    SHADOW_HOVER,
    SHADOW_OVERLAY,
    SKELETON_BASE,
    SKELETON_HI,
    SMA_FAST,
    SMA_SLOW,
    SUCCESS_FILL,
    SURFACE_BAND,
    SURFACE_BRAND_BAND,
    SURFACE_CARD,
    SURFACE_HOVER,
    SURFACE_PAGE,
    SURFACE_PAGE_HAZE,
    SURFACE_PAGE_VEIL,
    SURFACE_RAISED,
    SURFACE_SUNKEN,
    TEXT_FAINT,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TICK_FONT_MOBILE,
    TRANSPARENT,
    UP_COLOR,
    WARN_BAND,
    WARN_COLOR,
    WARN_ORANGE,
    calendar_css,
    chart_layout,
    ds_vars_css,
    hover_wrap,
    is_mobile,
    metric_cells,
    show_chart,
    viewer_tz,
)
from stocks.web.i18n import t as tr
from stocks.web.logos import (  # noqa: F401 — facade re-export
    BROKER_UNKNOWN,
    asset_logo,
    brand_logo,
    broker_chips_html,
    broker_name,
    company_name,
    logo,
)
from stocks.web.markup import slug
from stocks.web.nav import (  # noqa: F401 — facade re-export
    render_bottom_nav,
    render_topbar,
)
from stocks.web.portfolio_data import (  # noqa: F401 — facade re-export
    basket_history,
    custody_map,
    db_mtime,
    enriched_positions,
    eur_spot,
    held_tickers,
    last_session_moves,
    last_session_quote,
    ledger_history,
    ledger_state,
    native_base_rates,
    positions_table,
    recent_closes,
    trade_bars,
)
from stocks.web.search import (  # noqa: F401 — facade re-export
    FUZZY_CUTOFF,
    MIN_QUERY,
    STRONG_MATCH,
    inline_style,
    sec_matches,
    sec_title,
    topbar_search_panel,
    world_matches,
)
from stocks.web.tables import (  # noqa: F401 — facade re-export
    data_table,
    kpi_delta_chip,
    kpi_grid_html,
    responsive_ticker_table_html,
    signed_color,
    stacked_table_html,
    ticker_cell,
    ticker_pill_md,
    ticker_table_html,
)


def seed_selection() -> str | None:
    """Seed the shared "picker_selected" session key, once per session.

    Ticker navigation lives in the top-bar search and in ?ticker= deep links;
    nothing renders a watchlist list any more. The Ticker page still wants a
    sensible default on a cold session, so pick the first favorite, else the
    first watchlist entry, else a held-but-unlisted symbol from the ledger.
    Returns the selection (upper-cased) or None when there is nothing to show.
    """
    sel = st.session_state.get("picker_selected")
    if not sel:
        holdings = load_watchlist(auth.watchlist_path())
        order = [h.ticker for h in holdings if h.favorite]
        order += [h.ticker for h in holdings if not h.favorite]
        if not order:
            db = str(auth.db_path())
            order = sorted(held_tickers(db, db_mtime(db)))
        if order:
            st.session_state["picker_selected"] = sel = order[0]
    return sel.strip().upper() if sel else None


def ticker_actions(ticker: str, *, container=None, key: str = "ticker") -> None:
    """Per-ticker quick actions: favorite star toggle + tag-group editor.

    Both write to this account's watchlist.yaml (auth.toggle_favorite /
    auth.set_tags), creating the entry when the symbol isn't listed yet — so
    favoriting or tagging a custom-analyzed or held-only ticker also adds it
    to the watchlist. Tags are free-form groups ("semis", "EM dividend"…);
    the top-bar search matches them, so typing a tag filters to its group.

    Rendered in the ticker page header; pass `container` to place it
    elsewhere. Writes, so login-gated: anonymous visitors (on the shared
    guest watchlist) get a sign-in button instead of the actions.
    """
    box = container if container is not None else st
    if not auth.is_logged_in():
        if "auth" in st.secrets:
            box.button(
                tr("widgets.sign_in_favorite"),
                key=f"{key}_login_{slug(ticker)}",
                icon=":material/login:",
                on_click=auth.login,
                width="stretch",
            )
        return
    h = next(
        (
            x
            for x in load_watchlist(auth.watchlist_path())
            if x.ticker.upper() == ticker.upper()
        ),
        None,
    )
    fav = bool(h and h.favorite)
    tags = list(h.tags) if h else []
    alerts = list(h.alerts) if h else []
    ms_key = f"{key}_tags_{slug(ticker)}"

    def _toggle_fav() -> None:
        now = auth.toggle_favorite(ticker)
        msg = (
            tr("widgets.toast_fav_added", ticker=ticker)
            if now
            else tr("widgets.toast_fav_removed", ticker=ticker)
        )
        st.toast(msg, icon=":material/star:")

    def _save_tags() -> None:
        auth.set_tags(ticker, st.session_state[ms_key])

    def _tag_editor() -> None:
        st.multiselect(
            tr("widgets.tag_groups"),
            options=auth.all_tags(),
            default=tags,
            key=ms_key,
            accept_new_options=True,
            on_change=_save_tags,
            placeholder=tr("widgets.tags_placeholder"),
            help=tr("widgets.tags_help"),
        )

    # ------------------------------------------------------------- alerts
    # Rules live per-holding in this account's watchlist.yaml (config.Alert);
    # the hourly cron (notify/fanout.py) evaluates them and messages the
    # user's linked Telegram. The editor writes through auth.set_alerts, so a
    # rule on a not-yet-listed ticker also adds it to the watchlist.

    def _alert_to_dict(a: Alert) -> dict:
        return {
            k: v
            for k, v in (
                ("type", a.type), ("price", a.price), ("pct", a.pct),
                ("level", a.level), ("window", a.window),
            )
            if v is not None
        }

    def _alert_summary(a: Alert) -> str:
        parts = [tr(f"widgets.alert_t_{a.type}")]
        if a.price is not None:
            parts.append(f"{a.price:g}")
        if a.pct is not None:
            parts.append(f"{a.pct:g}%")
        if a.level is not None:
            parts.append(f"{a.level:g}")
        if a.window:
            parts.append(f"({a.window}d)")
        return " ".join(parts)

    _ALERT_TYPE_ORDER = ("above", "below", "pct_move", "drawdown", "rsi_below",
                         "rsi_above", "sma_cross", "high_52w", "low_52w")
    _WINDOW_DEFAULTS = {"rsi_below": 14, "rsi_above": 14, "sma_cross": 50,
                        "high_52w": 252, "low_52w": 252}

    def _alert_editor() -> None:
        st.caption(tr("widgets.alerts_caption"))
        if alerts:
            for i, a in enumerate(alerts):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f":small[{_alert_summary(a)}]", width="stretch")
                    if st.button(
                        ":material/delete:",
                        key=f"{key}_al_del_{i}_{slug(ticker)}",
                        help=tr("widgets.alert_removed"),
                    ):
                        auth.set_alerts(
                            ticker,
                            [_alert_to_dict(x) for j, x in enumerate(alerts) if j != i],
                        )
                        st.toast(tr("widgets.alert_removed"),
                                 icon=":material/notifications_off:")
                        st.rerun()
        else:
            st.caption(tr("widgets.alert_none", ticker=ticker))

        atype = st.selectbox(
            tr("widgets.alert_type"),
            _ALERT_TYPE_ORDER,
            format_func=lambda t: tr(f"widgets.alert_t_{t}"),
            key=f"{key}_al_type_{slug(ticker)}",
        )
        entry: dict = {"type": atype}
        fk = f"{key}_al_{atype}_{slug(ticker)}"  # per-type keys: no stale values
        if atype in ("above", "below"):
            entry["price"] = st.number_input(
                tr("widgets.alert_price"), min_value=0.0, key=f"{fk}_price"
            )
        elif atype in ("pct_move", "drawdown"):
            entry["pct"] = st.number_input(
                tr("widgets.alert_pct"), min_value=0.0, value=5.0, key=f"{fk}_pct"
            )
        elif atype in ("rsi_below", "rsi_above"):
            entry["level"] = st.number_input(
                tr("widgets.alert_level"), min_value=0.0, max_value=100.0,
                value=30.0 if atype == "rsi_below" else 70.0, key=f"{fk}_level",
            )
        if atype in _WINDOW_DEFAULTS:
            entry["window"] = int(st.number_input(
                tr("widgets.alert_window"), min_value=2,
                value=_WINDOW_DEFAULTS[atype], key=f"{fk}_window",
            ))

        incomplete = (entry.get("price") == 0.0 and atype in ("above", "below")) or (
            entry.get("pct") == 0.0 and atype in ("pct_move", "drawdown")
        )
        if st.button(
            tr("widgets.alert_add"),
            key=f"{fk}_add",
            icon=":material/notification_add:",
            disabled=incomplete,
        ):
            auth.set_alerts(ticker, [*(_alert_to_dict(a) for a in alerts), entry])
            st.toast(tr("widgets.alert_added", ticker=ticker),
                     icon=":material/notifications_active:")
            st.rerun()

    if is_mobile():
        # Phones: both actions fold into one compact kebab menu that sits inline
        # beside the title, instead of two full-width rows under the header. The
        # favorited state and current tags show once the menu is open.
        with box.popover(":material/more_vert:"):
            st.button(
                tr("widgets.remove_favorite") if fav else tr("widgets.add_favorite"),
                key=f"{key}_fav_{slug(ticker)}",
                icon=":material/star:",
                on_click=_toggle_fav,
                type="primary" if fav else "secondary",
                width="stretch",
            )
            _tag_editor()
            st.divider()
            _alert_editor()
        return

    c1, c2, c3 = box.columns([1, 2, 2], vertical_alignment="center")
    c1.button(
        ":material/star:",
        key=f"{key}_fav_{slug(ticker)}",
        on_click=_toggle_fav,
        help=tr("widgets.remove_favorite") if fav else tr("widgets.add_favorite"),
        # Primary fill marks the favorited state (label is the same star).
        type="primary" if fav else "secondary",
        width="stretch",
    )

    with c2.popover(f":material/label: {tr('widgets.tags')}", width="stretch"):
        _tag_editor()

    with c3.popover(
        f":material/notifications: {tr('widgets.alerts')}"
        + (f" ({len(alerts)})" if alerts else ""),
        width="stretch",
    ):
        _alert_editor()

    if tags:
        box.markdown(" ".join(f":gray-badge[{t}]" for t in tags))
