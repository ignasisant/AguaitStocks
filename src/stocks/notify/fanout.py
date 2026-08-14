"""Enumerate notification subscribers and run the per-user cron jobs.

The cron (GitHub Actions) starts from a bare checkout: no data/users/, no
watchlist.yaml. Accounts are discovered by listing the bucket
(data/users/<slug>/prefs.json, plus the owner's root data/prefs.json), each
account's prefs/watchlist/ledger are restored, and only accounts that linked
Telegram and left the matching toggle on are yielded. With storage
unconfigured (local dev) the same discovery runs over the local filesystem,
so `stocks alerts --all-users` is testable without a bucket.

Write discipline: the cron writes only alerts_state.json. prefs.json and
watchlist.yaml belong to live app sessions — disjoint writers, no races.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from stocks import storage
from stocks.config import DATA_DIR, WATCHLIST_FILE, Holding, load_watchlist
from stocks.data.fetch import fetch_history
from stocks.notify import telegram
from stocks.notify.alerts import ALERT_PERIOD, check_holdings
from stocks.notify.state import (
    fingerprint,
    is_blocked,
    load_state,
    mark_blocked,
    save_state,
    should_send,
)

USERS_DIR = DATA_DIR / "users"


@dataclass(frozen=True)
class NotifyUser:
    """One account subscribed to notifications, with its data restored."""

    label: str  # slug, or "owner"
    prefs: dict
    watchlist: Path
    db: Path
    prefs_path: Path
    state_path: Path  # alerts_state.json, sibling of prefs

    @property
    def chat_id(self) -> int | str:
        return self.prefs["telegram_chat_id"]

    @property
    def lang(self) -> str:
        return self.prefs.get("language") or "en"


def _read_prefs(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _slugs() -> set[str]:
    """Account dir names, from the bucket when configured, else local."""
    if storage.enabled():
        found = {
            key.split("/")[2]
            for key in storage.list_keys("data/users/")
            if key.endswith("/prefs.json") and len(key.split("/")) == 4
        }
    else:
        found = {p.parent.name for p in USERS_DIR.glob("*/prefs.json")}
    return found - {"_guest"}


def _restore_user_files(*paths: Path) -> None:
    if not storage.enabled():
        return
    for p in paths:
        storage.restore(p)


def iter_notify_users(kind: str) -> list[NotifyUser]:
    """Accounts with Telegram linked and `notify_<kind>` enabled, restored.

    kind: "digest" | "alerts". The owner (root data/prefs.json + repo-root
    watchlist.yaml) is just another entry, labelled "owner".
    """
    users: list[NotifyUser] = []

    for slug_name in sorted(_slugs()):
        root = USERS_DIR / slug_name
        prefs_path = root / "prefs.json"
        watchlist = root / "watchlist.yaml"
        db = root / "portfolio.db"
        _restore_user_files(prefs_path, watchlist, db, root / "alerts_state.json")
        users.append(
            NotifyUser(
                label=slug_name,
                prefs=_read_prefs(prefs_path),
                watchlist=watchlist,
                db=db,
                prefs_path=prefs_path,
                state_path=root / "alerts_state.json",
            )
        )

    owner_prefs = DATA_DIR / "prefs.json"
    _restore_user_files(
        owner_prefs, WATCHLIST_FILE, DATA_DIR / "portfolio.db",
        DATA_DIR / "alerts_state.json",
    )
    if owner_prefs.exists():
        users.append(
            NotifyUser(
                label="owner",
                prefs=_read_prefs(owner_prefs),
                watchlist=WATCHLIST_FILE,
                db=DATA_DIR / "portfolio.db",
                prefs_path=owner_prefs,
                state_path=DATA_DIR / "alerts_state.json",
            )
        )

    return [
        u
        for u in users
        if u.prefs.get("telegram_chat_id") and u.prefs.get(f"notify_{kind}", True)
    ]


def _fetch_frames(tickers: set[str], max_workers: int = 4) -> dict[str, pd.DataFrame]:
    """One history fetch per distinct ticker across every user.

    N users watching AAPL cost one request — the thing that matters on
    yfinance-throttled CI runners. Failures are skipped (that ticker's alerts
    just don't evaluate this run), never fatal.
    """

    def _one(t: str) -> tuple[str, pd.DataFrame | None]:
        try:
            return t, fetch_history(t, period=ALERT_PERIOD)
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = pool.map(_one, sorted(tickers))
    return {t: df for t, df in results if df is not None and not df.empty}


def run_alerts_fanout(now: datetime | None = None) -> dict[str, str]:
    """Evaluate every subscriber's watchlist alerts and message the new hits.

    Returns {label: status} for the job log. Per-user failures are reported,
    never propagated — one broken account must not block the rest.
    """
    from stocks.web.i18n import translate

    now = now or datetime.now(UTC)
    users = iter_notify_users("alerts")
    work: list[tuple[NotifyUser, list[Holding]]] = []
    for user in users:
        holdings = [h for h in load_watchlist(user.watchlist) if h.alerts]
        if holdings:
            work.append((user, holdings))

    frames = _fetch_frames(
        {h.ticker for _, holdings in work for h in holdings}
    )

    status: dict[str, str] = {}
    for user, holdings in work:
        try:
            state = load_state(user.state_path)
            if is_blocked(state):
                status[user.label] = "skipped: blocked"
                continue
            hits = check_holdings(holdings, frames=frames)
            hit_fps = {fingerprint(h.ticker, h.alert): h for h in hits if h.alert}
            active_fps = {
                fingerprint(h.ticker, a) for h in holdings for a in h.alerts
            }
            to_send = [
                hit
                for fp, hit in hit_fps.items()
                if should_send(state, fp, fired=True, now=now)
            ]
            for fp in active_fps - set(hit_fps):
                should_send(state, fp, fired=False, now=now)  # re-arm cleared rules

            if to_send:
                header = translate("notify.alerts_subject", user.lang)
                lines = [f"{h.ticker}: {h.message}" for h in to_send]
                try:
                    telegram.send_message(
                        "\n".join([header, *lines]), user.chat_id, parse_mode=None
                    )
                    status[user.label] = f"sent {len(to_send)}"
                except telegram.TelegramBlocked:
                    mark_blocked(state, now)
                    status[user.label] = "blocked"
                time.sleep(0.2)  # stay far below Telegram's global send rate
            else:
                status[user.label] = "no hits"
            save_state(state, user.state_path, active_fingerprints=active_fps)
        except Exception as exc:  # noqa: BLE001 — cron isolation per account
            status[user.label] = f"error: {exc}"
    return status
