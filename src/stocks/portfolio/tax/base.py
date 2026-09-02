"""Jurisdiction-neutral tax scaffolding shared by every country module.

The engine used to be one Spain-only module (`tax_es`). Two things are country
specific and everything else turned out not to be:

* **Which losses count this year.** Both jurisdictions we ship disallow a loss
  when the same security is bought back around the sale — Spain's art. 33.5.f
  "regla de los dos meses" and the US wash-sale rule (30 days either side).
  The mechanics are identical: block the loss in the sale year, re-integrate
  it as the replacement shares are themselves sold. Only the window differs,
  so `replacement_dates`/`recovered_losses` take the window as a predicate.
* **What the net is taxed at.** A flat progressive scale over one base (Spain)
  versus two buckets split by holding period, netted against each other, with
  the remainder deductible against ordinary income up to a cap (US).

So a jurisdiction is: a currency, a period summarizer, a bracket function and
a set of reporting-threshold flags. The web/CLI layers render whatever
`TaxPeriod.kpis()` and `.notes()` return, which is why adding a country does
not touch the tax tab.

Money is in the jurisdiction's own currency (`Jurisdiction.currency`) — the
ledger is replayed at that base, so a US filer's basis is USD at the trade
date, a Spanish filer's is EUR at the ECB rate. NOT tax advice; a planning aid.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from stocks.portfolio.positions import RealizedSale

# (sell_date, buy_date) -> True when that purchase blocks the sale's loss.
Window = Callable[[date, date], bool]


@dataclass(frozen=True)
class TaxSettings:
    """Per-filer knobs. Fields a jurisdiction doesn't use are ignored.

    `other_income` is other *taxable* ordinary income (after deductions) — the
    US brackets stack short-term gains on top of it, and we deliberately do not
    model the standard deduction: asking for a post-deduction figure is one
    input instead of five, and wrong by less.
    """

    filing_status: str = "single"  # US: single | mfj | mfs | hoh; DE: single | joint
    other_income: float = 0.0
    include_niit: bool = False  # US net investment income tax (3.8%)
    # DE: Kirchensteuer as a share of the tax itself (0.08 or 0.09), on top of
    # the flat rate and the solidarity surcharge.
    church_tax_rate: float = 0.0
    # Tickers the caller has classified as funds, for regimes that treat fund
    # income differently (DE Teilfreistellung). None means "nobody checked" —
    # distinct from an empty set, which means "checked, holds none".
    fund_tickers: frozenset[str] | None = None


@dataclass(frozen=True)
class Kpi:
    """One headline figure: an i18n key, an amount, and a help key."""

    key: str
    value: float
    help_key: str


@dataclass(frozen=True)
class Note:
    """A localized sentence the UI appends under the KPIs."""

    key: str
    kwargs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportingFlag:
    """A reporting threshold (Modelo 720, FBAR, Form 8938…).

    `reportable` means the threshold is crossed, not that a filing is due —
    whether it applies depends on where the assets are actually held, which the
    ledger doesn't know. Hence a flag, not a verdict.
    """

    name: str  # "modelo_720", "fbar", "form_8938" — i18n key suffix
    total_value: float
    threshold: float
    reportable: bool
    # Plain-English sentence for the CLI, which has no catalog. The web page
    # localizes from `name` instead and ignores this.
    message: str = ""


@dataclass
class TaxPeriod:
    """One period's realized result. Subclassed per jurisdiction.

    `period` is an ISO prefix: "YYYY" for a full tax year (the only real
    taxable base) or "YYYY-MM" for a breakdown of when a result was booked.
    """

    jurisdiction: str
    currency: str
    year: int
    period: str = ""
    realized_gain: float = 0.0  # sum of gains from winning sales
    realized_loss: float = 0.0  # sum of |losses| from losing sales
    disallowed_loss: float = 0.0  # blocked this period by the repurchase rule
    # Losses blocked earlier (or this period) that become deductible now
    # because the replacement shares were sold in this period.
    recovered_loss: float = 0.0
    sales: list[RealizedSale] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.period = self.period or f"{self.year:04d}"

    # ---------------------------------------------------------- common maths
    @property
    def deductible_loss(self) -> float:
        return self.realized_loss - self.disallowed_loss

    @property
    def net_taxable(self) -> float:
        """Net taxable result (may be negative -> carryforward)."""
        return self.realized_gain - self.deductible_loss - self.recovered_loss

    @property
    def estimated_tax(self) -> float:  # overridden per jurisdiction
        return 0.0

    @property
    def carryforward_loss(self) -> float:
        """Unused net loss carried to later periods. 0 when the net is a gain."""
        return max(0.0, -self.net_taxable)

    # ------------------------------------------------------------ UI surface
    # Chart series are shared: gains up, deductible losses and recovered
    # deferrals down, net as the marker. Jurisdictions differ in the KPIs and
    # the wording, not in the shape of the period chart.
    def kpis(self) -> list[Kpi]:
        return [
            Kpi("net_taxable", self.net_taxable, "net_taxable_help"),
            Kpi("estimated_tax", self.estimated_tax, "estimated_tax_help"),
            Kpi(
                "carryforward_loss",
                self.carryforward_loss,
                "carryforward_loss_help",
            ),
        ]

    def notes(self) -> list[Note]:
        return []


# ------------------------------------------------------------------ helpers


def progressive_tax(base: float, brackets: list[tuple[float, float]]) -> float:
    """Tax on `base` under (upper_bound, marginal_rate) brackets. 0 if base<=0."""
    if base <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if base <= lower:
            break
        tax += (min(base, upper) - lower) * rate
        lower = upper
    return tax


def shift_months(d: date, months: int) -> date:
    """d shifted by ±months, clamped to the target month's last valid day."""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    # clamp day (e.g. 31 Jan -2mo has no 31 Nov)
    last = [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, last))


def month_range(first: str, last: str) -> list[str]:
    """Every ISO month ("YYYY-MM") from `first` to `last`, inclusive.

    Quiet months included: a period breakdown that skipped them would
    compress the timeline.
    """
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out: list[str] = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def covers(period: str, sell_date: str, year_start: tuple[int, int] = (1, 1)) -> bool:
    """Whether `sell_date` falls in an ISO `period` under this tax calendar.

    `period` is "YYYY" for a tax year or "YYYY-MM" for one month of it, and
    `year_start` is the (month, day) the year opens on — (1, 1) nearly
    everywhere, (4, 6) in the UK, where "2025" means 6 April 2025 to 5 April
    2026. A calendar-year jurisdiction reduces to the prefix test this replaced.
    """
    if len(period) > 4:  # a month slice is a calendar month either way
        return sell_date.startswith(period)
    if year_start == (1, 1):
        return sell_date.startswith(period)
    year = int(period)
    month, day = year_start
    start = date(year, month, day)
    end = date(year + 1, month, day) - timedelta(days=1)
    return start <= date.fromisoformat(sell_date) <= end


def tax_year_of(sell_date: str, year_start: tuple[int, int] = (1, 1)) -> int:
    """Which tax year a disposal belongs to under this calendar.

    A UK disposal on 2 February 2026 belongs to the year that opened on 6
    April 2025 — bucketing it by its calendar year would put it in the wrong
    return, and the year selector reads these.
    """
    d = date.fromisoformat(sell_date)
    if year_start == (1, 1):
        return d.year
    month, day = year_start
    return d.year if (d.month, d.day) >= (month, day) else d.year - 1


def days_window(days: int) -> Window:
    """Calendar-day window: a buy within ±`days` of the sale blocks the loss."""

    def within(sell: date, buy: date) -> bool:
        return abs((buy - sell).days) <= days

    return within


def months_window(months: int) -> Window:
    """Calendar-month window: ±`months` around the sale, month-clamped."""

    def within(sell: date, buy: date) -> bool:
        return shift_months(sell, -months) <= buy <= shift_months(sell, months)

    return within


def replacement_dates(
    sale: RealizedSale, ticker_buy_dates: list[str], within: Window
) -> set[str]:
    """Buy dates that block this sale's loss.

    Homogeneous / substantially-identical shares acquired *after* the sold lot
    and inside the window — i.e. a genuine replacement position, not the sold
    lot's own purchase nor an older parcel. Same ticker stands in for both
    "homogéneas" (ES) and "substantially identical" (US); options, converts and
    cross-listings are not chased.
    """
    sell = date.fromisoformat(sale.sell_date)
    lot_buy = date.fromisoformat(sale.buy_date)
    out: set[str] = set()
    for d in ticker_buy_dates:
        b = date.fromisoformat(d)
        if b <= lot_buy:  # the sold lot itself, or an older one: no replacement
            continue
        if within(sell, b):
            out.add(d)
    return out


def recovered_losses(
    realized: list[RealizedSale],
    period: str,
    buy_dates: dict[str, list[str]],
    within: Window,
    blocked_filter: Callable[[RealizedSale], bool] | None = None,
) -> float:
    """Blocked losses that unlock in `period` because the replacement sold.

    A loss blocked by a repurchase becomes computable as the replacement shares
    are transmitted (ES art. 33.5.f second leg; in the US the same economics
    arrive via the basis bump on the replacement lot). FIFO sales carry their
    lot's buy date, so a sale of a replacement lot is any later sale whose lot
    was bought on one of the blocking dates. Buy quantities aren't in
    `buy_dates` (dates only), so each replacement share sold frees one blocked
    share's loss, pro-rata, capped at the full blocked amount.

    `blocked_filter` restricts which blocked sales are counted — the US module
    passes it twice to recover short- and long-term losses separately, since a
    recovered loss keeps the character of the loss that was blocked.
    """
    total = 0.0
    for s in realized:
        if s.gain >= 0 or s.quantity <= 0:
            continue
        if blocked_filter is not None and not blocked_filter(s):
            continue
        repl = replacement_dates(s, buy_dates.get(s.ticker, []), within)
        if not repl:
            continue
        consuming = sorted(
            (
                r
                for r in realized
                if r.ticker == s.ticker
                and r.buy_date in repl
                and r.sell_date >= s.sell_date
            ),
            key=lambda r: r.sell_date,
        )
        block = s.quantity  # shares whose loss the repurchase blocks
        cum = 0.0
        for r in consuming:
            prev = min(cum, block)
            cum += r.quantity
            if r.sell_date.startswith(period):
                total += (min(cum, block) - prev) / block * -s.gain
    return total


def flag(
    name: str, total: float, threshold: float, message: str = ""
) -> ReportingFlag:
    return ReportingFlag(name, total, threshold, total >= threshold, message)
