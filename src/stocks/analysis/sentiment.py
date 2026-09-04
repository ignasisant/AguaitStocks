"""Market-pulse math: the composite sentiment score, its inputs, and the
personalisation that ties a regime reading to one book's actual exposure.

Everything here is a pure function over price series, so the whole page is
testable without a network. The symbol registries below are the single source
of truth for what the page fetches — add a row and the bulk download, the
tiles and the tests all pick it up.

**Why a composite of our own.** CNN's Fear & Greed index is the obvious thing
to show, and its data endpoint answers a scripted request with
``I'm a teapot. You're a bot.`` — so it is not an option, and scraping around
that would be both fragile and rude. The composite here is built from seven
readings the app can fetch openly, and it explains itself: the page prints
each component's contribution next to the headline number, which a black-box
index could never do.

**How a component becomes a score.** Every component is a raw daily series
where "higher" consistently means one direction (VIX up = fear, HY spread up =
fear, breadth up = greed). Each is converted to 0-100 by its *rolling
percentile rank over the trailing year* — where today's reading sits against
its own last 252 sessions — and inverted where high means fear. That gives
one comparable scale for a volatility index, a credit spread and a price
ratio without hand-tuned thresholds, and because the rank is rolling the
whole thing has a real history: the composite series is the daily mean of the
component series, not a single number with a made-up past.

A percentile is a *relative* statement and must be read as one. A VIX at its
one-year low scores 100 whether that low is 11 or 25 — the composite says
"calm for this year", never "calm in absolute terms". The absolute reading
sits next to it on the page for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from stocks.analysis import naive_dates

TRADING_YEAR = 252
# Percentile windows need enough history to mean anything; below this a rank
# is noise dressed as a score, so the component drops out of the composite
# instead of contributing a made-up number.
MIN_PERIODS = 60

# How many sessions a lagging input may be carried forward before it stops
# counting. The composite mixes series that do not land together: FRED
# publishes the credit spread a day behind, ^VIX prints an in-progress bar
# while the equity closes are still yesterday's, and a national holiday moves
# one exchange and not another. Without a carry, the newest row holds whichever
# single series ran ahead and the "composite" is that one series. With an
# unlimited carry, a series Yahoo has stopped publishing would contribute its
# last known value forever. Five sessions covers every real reporting gap and
# expires a dead feed inside a week.
STALE_LIMIT = 5

# The composite is only quoted for a row carrying at least this many inputs,
# so a partial day cannot masquerade as a full reading.
MIN_COMPONENTS = 4


# ------------------------------------------------------------------- registries
@dataclass(frozen=True)
class Index:
    """One headline index tile. `countries` are the yfinance `info["country"]`
    spellings this index stands in for, which is how the page pins the indices
    that match a reader's own geography to the front of the row."""

    ticker: str
    name: str
    countries: tuple[str, ...] = ()


INDICES: tuple[Index, ...] = (
    Index("^GSPC", "S&P 500", ("United States",)),
    Index("^IXIC", "Nasdaq Composite", ("United States",)),
    Index("^DJI", "Dow Jones", ("United States",)),
    Index("^RUT", "Russell 2000", ("United States",)),
    Index("^STOXX50E", "Euro Stoxx 50", ("Netherlands", "Belgium", "Ireland")),
    Index("^GDAXI", "DAX", ("Germany",)),
    Index("^IBEX", "IBEX 35", ("Spain",)),
    Index("^FCHI", "CAC 40", ("France",)),
    Index("^FTSE", "FTSE 100", ("United Kingdom",)),
    Index("^N225", "Nikkei 225", ("Japan",)),
    Index("^HSI", "Hang Seng", ("Hong Kong", "China")),
    Index("ACWI", "MSCI ACWI"),
    Index("EEM", "MSCI Emerging Markets", ("Brazil", "India", "Taiwan", "Korea")),
)


@dataclass(frozen=True)
class Gauge:
    """A risk/volatility reading shown as level + one-year percentile.

    `high_is_fear` drives both the colour and the inversion: a high VIX is
    fear, a high VIX3M/VIX ratio is calm, and the tile must not paint them the
    same way.
    """

    ticker: str
    name: str
    high_is_fear: bool = True
    fmt: str = "{:.1f}"


# ^VIX9D / ^VIX3M / ^VIX6M are deliberately absent: Yahoo stopped publishing
# daily bars for the whole VIX-term family on 2026-07-17 (a `fast_info` quote
# still answers, the history does not), so a term-structure tile here would be
# a permanently stale number wearing today's date. The ratio still exists as a
# composite input, where the freshness rule drops it on its own and picks it
# back up untouched if Yahoo ever resumes the series.
GAUGES: tuple[Gauge, ...] = (
    Gauge("^VIX", "VIX"),
    Gauge("^VXN", "VXN"),
    Gauge("^VVIX", "VVIX"),
    Gauge("^MOVE", "MOVE"),
    Gauge("^OVX", "OVX"),
    Gauge("^GVZ", "GVZ"),
    Gauge("^SKEW", "SKEW", fmt="{:.0f}"),
)

# yfinance sector label -> the sector ETF that tracks it. The keys are spelled
# the way `info["sector"]` spells them (and the way data.funds.SECTOR_LABELS
# normalises fund look-through into), so a book's own sector weights join
# straight onto this table with no mapping layer in between.
SECTOR_ETFS: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

# Rotation read as a ratio of two tickers: the pair moves up when the first
# side leads. `key` names the i18n label; the page never hardcodes the pair.
FACTOR_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("growth_value", "IVW", "IVE"),
    ("small_large", "IWM", "SPY"),
    ("equal_cap", "RSP", "SPY"),
    ("us_world", "SPY", "ACWX"),
    ("cyclical_defensive", "XLY", "XLP"),
    ("tech_market", "XLK", "SPY"),
)

# Commodities, the dollar and crypto — the cross-asset row. `fmt` is the price
# format; the moves are always percentages.
MACRO_ASSETS: tuple[tuple[str, str, str], ...] = (
    ("DX-Y.NYB", "US Dollar Index", "{:.2f}"),
    ("EURUSD=X", "EUR/USD", "{:.4f}"),
    ("GC=F", "Gold", "{:,.0f}"),
    ("SI=F", "Silver", "{:,.2f}"),
    ("CL=F", "Crude (WTI)", "{:,.2f}"),
    ("HG=F", "Copper", "{:,.2f}"),
    ("BTC-USD", "Bitcoin", "{:,.0f}"),
    ("ETH-USD", "Ethereum", "{:,.0f}"),
)

# The composite's own inputs, plus the benchmarks the personalisation regresses
# a book against. Kept apart from the display registries because these are
# needed whether or not their tile is on screen.
PULSE_TICKERS = (
    "^GSPC", "SPY", "RSP", "EEM", "^VIX", "^VIX3M", "HYG", "LQD", "TLT", "IEF",
)
BENCHMARKS = ("^GSPC", "TLT", "HYG", "EEM")


def all_tickers() -> list[str]:
    """Every symbol the page downloads, deduplicated, in one bulk-request list."""
    seen: dict[str, None] = {}
    for t in (
        [i.ticker for i in INDICES]
        + [g.ticker for g in GAUGES]
        + list(SECTOR_ETFS.values())
        + [t for _, a, b in FACTOR_PAIRS for t in (a, b)]
        + [t for t, _, _ in MACRO_ASSETS]
        + list(PULSE_TICKERS)
        + list(BENCHMARKS)
    ):
        seen[t] = None
    return list(seen)


# ------------------------------------------------------------------- primitives
def naive_index(series: pd.Series) -> pd.Series:
    """`series` with any timezone stripped from its DatetimeIndex.

    yfinance stamps each symbol's bars in its own exchange timezone, so the
    same session reads 2026-09-02 00:00-04:00 for one ticker and
    2026-09-02 00:00+02:00 for another. Concatenating those puts one session on
    two rows. Dropping the zone is right here because these are daily bars —
    the timestamp is a session label, not an instant.
    """
    if getattr(series.index, "tz", None) is not None:
        series = pd.Series(
            series.to_numpy(), index=naive_dates(series.index), name=series.name
        )
    return series


def pct_over(series: pd.Series, days: int) -> float:
    """Total return over the last `days` observations, as a fraction.

    Trading days, not calendar days — the input is a close series, so "21" is
    a month of sessions. NaN when the series is too short to span the window.
    """
    s = series.dropna()
    if len(s) <= days or days <= 0:
        return float("nan")
    prev = float(s.iloc[-days - 1])
    return float(s.iloc[-1]) / prev - 1.0 if prev else float("nan")


def change_series(series: pd.Series, days: int) -> pd.Series:
    """Rolling `days`-session percent change, as a series (fraction)."""
    s = series.dropna()
    return s / s.shift(days) - 1.0


def ytd_return(series: pd.Series) -> float:
    """Return since the last close of the previous calendar year.

    Anchored on the previous year's final close rather than the first close of
    January, so the first trading day's own gap is inside the number — which
    is what every YTD figure a reader compares this to does.
    """
    s = series.dropna()
    if s.empty:
        return float("nan")
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = naive_dates(idx)
        s = pd.Series(s.to_numpy(), index=idx, name=s.name)
    year = idx[-1].year
    prior = s[idx < pd.Timestamp(year=year, month=1, day=1)]
    if prior.empty:
        return float("nan")
    base = float(prior.iloc[-1])
    return float(s.iloc[-1]) / base - 1.0 if base else float("nan")


def from_high(series: pd.Series, window: int = TRADING_YEAR) -> float:
    """Distance below the rolling high (<= 0), the drawdown a reader feels.

    Positive would mean "above its own high", which cannot happen when the
    high includes today, so this is always <= 0 and reads as a drawdown.
    """
    s = series.dropna().iloc[-window:]
    if s.empty:
        return float("nan")
    peak = float(s.max())
    return float(s.iloc[-1]) / peak - 1.0 if peak else float("nan")


def rank_series(
    raw: pd.Series,
    *,
    invert: bool = False,
    window: int = TRADING_YEAR,
    min_periods: int = MIN_PERIODS,
) -> pd.Series:
    """`raw` as a 0-100 rolling percentile rank over the trailing `window`.

    The score is where each day's value sits against the preceding year of its
    own history, so a volatility index, a credit spread and a price ratio come
    out on one comparable scale with no hand-set thresholds. `invert` flips it
    for the readings where a high number means fear.
    """
    s = raw.dropna()
    if s.empty:
        return pd.Series(dtype=float, name=raw.name)
    pct = s.rolling(window, min_periods=min_periods).rank(pct=True) * 100.0
    return (100.0 - pct if invert else pct).dropna()


def percentile_now(
    raw: pd.Series, *, window: int = TRADING_YEAR, min_periods: int = MIN_PERIODS
) -> float:
    """Today's percentile within the trailing window (0-100), NaN if too short."""
    ranked = rank_series(raw, window=window, min_periods=min_periods)
    return float(ranked.iloc[-1]) if not ranked.empty else float("nan")


# -------------------------------------------------------------------- composite
# How far back a component's "and where was it a month ago" tick looks.
COMPONENT_THEN = 21


@dataclass(frozen=True)
class Component:
    """One composite input, ready to print: its 0-100 score and what it read.

    `then` is the same score `COMPONENT_THEN` sessions earlier, so the page can
    mark where the component stood a month ago beside where it stands now — the
    same level-plus-direction contract every other row on the page keeps. NaN
    when the series does not reach back that far.
    """

    key: str
    score: float
    raw: float
    fmt: str = "{:.2f}"
    then: float = float("nan")

    @property
    def text(self) -> str:
        return self.fmt.format(self.raw) if self.raw == self.raw else "n/a"


@dataclass(frozen=True)
class Pulse:
    """The composite: today's score, its daily path, and its inputs.

    `missing` names the components that could not be built (a series the host
    could not fetch), so the page can say what the number is missing instead
    of quietly averaging fewer things.
    """

    score: float
    history: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    components: tuple[Component, ...] = ()
    missing: tuple[str, ...] = ()
    as_of: pd.Timestamp | None = None

    @property
    def regime(self) -> str:
        """Regime key for the headline label — the i18n suffix, not prose."""
        return regime_key(self.score)


# Band edges for the regime label. Deliberately wide in the middle: the
# honest reading of a mid-range composite is "no signal", and a five-band
# scale that calls 52 "greed" invites trading on noise.
REGIME_BANDS: tuple[tuple[float, str], ...] = (
    (20.0, "stress"),
    (40.0, "caution"),
    (60.0, "neutral"),
    (80.0, "appetite"),
    (100.1, "euphoria"),
)


def regime_key(score: float) -> str:
    """Regime label key for a 0-100 composite score ("unknown" for NaN)."""
    if score != score:
        return "unknown"
    for edge, key in REGIME_BANDS:
        if score < edge:
            return key
    return "euphoria"


def _component_inputs(
    closes: dict[str, pd.Series], hy_spread: pd.Series | None
) -> list[tuple[str, pd.Series, bool, str]]:
    """(key, raw daily series, high_is_fear, format) for each composite input.

    Each entry is the *level* series whose rolling percentile becomes the
    score. Anything whose inputs are missing simply isn't produced, and the
    composite reports it under `missing`.
    """
    out: list[tuple[str, pd.Series, bool, str]] = []

    def have(*tickers: str) -> bool:
        return all(
            t in closes and not closes[t].dropna().empty for t in tickers
        )

    if have("^GSPC"):
        spx = closes["^GSPC"].dropna()
        # Distance from the half-year trend, the plainest "is the market above
        # its own recent average" reading. 125 sessions ~ six months.
        out.append(
            ("momentum", spx / spx.rolling(125).mean() - 1.0, False, "{:+.1%}")
        )
    if have("RSP", "SPY"):
        # Equal-weight against cap-weight: when the average stock keeps up with
        # the index, the advance is broad. A rally only the top names join
        # shows here as a falling ratio, which no index level reveals.
        out.append(
            (
                "breadth",
                change_series(closes["RSP"] / closes["SPY"], 20),
                False,
                "{:+.2%}",
            )
        )
    if have("^VIX"):
        out.append(("volatility", closes["^VIX"].dropna(), True, "{:.1f}"))
    if have("^VIX", "^VIX3M"):
        # Three-month vol over spot vol. Above 1 is the normal upward-sloping
        # curve; a dip below it is backwardation — the market paying more for
        # protection now than for protection later, which is what acute stress
        # looks like before it shows up in the index level.
        out.append(
            ("term", closes["^VIX3M"] / closes["^VIX"], False, "{:.2f}")
        )
    if hy_spread is not None and not hy_spread.dropna().empty:
        out.append(("credit", hy_spread.dropna(), True, "{:.2f}"))
    elif have("HYG", "IEF"):
        # Fallback when the FRED spread series is unavailable: junk bonds
        # against equivalent-duration Treasuries is the same trade priced in
        # ETFs. Coarser than an option-adjusted spread, and it moves when
        # credit moves — better than dropping the credit leg entirely.
        out.append(
            ("credit", closes["HYG"] / closes["IEF"], False, "{:.3f}")
        )
    if have("SPY", "TLT"):
        # Stocks against long Treasuries over a month: which side of the
        # risk trade money actually went to.
        out.append(
            (
                "haven",
                change_series(closes["SPY"], 20) - change_series(closes["TLT"], 20),
                False,
                "{:+.2%}",
            )
        )
    if have("HYG", "LQD"):
        out.append(
            (
                "junk",
                change_series(closes["HYG"] / closes["LQD"], 20),
                False,
                "{:+.2%}",
            )
        )
    if have("EEM", "SPY"):
        # Emerging markets against the S&P: the global risk appetite leg. It
        # is the one input here that is not a read on US vol, US credit or the
        # US index, so it carries information the other six cannot — capital
        # leaves the periphery first when risk is coming off, and comes back
        # there last.
        out.append(
            (
                "global_risk",
                change_series(closes["EEM"] / closes["SPY"], 20),
                False,
                "{:+.2%}",
            )
        )
    return out


COMPONENT_KEYS = (
    "momentum", "breadth", "volatility", "term", "credit", "haven", "junk",
    "global_risk",
)


def pulse(
    closes: dict[str, pd.Series],
    *,
    hy_spread: pd.Series | None = None,
    window: int = TRADING_YEAR,
    min_periods: int = MIN_PERIODS,
    stale_limit: int = STALE_LIMIT,
    min_components: int = MIN_COMPONENTS,
) -> Pulse:
    """Build the composite from close series (and FRED's HY spread when it came).

    The history is the daily mean across the component scores, so the path is
    as real as the headline number — no back-filled constant, no single point
    pretending to be a trend.

    Alignment is the whole difficulty. The inputs do not land on one calendar:
    ^VIX prints an in-progress bar while the equity closes are yesterday's, the
    credit spread arrives a day late, and a series Yahoo has quietly stopped
    publishing keeps answering quotes long after its history dried up. So each
    score is carried forward at most `stale_limit` sessions, the composite is
    quoted only for a row holding at least `min_components` of them, and
    `as_of` says which row that was. Without those three rules the newest row
    holds whichever single series ran ahead and the composite silently becomes
    that series — which is exactly what the first version of this did.
    """
    inputs = _component_inputs(closes, hy_spread)
    scored: dict[str, pd.Series] = {}
    raw_levels: dict[str, pd.Series] = {}
    formats: dict[str, str] = {}
    for key, raw, high_is_fear, fmt in inputs:
        ranked = rank_series(
            raw, invert=high_is_fear, window=window, min_periods=min_periods
        )
        if ranked.empty:
            continue
        scored[key] = ranked
        raw_levels[key] = raw.dropna()
        formats[key] = fmt
    if not scored:
        return Pulse(score=float("nan"), missing=COMPONENT_KEYS)

    # Normalise away the tz-awareness yfinance attaches per exchange: two
    # series stamped for the same session in different zones would otherwise
    # land on different rows and never average together.
    frame = pd.DataFrame(
        {k: naive_index(v) for k, v in scored.items()}
    ).sort_index()
    frame = frame.ffill(limit=stale_limit)
    usable = frame[frame.count(axis=1) >= min(min_components, frame.shape[1])]
    if usable.empty:
        return Pulse(score=float("nan"), missing=COMPONENT_KEYS)

    history = usable.mean(axis=1).dropna()
    as_of = history.index[-1]
    raw_frame = (
        pd.DataFrame({k: naive_index(v) for k, v in raw_levels.items()})
        .sort_index()
        .reindex(frame.index)
        .ffill(limit=stale_limit)
    )
    # The row `COMPONENT_THEN` sessions before the one being quoted, for each
    # component's "where it was" tick. Positional rather than by date so a
    # holiday cannot shift it.
    at = frame.index.get_loc(as_of)
    back = frame.index[at - COMPONENT_THEN] if at >= COMPONENT_THEN else None
    components = tuple(
        Component(
            key=key,
            score=float(frame.at[as_of, key]),
            raw=float(raw_frame.at[as_of, key]),
            fmt=formats[key],
            then=(
                float(frame.at[back, key])
                if back is not None and pd.notna(frame.at[back, key])
                else float("nan")
            ),
        )
        for key in COMPONENT_KEYS
        if key in frame.columns
        and pd.notna(frame.at[as_of, key])
        and pd.notna(raw_frame.at[as_of, key])
    )
    live = {c.key for c in components}
    # Re-average over exactly the components being shown, so the printed
    # contributions add up to the printed headline. A component that survived
    # into the frame but has no raw level to display would otherwise be inside
    # the score and absent from the breakdown.
    score = (
        float(sum(c.score for c in components) / len(components))
        if components
        else float("nan")
    )
    return Pulse(
        score=score,
        history=history,
        components=components,
        missing=tuple(k for k in COMPONENT_KEYS if k not in live),
        as_of=as_of,
    )


# -------------------------------------------------------------- personalisation
def relative_strength(
    closes: dict[str, pd.Series], tickers: dict[str, str], bench: str, days: int
) -> pd.Series:
    """Excess return over `bench` for each {label: ticker}, over `days` sessions.

    Simple difference of total returns, not a beta-adjusted alpha: the reader's
    question is "which sectors led the index this month", and a difference of
    two numbers they can both see is the answer to that question.
    """
    if bench not in closes:
        return pd.Series(dtype=float)
    base = pct_over(closes[bench], days)
    if base != base:
        return pd.Series(dtype=float)
    out = {
        label: pct_over(closes[t], days) - base
        for label, t in tickers.items()
        if t in closes
    }
    return pd.Series(out, dtype=float).dropna().sort_values(ascending=False)


def rotation_capture(weights: pd.Series, excess: pd.Series) -> float:
    """Weight-weighted excess return: how much of the rotation a book caught.

    Positive means the book sat in the sectors that beat the index over the
    window; negative means it sat in the laggards. Only the sectors present in
    both are counted, and the result is scaled back up by the weight actually
    covered so an unmapped slice (crypto, a bond sleeve) can't dilute the
    reading toward zero.
    """
    common = weights.index.intersection(excess.index)
    if common.empty:
        return float("nan")
    covered = float(weights.reindex(common).sum())
    if covered <= 0:
        return float("nan")
    contribution = float((weights.reindex(common) * excess.reindex(common)).sum())
    return contribution / covered


def tilt(weights: pd.Series, benchmark: pd.Series) -> pd.Series:
    """Active weight per sector: the book's weight minus the benchmark's.

    Sectors either side holds are covered; a sector missing from one side
    counts as zero there, because "not held" is a real active position and
    dropping it would hide the biggest underweights.
    """
    idx = weights.index.union(benchmark.index)
    return (
        weights.reindex(idx).fillna(0.0) - benchmark.reindex(idx).fillna(0.0)
    ).sort_values(ascending=False)


def fx_exposure(
    currency_weights: pd.Series, moves: dict[str, float], base: str = "EUR"
) -> tuple[float, pd.Series]:
    """(total drag, per-currency contribution) from FX over some window.

    `moves` is currency -> move of that currency against `base` for the window
    (+0.02 = the currency gained 2% on the base). A base-currency investor
    holding foreign assets carries that move whether they wanted the position
    or not, and this is the part of the book's return that came from it rather
    than from the assets. The base currency's own weight contributes nothing
    by construction.
    """
    contrib = {}
    for ccy, w in currency_weights.items():
        if ccy == base:
            continue
        move = moves.get(str(ccy))
        if move is None or move != move:
            continue
        contrib[ccy] = float(w) * float(move)
    series = pd.Series(contrib, dtype=float).sort_values(ascending=False)
    return (float(series.sum()) if not series.empty else float("nan")), series


def pin_order(indices: tuple[Index, ...], country_weights: pd.Series) -> list[Index]:
    """`indices` reordered so the ones covering the reader's geography lead.

    Weight of the countries an index stands in for, descending, ties broken by
    the registry's own order. An index with no country mapping (a world ETF)
    scores zero and keeps its place at the back rather than being dropped —
    the global read is still worth showing to someone holding only Spain.
    """
    lookup = {str(k): float(v) for k, v in country_weights.items()}

    def score(pair: tuple[int, Index]) -> tuple[float, int]:
        i, idx = pair
        return (-sum(lookup.get(c, 0.0) for c in idx.countries), i)

    return [idx for _, idx in sorted(enumerate(indices), key=score)]


# ------------------------------------------------------------------- trends
# A level is a snapshot; almost every question a reader actually has is about
# direction. Everything below turns a series already in hand into a direction,
# a speed, or a regime label — no extra fetch anywhere.

# The horizons every trend row quotes, in trading sessions. One definition, so
# "3m" means the same 63 sessions on a yield tile and on an index row.
HORIZONS: dict[str, int] = {"week": 5, "month": 21, "quarter": 63, "year": 252}

# Moving-average pair behind `trend_state`. 20/100 rather than the trader's
# 50/200: this page reads regimes over weeks, and a 200-session average is
# still describing last spring by the time a regime has turned.
TREND_FAST, TREND_SLOW = 20, 100


def changes(
    series: pd.Series, horizons: dict[str, int] | None = None
) -> dict[str, float]:
    """Absolute change over each horizon, for series quoted in their own units.

    Rates and spreads live here: the move in a 4.79% yield is "+33 basis
    points", and a percent change of a percentage ("+7.4%") is a number that
    reads like a price move and is not one. Horizons the series is too short
    for are absent rather than NaN, so a caller can print what exists.
    """
    s = series.dropna()
    out = {}
    for name, days in (horizons or HORIZONS).items():
        if len(s) > days:
            out[name] = float(s.iloc[-1]) - float(s.iloc[-days - 1])
    return out


def pct_changes(
    series: pd.Series, horizons: dict[str, int] | None = None
) -> dict[str, float]:
    """`changes` for series quoted as prices — returns, as fractions."""
    out = {}
    for name, days in (horizons or HORIZONS).items():
        value = pct_over(series, days)
        if value == value:
            out[name] = value
    return out


def trend_state(
    series: pd.Series, *, fast: int = TREND_FAST, slow: int = TREND_SLOW
) -> str:
    """Where a series sits in its own trend: up / turning / down.

    Two facts, four states. **Position** is the level against the slow average
    — the trend it is in. **Direction** is the *slope of the fast average*,
    which is where it is heading right now:

        above + fast rising   -> "up"            established uptrend
        above + fast falling  -> "turning_down"  still up, losing it
        below + fast rising   -> "turning_up"    broke down, recovering
        below + fast falling  -> "down"          established downtrend

    A single "above its average" flag collapses the two middle states into the
    outer ones and calls a topping market healthy, which is the whole reason
    for the four.

    Direction is the fast average's slope rather than the usual "fast above
    slow" crossover on purpose. In a rollover the fast arm sits above the slow
    one for weeks purely because it has not caught down yet, so the crossover
    reports a market that is falling every day as heading up. Its own slope
    cannot do that.

    "unknown" when there is not enough history for the slow average.
    """
    s = series.dropna()
    if len(s) < slow:
        return "unknown"
    fast_ma = s.rolling(fast).mean().dropna()
    slow_ma = float(s.rolling(slow).mean().iloc[-1])
    if len(fast_ma) <= fast or slow_ma != slow_ma:
        return "unknown"
    above = float(s.iloc[-1]) > slow_ma
    rising = float(fast_ma.iloc[-1]) > float(fast_ma.iloc[-fast - 1])
    if above:
        return "up" if rising else "turning_down"
    return "turning_up" if rising else "down"


def percentile_then(
    raw: pd.Series,
    *,
    ago: int = 21,
    window: int = TRADING_YEAR,
    min_periods: int = MIN_PERIODS,
) -> float:
    """`percentile_now` as it stood `ago` sessions back.

    The pair is the point. A VIX at the 12th percentile is calm; a VIX that was
    at the 37th a month ago is calm *and newly so*, and the second reading is
    the one that tells a reader whether the regime is settling or turning.
    Measured against the trailing year as of that date, not against today's, so
    it is the number the page would have printed then.
    """
    s = raw.dropna()
    if len(s) <= ago:
        return float("nan")
    return percentile_now(s.iloc[: len(s) - ago], window=window, min_periods=min_periods)


def above_ma_share(
    closes: dict[str, pd.Series], tickers: list[str], window: int
) -> tuple[int, int]:
    """(how many of `tickers` trade above their own `window`-session average,
    how many could be read at all).

    Trend breadth, which is a different question from today's advance-decline:
    not how many rose this morning but how many are in an uptrend at all. An
    index at a high with a third of its sectors below trend is a narrowing
    market, and the index level alone cannot say so. Symbols with too little
    history are excluded from both numbers rather than counted as failures.
    """
    hits = total = 0
    for ticker in tickers:
        series = closes.get(ticker)
        if series is None:
            continue
        s = series.dropna()
        if len(s) < window:
            continue
        average = float(s.rolling(window).mean().iloc[-1])
        if average != average:
            continue
        total += 1
        hits += float(s.iloc[-1]) > average
    return hits, total


def rolling_correlation(
    first: pd.Series, second: pd.Series, window: int = 60
) -> pd.Series:
    """Rolling correlation of two price series' daily returns.

    Built for the stock/bond pair, where the *level* of the correlation is the
    whole story: negative means bonds cushion an equity drawdown, positive
    means both legs fall together and the diversification a reader thinks they
    have is not there. Both sides go through `naive_index` — the pair may span
    two exchange timezones, and on mismatched zones this silently returns
    nothing.
    """
    frame = pd.concat(
        [
            naive_index(first).pct_change().rename("a"),
            naive_index(second).pct_change().rename("b"),
        ],
        axis=1,
    ).dropna()
    if len(frame) < window:
        return pd.Series(dtype=float)
    return frame["a"].rolling(window).corr(frame["b"]).dropna()


def regime_run(history: pd.Series) -> int:
    """How many consecutive sessions the composite has held its current band.

    A score of 66 that crossed into "appetite" yesterday and a 66 that has sat
    there for two months are the same number and not the same market. Counts
    the run of identical `regime_key` labels ending at the last observation.
    """
    if history.empty:
        return 0
    bands = history.map(regime_key)
    current = bands.iloc[-1]
    run = 0
    for band in reversed(bands.tolist()):
        if band != current:
            break
        run += 1
    return run


# The four rates regimes, as a slope move crossed with a yield move. Each tells
# a different macro story, and the yield level alone tells none of them:
#   bear steepening  long end selling faster than the short — growth or supply
#   bear flattening  yields up, curve compressing — inflation, or policy late
#   bull steepening  yields down, short end faster — cuts being priced
#   bull flattening  yields down, long end faster — growth scare, duration bid
def rate_quadrant(
    yields: pd.Series, slope: pd.Series, *, days: int = 63
) -> str:
    """Which of the four rates regimes the last `days` sessions describe.

    `yields` is a benchmark long yield, `slope` a long-minus-short spread —
    both in the same units, both as FRED publishes them. "unknown" when either
    series is too short to span the window.
    """
    y, s = yields.dropna(), slope.dropna()
    if len(y) <= days or len(s) <= days:
        return "unknown"
    d_yield = float(y.iloc[-1]) - float(y.iloc[-days - 1])
    d_slope = float(s.iloc[-1]) - float(s.iloc[-days - 1])
    direction = "bear" if d_yield > 0 else "bull"
    shape = "steepening" if d_slope > 0 else "flattening"
    return f"{direction}_{shape}"


def rolling_beta(
    asset: pd.Series, benchmark: pd.Series, *, window: int = 60
) -> pd.Series:
    """Rolling beta of one return series against another.

    Both inputs are *returns*, not prices, so a caller that already built a
    portfolio return series can pass it straight in. Same timezone caveat as
    `rolling_correlation`, handled the same way.
    """
    frame = pd.concat(
        [naive_index(asset).rename("a"), naive_index(benchmark).rename("b")],
        axis=1,
    ).dropna()
    if len(frame) < window:
        return pd.Series(dtype=float)
    cov = frame["a"].rolling(window).cov(frame["b"])
    var = frame["b"].rolling(window).var()
    return (cov / var.where(var != 0)).dropna()


def drift(series: pd.Series, *, ago: int = 63) -> tuple[float, float]:
    """(value now, value `ago` sessions back) for a rolling statistic.

    The shape every "is this changing?" reading on the page takes — a rolling
    beta, a rolling correlation. NaN for whichever side the series is too short
    to reach.
    """
    s = series.dropna()
    if s.empty:
        return float("nan"), float("nan")
    now = float(s.iloc[-1])
    then = float(s.iloc[-ago - 1]) if len(s) > ago else float("nan")
    return now, then
