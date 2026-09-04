"""ETFs and traded funds — what they are, and what stands in for fundamentals.

A fund is a wrapper, not a business: it has no revenue, no EBIT, no insider
filings and no earnings date, so every fundamental surface in the app either
prints n/a for one (the KPI grid, the screener) or invents a shape that isn't
there (a moat score for VWCE). The three things a fund holder actually needs —
what it costs to hold, what it holds, and what that basket is exposed to —
were nowhere.

This module is the fund half of the instrument model, mirroring
stocks.data.crypto: a cheap classifier the render paths can gate on, and one
profile fetch that carries the fund's own numbers.

**Classification** keys off Yahoo's ``quoteType`` (ETF / MUTUALFUND), learned
once per symbol and kept in ``data/quote_types.json`` — an instrument's kind
is a global fact, not per-account, and it does not change, so the cache never
expires. `KNOWN_FUNDS` seeds it with the funds a European retail investor
actually meets (US majors plus the UCITS lines sold in Spain), which means the
common cases classify with no network at all. Everything else costs one
``.info`` call, once, and callers on hot paths pass ``fetch=False`` to get a
cache-only answer.

**Units** are normalised here because Yahoo is not consistent about them:
``fundOperations`` reports the expense ratio as a fraction (0.000945) while
``info.netExpenseRatio`` reports the same number as a percent (0.0945). Both
become a fraction. Two Yahoo fields are deliberately NOT surfaced: the
``Total Net Assets`` row of ``fundOperations`` (it repeats the category
average, so it is the sleeve's size, not the fund's) and ``equityHoldings``
valuation averages (UCITS lines report them reciprocated — a P/E of 0.0434 —
and a reciprocal that is silently wrong is worse than a blank).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from stocks.config import DATA_DIR
from stocks.formatting import finite
from stocks.fuzzy import FUZZY_CUTOFF, MIN_QUERY, fuzzy_ratio

# Yahoo quoteType values this module treats as a fund. INDEX is deliberately
# absent: an index is not holdable, and the app charts it as a benchmark.
FUND_TYPES = frozenset({"ETF", "MUTUALFUND"})

# symbol -> quoteType, learned once and never expired (see module docstring).
TYPE_CACHE = DATA_DIR / "quote_types.json"

# Yahoo's sector slugs spelled the way `info["sector"]` spells them for a
# single stock, so a fund's look-through weights land in the SAME allocation
# buckets as the shares held directly next to it.
SECTOR_LABELS = {
    "technology": "Technology",
    "financial_services": "Financial Services",
    "consumer_cyclical": "Consumer Cyclical",
    "consumer_defensive": "Consumer Defensive",
    "healthcare": "Healthcare",
    "communication_services": "Communication Services",
    "industrials": "Industrials",
    "energy": "Energy",
    "basic_materials": "Basic Materials",
    "real_estate": "Real Estate",
    "realestate": "Real Estate",  # Yahoo emits both spellings
    "utilities": "Utilities",
}

# Asset-class slugs from `funds_data.asset_classes`.
ASSET_CLASS_LABELS = {
    "stockPosition": "Equity",
    "bondPosition": "Bonds",
    "cashPosition": "Cash",
    "preferredPosition": "Preferred",
    "convertiblePosition": "Convertible",
    "otherPosition": "Other",
}

# The funds a EUR retail investor actually meets: US majors (held through any
# broker) and the UCITS lines sold in Spain. Seeds classification offline, name
# lookups (the picker, ledger imports) and the picker's fund tier. Symbols are
# Yahoo's; extend freely — a wrong entry only costs a bad search suggestion,
# and `scripts/check_fund_catalog.py` verifies the whole list against Yahoo.
KNOWN_FUNDS: dict[str, str] = {
    # --- US broad market -----------------------------------------------
    "SPY": "SPDR S&P 500 ETF Trust",
    "VOO": "Vanguard S&P 500 ETF",
    "IVV": "iShares Core S&P 500 ETF",
    "VTI": "Vanguard Total Stock Market ETF",
    "VT": "Vanguard Total World Stock ETF",
    "QQQ": "Invesco QQQ Trust",
    "QQQM": "Invesco NASDAQ 100 ETF",
    "DIA": "SPDR Dow Jones Industrial Average ETF",
    "IWM": "iShares Russell 2000 ETF",
    "RSP": "Invesco S&P 500 Equal Weight ETF",
    # --- US style / income ----------------------------------------------
    "SCHD": "Schwab US Dividend Equity ETF",
    "VIG": "Vanguard Dividend Appreciation ETF",
    "VYM": "Vanguard High Dividend Yield ETF",
    "JEPI": "JPMorgan Equity Premium Income ETF",
    "VUG": "Vanguard Growth ETF",
    "VTV": "Vanguard Value ETF",
    "ARKK": "ARK Innovation ETF",
    # --- sectors / themes -------------------------------------------------
    "XLK": "Technology Select Sector SPDR Fund",
    "XLF": "Financial Select Sector SPDR Fund",
    "XLE": "Energy Select Sector SPDR Fund",
    "XLV": "Health Care Select Sector SPDR Fund",
    "SMH": "VanEck Semiconductor ETF",
    "SOXX": "iShares Semiconductor ETF",
    "VGT": "Vanguard Information Technology ETF",
    "VNQ": "Vanguard Real Estate ETF",
    # --- ex-US / emerging --------------------------------------------------
    "VEA": "Vanguard FTSE Developed Markets ETF",
    "IEFA": "iShares Core MSCI EAFE ETF",
    "EFA": "iShares MSCI EAFE ETF",
    "VWO": "Vanguard FTSE Emerging Markets ETF",
    "IEMG": "iShares Core MSCI Emerging Markets ETF",
    "EEM": "iShares MSCI Emerging Markets ETF",
    "MCHI": "iShares MSCI China ETF",
    "KWEB": "KraneShares CSI China Internet ETF",
    "FXI": "iShares China Large-Cap ETF",
    "INDA": "iShares MSCI India ETF",
    "EWJ": "iShares MSCI Japan ETF",
    "EWZ": "iShares MSCI Brazil ETF",
    # --- bonds / commodities / crypto wrappers ------------------------------
    "AGG": "iShares Core US Aggregate Bond ETF",
    "BND": "Vanguard Total Bond Market ETF",
    "TLT": "iShares 20+ Year Treasury Bond ETF",
    "IEF": "iShares 7-10 Year Treasury Bond ETF",
    "LQD": "iShares iBoxx Investment Grade Corporate Bond ETF",
    "HYG": "iShares iBoxx High Yield Corporate Bond ETF",
    "GLD": "SPDR Gold Shares",
    "IAU": "iShares Gold Trust",
    "SLV": "iShares Silver Trust",
    "IBIT": "iShares Bitcoin Trust ETF",
    "FBTC": "Fidelity Wise Origin Bitcoin Fund",
    # --- UCITS: world / US ---------------------------------------------------
    "IWDA.AS": "iShares Core MSCI World UCITS ETF (Acc)",
    "IWDA.L": "iShares Core MSCI World UCITS ETF (Acc)",
    "EUNL.DE": "iShares Core MSCI World UCITS ETF (Acc)",
    "SWDA.MI": "iShares Core MSCI World UCITS ETF (Acc)",
    "XDWD.DE": "Xtrackers MSCI World UCITS ETF 1C",
    "VWCE.DE": "Vanguard FTSE All-World UCITS ETF (Acc)",
    "VWRL.AS": "Vanguard FTSE All-World UCITS ETF (Dist)",
    "VUSA.AS": "Vanguard S&P 500 UCITS ETF (Dist)",
    "VUAA.DE": "Vanguard S&P 500 UCITS ETF (Acc)",
    "CSPX.AS": "iShares Core S&P 500 UCITS ETF (Acc)",
    "CSPX.L": "iShares Core S&P 500 UCITS ETF (Acc)",
    "SXR8.DE": "iShares Core S&P 500 UCITS ETF (Acc)",
    "EQQQ.L": "Invesco EQQQ NASDAQ-100 UCITS ETF",
    "EQQQ.DE": "Invesco EQQQ NASDAQ-100 UCITS ETF",
    "SPPW.DE": "SPDR MSCI World UCITS ETF",
    "SXRV.DE": "iShares NASDAQ 100 UCITS ETF (Acc)",
    # --- UCITS: emerging / Europe / bonds --------------------------------------
    "EIMI.L": "iShares Core MSCI EM IMI UCITS ETF (Acc)",
    "IS3N.DE": "iShares Core MSCI EM IMI UCITS ETF (Acc)",
    "MEUD.PA": "Amundi Stoxx Europe 600 UCITS ETF (Acc)",
    "AGGH.MI": "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)",
    "EUNA.DE": "iShares Core Global Aggregate Bond UCITS ETF EUR Hedged (Acc)",
    "IB01.L": "iShares $ Treasury Bond 0-1yr UCITS ETF",
}


@dataclass(frozen=True)
class FundHolding:
    """One line of the fund's disclosed basket."""

    symbol: str
    name: str
    weight: float  # fraction of the fund, 0.075 == 7.5%


@dataclass(frozen=True)
class FundProfile:
    """What a fund is and what it holds — the fundamentals stand-in.

    Every figure is a fraction where it is a rate (expense ratio, yield,
    turnover, weights), absolute in `currency` where it is money (`aum`), and
    None where Yahoo has nothing. Yahoo's coverage of UCITS lines is thinner
    than of US funds: expect `aum`, `category` and `dividend_yield` to be
    missing for European listings and the profile to still be worth showing.
    """

    ticker: str
    name: str
    quote_type: str  # ETF | MUTUALFUND
    currency: str | None = None
    category: str | None = None
    family: str | None = None
    legal_type: str | None = None
    expense_ratio: float | None = None
    aum: float | None = None
    dividend_yield: float | None = None
    turnover: float | None = None
    bond_duration: float | None = None  # years, bond sleeves only
    bond_maturity: float | None = None  # years
    description: str = ""
    holdings: tuple[FundHolding, ...] = ()
    sectors: tuple[tuple[str, float], ...] = ()  # (display label, fraction)
    asset_classes: tuple[tuple[str, float], ...] = ()

    @property
    def top_weight(self) -> float | None:
        """Weight of the disclosed basket's largest line."""
        return self.holdings[0].weight if self.holdings else None

    @property
    def disclosed_weight(self) -> float:
        """Fraction of the fund the disclosed holdings add up to.

        Yahoo publishes the top ten only, so this is a floor on concentration
        (0.35 means "the ten biggest are 35% of it"), never the whole book.
        """
        return float(sum(h.weight for h in self.holdings))

    @property
    def is_bond_fund(self) -> bool:
        bonds = dict(self.asset_classes).get("Bonds", 0.0)
        return bonds > 0.5


# ------------------------------------------------------------- classification


def _read_types() -> dict[str, str]:
    try:
        data = json.loads(TYPE_CACHE.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


_types: dict[str, str] | None = None


def _known_types() -> dict[str, str]:
    """The learned cache, seeded with the catalog, read from disk once."""
    global _types
    if _types is None:
        _types = {s: "ETF" for s in KNOWN_FUNDS} | _read_types()
    return _types


def remember(ticker: str, quote_type: str | None) -> None:
    """Record `ticker`'s Yahoo quoteType, so no one has to fetch it again.

    Called wherever the app already holds an `.info` payload (the ticker page,
    the screener pull). Writing the cache is best-effort: a read-only or full
    filesystem costs a repeated lookup, never a failed render.
    """
    if not ticker or not quote_type:
        return
    key, value = ticker.upper(), str(quote_type).upper()
    cache = _known_types()
    if cache.get(key) == value:
        return
    cache[key] = value
    stored = _read_types() | {key: value}
    try:
        TYPE_CACHE.write_text(json.dumps(stored, indent=0, sort_keys=True))
    except OSError:
        pass


def quote_type(ticker: str, *, fetch: bool = True) -> str | None:
    """Yahoo quoteType for `ticker` ("EQUITY", "ETF", "MUTUALFUND"…).

    Cache first; `fetch=False` answers only from what is already known, which
    is what a loop over a whole watchlist wants. A network miss returns None
    and is not cached — an unclassified symbol is treated as a stock
    everywhere, the same behaviour the app had before funds existed.
    """
    key = (ticker or "").upper().strip()
    if not key:
        return None
    if hit := _known_types().get(key):
        return hit
    if not fetch:
        return None
    try:
        from stocks.data.fetch import info as quote_info

        info = quote_info(key)
    except Exception:
        return None
    found = info.get("quoteType")
    remember(key, found)
    return str(found).upper() if found else None


def is_fund_type(quote_type_: str | None) -> bool:
    """Whether a quoteType value names a fund."""
    return bool(quote_type_) and str(quote_type_).upper() in FUND_TYPES


def is_fund(ticker: str, *, fetch: bool = True) -> bool:
    """True when `ticker` is an ETF or a mutual fund.

    Unknown symbols are False: the app must never hide a stock's fundamentals
    because a lookup failed.
    """
    return is_fund_type(quote_type(ticker, fetch=fetch))


def fund_name(ticker: str) -> str | None:
    """Catalog display name for a fund symbol, None when it isn't in it."""
    return KNOWN_FUNDS.get((ticker or "").upper().strip())


def search_funds(query: str, limit: int = 4) -> list[tuple[str, str]]:
    """(symbol, name) catalog matches — the picker's offline fund tier.

    Yahoo's worldwide search already covers funds, but it is the tier that
    dies first when Yahoo throttles the deploy's egress IP (symbols.COOLDOWN),
    and "I can't find my own ETF" is the worst moment for that. Symbol matches
    rank before name matches, with the same fuzzy fallback as the coin tier.
    """
    q = (query or "").upper().strip()
    if not q:
        return []
    # Exact symbol, then symbols starting with the query, then symbols
    # containing it, then names: typing "VT" wants VT itself, not VTI because
    # the catalog happens to list it first.
    tiers: list[list[str]] = [
        [s for s in KNOWN_FUNDS if s == q or s.split(".")[0] == q],
        [s for s in KNOWN_FUNDS if s.startswith(q)],
        [s for s in KNOWN_FUNDS if q in s],
        [s for s, n in KNOWN_FUNDS.items() if q in n.upper()],
    ]
    matches: list[str] = []
    for tier in tiers:
        matches += [s for s in tier if s not in matches]
    if not matches and len(q) >= MIN_QUERY:
        scored = [
            (-score, i, s)
            for i, (s, n) in enumerate(KNOWN_FUNDS.items())
            if (score := max(fuzzy_ratio(q, s), fuzzy_ratio(q, n.upper())))
            >= FUZZY_CUTOFF
        ]
        matches = [s for _, _, s in sorted(scored)]
    return [(s, KNOWN_FUNDS[s]) for s in matches[:limit]]


# ------------------------------------------------------------------- profile


def _frame_value(frame, row: str) -> float | None:
    """First column's `row` from a yfinance two-column averages frame.

    Column one is the fund, column two the category average; only the fund's
    own reading is ever wanted here.
    """
    try:
        if frame is None or frame.empty or row not in frame.index:
            return None
        return finite(frame.iloc[:, 0].get(row))
    except Exception:
        return None


def _weights(raw: dict | None, labels: dict[str, str]) -> tuple[tuple[str, float], ...]:
    """Slug->fraction mapping as sorted (display label, fraction), zeros out.

    Slugs with no label keep their own name title-cased, so a bucket Yahoo
    adds later shows up instead of vanishing.
    """
    out: dict[str, float] = {}
    for slug, value in (raw or {}).items():
        weight = finite(value)
        if not weight or weight <= 0:
            continue
        label = labels.get(str(slug)) or str(slug).replace("_", " ").title()
        out[label] = out.get(label, 0.0) + weight
    return tuple(sorted(out.items(), key=lambda kv: -kv[1]))


def _holdings(frame) -> tuple[FundHolding, ...]:
    """`funds_data.top_holdings` as records; malformed rows drop out."""
    rows: list[FundHolding] = []
    try:
        if frame is None or frame.empty:
            return ()
        for symbol, row in frame.iterrows():
            weight = finite(row.get("Holding Percent"))
            if weight is None:
                continue
            rows.append(
                FundHolding(
                    symbol=str(symbol).upper(),
                    name=str(row.get("Name") or symbol),
                    weight=weight,
                )
            )
    except Exception:
        return ()
    return tuple(sorted(rows, key=lambda h: -h.weight))


def _expense_ratio(ops, info: dict) -> float | None:
    """Expense ratio as a fraction, from whichever field carries it.

    `fundOperations` is the precise one (0.000945 for SPY) but is blank for
    some UCITS lines; `info.netExpenseRatio` is the same figure as a percent
    (0.2 == 0.20%), so it is divided down.
    """
    ratio = _frame_value(ops, "Annual Report Expense Ratio")
    if ratio and ratio > 0:
        return ratio
    # A blank or zero ops row is common on European listings, and a fund that
    # genuinely costs nothing does not exist — fall through rather than
    # reporting 0.00%.
    percent = finite(info.get("netExpenseRatio")) or finite(
        info.get("annualReportExpenseRatio")
    )
    return percent / 100 if percent else None


def fetch_profile(ticker: str, info: dict | None = None) -> FundProfile | None:
    """The fund's own numbers, or None when `ticker` is not a fund.

    One yfinance round trip for `.info` (skipped when the caller already has
    it) and one for `funds_data`. Any failure below the classification —
    Yahoo dropped the holdings, the frame changed shape — degrades to a
    profile with fewer fields, never an exception: the ticker page renders
    this instead of the fundamentals block, so a raise here is a blank page.
    """
    key = (ticker or "").upper().strip()
    if not key:
        return None
    import yfinance as yf

    from stocks.data.fetch import resolve

    symbol = resolve(key)
    if info is None:
        try:
            from stocks.data.fetch import info as quote_info

            info = quote_info(symbol)
        except Exception:
            return None
    kind = str(info.get("quoteType") or "").upper()
    remember(key, kind)
    if not is_fund_type(kind):
        return None

    ops = holdings = sectors = asset_classes = bonds = None
    overview: dict = {}
    description = ""
    try:
        data = yf.Ticker(symbol).funds_data
        ops = data.fund_operations
        holdings = data.top_holdings
        sectors = data.sector_weightings
        asset_classes = data.asset_classes
        bonds = data.bond_holdings
        overview = data.fund_overview or {}
        description = str(data.description or "")
    except Exception:
        pass  # a fund with no disclosed basket still has its costs below

    return FundProfile(
        ticker=key,
        name=str(
            info.get("longName")
            or info.get("shortName")
            or fund_name(key)
            or key
        ),
        quote_type=kind,
        currency=info.get("currency"),
        category=info.get("category") or overview.get("categoryName"),
        family=info.get("fundFamily") or overview.get("family"),
        legal_type=info.get("legalType") or overview.get("legalType"),
        expense_ratio=_expense_ratio(ops, info),
        aum=finite(info.get("totalAssets")),
        dividend_yield=finite(info.get("yield"))
        or finite(info.get("trailingAnnualDividendYield")),
        turnover=_frame_value(ops, "Annual Holdings Turnover"),
        bond_duration=_frame_value(bonds, "Duration"),
        bond_maturity=_frame_value(bonds, "Maturity"),
        description=description,
        holdings=_holdings(holdings),
        sectors=_weights(sectors, SECTOR_LABELS),
        asset_classes=_weights(asset_classes, ASSET_CLASS_LABELS),
    )


def sector_split(profile: FundProfile | None) -> dict[str, float]:
    """A profile's sector exposure as {equity sector label: fraction}.

    The look-through the portfolio allocation uses: a fund's weight is spread
    over these buckets instead of landing in one "Unknown" slice, so a book of
    two world ETFs and four stocks reports one honest sector split. Normalised
    to sum to 1 — Yahoo's weights already do, bar rounding — so the caller can
    multiply straight through. Pure: `{}` for a stock, a bond sleeve, or a
    fund whose weights Yahoo doesn't publish.
    """
    if profile is None or not profile.sectors:
        return {}
    total = sum(w for _, w in profile.sectors)
    if total <= 0:
        return {}
    return {label: weight / total for label, weight in profile.sectors}


def sector_weights(ticker: str) -> dict[str, float]:
    """`sector_split` for a symbol — one profile fetch, `{}` for a stock."""
    return sector_split(fetch_profile(ticker))
