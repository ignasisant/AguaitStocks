"""Registry of statement platforms the Import page can parse.

One entry per broker/source: display label, accepted upload extensions, a
one-line exporting hint for the UI, and a parse callable with the uniform
signature ``parse(filename, data) -> ParseResult``. The page stays
platform-agnostic — adding a broker means writing its parser module and
appending a Platform here; validation, preview and commit are shared.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from stocks.portfolio import (
    clicktrade,
    degiro,
    generic,
    ibkr,
    revolut,
    revolut_crypto,
    revolut_pdf,
    trading212,
)
from stocks.portfolio.fees import broker_of
from stocks.portfolio.ledger import Transaction
from stocks.portfolio.revolut import ParseResult


@dataclass(frozen=True)
class Platform:
    key: str  # stable id, stored in the last-import record
    label: str
    file_types: tuple[str, ...]  # extensions st.file_uploader accepts
    hint: str  # one-liner: where to find the export on that platform
    parse: Callable[[str, bytes], ParseResult]  # (filename, raw bytes)
    domain: str | None = None  # brand website, for the selector logo


def _parse_revolut(filename: str, data: bytes) -> ParseResult:
    if filename.lower().endswith(".pdf"):
        return revolut_pdf.parse_pdf(data)
    return revolut.parse_csv(data.decode("utf-8-sig"))


def _parse_generic(filename: str, data: bytes) -> ParseResult:
    return generic.parse_csv(data.decode("utf-8-sig"))


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        key="revolut",
        label="Revolut",
        file_types=("csv", "pdf"),
        hint=(
            "Export from Revolut → Stocks → statement. CSV parses exactly; PDF "
            "is extracted from the transactions table and validated the same way."
        ),
        parse=_parse_revolut,
        domain="revolut.com",
    ),
    Platform(
        key="revolut_crypto",
        label="Revolut crypto",
        file_types=("csv",),
        hint=(
            "Export from Revolut → Crypto → statement (CSV). Coins import as "
            "Yahoo pairs in the statement currency (BTC → BTC-EUR); rewards, "
            "transfers and coin-to-coin exchanges are listed as skipped."
        ),
        parse=lambda filename, data: revolut_crypto.parse_csv(
            data.decode("utf-8-sig")
        ),
        domain="revolut.com",
    ),
    Platform(
        key="trading212",
        label="Trading 212",
        file_types=("csv",),
        hint=(
            "Export from Trading 212 → History → Export (CSV). The file is "
            "only imported if it has the exact Trading 212 columns; trades "
            "record in the instrument currency, dividends carry withholding "
            "as the fee when reported in that currency."
        ),
        parse=lambda filename, data: trading212.parse_csv(
            data.decode("utf-8-sig")
        ),
        domain="trading212.com",
    ),
    Platform(
        key="degiro",
        label="DEGIRO",
        file_types=("csv",),
        hint=(
            "Export from DEGIRO → Activity → Transactions → Export (CSV). "
            "Only imported if it has the exact DEGIRO columns (EN/ES). Rows "
            "import with the ISIN as ticker — map each ISIN to a Yahoo "
            "symbol under `aliases:` in watchlist.yaml. Dividends are in the "
            "Account statement, not this file; add them separately."
        ),
        parse=lambda filename, data: degiro.parse_csv(
            data.decode("utf-8-sig")
        ),
        domain="degiro.com",
    ),
    Platform(
        key="ibkr",
        label="Interactive Brokers",
        file_types=("csv",),
        hint=(
            "Export from IBKR → Performance & Reports → Statements → "
            "Activity (CSV). Only imported if the Trades/Dividends sections "
            "have the exact IBKR columns; stock orders and dividends import, "
            "withholding-tax rows are listed for manual review."
        ),
        parse=lambda filename, data: ibkr.parse_csv(
            data.decode("utf-8-sig")
        ),
        domain="interactivebrokers.com",
    ),
    Platform(
        key="clicktrade",
        label="ClickTrade / Saxo",
        file_types=("xlsx", "csv"),
        hint=(
            "Export from ClickTrade/SaxoTraderGO → Informes históricos → "
            "Operaciones ejecutadas (Trades executed) → Excel. Spanish and "
            "English headers are recognised; symbols map from the Saxo "
            "exchange code (TEF:xmce → TEF.MC), unknown ones import under "
            "the ISIN — map those in watchlist.yaml `aliases:`. Dividends "
            "are in the account statement, not this report; add them "
            "separately."
        ),
        parse=clicktrade.parse,
        domain="clicktrade.es",
    ),
    Platform(
        key="generic",
        label="Generic CSV",
        file_types=("csv",),
        hint=(
            "Any broker: a CSV with header "
            "`date,ticker,action,quantity,price,currency,fee,note` "
            "(action: buy/sell/dividend/fee/split; currency, fee, note optional)."
        ),
        parse=_parse_generic,
    ),
)


def by_key(key: str) -> Platform:
    """Look up a platform by its stable key; unknown keys fall back to Revolut
    (the only platform that existed before keys were recorded)."""
    for p in PLATFORMS:
        if p.key == key:
            return p
    return PLATFORMS[0]


# Ledger note prefixes (`fees.broker_of`) -> display name. The prefix is what
# the importers stamp, so it isn't always a Platform key: "revolut crypto ..."
# reads back as "revolut" (same broker), a hand-entered row as "manual", and
# an LLM-mapped export as whatever its own note says.
BROKER_NAMES = {
    "revolut": "Revolut",
    "trading212": "Trading 212",
    "degiro": "DEGIRO",
    "ibkr": "IBKR",
    "clicktrade": "ClickTrade",
    "saxo": "Saxo",
}


def broker_label(key: str) -> str:
    """Display name for a ledger broker prefix; unknown ones title-case."""
    return BROKER_NAMES.get(key, key.replace("_", " ").title())


def broker_domain(key: str) -> str | None:
    """Brand website for a ledger broker prefix (its logo), when a platform of
    that exact key declares one — "manual" and one-off notes have none."""
    for p in PLATFORMS:
        if p.key == key:
            return p.domain
    return None


# ------------------------------------------------------------ import origin
# Attribution is carried by the note's first word (`fees.broker_of`), and every
# broker parser stamps its own. A file no parser owned — an LLM-mapped export,
# a ledger-format CSV — arrives with nothing in front, so the origin has to be
# asked for at import time: without it the rows land under whatever their note
# happened to say and the Fees and Custody views can't place the shares.

OTHER = "other"  # a real origin, just not one this registry parses


def broker_options() -> tuple[str, ...]:
    """Keys to offer when the user names an upload's origin: every broker the
    ledger already knows by name, `OTHER` last."""
    return (*sorted(BROKER_NAMES), OTHER)


def broker_key(name: str) -> str:
    """A typed-in broker name as a ledger prefix — one lowercase word, since
    only the note's first word is ever read back as the broker."""
    return "_".join(re.findall(r"[a-z0-9]+", name.lower()))[:24] or OTHER


def detected_broker(transactions: list[Transaction]) -> str:
    """The origin the rows already name, when their parser stamped one and
    every row agrees; "" when nothing in the batch names a known broker, which
    is exactly when the user has to be asked."""
    keys = {broker_of(t) for t in transactions}
    key = keys.pop() if len(keys) == 1 else ""
    return key if key in BROKER_NAMES else ""


def stamp_broker(transactions: list[Transaction], broker: str) -> list[Transaction]:
    """Copies of `transactions` whose notes start with `broker`.

    A broker word already in front is replaced rather than having a second one
    stacked on it, so re-stating an origin (or overriding a parser's) stays
    idempotent; the rest of the note — an instrument name, a mapped label —
    stays behind it.
    """
    key = broker_key(broker)
    known = {p.key for p in PLATFORMS} | set(BROKER_NAMES) | {OTHER, "manual", key}
    out = []
    for tx in transactions:
        words = tx.note.split()
        if words and words[0].lower() in known:
            words = words[1:]
        out.append(replace(tx, note=" ".join([key, *words])[:120]))
    return out
