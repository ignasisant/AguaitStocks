"""Insider transactions — SEC Form 4 (corporate insiders buying/selling).

Officers, directors and 10%-owners must file a Form 4 within two business days
of trading their own company's stock. Cluster buying (several insiders buying on
the open market) is one of the few insider signals with any predictive value;
routine option-grant/tax-withholding noise is not, so `summarize` separates
open-market purchases (code P) and sales (code S) from the rest.

Source is EDGAR, matching the verification hierarchy (SEC primary). Non-US
filers have no CIK and degrade to an empty frame, exactly like the fundamentals
fallbacks. Parsing (`parse_form4`) and aggregation (`summarize`) are pure and
tested offline; only `insider_transactions` touches the network.
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import pandas as pd

from stocks.data.edgar import _user_agent, cik_for
from stocks.data.http import get_bytes

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

# Form 4 transaction codes. P/S are the discretionary open-market trades the
# market actually reads as a signal; the rest are grants, option mechanics,
# tax withholding and gifts — noise for signal purposes but shown for context.
CODE_LABELS = {
    "P": "Buy (open market)",
    "S": "Sell (open market)",
    "A": "Grant/award",
    "D": "Disposition to issuer",
    "F": "Tax withholding",
    "M": "Option exercise",
    "C": "Conversion",
    "X": "Option exercise",
    "G": "Gift",
    "W": "Inherited/will",
}
BUY_CODE = "P"
SELL_CODE = "S"


@dataclass(frozen=True)
class InsiderTx:
    """One non-derivative Form 4 transaction line."""

    date: date | None
    insider: str
    relationship: str  # e.g. "CEO", "Director", "10% owner"
    code: str  # raw Form 4 transaction code (P/S/A/M/…)
    acquired: bool  # True for an acquisition (A), False for a disposition (D)
    shares: float
    price: float | None
    ticker: str = ""

    @property
    def value(self) -> float | None:
        """Notional dollar value (shares × price); None when price is absent."""
        return None if self.price is None else self.shares * self.price

    @property
    def is_open_market(self) -> bool:
        return self.code in (BUY_CODE, SELL_CODE)


@dataclass(frozen=True)
class InsiderSummary:
    """Open-market buy/sell aggregate over a trailing window (pure output)."""

    window_days: int
    buy_count: int = 0
    sell_count: int = 0
    buy_shares: float = 0.0
    sell_shares: float = 0.0
    buy_value: float = 0.0
    sell_value: float = 0.0
    buyers: int = 0  # distinct insiders with ≥1 open-market buy
    sellers: int = 0  # distinct insiders with ≥1 open-market sell

    @property
    def net_value(self) -> float:
        """Open-market buy value minus sell value; positive = net buying."""
        return self.buy_value - self.sell_value

    @property
    def net_shares(self) -> float:
        return self.buy_shares - self.sell_shares

    @property
    def has_activity(self) -> bool:
        return bool(self.buy_count or self.sell_count)

    @property
    def cluster_buy(self) -> bool:
        """Multiple distinct insiders buying and outweighing sells — the signal."""
        return self.buyers >= 2 and self.net_value > 0


def _text(node: ET.Element | None, path: str) -> str | None:
    """Text at `path` under `node`, unwrapping the Form 4 <value> child."""
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    val = el.find("value")
    target = val if val is not None else el
    return target.text.strip() if target is not None and target.text else None


def _float(node: ET.Element | None, path: str) -> float | None:
    raw = _text(node, path)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _relationship(owner: ET.Element | None) -> str:
    """Short role label from a <reportingOwnerRelationship> block."""
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    if rel is None:
        return "Insider"
    title = _text(rel, "officerTitle")
    parts: list[str] = []
    if _text(rel, "isOfficer") in ("1", "true") and title:
        parts.append(title)
    elif _text(rel, "isOfficer") in ("1", "true"):
        parts.append("Officer")
    if _text(rel, "isDirector") in ("1", "true"):
        parts.append("Director")
    if _text(rel, "isTenPercentOwner") in ("1", "true"):
        parts.append("10% owner")
    return ", ".join(parts) or "Insider"


def parse_form4(xml_text: str) -> list[InsiderTx]:
    """Parse one Form 4 ownership document into non-derivative transactions.

    Pure. Returns [] for malformed XML or a filing with no non-derivative
    transactions (e.g. a holdings-only amendment). Derivative (option) lines are
    intentionally skipped — the share-count signal lives in the common-stock
    table.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    owner = root.find("reportingOwner")
    insider = _text(owner, "reportingOwnerId/rptOwnerName") or "Unknown"
    relationship = _relationship(owner)
    ticker = _text(root, "issuer/issuerTradingSymbol") or ""

    txs: list[InsiderTx] = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(t, "transactionCoding/transactionCode")
        shares = _float(t, "transactionAmounts/transactionShares")
        if code is None or shares is None:
            continue  # a holding line, not a transaction
        ad = _text(t, "transactionAmounts/transactionAcquiredDisposedCode")
        raw_date = _text(t, "transactionDate")
        try:
            tx_date = date.fromisoformat(raw_date) if raw_date else None
        except ValueError:
            tx_date = None
        txs.append(
            InsiderTx(
                date=tx_date,
                insider=insider,
                relationship=relationship,
                code=code,
                acquired=(ad == "A"),
                shares=shares,
                price=_float(t, "transactionAmounts/transactionPricePerShare"),
                ticker=ticker,
            )
        )
    return txs


def summarize(
    txs: list[InsiderTx], ref: date | None = None, within_days: int = 180
) -> InsiderSummary:
    """Aggregate open-market buys (P) vs sells (S) over the trailing window.

    Pure: `ref` defaults to today, but pass it in tests for a fixed clock. Only
    codes P/S count — grants, exercises and tax withholding are excluded so the
    net is a real discretionary buy/sell balance, not grant noise.
    """
    ref = ref or date.today()
    cutoff = ref - timedelta(days=within_days)
    buyers: set[str] = set()
    sellers: set[str] = set()
    agg = {
        "buy_count": 0, "sell_count": 0, "buy_shares": 0.0,
        "sell_shares": 0.0, "buy_value": 0.0, "sell_value": 0.0,
    }
    for t in txs:
        if not t.is_open_market or t.date is None or t.date < cutoff:
            continue
        side = "buy" if t.code == BUY_CODE else "sell"
        agg[f"{side}_count"] += 1
        agg[f"{side}_shares"] += t.shares
        agg[f"{side}_value"] += t.value or 0.0
        (buyers if side == "buy" else sellers).add(t.insider)
    return InsiderSummary(
        window_days=within_days,
        buyers=len(buyers),
        sellers=len(sellers),
        **agg,
    )


def transactions_frame(txs: list[InsiderTx]) -> pd.DataFrame:
    """Newest-first display frame of transactions (pure)."""
    cols = ["Date", "Insider", "Role", "Type", "Shares", "Price", "Value"]
    if not txs:
        return pd.DataFrame(columns=cols)
    rows = [
        {
            "Date": t.date,
            "Insider": t.insider.title(),
            "Role": t.relationship,
            "Type": CODE_LABELS.get(t.code, t.code),
            "Shares": (t.shares if t.acquired else -t.shares),
            "Price": t.price,
            "Value": (t.value if t.acquired else -(t.value or 0)) if t.value else None,
        }
        for t in txs
    ]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values("Date", ascending=False, na_position="last").reset_index(
        drop=True
    )


def _get(url: str) -> bytes:
    return get_bytes(url, user_agent=_user_agent())


def _recent_form4_docs(cik: str, limit: int) -> list[str]:
    """URLs of the most recent Form 4 primary documents for a CIK."""
    import json

    data = json.loads(_get(SUBMISSIONS_URL.format(cik=cik)))
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    cik_int = str(int(cik))  # archive paths use the CIK without leading zeros
    urls: list[str] = []
    for form, acc, doc in zip(forms, accessions, docs, strict=False):
        if form not in ("4", "4/A") or not doc:
            continue
        # primaryDocument points at the XSL-rendered HTML (e.g.
        # "xslF345X06/form4.xml"); the raw ownership XML is the same filename at
        # the accession root, so drop any leading render-directory prefix.
        raw_doc = doc.rsplit("/", 1)[-1]
        urls.append(
            ARCHIVE_DOC_URL.format(
                cik=cik_int, accession=acc.replace("-", ""), doc=raw_doc
            )
        )
        if len(urls) >= limit:
            break
    return urls


def insider_transactions(ticker: str, limit: int = 40) -> list[InsiderTx]:
    """Recent Form 4 transactions for a ticker, newest filing first.

    Best-effort: non-US filers (no CIK) and any network/parse failure yield an
    empty list rather than raising, matching the rest of the data layer. `limit`
    caps how many Form 4 filings are fetched (each is one HTTP request).
    """
    try:
        cik = cik_for(ticker)
    except (urllib.error.URLError, TimeoutError):
        return []
    if cik is None:
        return []
    try:
        doc_urls = _recent_form4_docs(cik, limit)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []

    out: list[InsiderTx] = []
    for url in doc_urls:
        try:
            xml = _get(url).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError):
            continue
        out.extend(parse_form4(xml))
    return out
