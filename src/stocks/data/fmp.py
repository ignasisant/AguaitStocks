"""Financial Modeling Prep client — fallback fundamentals for non-US filers.

EDGAR only covers SEC filers, so emerging-market / foreign names (the user's
S&P + Nasdaq + EM universe) fall back here. Requires a free API key in
FMP_API_KEY; without it every call returns empty and callers degrade to
"EDGAR only". Free tier is rate-limited (~250 req/day) and history-capped,
so treat this as best-effort, not a primary source.
"""

from __future__ import annotations

import json
import os
import urllib.error

import pandas as pd

from stocks.data.http import get_json

INCOME_URL = (
    "https://financialmodelingprep.com/api/v3/income-statement/"
    "{ticker}?period=quarter&limit=40&apikey={key}"
)


def api_key() -> str | None:
    return os.getenv("FMP_API_KEY") or None


def has_key() -> bool:
    return api_key() is not None


def diluted_eps_facts(ticker: str) -> pd.DataFrame:
    """Per-quarter diluted EPS (as reported) from FMP, oldest-first.

    Same columns as edgar.diluted_eps_facts (end/filed/eps/kind) so the P/E
    pipeline is source-agnostic. FMP reports discrete quarters, so kind is
    always 'Q' — no Q4 reconstruction needed. Empty frame when no key is set
    or the request fails.
    """
    cols = ["end", "filed", "eps", "kind"]
    key = api_key()
    if key is None:
        return pd.DataFrame(columns=cols)
    url = INCOME_URL.format(ticker=ticker.upper(), key=key)
    try:
        data = get_json(url)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return pd.DataFrame(columns=cols)
    if not isinstance(data, list):
        return pd.DataFrame(columns=cols)

    rows = []
    for d in data:
        eps = d.get("epsdiluted", d.get("epsDiluted"))
        end = d.get("date")
        filed = d.get("fillingDate") or d.get("filingDate") or end
        if eps is None or end is None:
            continue
        rows.append((pd.to_datetime(end).date(), filed, float(eps), "Q"))
    return pd.DataFrame(sorted(rows), columns=cols)
