"""Per-account alert delivery state — dedupe so hourly checks don't spam.

An alert condition ("NVDA above 190") often stays true for days; the hourly
cron must message on the rising edge, then stay quiet until either the
condition clears (re-arming the edge) or a 24h cooldown passes (one daily
re-reminder while it holds). State lives in alerts_state.json next to the
account's prefs.json and is mirrored to the bucket like every user file —
the cron is its only writer, so it never races the live app (which only
writes prefs/watchlist).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from stocks import storage
from stocks.config import Alert

VERSION = 1
COOLDOWN_HOURS = 24
# Digest highlights kept for the repetition guard (notify/narrative.py). Long
# enough that "the same sentence again" is caught across a working week, short
# enough that the prompt it is pasted into stays small.
HIGHLIGHTS_KEPT = 5


def fingerprint(ticker: str, alert: Alert) -> str:
    """Stable identity of one alert rule: same rule → same key across runs."""
    threshold = next(
        (f"{name}={val:g}" for name, val in (
            ("price", alert.price), ("pct", alert.pct), ("level", alert.level),
        ) if val is not None),
        "",
    )
    return f"{ticker.upper()}|{alert.type}|{threshold}|w={alert.window or ''}"


def load_state(path: Path) -> dict:
    """State from disk; a fresh structure when missing or corrupt."""
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and isinstance(raw.get("alerts"), dict):
            raw.setdefault("version", VERSION)
            raw.setdefault("delivery", {"blocked": False, "blocked_at": None})
            return raw
    except (OSError, ValueError):
        pass
    return {
        "version": VERSION,
        "alerts": {},
        "delivery": {"blocked": False, "blocked_at": None},
    }


def save_state(
    state: dict, path: Path, active_fingerprints: set[str] | None = None
) -> None:
    """Write state (pruning rules no longer on the watchlist) and mirror it."""
    if active_fingerprints is not None:
        state["alerts"] = {
            fp: rec for fp, rec in state["alerts"].items() if fp in active_fingerprints
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))
    storage.persist(path)


def should_send(
    state: dict,
    fp: str,
    fired: bool,
    now: datetime,
    cooldown_h: int = COOLDOWN_HOURS,
) -> bool:
    """Decide whether this run messages for `fp`, updating the state in place.

    fired, was clear            -> send (rising edge)
    fired, active, cooldown up  -> send again (daily re-reminder)
    fired, active, in cooldown  -> suppress
    not fired                   -> clear `active` so the next fire is an edge
    """
    rec = state["alerts"].get(fp, {"active": False, "last_sent": None})
    if not fired:
        if rec["active"]:
            state["alerts"][fp] = {**rec, "active": False}
        return False
    if rec["active"] and rec.get("last_sent"):
        last = datetime.fromisoformat(rec["last_sent"])
        if now - last < timedelta(hours=cooldown_h):
            state["alerts"][fp] = rec
            return False
    state["alerts"][fp] = {"active": True, "last_sent": now.isoformat()}
    return True


def recent_highlights(state: dict) -> list[str]:
    """The last few digest highlight lines, oldest first."""
    got = state.get("highlights")
    if not isinstance(got, list):
        return []
    return [str(x) for x in got if str(x).strip()][-HIGHLIGHTS_KEPT:]


def remember_highlight(state: dict, line: str, keep: int = HIGHLIGHTS_KEPT) -> None:
    """Record a sent highlight so tomorrow's prompt can avoid repeating it."""
    if not line.strip():
        return
    state["highlights"] = [*recent_highlights(state), line][-keep:]


def mark_blocked(state: dict, now: datetime) -> None:
    state["delivery"] = {"blocked": True, "blocked_at": now.isoformat()}


def is_blocked(state: dict) -> bool:
    return bool(state.get("delivery", {}).get("blocked"))
