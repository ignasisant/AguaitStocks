"""Crypto assets, identified by Yahoo Finance pair symbols (BTC-USD, ETH-EUR).

A crypto holding is stored everywhere — watchlist, ledger, picker — as the
full pair symbol, never the bare coin code: bare codes collide with real stock
tickers (SOL is Emeren Group on NYSE, LINK is Interlink Electronics), so
nothing here ever guesses that a bare symbol means a coin. The Revolut crypto
importer normalizes coins to pairs at parse time (coin + statement fiat
currency), which keeps the transaction currency and the quote currency of the
price series identical — FIFO, FX and tax then work unchanged.

The curated name map below powers the picker search ("bitcoin" -> BTC-USD),
display names and logos; pairs outside the map still count as crypto (any
BASE-USD/EUR/GBP form) — they just render without a friendly name.
"""

from __future__ import annotations

import re

from stocks.fuzzy import FUZZY_CUTOFF, MIN_QUERY, fuzzy_ratio

# Fiat quote currencies with reliable Yahoo pairs (also ECB currencies, so the
# FX layer can convert positions on statement currency alone).
QUOTE_CURRENCIES = ("USD", "EUR", "GBP")

_PAIR_RE = re.compile(rf"^([A-Z0-9]{{2,10}})-({'|'.join(QUOTE_CURRENCIES)})$")

# Coin code -> display name, major coins only. Extend freely; used for names,
# search and logos — never to reinterpret a bare symbol as crypto.
CRYPTO_NAMES: dict[str, str] = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "USDT": "Tether",
    "BNB": "BNB",
    "SOL": "Solana",
    "XRP": "XRP",
    "USDC": "USD Coin",
    "ADA": "Cardano",
    "DOGE": "Dogecoin",
    "TON": "Toncoin",
    "TRX": "TRON",
    "AVAX": "Avalanche",
    "SHIB": "Shiba Inu",
    "DOT": "Polkadot",
    "LINK": "Chainlink",
    "BCH": "Bitcoin Cash",
    "LTC": "Litecoin",
    "MATIC": "Polygon",
    "POL": "Polygon Ecosystem Token",
    "UNI": "Uniswap",
    "NEAR": "NEAR Protocol",
    "ICP": "Internet Computer",
    "APT": "Aptos",
    "XLM": "Stellar",
    "ETC": "Ethereum Classic",
    "FIL": "Filecoin",
    "ARB": "Arbitrum",
    "OP": "Optimism",
    "ATOM": "Cosmos",
    "SUI": "Sui",
    "HBAR": "Hedera",
    "VET": "VeChain",
    "IMX": "Immutable",
    "INJ": "Injective",
    "RNDR": "Render",
    "GRT": "The Graph",
    "ALGO": "Algorand",
    "SEI": "Sei",
    "PEPE": "Pepe",
    "FTM": "Fantom",
    "RUNE": "THORChain",
    "AAVE": "Aave",
    "MKR": "Maker",
    "EOS": "EOS",
    "XTZ": "Tezos",
    "SAND": "The Sandbox",
    "MANA": "Decentraland",
    "CRO": "Cronos",
    "KAS": "Kaspa",
    "DYDX": "dYdX",
}


def split_pair(ticker: str) -> tuple[str, str] | None:
    """(coin, fiat) for a crypto pair symbol, None for anything else.

    Matches BASE-USD/EUR/GBP only — stock class shares (BRK-B, HEI-A) and
    exchange suffixes (RMS.PA) never match.
    """
    m = _PAIR_RE.match(ticker.upper().strip())
    return (m.group(1), m.group(2)) if m else None


def is_crypto(ticker: str) -> bool:
    """True when the symbol is a crypto pair (BTC-USD form)."""
    return split_pair(ticker) is not None


def to_pair(coin: str, currency: str = "USD") -> str:
    """Yahoo pair symbol for a coin code and fiat currency, e.g. BTC-EUR.

    Fiat currencies without a reliable Yahoo pair fall back to USD (the
    transaction keeps its native currency; only the price series differs).
    """
    ccy = currency.upper().strip()
    if ccy not in QUOTE_CURRENCIES:
        ccy = "USD"
    return f"{coin.upper().strip()}-{ccy}"


def crypto_name(ticker: str) -> str | None:
    """Display name for a pair symbol or bare coin code, None when unknown."""
    pair = split_pair(ticker)
    coin = pair[0] if pair else ticker.upper().strip()
    return CRYPTO_NAMES.get(coin)


def search_crypto(query: str, limit: int = 6) -> list[tuple[str, str]]:
    """(pair, name) matches for a query against coin codes and names.

    Returns USD pairs (the deepest Yahoo series); code matches rank before
    name matches so "btc" puts Bitcoin first.
    """
    q = query.upper().strip()
    if not q:
        return []
    by_code = [c for c in CRYPTO_NAMES if q in c]
    by_name = [
        c for c, n in CRYPTO_NAMES.items() if q in n.upper() and c not in by_code
    ]
    matches = by_code + by_name
    if not matches and len(q) >= MIN_QUERY:
        # Typo fallback ("bitcon"): fuzzy over code and name, best first.
        # Score ties keep CRYPTO_NAMES order (major coins first), so Bitcoin
        # beats Bitcoin Cash instead of losing the alphabetical tie-break.
        scored = [
            (-s, i, c)
            for i, (c, n) in enumerate(CRYPTO_NAMES.items())
            if (s := max(fuzzy_ratio(q, c), fuzzy_ratio(q, n.upper())))
            >= FUZZY_CUTOFF
        ]
        matches = [c for _, _, c in sorted(scored)]
    return [(to_pair(c), CRYPTO_NAMES[c]) for c in matches[:limit]]
