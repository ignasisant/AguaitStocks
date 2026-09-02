"""BaFin directors' dealings — Art. 19 MAR managers' transactions (Germany).

The German half of what `insiders.py` does for the US. Form 4 is a US-only
artifact, so a Frankfurt or Xetra line degrades to an empty insider section no
matter how much its board is buying. MAR Article 19 is the EU equivalent —
officers, directors and people closely associated with them must notify the
issuer and the national regulator within three business days — and BaFin
republishes every German notification in a public database.

That database exports its result table as XML (displaytag's `export` hook), so
one GET per issuer yields the same fields Form 4 carries: who, their role, buy
or sell, the date, the venue, the average price and the aggregated volume.
Volume is a money amount in BaFin's export, not a share count, so shares are
derived (volume / price) to match `InsiderTx`.

Coverage note: German issuers only, and only notifications published within the
last twelve months — BaFin drops older rows. Rows are EUR-denominated, so
`InsiderTx.currency` says EUR here where EDGAR rows say USD.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
from datetime import date
from xml.etree import ElementTree as ET

from stocks.data.http import get_bytes
from stocks.data.insiders import BUY_CODE, SELL_CODE, InsiderTx

SEARCH_URL = "https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do"

# displaytag paginates and exports by a per-table id baked into the page. The
# id is stable across searches on this database; `export=1` is spelled as the
# hex of the word because that is how the portal's own links spell it.
EXPORT_TABLE_ID = "d-4000784"
EXPORT_FLAG = "6578706f7274"  # hex("export")
EXPORT_XML = "3"  # displaytag media type: 1 = CSV, 3 = XML

# BaFin's Geschäftsart, mapped onto the Form 4 codes `summarize` reads. Only
# outright purchases and sales are the discretionary open-market signal;
# subscriptions, exercises and everything else land on "O" and are shown but
# excluded from the buy/sell net, exactly like grants are on the US side.
TRANSACTION_CODES = {
    "kauf": BUY_CODE,
    "verkauf": SELL_CODE,
    "purchase": BUY_CODE,
    "sale": SELL_CODE,
}
OTHER_CODE = "O"

# Position/Status as BaFin words it. The US side renders roles in English
# ("Chief Executive Officer", "Director"), so these are translated rather than
# left as the only German strings in an otherwise localized table.
# Verified against a live sweep of the database: BaFin only ever writes these
# three, and "Sonstiges" is the only Geschäftsart besides Kauf and Verkauf.
ROLES = {
    "vorstand": "Management Board",
    "aufsichtsrat": "Supervisory Board",
    "in enger beziehung": "Closely associated",
    "geschäftsführer": "Managing Director",
    "persönlich haftender gesellschafter": "General Partner",
}


def _search_url(*, issuer: str | None = None, isin: str | None = None) -> str:
    """Export URL for one issuer search (by ISIN when known, else by name)."""
    params = {
        "emittentIsin": isin or "",
        "emittentName": "" if isin else (issuer or ""),
        "emittentButton": "Suche Emittent",
        EXPORT_FLAG: "1",
        f"{EXPORT_TABLE_ID}-e": EXPORT_XML,
    }
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _amount(raw: str | None) -> tuple[float | None, str]:
    """Parse "267.450,00 EUR" into (267450.0, "EUR").

    German number format: "." groups thousands, "," is the decimal mark. The
    currency travels with the number in every BaFin money column.
    """
    if not raw:
        return None, ""
    parts = raw.split()
    ccy = parts[-1] if len(parts) > 1 and parts[-1].isalpha() else ""
    num = parts[0].replace(".", "").replace(",", ".")
    try:
        return float(num), ccy
    except ValueError:
        return None, ccy


def _date(raw: str | None) -> date | None:
    """Parse a BaFin date — "26.08.2026" in the list export, ISO in details."""
    if not raw:
        return None
    text = raw.split()[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_export(xml_text: str, ticker: str = "") -> list[InsiderTx]:
    """Parse a BaFin XML export into transactions, newest first. Pure.

    The export's first <row> is the header, and column order is positional, so
    the header is read into an index rather than assumed — BaFin has reordered
    these columns before (the notification date sits between the money and the
    trade date in the list export, but not in the per-notification one).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    rows = root.findall("row")
    if len(rows) < 2:
        return []

    def cells(row: ET.Element) -> list[str]:
        return [(c.text or "").strip() for c in row.findall("column")]

    header = {name.lower(): i for i, name in enumerate(cells(rows[0]))}

    def field(values: list[str], *names: str) -> str | None:
        for name in names:
            idx = header.get(name.lower())
            if idx is not None and idx < len(values):
                return values[idx] or None
        return None

    out: list[InsiderTx] = []
    for row in rows[1:]:
        values = cells(row)
        if not any(values):
            continue
        kind = (field(values, "Geschäftsart", "Art des Geschäfts") or "").lower()
        price, ccy = _amount(field(values, "Durchschnittspreis"))
        volume, vol_ccy = _amount(field(values, "Aggregiertes Volumen"))
        # BaFin reports money, not share counts. Only the quotient is a share
        # count, and only when both legs parsed and the price isn't zero.
        shares = volume / price if price and volume is not None else None
        if shares is None:
            continue
        code = TRANSACTION_CODES.get(kind, OTHER_CODE)
        role = field(values, "Position / Status", "Position/Status") or ""
        out.append(
            InsiderTx(
                date=_date(field(values, "Datum des Geschäfts", "Geschäftsdatum")),
                insider=field(values, "Meldepflichtiger") or "Unknown",
                relationship=ROLES.get(role.lower(), role or "Insider"),
                code=code,
                acquired=(code == BUY_CODE),
                shares=shares,
                price=price,
                ticker=ticker.upper(),
                currency=ccy or vol_ccy or "EUR",
                source="BaFin",
            )
        )
    out.sort(key=lambda t: (t.date is not None, t.date), reverse=True)
    return out


def insider_transactions(
    ticker: str = "", *, issuer: str | None = None, isin: str | None = None
) -> list[InsiderTx]:
    """Art. 19 MAR notifications for one German issuer, newest first.

    Best-effort like its EDGAR counterpart: an unknown issuer, a network
    failure or a portal redesign yields an empty list rather than raising.
    Search by `isin` when it is known (exact); `issuer` matches on a name
    prefix, which is what a display name like "Nemetschek SE" gives us.
    """
    if not (issuer or isin):
        return []
    try:
        body = get_bytes(_search_url(issuer=issuer, isin=isin), timeout=20)
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    # A search the portal doesn't understand renders the HTML form back at us
    # instead of the XML export; parse_export shrugs that off as empty.
    return parse_export(body.decode("utf-8", errors="replace"), ticker)
