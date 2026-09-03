"""Candidate actions for the dashboard's daily card — computed, not written.

The daily card is meant to answer "what should I DO today", and an action is a
decision with a trigger behind it: *sell X, your own exit alert just fired*,
*X is 2% from the level you set*, *realising the loss on X offsets the gain you
already booked this year*. A model asked for that from raw portfolio numbers
invents triggers — it has no way to know the user's exit rule, and every
sentence it produces reads equally confident.

So the triggers are computed here, in plain Python, from things the user
actually declared or the ledger actually holds:

  - **their own alerts** — a `below`/`above` price rule on a watchlist entry is
    the user's own exit or entry level, the closest thing to a stated kill
    criterion this app has. Fired, or within `NEAR_PCT` of firing.
  - **the ledger** — a position deep under its cost, a weight that has drifted
    past `CONCENTRATION_PCT`, and the loss-harvesting arithmetic: an open loss
    is only worth surfacing when there is a realised gain this tax year for it
    to offset, and the jurisdiction's repurchase window is the trap that goes
    with it.
  - **the calendar** — a print inside a few days is a decision date, not news.
  - **the price** — a watchlist name at its 52-week low is the "getting close
    to interesting" case, which is about candidates, not holdings.

Each candidate carries its numbers and an urgency; stocks/chat/daily.py hands
the top few to the model, which picks and phrases — it never gets to invent one
— and renders them directly when no model is available. Nothing here fetches:
every input is a frame or list the dashboard already loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# An alert this close to its level is "about to fire" — near enough to plan
# around today, far enough that it does not just repeat the fired ones.
NEAR_PCT = 3.0
# A position this far under its own cost is where a thesis gets re-read. Not a
# rule the app invents on the user's behalf: the card asks them to review it,
# never to sell.
DRAWDOWN_PCT = 25.0
# Below this the tax tail wags the dog — harvesting a €40 loss costs more in
# spread and attention than it saves.
HARVEST_MIN = 150.0
CONCENTRATION_PCT = 30.0
EARNINGS_DAYS = 5
# Within this of the 52-week low, a watchlist name is worth a look.
LOW_52W_PCT = 3.0

# Kinds, and the base urgency each starts from (higher sorts first). A fired
# alert is the user's own trigger going off today; a concentration drift has
# been true for weeks and will still be true tomorrow.
ALERT_HIT = "alert_hit"
HARVEST = "harvest"
EARNINGS = "earnings"
ALERT_NEAR = "alert_near"
DRAWDOWN = "drawdown"
LOW_52W = "low_52w"
CONCENTRATION = "concentration"

_URGENCY = {
    ALERT_HIT: 90,
    HARVEST: 75,
    EARNINGS: 70,
    ALERT_NEAR: 60,
    DRAWDOWN: 55,
    LOW_52W: 45,
    CONCENTRATION: 35,
}


@dataclass(frozen=True)
class Signal:
    """One candidate action. `data` holds the numbers its phrasing needs."""

    kind: str
    ticker: str
    urgency: int
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ticker": self.ticker, **self.data}


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _round(value, digits: int = 2) -> float | None:
    out = _num(value)
    return None if out is None else round(out, digits)


# ------------------------------------------------------------ the user's own


def _alert_signals(holdings, closes: dict, held: set[str]) -> list[Signal]:
    """Price-threshold alerts against the last close, fired and nearly fired.

    Only `above`/`below` rules: they carry an explicit price, which is what
    makes them readable as the user's own level ("your exit at 150"). The
    history-based rules (drawdown, RSI, SMA cross) are evaluated by
    notify/alerts.py against a full price history — a fetch this card has no
    business making, and the notification path already covers them.

    Comparison is in the ticker's own quote currency, because that is the
    currency the user typed the level in.
    """
    out: list[Signal] = []
    for h in holdings:
        prices = closes.get(h.ticker) or []
        price = _num(prices[-1]) if prices else None
        if price is None:
            continue
        for alert in h.alerts:
            if alert.type not in ("above", "below") or alert.price is None:
                continue
            level = float(alert.price)
            if not level:
                continue
            gap_pct = (price / level - 1) * 100
            data = {
                "rule": alert.type,
                "level": _round(level),
                "price": _round(price),
                "held": h.ticker in held,
                "gap_pct": _round(abs(gap_pct)),
            }
            if alert.triggered(price):
                out.append(Signal(ALERT_HIT, h.ticker, _URGENCY[ALERT_HIT], data))
            elif abs(gap_pct) <= NEAR_PCT:
                out.append(Signal(ALERT_NEAR, h.ticker, _URGENCY[ALERT_NEAR], data))
    return out


# ------------------------------------------------------------- the ledger


def _position_signals(tbl, currency: str) -> list[Signal]:
    """Drawdown against cost and weight drift, straight off the positions frame."""
    out: list[Signal] = []
    if tbl is None or tbl.empty:
        return out
    for ticker, row in tbl.iterrows():
        pnl_pct = _num(row.get("pnl_pct"))
        if pnl_pct is not None and pnl_pct * 100 <= -DRAWDOWN_PCT:
            out.append(Signal(
                DRAWDOWN, str(ticker), _URGENCY[DRAWDOWN],
                {
                    "pnl_pct": _round(pnl_pct * 100),
                    "pnl": _round(row.get("pnl")),
                    "currency": currency,
                },
            ))
        weight = _num(row.get("weight"))
        if weight is not None and weight * 100 >= CONCENTRATION_PCT:
            out.append(Signal(
                CONCENTRATION, str(ticker), _URGENCY[CONCENTRATION],
                {"weight_pct": _round(weight * 100), "currency": currency},
            ))
    return out


def realized_this_year(realized, jurisdiction=None, today: date | None = None) -> float:
    """Net realised result booked in the tax year `today` falls in.

    The jurisdiction decides where the year starts (6 April in the UK, 1 July
    in Australia) — the same boundary the tax tab reports on, so the figure the
    card quotes is the one the user can go and check.
    """
    day = today or date.today()
    if jurisdiction is not None:
        year = jurisdiction.tax_year_of(day.isoformat())
        in_year = [s for s in realized if jurisdiction.tax_year_of(s.sell_date) == year]
    else:
        in_year = [s for s in realized if s.sell_date[:4] == f"{day.year:04d}"]
    return sum(s.gain for s in in_year)


def _harvest_signals(
    tbl, realized, jurisdiction, currency: str, today: date | None
) -> list[Signal]:
    """Open losses worth realising *because* there is a booked gain to offset.

    Deliberately gated on the realised side: an unrealised loss on its own is
    not an action, it is a fact the P/L column already shows. It becomes one
    when the user has a taxable gain this year that the loss would cancel — and
    then the repurchase window is the part that costs money to get wrong, so it
    travels with the signal.
    """
    if tbl is None or tbl.empty:
        return []
    net_gain = realized_this_year(realized, jurisdiction, today)
    if net_gain <= 0:
        return []
    out: list[Signal] = []
    for ticker, row in tbl.iterrows():
        pnl = _num(row.get("pnl"))
        if pnl is None or pnl > -HARVEST_MIN:
            continue
        out.append(Signal(
            HARVEST, str(ticker), _URGENCY[HARVEST],
            {
                "loss": _round(abs(pnl)),
                "gain_ytd": _round(net_gain),
                "offset": _round(min(abs(pnl), net_gain)),
                "pnl_pct": _round((_num(row.get("pnl_pct")) or 0) * 100),
                "currency": currency,
                "jurisdiction": getattr(jurisdiction, "code", None),
                "repurchase_window": getattr(jurisdiction, "repurchase_window", ""),
            },
        ))
    return out


# --------------------------------------------------------- calendar & price


def _earnings_signals(earnings, held: set[str]) -> list[Signal]:
    """Prints inside EARNINGS_DAYS — a date the user can still act before.

    Held names only: a print on a name you do not own is news, not a decision.
    The closer the date the higher it sorts, so tomorrow's print outranks
    Friday's.
    """
    out: list[Signal] = []
    for event in earnings:
        days = getattr(event, "days_until", None)
        if getattr(event, "date", None) is None or days is None:
            continue
        if event.ticker not in held or not 0 <= days <= EARNINGS_DAYS:
            continue
        out.append(Signal(
            EARNINGS, event.ticker, _URGENCY[EARNINGS] + (EARNINGS_DAYS - days),
            {"in_days": int(days), "date": event.date.isoformat()},
        ))
    return out


def _low_signals(extremes, held: set[str]) -> list[Signal]:
    """Watchlist names at their 52-week low — the "getting interesting" case.

    Held names are excluded on purpose: for something already owned this is the
    drawdown signal's territory, and printing both would say the same thing
    twice in a card with three lines.
    """
    out: list[Signal] = []
    for ticker, price, kind, distance in extremes:
        if kind != "low" or ticker in held:
            continue
        gap = abs(_num(distance) or 0.0) * 100
        if gap > LOW_52W_PCT:
            continue
        out.append(Signal(
            LOW_52W, ticker, _URGENCY[LOW_52W],
            {"price": _round(price), "gap_pct": _round(gap)},
        ))
    return out


# ------------------------------------------------------------------ the set


def candidates(
    *,
    holdings=(),
    tbl=None,
    closes: dict | None = None,
    realized=(),
    earnings=(),
    extremes=(),
    jurisdiction=None,
    currency: str = "EUR",
    today: date | None = None,
    limit: int = 8,
) -> list[Signal]:
    """Every action the book currently justifies, most urgent first.

    One ticker can raise several (a name can be both deeply down and the
    harvest candidate); the card's job is to choose, so they all come through
    and only the tail past `limit` is dropped. Ties keep ticker order, which
    keeps the list stable between reruns of an unchanged book.
    """
    held = set(tbl.index.astype(str)) if tbl is not None and not tbl.empty else set()
    out = [
        *_alert_signals(holdings, closes or {}, held),
        *_harvest_signals(tbl, realized, jurisdiction, currency, today),
        *_earnings_signals(earnings, held),
        *_position_signals(tbl, currency),
        *_low_signals(extremes, held),
    ]
    out.sort(key=lambda s: (-s.urgency, s.ticker))
    return out[:limit]
