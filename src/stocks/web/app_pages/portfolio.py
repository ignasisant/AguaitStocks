"""Portfolio page — driven entirely by the transaction ledger (data/portfolio.db).

Upload a Revolut CSV on the Import page first; everything here derives from
those transactions via FIFO: open positions & P/L, allocation & risk, realized
gains & Spanish tax, and dividends. Weights are put on an EUR footing so a
multi-currency book (USD + EUR) is measured consistently.

Tabs are dynamic (on_change="rerun"): only the visible tab's content runs, so
the price/profile fetch behind "Allocation & risk" doesn't block the others.
The Positions tab's two sections are parallel fragments, so the live-price
table and the ledger-history chart load concurrently on a full rerun.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from stocks.analysis.portfolio import (
    analyze,
    basket_change,
    correlation_matrix,
    cumulative_returns,
    effective_positions,
    holdings_from_positions,
    market_live,
    market_value_weights_eur,
    portfolio_returns,
    top_n_weight,
    us_market_open,
)
from stocks.portfolio import dividends
from stocks.portfolio.tax_es import fiscal_year, modelo_720_flag
from stocks.web import auth
from stocks.web.i18n import t as tr
from stocks.web.portfolio_data import (
    basket_history,
    enriched_positions,
    eur_spot,
    ledger_history,
    ledger_state,
    positions_table,
)
from stocks.web.widgets import (
    LOSS_COLOR,
    PROFIT_COLOR,
    TEXT_MUTED,
    chart_layout,
    db_mtime,
    is_mobile,
    metric_cells,
    show_chart,
    ticker_table_html,
)

_MOBILE = is_mobile()

# Everything below derives from the personal transaction ledger.
auth.require_login()

st.title(tr("nav.portfolio"))

# This session user's ledger; with its mtime it keys every ledger-derived
# cache (web/portfolio_data.py) so concurrent users never read each other's
# book and entries stay hot until the next import.
DB = str(auth.db_path())

txs, positions, realized = ledger_state(DB, db_mtime(DB))
if not txs:
    st.warning(tr("portfolio.no_transactions"))
    st.stop()
if not positions and not realized:
    st.warning(tr("portfolio.ledger_no_positions"))
    st.stop()

tab_pos, tab_risk, tab_tax, tab_div = st.tabs(
    [tr("portfolio.tab_positions"), tr("portfolio.tab_alloc_risk"),
     tr("portfolio.tab_realized_tax"), tr("portfolio.tab_dividends")],
    on_change="rerun",
)


# ------------------------------------------------------------------- Positions
# Both sections are parallel fragments: a full rerun dispatches each to a
# thread pool, so the live-price table and the ledger-history chart build
# concurrently instead of back to back.
@st.fragment(parallel=True)
def _positions_section() -> None:
    st.subheader(tr("portfolio.open_positions_pl"))
    # Shared loader (web/portfolio_data.py) — already weight/day-enriched and
    # weight-sorted; Home shows the same frame, so the price burst is shared.
    tbl = enriched_positions(DB, db_mtime(DB))
    if tbl.empty:
        st.caption(tr("portfolio.no_open_positions"))
    else:
        cost = tbl["cost_eur"].sum()
        value = tbl["value_eur"].dropna().sum()
        # Headline figures honor the profile's display-currency preference
        # (spot-converted); the table and tax tabs stay EUR.
        ccy = auth.display_currency()
        fx = 1.0 if ccy == "EUR" else eur_spot(ccy)
        if fx is None:
            ccy, fx = "EUR", 1.0
        sym = auth.CURRENCY_SYMBOL[ccy]
        c1, c2, c3, c4 = metric_cells(4)
        c1.metric(tr("portfolio.cost_basis"), f"{sym}{cost * fx:,.0f}")
        c2.metric(tr("portfolio.market_value"), f"{sym}{value * fx:,.0f}")
        c3.metric(
            tr("portfolio.unrealised_pl"),
            f"{sym}{(value - cost) * fx:,.0f}",
            f"{(value / cost - 1) * 100:+.1f}%",
        )
        realized_gain = sum(s.gain_eur for s in realized)
        realized_cost = sum(s.cost_eur for s in realized)
        c4.metric(
            tr("portfolio.realised_pl"),
            f"{sym}{realized_gain * fx:,.0f}",
            f"{realized_gain / realized_cost * 100:+.1f}%" if realized_cost else None,
            help=tr("portfolio.realised_pl_help"),
        )
        if ccy != "EUR":
            st.caption(tr("portfolio.headline_converted", ccy=ccy))

        vals = basket_history(DB, db_mtime(DB))
        # Market closed → "Today" is the last completed session's move. Sum the
        # per-row day_eur (already overridden to the last-session move) instead
        # of the basket's close-to-close, which can be a flat premarket 0%; grey
        # its delta ("off") to flag it isn't live.
        mkt_open = us_market_open()
        today_closed = None
        if not mkt_open:
            d_eur = tbl["day_eur"].dropna().sum()
            base = value - d_eur
            today_closed = (d_eur, d_eur / base if base else 0.0)
        d1, d2, d3 = metric_cells(3)
        for col, label, days in (
            (d1, tr("portfolio.today"), 1),
            (d2, tr("portfolio.one_week"), 7),
            (d3, tr("portfolio.one_month"), 30),
        ):
            chg = today_closed if days == 1 and today_closed else basket_change(vals, days)
            if chg is None:
                col.metric(label, tr("portfolio.na"))
            else:
                col.metric(
                    label,
                    f"{sym}{chg[0] * fx:+,.0f}",
                    f"{chg[1]:+.2%}",
                    delta_color="off" if days == 1 and not mkt_open else "normal",
                )
        if not mkt_open:
            st.caption(tr("portfolio.market_closed_note"))

        # Shared Positions-style HTML table (logo+name cells, semantic P/L
        # colors — see widgets.ticker_table_html). Rows come pre-sorted by
        # weight; click-to-sort is the only capability given up.
        tbl.insert(0, "ticker", tbl.index)
        tbl = tbl[
            ["ticker", "shares", "ccy", "cost_eur", "value_eur", "weight",
             "day_eur", "day_pct", "pnl_eur", "pnl_pct"]
        ]

        fmt = {
            "shares": "{:.4f}",
            "cost_eur": "€{:,.0f}",
            "value_eur": "€{:,.0f}",
            "weight": "{:.1%}",
            "day_eur": "€{:+,.0f}",
            "day_pct": "{:+.1%}",
            "pnl_eur": "€{:,.0f}",
            "pnl_pct": "{:+.1%}",
        }
        pnl_cols = ("day_eur", "day_pct", "pnl_eur", "pnl_pct")
        # Off-session rows: dim only the day columns (last-close move, not live);
        # total P/L stays full color. Crypto is 24/7 so it never dims.
        muted = {t for t in tbl["ticker"] if not market_live(t)}
        day_cols = ("day_eur", "day_pct")

        if _MOBILE:
            # Dense Revolut-style rows — value + day% on the right, weight and
            # total P/L in the dim second line; nothing pans horizontally.
            # Full table one expander below.
            st.html(ticker_table_html(
                tbl, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols,
                mobile={"value": "value_eur", "delta": "day_pct",
                        "sub": ("weight", "pnl_pct"),
                        "sub_labels": {"pnl_pct": "P/L"}}))
            with st.expander(tr("portfolio.all_columns")):
                st.html(ticker_table_html(
                    tbl, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols))
        else:
            st.html(ticker_table_html(
                tbl, fmt=fmt, signed=pnl_cols, muted=muted, muted_cols=day_cols))
        st.caption(tr("portfolio.positions_caption"))


@st.fragment(parallel=True)
def _history_section() -> None:
    st.subheader(tr("portfolio.injected_vs_value"))

    hist, _, missing = ledger_history((len(txs), txs[-1].date, date.today()), DB)
    if hist.empty:
        st.caption(tr("portfolio.not_enough_history"))
    else:
        # Filter in Python (not plotly rangeselector) so the y-axis rescales
        # to the visible window.
        SPANS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}
        # "All" is both a display label and the default/comparison key, so bind
        # the translated label to a variable; "YTD" and the SPANS codes stay
        # untranslated (they're compared/keyed against the raw values).
        ALL = tr("portfolio.range_all")
        sel = st.segmented_control(
            tr("portfolio.range"),
            [*SPANS, "YTD", ALL],
            default=ALL,
            key="hist_range",
            label_visibility="collapsed",
        )
        sel = sel or ALL  # segmented_control returns None if cleared.
        if sel in SPANS:
            hist = hist[hist.index >= hist.index.max() - pd.Timedelta(days=SPANS[sel])]
        elif sel == "YTD":
            hist = hist[hist.index >= pd.Timestamp(date.today().year, 1, 1)]

        GREEN, RED = PROFIT_COLOR, LOSS_COLOR

        def _pct_span(p: float) -> str:
            if pd.isna(p):
                return "—"
            color = GREEN if p >= 0 else RED
            return f'<span style="color:{color}"><b>{p:+.1%}</b></span>'

        customdata = [
            [inj, _pct_span(p)]
            for inj, p in zip(hist["injected_eur"], hist["pnl_pct"], strict=True)
        ]
        # Split the value line by sign of P/L; masks overlap one point at
        # each crossing so the green and red segments stay connected.
        gain = hist["value_eur"] >= hist["injected_eur"]
        up = hist["value_eur"].where(gain | gain.shift(1, fill_value=False))
        down = hist["value_eur"].where(~gain | (~gain).shift(1, fill_value=False))
        GREEN_FILL, RED_FILL = "rgba(42,199,126,0.18)", "rgba(240,82,106,0.18)"

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=hist["injected_eur"], name=tr("portfolio.series_injected"),
                line=dict(color=TEXT_MUTED, width=2, shape="hv"),
                hoverinfo="skip",
            )
        )
        # Green band where value ≥ injected …
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=hist[["value_eur", "injected_eur"]].max(axis=1),
                line=dict(width=0), fill="tonexty", fillcolor=GREEN_FILL,
                hoverinfo="skip", showlegend=False,
            )
        )
        # … re-anchor on injected, then red band where value < injected.
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=hist["injected_eur"],
                line=dict(width=0), hoverinfo="skip", showlegend=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=hist[["value_eur", "injected_eur"]].min(axis=1),
                line=dict(width=0), fill="tonexty", fillcolor=RED_FILL,
                hoverinfo="skip", showlegend=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=up, name=tr("portfolio.series_value_profit"),
                line=dict(color=GREEN, width=2),
                hoverinfo="skip", showlegend=bool(gain.any()),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=down, name=tr("portfolio.series_value_loss"),
                line=dict(color=RED, width=2),
                hoverinfo="skip", showlegend=bool((~gain).any()),
            )
        )
        # Invisible full-coverage trace: one tooltip that works everywhere,
        # regardless of which colored segment is under the cursor.
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=hist["value_eur"],
                line=dict(width=0), opacity=0, showlegend=False,
                customdata=customdata,
                hovertemplate=tr("portfolio.hist_hover_tmpl"),
            )
        )
        fig.update_layout(
            **chart_layout(height=380, top_legend=True),
            hovermode="x",
            yaxis=dict(title="EUR", fixedrange=True),
        )
        fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1)
        show_chart(fig)
        notes = [tr("portfolio.hist_note_injected")]
        if missing:
            notes.append(
                tr("portfolio.hist_note_missing", tickers=", ".join(missing))
            )
        st.caption(" ".join(notes))


if tab_pos.open:
    with tab_pos:
        _positions_section()
        _history_section()

# ----------------------------------------------------------- Allocation & risk
if tab_risk.open:
    with tab_risk:
        FROM_START = tr("portfolio.from_start")
        choice = st.selectbox(
            tr("portfolio.return_window"), [FROM_START, "6mo", "1y", "2y", "5y"], index=0
        )
        if not positions:
            st.caption(tr("portfolio.no_positions_analyse"))
        else:
            tickers = tuple(p.ticker for p in positions)
            first_tx = min(t.date for t in txs)
            if choice == FROM_START:
                # Fetch enough history to cover the ledger, then clip below so
                # metrics start at the first transaction, not the stock's IPO.
                span = (date.today() - date.fromisoformat(first_tx)).days
                period = (
                    "1y" if span <= 360
                    else "2y" if span <= 700
                    else "5y" if span <= 1780
                    else "max"
                )
            else:
                period = choice

            # (db, mtime) must be arguments: st.cache_data keys on arguments
            # only, and the cache is shared across sessions — reading the
            # session's positions through the closure would serve one user's
            # report to another whose ticker tuple matches.
            @st.cache_data(ttl=3600, show_spinner=tr("portfolio.loading_prices_profiles"))
            def _report(period: str, tickers: tuple[str, ...], db: str, mtime: float):
                pos = ledger_state(db, mtime)[1]
                holds = holdings_from_positions([p for p in pos if p.ticker in tickers])
                return analyze(period=period, holdings=holds)

            rep = _report(period, tickers, DB, db_mtime(DB))
            if choice == FROM_START:
                start = pd.Timestamp(first_tx)

                def _since(obj):
                    idx = obj.index
                    if getattr(idx, "tz", None) is not None:
                        idx = idx.tz_localize(None)
                    return obj[idx >= start]

                rep.returns = _since(rep.returns)
                rep.bench_returns = {b: _since(r) for b, r in rep.bench_returns.items()}
            # Put weights on an EUR footing, then rebuild the portfolio return series.
            rep.weights = market_value_weights_eur(positions, rep.prices, rep.meta)
            rep.port_returns = portfolio_returns(rep.returns, rep.weights)

            c1, c2, c3, c4 = metric_cells(4)
            c1.metric(tr("portfolio.annualised_return"), f"{rep.cagr * 100:.1f}%",
                      help=tr("portfolio.annualised_return_help"))
            c2.metric(tr("portfolio.annualised_vol"), f"{rep.volatility * 100:.1f}%",
                      help=tr("portfolio.annualised_vol_help"))
            c3.metric(tr("portfolio.max_drawdown"), f"{rep.max_drawdown * 100:.1f}%",
                      help=tr("portfolio.max_drawdown_help"))
            c4.metric(tr("portfolio.effective_names"), f"{effective_positions(rep.weights):.1f}",
                      help=tr("portfolio.effective_names_help"))

            betas = " · ".join(f"β {b} {rep.beta_vs(b):.2f}" for b in rep.bench_returns)
            if betas:
                st.caption(
                    tr("portfolio.beta_caption", betas=betas,
                       pct=f"{top_n_weight(rep.weights, 5) * 100:.0f}")
                )

            st.subheader(tr("portfolio.allocation"))
            cols = st.columns(3)
            for col, key, title in zip(
                cols, ("sector", "country", "currency"),
                (tr("portfolio.alloc_sector"), tr("portfolio.alloc_geography"),
                 tr("portfolio.alloc_currency")),
                strict=True,
            ):
                alloc = rep.allocation(key)
                if alloc.empty:
                    col.caption(tr("portfolio.no_alloc_data", kind=title.lower()))
                    continue
                pie = go.Figure(
                    go.Pie(
                        labels=alloc.index, values=alloc.values, hole=0.45,
                        hovertemplate="<b>%{label}</b><br>%{percent}<extra></extra>",
                    )
                )
                pie.update_layout(**chart_layout(title=title, height=300))
                show_chart(pie, container=col)

            st.subheader(tr("portfolio.cumulative_return"))
            _, twr, twr_missing = ledger_history(
                (len(txs), txs[-1].date, date.today()), DB
            )
            # Clip the ledger TWR to the window so it rebases with the benchmarks.
            win_start = rep.returns.index[0]
            if getattr(win_start, "tz", None) is not None:
                win_start = win_start.tz_localize(None)
            twr_win = twr[twr.index >= win_start.normalize()]

            line = go.Figure()
            if not twr_win.empty:
                twr_cum = cumulative_returns(twr_win)
                line.add_trace(
                    go.Scatter(
                        x=twr_cum.index, y=twr_cum * 100, name=tr("portfolio.series_portfolio_twr"),
                        hovertemplate=tr("portfolio.hover_portfolio_twr"),
                    )
                )
            basket_cum = cumulative_returns(rep.port_returns)
            line.add_trace(
                go.Scatter(
                    x=basket_cum.index, y=basket_cum * 100,
                    name=tr("portfolio.series_current_basket"), line=dict(dash="dot"),
                    hovertemplate=tr("portfolio.hover_current_basket"),
                )
            )
            for b, r in rep.bench_returns.items():
                cum = cumulative_returns(r)
                line.add_trace(
                    go.Scatter(
                        x=cum.index, y=cum * 100, name=b,
                        hovertemplate=f"{b}  <b>%{{y:+.1f}}%</b><extra></extra>",
                    )
                )
            # One box per date comparing portfolio, basket and every benchmark.
            line.update_layout(
                height=420, hovermode="x unified", yaxis=dict(title="%", fixedrange=True)
            )
            show_chart(line)
            notes = [tr("portfolio.twr_note")]
            if twr_missing:
                notes.append(
                    tr("portfolio.twr_note_missing", tickers=", ".join(twr_missing))
                )
            st.caption(" ".join(notes))

            st.subheader(tr("portfolio.return_correlation"))
            corr = correlation_matrix(rep.returns)
            if not corr.empty:
                heat = go.Figure(
                    go.Heatmap(
                        z=corr.values, x=corr.columns, y=corr.index,
                        zmin=-1, zmax=1, colorscale="RdBu", reversescale=True,
                        hovertemplate=tr("portfolio.hover_correlation"),
                    )
                )
                # ≥24px per row so ticker labels never collide; headroom covers
                # the auto-expanding tick-label margins.
                heat.update_layout(
                    height=max(400, 24 * len(corr) + 120),
                    margin=dict(l=0, r=0, t=10, b=0),
                )
                show_chart(heat)

# --------------------------------------------------------------- Realized & tax
if tab_tax.open:
    with tab_tax:
        sell_years = sorted({int(s.sell_date[:4]) for s in realized}, reverse=True)
        if not sell_years:
            st.caption(tr("portfolio.no_realized_sales"))
        else:
            year = st.selectbox(tr("portfolio.fiscal_year"), sell_years, key="tax_year")
            buy_dates: dict[str, list[str]] = defaultdict(list)
            for t in txs:
                if t.action == "buy":
                    buy_dates[t.ticker].append(t.date)
            ty = fiscal_year(realized, year, buy_dates)

            st.subheader(tr("portfolio.irpf_savings_base", year=year))
            st.caption(tr("portfolio.irpf_caption"))
            c1, c2, c3 = metric_cells(3)
            c1.metric(tr("portfolio.net_taxable"), f"€{ty.net_taxable_eur:,.0f}",
                      help=tr("portfolio.net_taxable_help"))
            c2.metric(tr("portfolio.estimated_tax"), f"€{ty.estimated_tax_eur:,.0f}",
                      help=tr("portfolio.estimated_tax_help"))
            c3.metric(tr("portfolio.carryforward_loss"), f"€{ty.carryforward_loss_eur:,.0f}",
                      help=tr("portfolio.carryforward_loss_help"))
            summary = tr(
                "portfolio.realized_summary",
                gain=f"{ty.realized_gain_eur:,.0f}",
                loss=f"{ty.deductible_loss_eur:,.0f}",
            )
            if ty.deferred_loss_eur:
                summary += tr(
                    "portfolio.deferred_note", deferred=f"{ty.deferred_loss_eur:,.0f}"
                )
            st.caption(summary)

            rows = [
                {
                    "ticker": s.ticker, "buy": s.buy_date, "sell": s.sell_date,
                    "qty": s.quantity, "cost_eur": s.cost_eur,
                    "proceeds_eur": s.proceeds_eur, "gain_eur": s.gain_eur,
                }
                for s in ty.sales
            ]
            if rows:
                st.html(
                    ticker_table_html(
                        pd.DataFrame(rows),
                        fmt={
                            "qty": "{:,.4f}",
                            "cost_eur": "€{:,.2f}",
                            "proceeds_eur": "€{:,.2f}",
                            "gain_eur": "€{:+,.2f}",
                        },
                        signed=("gain_eur",),
                        left_cols=("buy", "sell"),
                    )
                )

            # Mark to market from the cached positions table — shares the
            # Positions tab's price burst; unpriced names fall back to cost.
            ptbl = positions_table(DB, db_mtime(DB))
            foreign = (
                float(ptbl["value_eur"].fillna(ptbl["cost_eur"]).sum())
                if not ptbl.empty
                else 0.0
            )
            # Build the message web-side from the flag's fields (tax_es stays
            # English for the CLI); localize the ≥/< 50k branch here.
            _flag = modelo_720_flag(foreign)
            _msg = tr(
                "portfolio.modelo_720_reportable"
                if _flag.reportable
                else "portfolio.modelo_720_ok",
                val=f"{_flag.total_value_eur:,.0f}",
            )
            st.caption(tr("portfolio.modelo_720", message=_msg))
            st.caption(tr("portfolio.planning_aid"))

# ------------------------------------------------------------------- Dividends
if tab_div.open:
    with tab_div:
        years = dividends.by_year(txs)
        if not years:
            st.caption(tr("portfolio.no_dividends"))
        else:
            rows = [
                {
                    "year": yr, "gross_eur": d.gross_eur, "withheld_eur": d.withheld_eur,
                    "net_eur": d.net_eur, "creditable_eur": d.creditable_eur,
                    "reclaimable_eur": d.reclaimable_eur,
                }
                for yr, d in sorted(years.items())
            ]
            st.dataframe(
                pd.DataFrame(rows).set_index("year").style.format("€{:,.0f}"),
            )
            st.caption(tr("portfolio.dividends_caption"))
