"""Portfolio analytics: allocation, concentration, risk, benchmarking.

Two layers:
  * pure functions over price/return frames and a weights dict — run offline,
    fully unit-tested;
  * a thin network layer (load_closes / load_meta / analyze) that pulls prices
    and profile data via yfinance and assembles a PortfolioReport.

Weighting: if any watchlist entry has `shares > 0`, the book is weighted by
market value across those positions; otherwise the whole watchlist is
equal-weighted so the risk/correlation view still works before you enter sizes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from stocks.config import Holding, load_watchlist
from stocks.data.fetch import fetch_many
from stocks.data.fx import ToEur
from stocks.data.fx import to_eur as _default_to_eur

if TYPE_CHECKING:
    from datetime import datetime

TRADING_DAYS = 252
# Default benchmarks: US large-cap, US tech, emerging markets.
DEFAULT_BENCHMARKS = ("SPY", "QQQ", "EEM")


# ----------------------------------------------------------------- weighting
def equal_weights(tickers: list[str]) -> dict[str, float]:
    n = len(tickers)
    return {t: 1 / n for t in tickers} if n else {}


def market_value_weights(
    shares: dict[str, float], prices: dict[str, float]
) -> dict[str, float]:
    """Weights proportional to shares * price. Tickers without a price drop out."""
    values = {t: shares[t] * prices[t] for t in shares if prices.get(t)}
    total = sum(values.values())
    return {t: v / total for t, v in values.items()} if total else {}


def weights_from_holdings(
    holdings: list[Holding], prices: dict[str, float]
) -> dict[str, float]:
    """Market-value weights over real positions, else equal-weight watchlist."""
    positions = {h.ticker: h.shares for h in holdings if h.is_position}
    if positions:
        return market_value_weights(positions, prices)
    return equal_weights([h.ticker for h in holdings])


# ----------------------------------------------------------------- returns
def returns_frame(closes: dict[str, pd.Series]) -> pd.DataFrame:
    """Daily simple returns, tickers as columns, aligned on the date index."""
    px = pd.DataFrame({t: s for t, s in closes.items() if s is not None})
    return px.pct_change().iloc[1:]


def portfolio_returns(returns: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Daily portfolio return, weights renormalised per date over tickers with data.

    Tickers with shorter histories (recent IPOs) contribute only from their first
    quote; earlier dates redistribute their weight over the rest of the book
    instead of truncating the whole series to the common history.
    """
    cols = [c for c in returns.columns if c in weights and weights[c]]
    if not cols:
        return pd.Series(dtype=float)
    r = returns[cols].dropna(how="all")
    w = pd.Series({c: weights[c] for c in cols})
    present = r.notna().mul(w, axis=1).sum(axis=1)
    port = r.fillna(0.0).mul(w, axis=1).sum(axis=1) / present
    return port[present > 0]


def annualized_volatility(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    if returns.empty:
        return float("nan")
    return float(returns.std(ddof=0) * periods**0.5)


def annualized_return(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """Geometric annualised return from a daily-return series."""
    n = len(returns)
    if n == 0:
        return float("nan")
    growth = float((1 + returns).prod())
    if growth <= 0:
        return float("nan")
    return growth ** (periods / n) - 1


def beta(asset: pd.Series, benchmark: pd.Series) -> float:
    """Population beta of asset vs benchmark on their common dates."""
    df = pd.concat([asset, benchmark], axis=1).dropna()
    if len(df) < 2:
        return float("nan")
    a, b = df.iloc[:, 0], df.iloc[:, 1]
    var = ((b - b.mean()) ** 2).mean()
    if not var:
        return float("nan")
    cov = ((a - a.mean()) * (b - b.mean())).mean()
    return float(cov / var)


def max_drawdown(prices: pd.Series) -> float:
    """Worst peak-to-trough decline over the series (<= 0)."""
    prices = prices.dropna()
    if prices.empty:
        return float("nan")
    return float((prices / prices.cummax() - 1).min())


def cumulative_returns(returns: pd.Series) -> pd.Series:
    """Cumulative growth path from daily returns (starts near 0)."""
    return (1 + returns).cumprod() - 1


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.corr()


# ----------------------------------------------------------------- concentration
def hhi(weights: dict[str, float]) -> float:
    """Herfindahl-Hirschman index (sum of squared weights); 1.0 = single name."""
    return float(sum(w**2 for w in weights.values()))


def effective_positions(weights: dict[str, float]) -> float:
    """1 / HHI — how many equal-sized names the book behaves like."""
    h = hhi(weights)
    return float(1 / h) if h else float("nan")


def top_n_weight(weights: dict[str, float], n: int = 5) -> float:
    return float(sum(sorted(weights.values(), reverse=True)[:n]))


def allocation(weights: dict[str, float], meta: dict[str, dict], key: str) -> pd.Series:
    """Sum weights grouped by a metadata field ('sector' | 'country' | 'currency')."""
    agg: dict[str, float] = {}
    for ticker, w in weights.items():
        label = (meta.get(ticker) or {}).get(key) or "Unknown"
        agg[label] = agg.get(label, 0.0) + w
    return pd.Series(agg, dtype=float).sort_values(ascending=False)


def position_table(holdings: list[Holding], prices: dict[str, float]) -> pd.DataFrame:
    """Value and unrealised P/L per real position (shares > 0)."""
    rows = []
    for h in holdings:
        if not h.is_position:
            continue
        price = prices.get(h.ticker)
        value = h.shares * price if price else None
        pnl = pnl_pct = None
        if price and h.cost:
            basis = h.shares * h.cost
            pnl = value - basis
            pnl_pct = value / basis - 1 if basis else None
        rows.append(
            {
                "ticker": h.ticker,
                "shares": h.shares,
                "cost": h.cost,
                "price": price,
                "value": value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()


# ------------------------------------------------------------ injected vs value
# (amount, currency, iso_date) -> EUR, injectable so tests skip the network.
# The live converter is stocks.data.fx.to_eur (bound above as _default_to_eur).
ToEurFn = ToEur | None


def shares_frame(transactions, end: str | None = None) -> pd.DataFrame:
    """Daily shares held per ticker, replaying buys/sells/splits in ledger order.

    Calendar-daily index from the first transaction to `end` (default today),
    forward-filled between events; 0 before a ticker's first buy.
    """
    txs = sorted(transactions, key=lambda t: (t.date, t.id or 0))
    if not txs:
        return pd.DataFrame()
    qty: dict[str, float] = {}
    snap: dict[str, dict[str, float]] = {}
    for t in txs:
        if t.action == "buy":
            qty[t.ticker] = qty.get(t.ticker, 0.0) + t.quantity
        elif t.action == "sell":
            qty[t.ticker] = qty.get(t.ticker, 0.0) - t.quantity
        elif t.action == "split":
            qty[t.ticker] = qty.get(t.ticker, 0.0) * t.quantity
        else:
            continue
        snap.setdefault(t.date, {})[t.ticker] = qty[t.ticker]
    frame = pd.DataFrame.from_dict(snap, orient="index").sort_index()
    frame.index = pd.to_datetime(frame.index)
    idx = pd.date_range(frame.index[0], end or pd.Timestamp.today().normalize(), freq="D")
    return frame.reindex(idx).ffill().fillna(0.0).clip(lower=0.0)


def injected_series(transactions, to_eur: ToEurFn = None) -> pd.Series:
    """Cumulative net cash put into the book, EUR at the transaction-date rate.

    Buys add cost incl. commission; sells subtract net proceeds. Dividends and
    standalone fees don't move contributed capital. Index = transaction dates.
    """
    to_eur = to_eur or _default_to_eur
    flows: dict[str, float] = {}
    for t in sorted(transactions, key=lambda t: (t.date, t.id or 0)):
        if t.action == "buy":
            amt = to_eur(t.quantity * t.price + t.fee, t.currency, t.date)
        elif t.action == "sell":
            amt = -to_eur(t.quantity * t.price - t.fee, t.currency, t.date)
        else:
            continue
        flows[t.date] = flows.get(t.date, 0.0) + amt
    if not flows:
        return pd.Series(dtype=float)
    s = pd.Series(flows).sort_index()
    s.index = pd.to_datetime(s.index)
    return s.cumsum()


def injected_vs_value(
    transactions,
    closes: dict[str, pd.Series],
    fx: dict[str, pd.Series] | None = None,
    to_eur: ToEurFn = None,
) -> pd.DataFrame:
    """Daily injected capital vs mark-to-market EUR value of the book.

    `closes` = native-currency close series per ticker; `fx` = daily
    currency->EUR rate series (EUR itself implied 1.0). Held days without a
    usable close/FX quote — ticker absent, delisted mid-hold, or (like a
    delisted ORGN) a stray quote outside the holding window — are carried at
    cost (cumulative net invested EUR): no fake loss, no mark-to-market either.
    Columns: injected_eur, value_eur, pnl_pct. Tickers ever carried at cost
    while held are listed in df.attrs['carried_at_cost'].
    """
    shares = shares_frame(transactions)
    if shares.empty:
        return pd.DataFrame()
    idx = shares.index
    fx = fx or {}
    ccy_of = {t.ticker: t.currency for t in transactions if t.action in ("buy", "sell")}

    value = pd.Series(0.0, index=idx)
    carried: list[str] = []
    for ticker in shares.columns:
        held = shares[ticker] > 0
        mtm = None
        px = closes.get(ticker)
        if px is not None and not px.empty:
            px = px.copy()
            px.index = pd.to_datetime(px.index)
            if px.index.tz is not None:
                px.index = px.index.tz_localize(None)
            px = px.reindex(idx).ffill()
            ccy = (ccy_of.get(ticker) or "EUR").upper()
            if ccy == "EUR":
                rate = pd.Series(1.0, index=idx)
            else:
                rate = fx.get(ccy)
            if rate is not None:
                rate = rate.copy()
                rate.index = pd.to_datetime(rate.index)
                rate = rate.reindex(idx).ffill().bfill()
                mtm = shares[ticker] * px * rate
        # Held days with no quote fall back to cost, never below zero.
        gap = held if mtm is None else (mtm.isna() & held)
        if gap.any():
            proxy = injected_series(
                [t for t in transactions if t.ticker == ticker], to_eur
            ).reindex(idx).ffill().fillna(0.0)
            value += proxy.where(gap, 0.0).clip(lower=0.0)
            carried.append(ticker)
        if mtm is not None:
            value += mtm.fillna(0.0)

    injected = injected_series(transactions, to_eur).reindex(idx).ffill().fillna(0.0)
    pct = pd.Series(float("nan"), index=idx)
    mask = injected > 0
    pct[mask] = value[mask] / injected[mask] - 1
    df = pd.DataFrame({"injected_eur": injected, "value_eur": value, "pnl_pct": pct})
    df.attrs["carried_at_cost"] = carried
    return df


def flow_series(
    transactions, to_eur: ToEurFn = None, tickers: set[str] | None = None
) -> pd.Series:
    """Net external cash flow per date in EUR: buys +, sells −, dividends −.

    Dividends count as withdrawals so the time-weighted return gets credit for
    them (the market value path never includes cash). Leave `tickers` unset when
    the value path carries unpriced names at cost (injected_vs_value); only
    restrict it when those names are absent from the value entirely, otherwise
    their buys read as instant losses in the TWR.
    """
    to_eur = to_eur or _default_to_eur
    flows: dict[str, float] = {}
    for t in transactions:
        if tickers is not None and t.ticker not in tickers:
            continue
        if t.action == "buy":
            amt = to_eur(t.quantity * t.price + t.fee, t.currency, t.date)
        elif t.action == "sell":
            amt = -to_eur(t.quantity * t.price - t.fee, t.currency, t.date)
        elif t.action == "dividend":
            amt = -to_eur(t.price - t.fee, t.currency, t.date)
        else:
            continue
        flows[t.date] = flows.get(t.date, 0.0) + amt
    s = pd.Series(flows, dtype=float).sort_index()
    s.index = pd.to_datetime(s.index)
    return s


def time_weighted_returns(value: pd.Series, flows: pd.Series) -> pd.Series:
    """Daily time-weighted returns from a value path and external flows.

    r_t = (V_t - F_t) / V_{t-1} - 1, flows treated as arriving at day-t close.
    Days with no prior value (before the first buy, or after the book was
    emptied) drop out; performance is unaffected by deposit/withdrawal timing.
    """
    if value.empty:
        return pd.Series(dtype=float)
    f = flows.reindex(value.index, fill_value=0.0) if not flows.empty else 0.0
    prev = value.shift(1)
    r = (value - f) / prev - 1
    return r[prev > 1e-9]


# ----------------------------------------------------------------- orchestration
@dataclass
class PortfolioReport:
    weights: dict[str, float]
    meta: dict[str, dict]
    returns: pd.DataFrame
    port_returns: pd.Series
    prices: dict[str, float] = field(default_factory=dict)
    bench_returns: dict[str, pd.Series] = field(default_factory=dict)

    @property
    def volatility(self) -> float:
        return annualized_volatility(self.port_returns)

    @property
    def cagr(self) -> float:
        return annualized_return(self.port_returns)

    @property
    def max_drawdown(self) -> float:
        return max_drawdown(cumulative_returns(self.port_returns) + 1)

    def beta_vs(self, benchmark: str) -> float:
        bench = self.bench_returns.get(benchmark)
        if bench is None:
            return float("nan")
        return beta(self.port_returns, bench)

    def allocation(self, key: str) -> pd.Series:
        return allocation(self.weights, self.meta, key)


def load_closes(tickers: list[str], period: str = "1y") -> dict[str, pd.Series]:
    """Close series per ticker from ONE bulk download; no-data tickers drop out."""
    out: dict[str, pd.Series] = {}
    for t, df in fetch_many(tickers, period=period).items():
        s = df["Close"].dropna() if "Close" in df else pd.Series(dtype=float)
        if not s.empty:
            out[t] = s.rename(t)
    return out


def _profile(ticker: str) -> dict:
    import yfinance as yf

    from stocks.data.crypto import split_pair
    from stocks.data.fetch import resolve

    # Crypto pairs: yfinance has no sector/country for coins — label the
    # allocation bucket directly and skip the profile fetch.
    if pair := split_pair(ticker):
        return {"sector": "Crypto", "country": None, "currency": pair[1]}
    try:
        info = yf.Ticker(resolve(ticker)).info or {}
    except Exception:
        info = {}
    return {
        "sector": info.get("sector"),
        "country": info.get("country"),
        "currency": info.get("currency"),
    }


def load_meta(tickers: list[str], max_workers: int = 8) -> dict[str, dict]:
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        profiles = pool.map(lambda t: (t, _profile(t)), tickers)
    return dict(profiles)


# ----------------------------------------------------------------- ledger bridge
def ledger_positions():
    """Open positions (FIFO) from the transaction ledger. Empty if no ledger."""
    from stocks.portfolio.ledger import all_transactions
    from stocks.portfolio.positions import build

    positions, _ = build(all_transactions())
    return positions


def holdings_from_positions(positions) -> list[Holding]:
    """Ledger positions as watchlist Holdings (shares + native avg cost)."""
    return [
        Holding(ticker=p.ticker, shares=p.quantity, cost=p.avg_cost_native)
        for p in positions
    ]


def market_value_eur(ticker: str, quantity: float, currency: str) -> float | None:
    """Live EUR market value of a position; None if price/FX lookup fails."""
    from stocks.data.fetch import latest_price
    from stocks.data.fx import spot

    try:
        price = latest_price(ticker)
        rate, _ = spot(currency, "EUR")
    except Exception:
        return None
    return quantity * price * rate


def market_values_eur(positions, max_workers: int = 8) -> dict[str, float]:
    """Live EUR market value per open position, fetched concurrently.

    Shared by the CLI (positions/tax) and the dashboard so neither loops the
    network serially. Positions whose price/FX lookup fails are absent.
    """
    from stocks.data.fx import spot

    # Warm the spot memo once per currency so the pool doesn't race N
    # identical FX fetches for the same pair.
    for ccy in {p.currency for p in positions}:
        try:
            spot(ccy, "EUR")
        except Exception:
            pass  # workers fall back to per-position lookups / None
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pairs = pool.map(
            lambda p: (p.ticker, market_value_eur(p.ticker, p.quantity, p.currency)),
            positions,
        )
    return {t: v for t, v in pairs if v is not None}


def market_value_weights_eur(
    positions, prices: dict[str, float], meta: dict[str, dict]
) -> dict[str, float]:
    """EUR market-value weights: qty * latest native price * native->EUR spot.

    Mixing currencies makes native-value weighting wrong for a multi-currency
    book (Revolut = mostly USD + some EUR), so weights are put on a common EUR
    footing here. Tickers without a price drop out and the rest renormalise.
    """
    from stocks.data.fx import spot

    values: dict[str, float] = {}
    for p in positions:
        px = prices.get(p.ticker)
        if not px:
            continue
        ccy = (meta.get(p.ticker) or {}).get("currency") or p.currency
        try:
            rate, _ = spot(ccy, "EUR")
        except Exception:
            rate = 1.0
        values[p.ticker] = p.quantity * px * rate
    total = sum(values.values())
    return {t: v / total for t, v in values.items()} if total else {}


def positions_frame_eur(positions) -> pd.DataFrame:
    """Per-position EUR table: qty, cost, live value, unrealised P/L.

    Live prices come from market_values_eur (thread pool), so a 20-name book
    costs one concurrent burst, not 20 serial price+FX round-trips.
    """
    values = market_values_eur(positions)
    rows = []
    for p in positions:
        value = values.get(p.ticker)
        pnl = value - p.cost_eur if value is not None else None
        pnl_pct = (value / p.cost_eur - 1) if value and p.cost_eur else None
        rows.append(
            {
                "ticker": p.ticker,
                "shares": p.quantity,
                "ccy": p.currency,
                "cost_eur": p.cost_eur,
                "value_eur": value,
                "pnl_eur": pnl,
                "pnl_pct": pnl_pct,
            }
        )
    return pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame()


def position_values_history(positions, period: str = "3mo") -> pd.DataFrame:
    """Daily EUR value per open position at *today's* quantities (fixed basket).

    Current shares × daily native close × daily ECB FX, forward-filled onto the
    price index. Powers the day/week/month change chips and the per-ticker day
    change, so a position's move includes its FX move — the book is judged in
    EUR. Flows inside the window are NOT adjusted (that's the TWR view's job).
    Tickers without a usable price (or FX) series are absent.
    """
    from datetime import date

    from stocks.data.fx import rates_range

    closes = load_closes([p.ticker for p in positions], period=period)
    if not closes:
        return pd.DataFrame()
    px = pd.DataFrame(closes).sort_index()
    px.index = pd.to_datetime(px.index)
    if px.index.tz is not None:
        px.index = px.index.tz_localize(None)
    px = px.ffill()

    fx: dict[str, pd.Series] = {}
    start = px.index[0].date().isoformat()
    for ccy in {p.currency for p in positions if p.currency != "EUR"}:
        try:
            rates = rates_range(start, date.today().isoformat(), ccy, "EUR")
        except Exception:
            rates = {}
        if rates:
            s = pd.Series(rates, dtype=float)
            s.index = pd.to_datetime(s.index)
            fx[ccy] = s.reindex(px.index).ffill().bfill()

    values: dict[str, pd.Series] = {}
    for p in positions:
        if p.ticker not in px.columns:
            continue
        if p.currency == "EUR":
            values[p.ticker] = p.quantity * px[p.ticker]
        elif p.currency in fx:
            values[p.ticker] = p.quantity * px[p.ticker] * fx[p.currency]
    return pd.DataFrame(values)


def basket_change(values: pd.DataFrame, days: int) -> tuple[float, float] | None:
    """(EUR change, pct change) of the basket over the last ~`days` calendar days.

    `days=1` compares the last two rows (previous trading day); longer windows
    anchor on the last row at or before `end - days`. Only tickers priced at
    both endpoints count, so a name quoted only mid-window doesn't read as a
    gain. None when the frame doesn't cover the window.
    """
    if len(values) < 2:
        return None
    end = values.index[-1]
    if days <= 1:
        start = values.index[-2]
    else:
        prior = values.index[values.index <= end - pd.Timedelta(days=days)]
        if prior.empty:
            return None
        start = prior[-1]
    both = values.loc[start].notna() & values.loc[end].notna()
    v0 = float(values.loc[start, both].sum())
    v1 = float(values.loc[end, both].sum())
    if not v0:
        return None
    return v1 - v0, v1 / v0 - 1


# --------------------------------------------------------------- market clock
# The day-change columns come from daily closes, so outside a live regular
# session they show the LAST completed session's move, not a live tick. These
# helpers flag that state so the UI can render those cells muted ("market
# closed — last close") instead of implying they're moving right now.
#
# Regular-session hours per exchange, keyed by the Yahoo symbol suffix
# (resolve() gives it — e.g. RCF -> TEP.PA -> "PA"). (IANA tz, open, close) in
# exchange-local time; continuous session (HK/Tokyo lunch breaks ignored — a
# soft cue, not a trading gate). No suffix / unknown = US.
_EXCHANGE_HOURS: dict[str, tuple[str, tuple[int, int], tuple[int, int]]] = {
    # Euronext + Southern Europe (CET)
    "PA": ("Europe/Paris", (9, 0), (17, 30)),
    "AS": ("Europe/Amsterdam", (9, 0), (17, 30)),
    "BR": ("Europe/Brussels", (9, 0), (17, 30)),
    "LS": ("Europe/Lisbon", (8, 0), (16, 30)),
    "MI": ("Europe/Rome", (9, 0), (17, 30)),
    "MC": ("Europe/Madrid", (9, 0), (17, 30)),
    "VI": ("Europe/Vienna", (9, 0), (17, 30)),
    # Germany (Xetra + Frankfurt floor)
    "DE": ("Europe/Berlin", (9, 0), (17, 30)),
    "F": ("Europe/Berlin", (8, 0), (20, 0)),
    # UK / Ireland
    "L": ("Europe/London", (8, 0), (16, 30)),
    "IR": ("Europe/Dublin", (8, 0), (16, 30)),
    # Switzerland + Nordics
    "SW": ("Europe/Zurich", (9, 0), (17, 30)),
    "ST": ("Europe/Stockholm", (9, 0), (17, 30)),
    "HE": ("Europe/Helsinki", (10, 0), (18, 30)),
    "CO": ("Europe/Copenhagen", (9, 0), (17, 0)),
    "OL": ("Europe/Oslo", (9, 0), (16, 30)),
    # Asia-Pacific
    "HK": ("Asia/Hong_Kong", (9, 30), (16, 0)),
    "T": ("Asia/Tokyo", (9, 0), (15, 0)),
    "SS": ("Asia/Shanghai", (9, 30), (15, 0)),
    "SZ": ("Asia/Shanghai", (9, 30), (15, 0)),
    "KS": ("Asia/Seoul", (9, 0), (15, 30)),
    "TW": ("Asia/Taipei", (9, 0), (13, 30)),
    "NS": ("Asia/Kolkata", (9, 15), (15, 30)),
    "BO": ("Asia/Kolkata", (9, 15), (15, 30)),
    "AX": ("Australia/Sydney", (10, 0), (16, 0)),
    # Other Americas
    "TO": ("America/Toronto", (9, 30), (16, 0)),
    "SA": ("America/Sao_Paulo", (10, 0), (17, 0)),
}


def _session_open(
    tz_name: str,
    open_hm: tuple[int, int],
    close_hm: tuple[int, int],
    now_utc: datetime | None = None,
) -> bool:
    """Whether `now` is inside a Mon–Fri open→close window in `tz_name`.

    Time-based only — ignores exchange holidays (a holiday reads as "open"),
    fine for a soft display cue. `now_utc` is injectable for tests."""
    from datetime import datetime, time, timezone
    from zoneinfo import ZoneInfo

    now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return time(*open_hm) <= now.time() < time(*close_hm)


def us_market_open(now_utc: datetime | None = None) -> bool:
    """Whether the US equity regular session (Mon–Fri, 09:30–16:00 America/
    New_York) is open right now. See `_session_open` for the holiday caveat."""
    return _session_open("America/New_York", (9, 30), (16, 0), now_utc)


def market_live(ticker: str, now_utc: datetime | None = None) -> bool:
    """Whether `ticker` is in a live regular session on its own exchange now.

    Crypto trades 24/7 (always live). Equities key off the Yahoo symbol suffix
    (`_EXCHANGE_HOURS`) so a Paris/London/HK name follows its local hours; an
    unsuffixed or unknown symbol falls back to US hours. Exchange-local, so a
    European stock reads open during CET hours even while US premarket is closed.
    """
    from stocks.data.crypto import is_crypto
    from stocks.data.fetch import resolve

    if is_crypto(ticker):
        return True
    symbol = resolve(ticker)
    suffix = symbol.rsplit(".", 1)[1].upper() if "." in symbol else ""
    hours = _EXCHANGE_HOURS.get(suffix)
    return _session_open(*hours, now_utc) if hours else us_market_open(now_utc)


def _session_move(ticker: str) -> float | None:
    """Last regular-session % move (native) for `ticker` via fast_info:
    lastPrice / regularMarketPreviousClose - 1.

    Robust when the market's closed — regularMarketPreviousClose is always the
    prior regular close, so this reads the last completed session's move even
    while a stale/flat premarket daily bar makes the close-to-close basket
    collapse to ~0%. None when either quote is missing."""
    import yfinance as yf

    from stocks.data.fetch import resolve

    try:
        fi = yf.Ticker(resolve(ticker)).fast_info
        last, prev = fi["lastPrice"], fi["regularMarketPreviousClose"]
        if last and prev:
            return float(last) / float(prev) - 1
    except Exception:
        pass
    return None


def session_moves(tickers: list[str], max_workers: int = 8) -> dict[str, float]:
    """Last regular-session % move per ticker (native), fetched concurrently.

    Feeds the "market closed → last completed session" day-change cells so they
    show the real move instead of a flat premarket 0%. Tickers whose quote is
    unavailable are absent (caller falls back to the basket value)."""
    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pairs = pool.map(lambda t: (t, _session_move(t)), tickers)
    return {t: v for t, v in pairs if v is not None}


def analyze(
    period: str = "1y",
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS,
    holdings: list[Holding] | None = None,
) -> PortfolioReport:
    """Fetch prices + profiles for the watchlist and build a PortfolioReport."""
    holdings = holdings if holdings is not None else load_watchlist()
    tickers = [h.ticker for h in holdings]
    # Watchlist + benchmarks in one bulk download.
    all_closes = load_closes([*tickers, *benchmarks], period=period)
    closes = {t: s for t, s in all_closes.items() if t in set(tickers)}
    latest = {t: float(s.iloc[-1]) for t, s in closes.items()}
    weights = weights_from_holdings(holdings, latest)
    returns = returns_frame(closes)
    port = portfolio_returns(returns, weights)
    bench_returns = {
        b: all_closes[b].pct_change().iloc[1:] for b in benchmarks if b in all_closes
    }
    return PortfolioReport(
        weights=weights,
        meta=load_meta(tickers),
        returns=returns,
        port_returns=port,
        prices=latest,
        bench_returns=bench_returns,
    )
