"""Validate parsed transactions before they touch the ledger.

The Revolut parser (revolut.py / revolut_pdf.py) does per-row shape checks;
this module does everything that needs context beyond one row:

* dates — parseable ISO, not in the future, not implausibly old
* tickers — checked against the local EDGAR map (data/edgar_tickers.json),
  with an optional live lookup fallback for non-US symbols; unknown tickers
  are a *warning*, not an error, because Revolut lists EU stocks under bare
  local symbols (DHER, NA9…) that no US source knows
* oversells — a sell larger than the position held at that date (replayed
  FIFO-style over prior ledger + the new batch, splits applied)
* duplicates — rows identical to something already in the ledger, the
  classic re-import-of-an-overlapping-export accident
* splits — Revolut reports shares *added* by a split, not the ratio
  positions.py needs; the ratio is derived from the quantity held the day
  of the split and snapped to a plausible ratio (6:1, 3:2, …)

Errors quarantine a row (not importable); warnings flag it but let it
through. Nothing here writes to the ledger.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from stocks.config import DATA_DIR, WATCHLIST_FILE, load_watchlist, ticker_aliases
from stocks.portfolio.ledger import DB_PATH, Transaction
from stocks.portfolio.revolut import ParseResult

EDGAR_TICKER_CACHE = DATA_DIR / "edgar_tickers.json"

# Bare US-style symbol or one with an exchange suffix (RMS.PA, BRK-B).
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,6}([.\-][A-Z0-9]{1,4})?$")

# Earliest plausible trade date: Revolut launched stock trading in 2019.
_MIN_DATE = "2018-01-01"

# Snap targets for derived split ratios: forward N:1 and the common 3:2.
_SPLIT_RATIOS = [1.5] + [float(n) for n in range(2, 51)]

# lookup(ticker) -> True (exists), False (doesn't), None (couldn't check)
Lookup = Callable[[str], bool | None]


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    field: str
    message: str


@dataclass
class Checked:
    """One parsed transaction plus everything validation found on it."""

    tx: Transaction
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]


@dataclass
class Validation:
    checked: list[Checked]

    @property
    def importable(self) -> list[Transaction]:
        """Clean + warned rows; errors stay quarantined."""
        return [c.tx for c in self.checked if not c.errors]

    @property
    def rejected(self) -> list[Checked]:
        return [c for c in self.checked if c.errors]

    @property
    def flagged(self) -> list[Checked]:
        return [c for c in self.checked if c.warnings and not c.errors]

    @property
    def summary(self) -> str:
        return (
            f"{len(self.importable)} importable "
            f"({len(self.flagged)} with warnings), {len(self.rejected)} rejected"
        )


def known_tickers(
    watchlist_path: Path = WATCHLIST_FILE, db_path: Path = DB_PATH
) -> set[str]:
    """Symbols we can vouch for offline: EDGAR map + watchlist + aliases + ledger.

    Watchlist and ledger are per-user in the web app — pass that user's paths.
    Aliases stay global (root watchlist.yaml): broker-code mappings are
    reference data, not personal data.
    """
    known: set[str] = set()
    if EDGAR_TICKER_CACHE.exists():
        table = json.loads(EDGAR_TICKER_CACHE.read_text())
        known.update(row["ticker"].upper() for row in table.values())
    known.update(h.ticker.upper() for h in load_watchlist(watchlist_path))
    known.update(ticker_aliases())
    from stocks.portfolio.ledger import all_transactions

    known.update(t.ticker for t in all_transactions(db_path))
    return known


def validate(
    result: ParseResult,
    prior: list[Transaction],
    *,
    known: set[str] | None = None,
    lookup: Lookup | None = None,
    today: date | None = None,
) -> Validation:
    """Check a parsed batch against itself and the existing ledger.

    Mutates `result.skipped` only by resolving split rows into transactions
    (they move from skipped to checked). `prior` is the current ledger.
    """
    known = known_tickers() if known is None else known
    today = today or date.today()

    txs = list(result.transactions) + resolve_splits(result, prior)
    seen = {_dupe_key(t) for t in prior}
    checked = [Checked(tx=t) for t in txs]

    for c in checked:
        _check_date(c, today)
        _check_ticker(c, known, lookup)
        if c.tx.action == "sell" and c.tx.price == 0:
            c.issues.append(
                Issue(
                    "warning",
                    "price",
                    "sold at 0 — worthless disposal/delisting? this realizes "
                    "the full loss of the position",
                )
            )
        if _dupe_key(c.tx) in seen:
            c.issues.append(
                Issue(
                    "warning",
                    "duplicate",
                    "identical row already in ledger — re-importing an "
                    "overlapping export doubles the position",
                )
            )
    _check_oversells(checked, prior)
    return Validation(checked=checked)


# ------------------------------------------------------------------ split rows
def resolve_splits(result: ParseResult, prior: list[Transaction]) -> list[Transaction]:
    """Turn skipped STOCK SPLIT rows into split transactions when the ratio
    is derivable: ratio = (held + shares_added) / held at the split date.
    Resolved entries are removed from `result.skipped`; ambiguous ones stay
    there with the reason updated."""
    splits = [
        s
        for s in result.skipped
        if "SPLIT" in s.get("type", "").upper() and s.get("ticker")
    ]
    if not splits:
        return []

    resolved: list[Transaction] = []
    base = list(prior) + list(result.transactions)
    for s in sorted(splits, key=lambda s: s.get("date", "")):
        day = _iso_date(s.get("date", ""))
        added = float(s.get("quantity") or 0)
        held = _held_at(base + resolved, s["ticker"], day)
        ratio = _snap_ratio((held + added) / held) if held > 1e-9 and added > 0 else None
        if ratio is None:
            s["reason"] = (
                "stock split — ratio underivable from held quantity "
                f"({held:.4f} held, {added:.4f} added); add manually"
            )
            continue
        result.skipped.remove(s)
        resolved.append(
            Transaction(
                date=day,
                ticker=s["ticker"],
                action="split",
                quantity=ratio,
                currency=s.get("currency") or "USD",
                note=f"revolut split {ratio:g}:1 (derived from +{added:g} shares)",
            )
        )
    return resolved


def _snap_ratio(raw: float) -> float | None:
    for target in _SPLIT_RATIOS:
        if abs(raw - target) / target < 0.005:
            return target
    return None


def _held_at(txs: list[Transaction], ticker: str, day: str) -> float:
    """Share count held in `ticker` just before end of `day` (splits applied)."""
    qty = 0.0
    ordered = sorted(
        (t for t in txs if t.ticker == ticker.upper() and t.date <= day),
        key=lambda t: (t.date, t.id or 0),
    )
    for t in ordered:
        if t.action == "buy":
            qty += t.quantity
        elif t.action == "sell":
            qty -= t.quantity
        elif t.action == "split" and t.quantity > 0:
            qty *= t.quantity
    return qty


# ------------------------------------------------------------------ single-tx checks
def _check_date(c: Checked, today: date) -> None:
    d = _iso_date(c.tx.date)
    try:
        parsed = date.fromisoformat(d)
    except ValueError:
        c.issues.append(Issue("error", "date", f"unparseable date {c.tx.date!r}"))
        return
    if parsed > today:
        c.issues.append(Issue("error", "date", f"date {d} is in the future"))
    elif d < _MIN_DATE:
        c.issues.append(
            Issue("error", "date", f"date {d} predates Revolut stock trading")
        )


def _check_ticker(c: Checked, known: set[str], lookup: Lookup | None) -> None:
    t = c.tx.ticker
    if not t:
        c.issues.append(Issue("error", "ticker", "missing ticker"))
        return
    if not _TICKER_RE.match(t):
        c.issues.append(Issue("error", "ticker", f"malformed ticker {t!r}"))
        return
    if t in known:
        return
    found = lookup(t) if lookup else None
    if found:
        known.add(t)  # don't re-look-up the same symbol within a batch
        return
    c.issues.append(
        Issue(
            "warning",
            "ticker",
            f"{t} not in EDGAR/watchlist/aliases"
            + ("/yfinance" if lookup and found is False else "")
            + " — EU or OTC broker code? map it to a Yahoo symbol under "
            "`aliases:` in watchlist.yaml or prices won't resolve",
        )
    )


# ------------------------------------------------------------------ cross-row checks
def _check_oversells(checked: list[Checked], prior: list[Transaction]) -> None:
    """Replay quantities per ticker over prior + new rows in date order; a sell
    exceeding the running position marks that row as an error."""
    events: list[tuple[str, int, Transaction, Checked | None]] = [
        (t.date, t.id or 0, t, None) for t in prior
    ]
    # Batch rows have no id yet; a large sort key keeps them after ledger rows
    # on the same date, matching how positions.py will replay them post-commit.
    events += [(c.tx.date, 10**9 + i, c.tx, c) for i, c in enumerate(checked)]

    held: dict[str, float] = defaultdict(float)
    for _, _, tx, c in sorted(events, key=lambda e: (e[0], e[1])):
        if c and c.errors:  # quarantined rows won't be committed — don't count them
            continue
        q = held[tx.ticker]
        if tx.action == "buy":
            held[tx.ticker] = q + tx.quantity
        elif tx.action == "sell":
            if tx.quantity - q > 1e-6 and c is not None:
                c.issues.append(
                    Issue(
                        "error",
                        "quantity",
                        f"sell of {tx.quantity:g} exceeds {q:.4f} held on "
                        f"{tx.date} — missing earlier buys or a split?",
                    )
                )
                continue
            held[tx.ticker] = q - tx.quantity
        elif tx.action == "split" and tx.quantity > 0:
            held[tx.ticker] = q * tx.quantity


def _dupe_key(t: Transaction) -> tuple:
    return (t.date, t.ticker, t.action, round(t.quantity, 6), round(t.price, 4))


def _iso_date(value: str) -> str:
    return value.split("T", 1)[0].split(" ", 1)[0]
