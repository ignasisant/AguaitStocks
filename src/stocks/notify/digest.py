"""Daily portfolio digest — computed headless, rendered as Telegram HTML.

compute_digest_data does the network work (prices, FX, earnings) off the pure
analytics in stocks.analysis.portfolio; render_digest is pure text so it tests
offline. Every section is individually fault-tolerant: a failed FX fetch or a
throttled earnings lookup drops that section, the digest still sends.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from stocks.config import CURRENCY_SYMBOL, load_watchlist
from stocks.data.earnings import EarningsEvent, upcoming
from stocks.portfolio.ledger import all_transactions
from stocks.portfolio.positions import build

_WEEKDAYS = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
}
_MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "es": ("ene", "feb", "mar", "abr", "may", "jun",
           "jul", "ago", "sep", "oct", "nov", "dic"),
}
MOVERS_SHOWN = 3


@dataclass
class DigestData:
    date: date
    total: float | None = None
    day: tuple[float, float] | None = None  # (change, pct) over 1 day
    week: tuple[float, float] | None = None  # over ~7 days
    movers: list[tuple[str, float]] = field(default_factory=list)  # (ticker, pct) desc
    earnings: list[EarningsEvent] = field(default_factory=list)
    highlight: str | None = None  # optional LLM line, filled by the caller
    watchlist_only: bool = False  # no ledger -> movers/earnings-only digest
    # The account's reporting currency; every figure above is in it.
    currency: str = "EUR"


def compute_digest_data(
    watchlist: Path, db: Path, base: str = "EUR"
) -> DigestData:
    """Gather one account's digest inputs, in `base`. Sections fail alone."""
    from stocks.analysis.portfolio import (
        basket_change,
        position_values_history,
        session_moves,
    )

    data = DigestData(date=date.today(), currency=base)
    holdings = load_watchlist(watchlist)

    positions = []
    try:
        txs = all_transactions(db)
        if txs:
            positions, _ = build(txs, base=base)
    except Exception:
        positions = []
    data.watchlist_only = not positions

    if positions:
        try:
            values = position_values_history(positions, period="1mo", base=base)
            if not values.empty:
                last = values.iloc[-1].dropna()
                data.total = float(last.sum()) if not last.empty else None
                data.day = basket_change(values, 1)
                data.week = basket_change(values, 7)
        except Exception:
            pass

    tickers = (
        [p.ticker for p in positions]
        if positions
        else [h.ticker for h in holdings]
    )
    try:
        moves = session_moves(tickers)
        data.movers = sorted(moves.items(), key=lambda kv: kv[1], reverse=True)
    except Exception:
        pass

    try:
        data.earnings = upcoming(holdings, within_days=7)
    except Exception:
        pass

    return data


# ---------------------------------------------------------------- rendering


def _money(amount: float, currency: str = "EUR") -> str:
    """€48,230 — always thousands-separated, no decimals for totals."""
    return f"{CURRENCY_SYMBOL.get(currency, '')}{amount:,.0f}"


def _delta(change: float, pct: float, currency: str = "EUR") -> str:
    return f"{change:+,.0f} {CURRENCY_SYMBOL.get(currency, '')} ({pct * 100:+.2f}%)"


def _date_line(d: date, lang: str) -> str:
    wd = _WEEKDAYS.get(lang, _WEEKDAYS["en"])[d.weekday()]
    mo = _MONTHS.get(lang, _MONTHS["en"])[d.month - 1]
    return f"{wd} {d.day} {mo}"


def render_digest(data: DigestData, lang: str) -> str:
    """The digest as Telegram HTML (parse_mode='HTML'), all dynamic text escaped."""
    from stocks.web.i18n import translate

    def tr(key: str, **kw) -> str:
        return translate(f"notify.{key}", lang, **kw)

    parts: list[str] = [
        f"<b>📊 {html.escape(tr('digest_title'))}</b> · {_date_line(data.date, lang)}"
    ]

    if data.total is not None:
        line = (
            f"<b>{html.escape(tr('portfolio'))}</b> "
            f"{_money(data.total, data.currency)}"
        )
        deltas = []
        if data.day:
            deltas.append(f"{html.escape(tr('day'))} {_delta(*data.day, data.currency)}")
        if data.week:
            deltas.append(
                f"{html.escape(tr('week'))} {_delta(*data.week, data.currency)}"
            )
        parts.append(line + ("\n" + " · ".join(deltas) if deltas else ""))

    if data.movers:
        gainers = [(t, v) for t, v in data.movers if v > 0][:MOVERS_SHOWN]
        losers = [(t, v) for t, v in reversed(data.movers) if v < 0][:MOVERS_SHOWN]
        lines = []
        if gainers:
            lines.append(
                "  ".join(f"▲ {html.escape(t)} {v * 100:+.1f}%" for t, v in gainers)
            )
        if losers:
            lines.append(
                "  ".join(f"▼ {html.escape(t)} {v * 100:+.1f}%" for t, v in losers)
            )
        if lines:
            parts.append(f"<b>{html.escape(tr('movers'))}</b>\n" + "\n".join(lines))

    if data.earnings:
        rows = [
            f"• {html.escape(e.ticker)} — {_date_line(e.date, lang)} (T-{e.days_until})"
            for e in data.earnings
            if e.date is not None
        ]
        if rows:
            parts.append(f"<b>{html.escape(tr('earnings_7d'))}</b>\n" + "\n".join(rows))

    if data.highlight:
        parts.append(f"💡 {html.escape(data.highlight)}")

    return "\n\n".join(parts)


# ------------------------------------------------------------------ fan-out


def run_digest_fanout(dry_run: bool = False) -> dict[str, str]:
    """Compute and send every subscriber's digest. Returns {label: status}."""
    from stocks.notify import narrative, telegram
    from stocks.notify.fanout import iter_notify_users
    from stocks.notify.state import is_blocked, load_state, mark_blocked, save_state

    now = datetime.now(UTC)
    status: dict[str, str] = {}
    for user in iter_notify_users("digest"):
        try:
            state = load_state(user.state_path)
            if is_blocked(state):
                status[user.label] = "skipped: blocked"
                continue
            base = str(user.prefs.get("currency") or "EUR").upper()
            data = compute_digest_data(user.watchlist, user.db, base)
            if data.total is None and not data.movers and not data.earnings:
                status[user.label] = "skipped: no data"
                continue
            data.highlight = narrative.highlight(data, user.prefs, user.lang)
            text = render_digest(data, user.lang)
            if dry_run:
                print(f"── {user.label} ──\n{text}\n")
                status[user.label] = "dry-run"
                continue
            try:
                telegram.send_message(text, user.chat_id, parse_mode="HTML")
                status[user.label] = "sent"
            except telegram.TelegramBlocked:
                mark_blocked(state, now)
                save_state(state, user.state_path)
                status[user.label] = "blocked"
            time.sleep(0.2)  # stay far below Telegram's global send rate
        except Exception as exc:  # noqa: BLE001 — cron isolation per account
            status[user.label] = f"error: {exc}"
    return status
