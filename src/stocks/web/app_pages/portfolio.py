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
    market_value_weights_eur,
    portfolio_returns,
    top_n_weight,
)
from stocks.portfolio import dividends
from stocks.portfolio.tax_es import fiscal_year, modelo_720_flag
from stocks.web import auth
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

st.title("Portfolio")

# This session user's ledger; with its mtime it keys every ledger-derived
# cache (web/portfolio_data.py) so concurrent users never read each other's
# book and entries stay hot until the next import.
DB = str(auth.db_path())

txs, positions, realized = ledger_state(DB, db_mtime(DB))
if not txs:
    st.warning("No transactions yet — upload a Revolut CSV on the **Import** page.")
    st.stop()
if not positions and not realized:
    st.warning("Ledger has rows but no positions could be built. Check the Import preview.")
    st.stop()

tab_pos, tab_risk, tab_tax, tab_div = st.tabs(
    ["Positions", "Allocation & risk", "Realized & tax", "Dividends"],
    on_change="rerun",
)


# ------------------------------------------------------------------- Positions
# Both sections are parallel fragments: a full rerun dispatches each to a
# thread pool, so the live-price table and the ledger-history chart build
# concurrently instead of back to back.
@st.fragment(parallel=True)
def _positions_section() -> None:
    st.subheader("Open positions & P/L (EUR)")
    # Shared loader (web/portfolio_data.py) — already weight/day-enriched and
    # weight-sorted; Home shows the same frame, so the price burst is shared.
    tbl = enriched_positions(DB, db_mtime(DB))
    if tbl.empty:
        st.caption("No open positions (everything sold).")
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
        c1.metric("Cost basis", f"{sym}{cost * fx:,.0f}")
        c2.metric("Market value", f"{sym}{value * fx:,.0f}")
        c3.metric(
            "Unrealised P/L",
            f"{sym}{(value - cost) * fx:,.0f}",
            f"{(value / cost - 1) * 100:+.1f}%",
        )
        realized_gain = sum(s.gain_eur for s in realized)
        realized_cost = sum(s.cost_eur for s in realized)
        c4.metric(
            "Realised P/L",
            f"{sym}{realized_gain * fx:,.0f}",
            f"{realized_gain / realized_cost * 100:+.1f}%" if realized_cost else None,
            help="All-time FIFO gains from closed sales, at transaction-date ECB "
            "rates. Per-year breakdown and tax on the Realized & tax tab.",
        )
        if ccy != "EUR":
            st.caption(f"Headline converted EUR→{ccy} at the latest spot rate.")

        vals = basket_history(DB, db_mtime(DB))
        d1, d2, d3 = metric_cells(3)
        for col, label, days in (
            (d1, "Today", 1),
            (d2, "1 week", 7),
            (d3, "1 month", 30),
        ):
            chg = basket_change(vals, days)
            if chg is None:
                col.metric(label, "n/a")
            else:
                col.metric(label, f"{sym}{chg[0] * fx:+,.0f}", f"{chg[1]:+.2%}")

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

        if _MOBILE:
            # Slim view — value, weight, day and total P/L% are what a
            # phone glance needs; horizontal panning inside the table is
            # clunky on touch. Full table one expander below.
            slim = tbl[["ticker", "value_eur", "weight", "day_pct", "pnl_pct"]]
            st.html(ticker_table_html(slim, fmt=fmt, signed=pnl_cols))
            with st.expander("All columns (shares, cost, day €, P/L €)"):
                st.html(ticker_table_html(tbl, fmt=fmt, signed=pnl_cols))
        else:
            st.html(ticker_table_html(tbl, fmt=fmt, signed=pnl_cols))
        st.caption(
            "Live prices via yfinance, converted at the ECB spot rate. "
            "Today/week/month mark today's holdings at daily closes × daily ECB FX "
            "(fixed basket — buys/sells inside the window aren't flow-adjusted; "
            "per-ticker day change includes the FX move). n/a = price/FX unavailable."
        )


@st.fragment(parallel=True)
def _history_section() -> None:
    st.subheader("Injected capital vs market value")

    hist, _, missing = ledger_history((len(txs), txs[-1].date, date.today()), DB)
    if hist.empty:
        st.caption("Not enough data to build the history.")
    else:
        # Filter in Python (not plotly rangeselector) so the y-axis rescales
        # to the visible window.
        SPANS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}
        sel = st.segmented_control(
            "Range",
            [*SPANS, "YTD", "All"],
            default="All",
            key="hist_range",
            label_visibility="collapsed",
        )
        sel = sel or "All"  # segmented_control returns None if cleared.
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
        GREEN_FILL, RED_FILL = "rgba(9,171,59,0.18)", "rgba(255,75,75,0.18)"

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=hist["injected_eur"], name="Injected",
                line=dict(color="#9aa4b2", width=2, shape="hv"),
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
                x=hist.index, y=up, name="Value (profit)",
                line=dict(color=GREEN, width=2),
                hoverinfo="skip", showlegend=bool(gain.any()),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=hist.index, y=down, name="Value (loss)",
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
                hovertemplate=(
                    "<b>%{x|%d %b %Y}</b><br>"
                    "Value  <b>€%{y:,.0f}</b><br>"
                    "Injected  €%{customdata[0]:,.0f}<br>"
                    "P/L  %{customdata[1]}<extra></extra>"
                ),
            )
        )
        fig.update_layout(
            **chart_layout(height=380, top_legend=True),
            hovermode="x",
            yaxis=dict(title="EUR", fixedrange=True),
        )
        fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1)
        show_chart(fig)
        notes = [
            "Injected = cumulative buys (incl. fees) minus sale proceeds, at "
            "transaction-date ECB rates. Value = daily closes × ECB daily FX. "
            "Dividends excluded."
        ]
        if missing:
            notes.append(
                f"No price history for: {', '.join(missing)} — carried at cost."
            )
        st.caption(" ".join(notes))


if tab_pos.open:
    with tab_pos:
        _positions_section()
        _history_section()

# ----------------------------------------------------------- Allocation & risk
if tab_risk.open:
    with tab_risk:
        FROM_START = "From the beginning"
        choice = st.selectbox(
            "Return window", [FROM_START, "6mo", "1y", "2y", "5y"], index=0
        )
        if not positions:
            st.caption("No open positions to analyse.")
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

            @st.cache_data(ttl=3600, show_spinner="Loading prices & company profiles…")
            def _report(period: str, tickers: tuple[str, ...]):
                holds = holdings_from_positions([p for p in positions if p.ticker in tickers])
                return analyze(period=period, holdings=holds)

            rep = _report(period, tickers)
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
            c1.metric("Annualised return", f"{rep.cagr * 100:.1f}%",
                      help="Geometric yearly return of the weighted book over the "
                      "selected window.")
            c2.metric("Annualised vol", f"{rep.volatility * 100:.1f}%",
                      help="Standard deviation of daily returns scaled to a year — "
                      "how bumpy the ride is.")
            c3.metric("Max drawdown", f"{rep.max_drawdown * 100:.1f}%",
                      help="Worst peak-to-trough fall over the window — the most you "
                      "would have been down buying the top.")
            c4.metric("Effective names", f"{effective_positions(rep.weights):.1f}",
                      help="Diversification (1/HHI): how many equal-size positions the "
                      "book behaves like. 10 holdings concentrated in 2 big ones ≈ 3–4 "
                      "effective names.")

            betas = " · ".join(f"β {b} {rep.beta_vs(b):.2f}" for b in rep.bench_returns)
            if betas:
                st.caption(
                    f"Beta vs benchmarks: {betas} · Top-5 concentration "
                    f"{top_n_weight(rep.weights, 5) * 100:.0f}%. "
                    "β > 1 amplifies the benchmark's moves, < 1 dampens them."
                )

            st.subheader("Allocation")
            cols = st.columns(3)
            for col, key, title in zip(
                cols, ("sector", "country", "currency"), ("Sector", "Geography", "Currency"),
                strict=True,
            ):
                alloc = rep.allocation(key)
                if alloc.empty:
                    col.caption(f"No {title.lower()} data.")
                    continue
                pie = go.Figure(
                    go.Pie(
                        labels=alloc.index, values=alloc.values, hole=0.45,
                        hovertemplate="<b>%{label}</b><br>%{percent}<extra></extra>",
                    )
                )
                pie.update_layout(**chart_layout(title=title, height=300))
                show_chart(pie, container=col)

            st.subheader("Cumulative return vs benchmarks")
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
                        x=twr_cum.index, y=twr_cum * 100, name="Portfolio (actual, TWR)",
                        hovertemplate="Portfolio (TWR)  <b>%{y:+.1f}%</b><extra></extra>",
                    )
                )
            basket_cum = cumulative_returns(rep.port_returns)
            line.add_trace(
                go.Scatter(
                    x=basket_cum.index, y=basket_cum * 100,
                    name="Current basket (backtest)", line=dict(dash="dot"),
                    hovertemplate="Current basket  <b>%{y:+.1f}%</b><extra></extra>",
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
            notes = [
                "Actual (TWR) = daily time-weighted return of the ledger book — "
                "flow-adjusted, so deposits/withdrawals don't count as performance; "
                "starts at your first transaction. Current basket = today's holdings "
                "backtested at fixed weights over the window."
            ]
            if twr_missing:
                notes.append(
                    f"No usable price history for: {', '.join(twr_missing)} — "
                    "carried at cost in the TWR."
                )
            st.caption(" ".join(notes))

            st.subheader("Return correlation")
            corr = correlation_matrix(rep.returns)
            if not corr.empty:
                heat = go.Figure(
                    go.Heatmap(
                        z=corr.values, x=corr.columns, y=corr.index,
                        zmin=-1, zmax=1, colorscale="RdBu", reversescale=True,
                        hovertemplate=(
                            "<b>%{y} × %{x}</b><br>"
                            "correlation  <b>%{z:.2f}</b><extra></extra>"
                        ),
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
            st.caption("No realized sales yet.")
        else:
            year = st.selectbox("Fiscal year", sell_years, key="tax_year")
            buy_dates: dict[str, list[str]] = defaultdict(list)
            for t in txs:
                if t.action == "buy":
                    buy_dates[t.ticker].append(t.date)
            ty = fiscal_year(realized, year, buy_dates)

            st.subheader(f"IRPF savings base — FY {year}")
            st.caption(
                "IRPF = Spanish personal income tax; capital gains and dividends fall "
                "in its *savings base*, taxed at progressive 19–28% brackets. FIFO: "
                "sales match your oldest shares first (art. 37 LIRPF)."
            )
            c1, c2, c3 = metric_cells(3)
            c1.metric("Net taxable", f"€{ty.net_taxable_eur:,.0f}",
                      help="Realized gains minus deductible losses for the year, in EUR "
                      "at each transaction date's ECB rate.")
            c2.metric("Estimated tax", f"€{ty.estimated_tax_eur:,.0f}",
                      help="Savings-base brackets applied to the net taxable amount — "
                      "ignores your other income and regional quirks.")
            c3.metric("Carryforward loss", f"€{ty.carryforward_loss_eur:,.0f}",
                      help="Net loss you can offset against gains in the next 4 years.")
            st.caption(
                f"Realized gains €{ty.realized_gain_eur:,.0f} · deductible losses "
                f"€{ty.deductible_loss_eur:,.0f}"
                + (f" · deferred (2-month rule) €{ty.deferred_loss_eur:,.0f}"
                   if ty.deferred_loss_eur else "")
            )

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
            st.caption(
                "Modelo 720 (informative declaration of assets held abroad, "
                "50.000 EUR line): " + modelo_720_flag(foreign).message
            )
            st.caption("Planning aid, not tax advice — verify with your gestor / Renta.")

# ------------------------------------------------------------------- Dividends
if tab_div.open:
    with tab_div:
        years = dividends.by_year(txs)
        if not years:
            st.caption("No dividends recorded. (Revolut dividends import as gross, 0 withholding.)")
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
            st.caption(
                "Creditable = Spanish double-tax credit (capped at 15% treaty rate). "
                "Withholding shows 0 unless you edited dividend fees — Revolut's CSV "
                "doesn't break it out."
            )
