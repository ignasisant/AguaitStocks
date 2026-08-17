"""SEC EDGAR client — primary source for US filers (10-K/10-Q facts).

Free, no API key. SEC requires a descriptive User-Agent with contact info:
set EDGAR_USER_AGENT in .env (e.g. "stocks-toolkit you@example.com").

Used to cross-check yfinance numbers against filed XBRL facts, per the
verification hierarchy in stocks.analysis.fundamentals.KPI_SOURCES.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd

from stocks.config import DATA_DIR
from stocks.data.http import get_json
from stocks.fuzzy import FUZZY_CUTOFF, MIN_QUERY, fuzzy_ratio

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
TICKER_CACHE = DATA_DIR / "edgar_tickers.json"

# XBRL revenue tag varies by filer; try in order.
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
NET_INCOME_TAGS = ["NetIncomeLoss"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]


def _user_agent() -> str:
    return os.getenv("EDGAR_USER_AGENT") or (
        "stocks-toolkit (set EDGAR_USER_AGENT in .env)"
    )


def _get_json(url: str) -> dict:
    return get_json(url, user_agent=_user_agent())


# ticker -> zero-padded CIK / company title, built once per process (the map
# file is ~10k rows; re-reading and linear-scanning it per lookup was the old
# behaviour). _ROWS keeps the file's order — roughly by market cap — so
# search results break rank ties sensibly.
_CIK_MAP: dict[str, str] | None = None
_TITLE_MAP: dict[str, str] | None = None
_ROWS: list[tuple[str, str, int]] | None = None


def _load_map() -> None:
    global _CIK_MAP, _TITLE_MAP, _ROWS
    if _CIK_MAP is not None:
        return
    if not TICKER_CACHE.exists():
        TICKER_CACHE.write_text(json.dumps(_get_json(TICKER_MAP_URL)))
    table = json.loads(TICKER_CACHE.read_text())
    _CIK_MAP = {
        row["ticker"].upper(): str(row["cik_str"]).zfill(10)
        for row in table.values()
    }
    _TITLE_MAP = {
        row["ticker"].upper(): row["title"]
        for row in table.values()
        if row.get("title")
    }
    _ROWS = [
        (row["ticker"].upper(), row.get("title") or "", int(row["cik_str"]))
        for row in table.values()
    ]


def _cik_map() -> dict[str, str]:
    _load_map()
    return _CIK_MAP or {}


def _display_title(title: str) -> str:
    """SEC titles are often shouty ("MICROSOFT CORP"); title-case all-caps."""
    return title.title() if title.isupper() else title


def cik_for(ticker: str) -> str | None:
    """10-digit CIK for a ticker; ticker map cached under data/ and in memory."""
    return _cik_map().get(ticker.upper())


def title_for(ticker: str) -> str | None:
    """Company title from the SEC ticker map (US listings only), or None."""
    _load_map()
    title = (_TITLE_MAP or {}).get(ticker.upper())
    return _display_title(title) if title else None


def search_companies(query: str, limit: int = 8) -> list[tuple[str, str]]:
    """Search the SEC ticker map by symbol or company name.

    Up to `limit` (ticker, display name) pairs, best match first: exact
    ticker, then ticker prefix, then name word-prefix, then any substring
    of ticker or name (this last tier is what catches multi-word queries
    like "bank of america"). Ties keep the file order, which is roughly
    descending market cap. US listings only — the SEC map carries no
    foreign-exchange symbols.

    Hyphenated share classes of an already-matched company (BAC-PB and the
    other preferred series behind "bank of america") are skipped so a name
    query doesn't return one issuer `limit` times; distinct plain listings
    (GOOG next to GOOGL) survive.

    When no tier matches at all, a fuzzy pass catches typos ("nvidai",
    "microsft") — score-ordered, same share-class dedup.
    """
    q = query.strip().upper()
    if not q:
        return []
    _load_map()
    ranked: list[tuple[int, int, str, str]] = []
    seen_cik: set[int] = set()
    for i, (ticker, title, cik) in enumerate(_ROWS or []):
        name = title.upper()
        if ticker == q:
            rank = 0
        elif ticker.startswith(q):
            rank = 1
        elif any(word.startswith(q) for word in name.split()):
            rank = 2
        elif q in ticker or q in name:
            rank = 3
        else:
            continue
        if "-" in ticker and cik in seen_cik and ticker != q:
            continue
        seen_cik.add(cik)
        ranked.append((rank, i, ticker, title))
    if not ranked and len(q) >= MIN_QUERY:
        return _fuzzy_companies(q, limit)
    ranked.sort()
    return [(t, _display_title(n)) for _, _, t, n in ranked[:limit]]


def _fuzzy_companies(q: str, limit: int) -> list[tuple[str, str]]:
    """Typo fallback for `search_companies` — best fuzzy scores first."""
    scored: list[tuple[float, int, str, str]] = []
    seen_cik: set[int] = set()
    for i, (ticker, title, cik) in enumerate(_ROWS or []):
        score = max(fuzzy_ratio(q, ticker), fuzzy_ratio(q, title.upper()))
        if score < FUZZY_CUTOFF:
            continue
        if "-" in ticker and cik in seen_cik:
            continue
        seen_cik.add(cik)
        scored.append((-score, i, ticker, title))
    scored.sort()
    return [(t, _display_title(n)) for _, _, t, n in scored[:limit]]


def company_facts(ticker: str) -> dict | None:
    """Full XBRL companyfacts payload, or None for non-US filers."""
    cik = cik_for(ticker)
    return _get_json(FACTS_URL.format(cik=cik)) if cik else None


def latest_annual_fact(facts: dict, tags: list[str]) -> tuple[str, float] | None:
    """Most recent 10-K value for the first matching tag: (fy_end, value)."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        units = gaap.get(tag, {}).get("units", {})
        annual = [
            f
            for f in units.get("USD", [])
            if f.get("form") == "10-K" and f.get("fp") == "FY" and "end" in f
        ]
        if annual:
            latest = max(annual, key=lambda f: f["end"])
            return latest["end"], float(latest["val"])
    return None


def cross_check(ticker: str) -> dict[str, tuple[str, float] | None]:
    """Latest filed annual revenue and net income — the 'fact' anchor."""
    facts = company_facts(ticker)
    if facts is None:
        return {"revenue": None, "net_income": None}
    return {
        "revenue": latest_annual_fact(facts, REVENUE_TAGS),
        "net_income": latest_annual_fact(facts, NET_INCOME_TAGS),
    }


def _duration_days(fact: dict) -> int | None:
    try:
        return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
    except (KeyError, ValueError):
        return None


def diluted_eps_facts(ticker: str) -> pd.DataFrame:
    """Raw diluted-EPS facts from XBRL: columns end/filed/eps/kind, oldest-first.

    `kind` is 'Q' for a discrete ~3-month period or 'FY' for a full fiscal year
    (10-Ks report the year, never a discrete Q4). Values are AS REPORTED — not
    split-adjusted and not Q4-reconstructed; both happen in analysis.pe_history,
    which must split-adjust to a common basis *before* deriving Q4 = FY − 3Q
    (a split mid-year otherwise mixes per-share bases and corrupts the result).

    `filed` is the earliest date each figure was public, so downstream TTM logic
    can avoid look-ahead bias. Empty for non-US filers (no CIK) or no EPS facts.
    """
    facts = company_facts(ticker)
    if facts is None:
        return pd.DataFrame(columns=["end", "filed", "eps", "kind"])
    gaap = facts.get("facts", {}).get("us-gaap", {})
    raw: list[dict] = []
    for tag in EPS_TAGS:
        for arr in gaap.get(tag, {}).get("units", {}).values():
            raw += arr
        if raw:
            break

    picked: dict[tuple[date, str], tuple[float, str]] = {}
    for f in raw:
        dur = _duration_days(f)
        if dur is None or "val" not in f:
            continue
        if 80 <= dur <= 100:
            kind = "Q"
        elif 350 <= dur <= 385:
            kind = "FY"
        else:
            continue
        end, filed, val = date.fromisoformat(f["end"]), f["filed"], float(f["val"])
        key = (end, kind)
        if key not in picked or filed < picked[key][1]:  # earliest filing wins
            picked[key] = (val, filed)

    rows = sorted((e, filed, val, kind) for (e, kind), (val, filed) in picked.items())
    return pd.DataFrame(rows, columns=["end", "filed", "eps", "kind"])
