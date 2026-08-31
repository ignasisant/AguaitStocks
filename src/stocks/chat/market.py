"""Live quotes for whatever the message is about — the chat's market feed.

The system prompt already carries the user's own book valued live
(engine.book_snapshot), so "how is my Nvidia doing?" is grounded. Anything
*outside* the book was not: asked about a ticker they do not hold, the model
answered from training-data prices, and web snippets quote whatever day the
page was indexed. This module closes that gap — the tickers named in the
message are resolved and quoted at send time, and the figures ride on the
outgoing copy of the user turn exactly like web hits do (chat/../web/chat_web
.augment), so provider prompt caches stay warm and the stored history keeps
the user's own text.

Resolution is deliberately cheap-first: ticker-shaped tokens and the account's
own watchlist names cost nothing, and only when neither hits does one company
name go to Yahoo's search endpoint (data/symbols.py). Everything degrades to
"no quotes": an unresolvable name, a throttled Yahoo (routine on shared cloud
egress IPs) or a slow call all yield [], and the answer proceeds on the web
hits and the model's own knowledge.

Streamlit-free like the rest of stocks.chat, and the fetcher is injectable so
the parsing and formatting are testable without a network.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

MAX_TICKERS = 3  # quotes fetched per message
LOOKUP_TIMEOUT = 8.0  # seconds for the whole quote batch
_MAX_LOOKUPS = 1  # company-name searches against Yahoo per message

# Ticker-shaped tokens as the app knows them: plain symbols (NVDA), venue
# suffixes (MIPS.ST), class shares (BRK.B), crypto pairs (BTC-EUR).
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}(?:[.\-][A-Z]{1,4})?\b")

# Uppercase words that are not tickers. Without this every "ETF", "USA" or
# "OK" in a message would cost a quote lookup (and Yahoo does quote some of
# them, which is worse — wrong data, confidently formatted). Public because
# web/chat_web.py screens the same false positives when it decides, without a
# model, whether a message names a company worth searching for.
NOT_TICKERS = {
    "A", "AI", "ALL", "AND", "ANY", "API", "AT", "BE", "BUY", "BY", "CEO",
    "CFO", "CPI", "DCF", "DE", "DO", "EBIT", "EL", "EPS", "ES", "ETF", "ETFS",
    "EU", "EUR", "FED", "FOR", "FX", "GDP", "GO", "HOY", "I", "IA", "IF",
    "IN", "IPO", "IRPF", "IS", "IT", "LA", "LO", "MI", "MY", "NO", "NOT",
    "OF", "OK", "ON", "OR", "P", "PE", "PEG", "PER", "PIB", "PM", "PS", "QQQ",
    "ROE", "ROIC", "SEC", "SELL", "SI", "SP", "TAM", "TO", "TWR", "UK", "US",
    "USA", "USD", "VS", "WACC", "Y", "YES", "YOY",
}

# Capitalized runs that could be a company name ("Nvidia", "Banco Santander").
# Only consulted when nothing cheaper resolved — see `mentioned`.
_WORD = r"[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ&.]+"
_NAME_RE = re.compile(rf"\b{_WORD}(?: {_WORD})?")
_NOT_NAMES = {
    "Que", "Qué", "Como", "Cómo", "Cuando", "Cuándo", "Donde", "Dónde", "Por",
    "Para", "Hoy", "Ayer", "Mañana", "The", "What", "When", "Where", "Why",
    "How", "Should", "Would", "Could", "Is", "Are", "Do", "Does", "My", "Mi",
    "Me", "Puedo", "Tengo", "Dame", "Give", "Tell", "Explain", "Analiza",
    "Analyze", "Compare", "Compara", "Hola", "Hi", "Hello", "Buenos",
}


@dataclass(frozen=True)
class Quote:
    """One live snapshot. Every field past `ticker` may be missing — Yahoo
    returns partial rows for thin listings, and half a quote still beats a
    remembered price."""

    ticker: str
    name: str = ""
    price: float | None = None
    currency: str = ""
    prev_close: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    market_cap: float | None = None

    @property
    def day_pct(self) -> float | None:
        if self.price is None or not self.prev_close:
            return None
        return self.price / self.prev_close - 1

    def line(self) -> str:
        """The prompt line for this quote — figures only, no interpretation."""
        head = f"{self.ticker}" + (f" ({self.name})" if self.name else "")
        bits = []
        if self.price is not None:
            bits.append(f"last {self.price:,.2f} {self.currency}".strip())
        if self.day_pct is not None:
            bits.append(f"today {self.day_pct:+.2%}")
        if self.year_low is not None and self.year_high is not None:
            bits.append(f"52w range {self.year_low:,.2f}–{self.year_high:,.2f}")
        if self.market_cap:
            bits.append(f"market cap {self.market_cap / 1e9:,.1f}B")
        return f"- {head}: " + " | ".join(bits) if bits else f"- {head}: no data"


# ---------------------------------------------------------- ticker mentions


def watchlist_names(path: Path) -> dict[str, str]:
    """{upper name or symbol: ticker} for the account's own watchlist.

    The free half of resolution: "how is Santander doing" needs no network
    when Santander is already on the list."""
    try:
        from stocks.config import load_watchlist

        out: dict[str, str] = {}
        for h in load_watchlist(path):
            out[h.ticker.upper()] = h.ticker
            if h.name:
                out[h.name.upper()] = h.ticker
        return out
    except Exception:
        return {}


def _lookup(name: str) -> str:
    """Yahoo's best symbol for a company name, or '' (throttled/unknown)."""
    try:
        from stocks.data.symbols import search_symbols

        hits = search_symbols(name, limit=1)
    except Exception:
        return ""
    return hits[0][0] if hits else ""


def mentioned(
    message: str,
    known: dict[str, str] | None = None,
    focus: str = "",
    lookup: Callable[[str], str] = _lookup,
) -> list[str]:
    """Tickers to quote for this message, best-effort and capped.

    Order of trust: the ticker in focus (the page the user is looking at),
    then ticker-shaped tokens, then watchlist names, then — only when nothing
    else resolved — one Yahoo name lookup, which is what makes plain "Nvidia"
    or "Telefónica" work without the user typing a symbol."""
    known = known or {}
    out: list[str] = []

    def add(tk: str) -> None:
        tk = (tk or "").strip().upper()
        if tk and tk not in out and len(out) < MAX_TICKERS:
            out.append(tk)

    upper = message.upper()
    if focus:
        # The page the user is on answers "and how does it look today?" with
        # no ticker in the message at all — the strongest signal there is.
        add(focus)
    # The regex only matches all-caps runs, so a token here is one the user
    # actually typed in caps; the stop-list keeps "ETF"/"USA"/"CEO" out.
    for tok in _TICKER_RE.findall(message):
        if tok not in NOT_TICKERS:
            add(known.get(tok, tok))
    for name, tk in known.items():
        if len(name) > 2 and name in upper:
            add(tk)
    if not out:
        for cand in _NAME_RE.findall(message)[:_MAX_LOOKUPS + 2]:
            if cand in _NOT_NAMES or cand.upper() in NOT_TICKERS:
                continue
            sym = lookup(cand)
            if sym:
                add(sym)
                break
    return out[:MAX_TICKERS]


# ----------------------------------------------------------------- quotes


def _fetch_quote(ticker: str) -> Quote | None:
    """One live snapshot via yfinance's fast path (no `.info` scrape).

    fast_info is a single quote request; `.info` is a much heavier call that
    Yahoo throttles first, and none of its extra fields are worth adding a
    second round-trip per message."""
    try:
        import yfinance as yf

        from stocks.data.fetch import resolve

        fi = dict(yf.Ticker(resolve(ticker)).fast_info)
    except Exception:
        return None
    if not fi:
        return None

    def num(key: str) -> float | None:
        try:
            val = float(fi.get(key))
        except (TypeError, ValueError):
            return None
        return val if val == val else None  # val==val screens NaN

    price = num("lastPrice")
    if price is None:
        return None
    return Quote(
        ticker=ticker.upper(),
        price=price,
        currency=str(fi.get("currency") or ""),
        prev_close=num("previousClose") or num("regularMarketPreviousClose"),
        year_high=num("yearHigh"),
        year_low=num("yearLow"),
        market_cap=num("marketCap"),
    )


def quotes(
    tickers: Iterable[str],
    fetch: Callable[[str], Quote | None] = _fetch_quote,
    timeout: float = LOOKUP_TIMEOUT,
) -> list[Quote]:
    """Live snapshots for `tickers`, in order, skipping whatever failed.

    Fetched concurrently and behind one wall-clock budget: a throttled Yahoo
    must cost the answer a few seconds, not the whole turn. No `with` on the
    pool — shutdown would block on the hung worker the timeout just escaped."""
    names = [t for t in dict.fromkeys(t.upper() for t in tickers) if t][:MAX_TICKERS]
    if not names:
        return []
    pool = ThreadPoolExecutor(max_workers=len(names))
    try:
        futures = [pool.submit(fetch, t) for t in names]
        out = []
        for f in futures:
            try:
                q = f.result(timeout=timeout)
            except Exception:
                continue
            if q is not None:
                out.append(q)
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ----------------------------------------------------------------- prompt


def augment(message: str, quotes_: list[Quote]) -> str:
    """The user message with the live quotes appended (unchanged when none)."""
    if not quotes_:
        return message
    return (
        message
        + "\n\n---\nLive market data fetched for this message (these are "
        "current prices — trust them over any figure you remember, and over "
        "prices quoted in web results):\n"
        + "\n".join(q.line() for q in quotes_)
    )


def lookup_for(
    message: str, watchlist: Path | None = None, focus: str = ""
) -> list[Quote]:
    """The whole path in one call: mentions → live quotes.

    The convenience entry point both chat surfaces use; every failure inside
    degrades to []."""
    try:
        known = watchlist_names(watchlist) if watchlist else {}
        return quotes(mentioned(message, known, focus))
    except Exception:
        return []
