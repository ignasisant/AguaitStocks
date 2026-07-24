"""Streamlit dashboard for the watchlist.

Run: uv run stocks dashboard   (or: uv run streamlit run src/stocks/web/app.py)
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from stocks.analysis.indicators import add_indicators
from stocks.config import load_watchlist
from stocks.data.fetch import fetch_history

st.set_page_config(page_title="Stocks", layout="wide")
st.title("📈 Stock Watchlist")

holdings = load_watchlist()
if not holdings:
    st.warning("Watchlist empty. Edit watchlist.yaml at the project root.")
    st.stop()

labels = {h.ticker: (h.name or h.ticker) for h in holdings}
tickers = list(labels)

ticker = st.sidebar.selectbox(
    "Ticker", tickers, format_func=lambda t: f"{t} — {labels[t]}"
)
period = st.sidebar.selectbox(
    "Period", ["1mo", "3mo", "6mo", "1y", "2y", "5y"], index=3
)

df = add_indicators(fetch_history(ticker, period=period))

last = float(df["Close"].iloc[-1])
prev = float(df["Close"].iloc[-2])
c1, c2, c3 = st.columns(3)
c1.metric("Price", f"{last:,.2f}", f"{(last - prev) / prev * 100:+.2f}%")
c2.metric("RSI (14)", f"{df['RSI14'].iloc[-1]:.1f}")
c3.metric("SMA20", f"{df['SMA20'].iloc[-1]:,.2f}")

fig = go.Figure()
fig.add_trace(
    go.Candlestick(
        x=df.index,
        open=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        name="Price",
    )
)
fig.add_trace(go.Scatter(x=df.index, y=df["SMA20"], name="SMA20"))
fig.add_trace(go.Scatter(x=df.index, y=df["SMA50"], name="SMA50"))
fig.update_layout(height=600, xaxis_rangeslider_visible=False)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Recent data")
st.dataframe(df.tail(30), use_container_width=True)
