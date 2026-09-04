"""Login gate and per-user data resolution for the web app.

Authentication is Streamlit-native OIDC (st.login / st.user) configured in
.streamlit/secrets.toml under [auth] — see the README "Login (web app)"
section for the required keys.

Browsing is public: app.py calls resolve_user() before building the
navigation, which maps anonymous visitors to a shared read-only guest dir
(data/users/_guest/) seeded with the starter watchlist. Login is required
only where personal data is read or written — the Portfolio, Import and
Profile pages call require_login() at the top, and mutating widgets
(favorites, tags, watchlist editor) check is_logged_in().

Every account gets its own data under data/users/<slug>/ — watchlist.yaml,
portfolio.db, last_import.json, prefs.json — keyed by the verified OIDC
email. The optional [app].owner_email account maps to the repo-root files
instead (watchlist.yaml, data/portfolio.db), so the CLI — which is
single-user and always works on the root files — stays in sync with the
owner's web session. Broker-code aliases stay global (root watchlist.yaml):
they're reference data, not personal data.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import streamlit as st

from stocks import storage
from stocks.chat import memory
from stocks.config import (
    CURRENCIES,
    DATA_DIR,
    PROJECT_ROOT,
    WATCHLIST_FILE,
    stat_key,
    yaml_dump,
    yaml_load,
)
from stocks.web import css
from stocks.web.i18n import t as tr

USERS_DIR = DATA_DIR / "users"
GUEST_DIR = USERS_DIR / "_guest"

RECENT_SEARCHES_MAX = 5
DEFAULT_PREFS = {  # language None = auto (browser)
    "currency": "EUR",
    "language": None,
    "recent_searches": [],  # tickers clicked from the top-bar search, newest first
    # Registration accounting, stamped by mark_login(). first_seen is the
    # signup moment (ISO, UTC); last_seen is a date, rewritten once a day.
    "first_seen": None,
    "last_seen": None,
    "first_seen_estimated": False,
    # The signed-in address, for the jobs that have a prefs.json and no
    # session (and for the free-chain allowlist). None until the next login.
    "email": None,
    # Telegram notifications: chat_id is set by the Profile linking flow; the
    # toggles only take effect once it is. The cron (notify/fanout.py) reads
    # these headless straight from prefs.json.
    "telegram_chat_id": None,
    "notify_digest": True,
    "notify_alerts": True,
    # Tax residence drives which jurisdiction's rules the Realized & tax tab
    # applies and which currency the ledger is replayed in (see
    # stocks.portfolio.tax). None = auto, resolved from the browser region.
    # The rest are bracket inputs only some jurisdictions read.
    "tax_residence": None,
    "tax_filing_status": "single",
    "tax_other_income": 0.0,
    "tax_niit": False,
    "tax_subnational_rate": 0.0,
    # Whether the assistant drawer was open when the tab was last rendered, so
    # a reload puts the reader back in the conversation instead of behind the
    # launcher icon. Written by chat_core.render_side_panel.
    "chat_panel_open": False,
}

# Seeded on first login so a brand-new account has a working app instead of a
# page of empty states. Deliberately a *watchlist* and nothing more: no
# `shares`/`cost`, so every figure the app shows for these rows is live market
# data and nothing is ever presented as a holding the user does not own.
#
# The spread is the point — sectors, currencies and two asset classes — because
# the sections that make the first visit worth anything all rank or group across
# the list: the screener's P/E table, the earnings calendar, the 52-week
# extremes scan, the sentiment pass, the daily AI card. Two US mega-caps gave
# all of them one row and nothing to compare. The tags seed the dashboard's
# group expanders and the earnings filter pills; the untagged rows keep the
# plain "Watchlist" group populated too.
STARTER_WATCHLIST = """\
# Personal watchlist — managed from the Profile page.
#
# These are examples to explore with, not holdings: replace them with the
# tickers you actually follow. Nothing here counts as a position.
#
# Per-entry fields:
#   favorite: true   -> pinned to top of the dashboard + quick-access buttons
#   tags: [Tech]     -> groups the dashboard expanders and the earnings filters
#   shares: 12       -> makes it a real position (portfolio weights by value)
#   cost: 145.30     -> average buy price/share, for unrealised P/L
watchlist:
  - ticker: AAPL
    name: Apple
    favorite: true
    tags: [Tech]
  - ticker: MSFT
    name: Microsoft
    tags: [Tech]
  - ticker: NVDA
    name: Nvidia
    favorite: true
    tags: [Tech]
  - ticker: ASML
    name: ASML Holding
    tags: [Europe]
  - ticker: NVO
    name: Novo Nordisk
    tags: [Europe]
  - ticker: ITX.MC
    name: Inditex
    tags: [Europe]
  - ticker: TSM
    name: Taiwan Semiconductor
  - ticker: JPM
    name: JPMorgan Chase
  - ticker: XOM
    name: Exxon Mobil
  - ticker: BTC-EUR
    name: Bitcoin
"""


@dataclass(frozen=True)
class UserPaths:
    """Where one account's data lives."""

    root: Path
    watchlist: Path
    db: Path
    last_import: Path
    prefs: Path
    chat: Path
    bank: Path
    action: Path  # the dashboard's daily AI card (chat/daily.py)


def slug(email: str) -> str:
    """Filesystem-safe, collision-proof directory name for an account email.

    The readable base maps every non-alphanumeric run to "_", so distinct
    addresses can collide ("a.b@c.com" and "a@b.c.com" both give
    "a_b_c_com"). The digest suffix ties the directory to the exact address,
    so the second account to sign in can never land in the first one's data.
    """
    e = email.strip().lower()
    base = re.sub(r"[^a-z0-9]+", "_", e).strip("_")
    return f"{base}_{hashlib.sha256(e.encode()).hexdigest()[:8]}"


def _legacy_slug(email: str) -> str:
    """slug() as it was before the digest suffix — kept only so existing
    account dirs (local or in the bucket) can be migrated on next login."""
    return re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_")


def paths_for(
    email: str, owner_email: str | None = None, users_dir: Path = USERS_DIR
) -> UserPaths:
    """Resolve an account's data paths.

    The owner account maps to the repo-root watchlist and data/portfolio.db
    so the single-user CLI and the owner's web session share one book; every
    other account lives under data/users/<slug>/.
    """
    if owner_email and email.strip().lower() == owner_email.strip().lower():
        return UserPaths(
            root=PROJECT_ROOT,
            watchlist=WATCHLIST_FILE,
            db=DATA_DIR / "portfolio.db",
            last_import=DATA_DIR / "last_import.json",
            prefs=DATA_DIR / "prefs.json",
            chat=DATA_DIR / "chat.json",
            bank=DATA_DIR / "bank.json",
            action=DATA_DIR / "daily_action.json",
        )
    d = users_dir / slug(email)
    return UserPaths(
        root=d,
        watchlist=d / "watchlist.yaml",
        db=d / "portfolio.db",
        last_import=d / "last_import.json",
        prefs=d / "prefs.json",
        chat=d / "chat.json",
        bank=d / "bank.json",
        action=d / "daily_action.json",
    )


def guest_paths() -> UserPaths:
    """The anonymous visitors' shared data dir.

    Read-only through the UI: every write path (favorites, tags, watchlist
    editor, imports, prefs) sits behind require_login()/is_logged_in(), so
    guests only ever read the starter watchlist and an empty ledger.
    """
    return UserPaths(
        root=GUEST_DIR,
        watchlist=GUEST_DIR / "watchlist.yaml",
        db=GUEST_DIR / "portfolio.db",
        last_import=GUEST_DIR / "last_import.json",
        prefs=GUEST_DIR / "prefs.json",
        chat=GUEST_DIR / "chat.json",
        bank=GUEST_DIR / "bank.json",
        action=GUEST_DIR / "daily_action.json",
    )


_USER_FILES = (
    "watchlist.yaml", "portfolio.db", "last_import.json", "prefs.json", "chat.json",
    "bank.json", "daily_action.json", memory.FILE,
)


def _persist(path: Path) -> None:
    """Mirror to the bucket after a committed local write. Cloud failure
    must not crash the interaction — but surface it: the local copy is
    fine now and still vanishes on the next container restart."""
    try:
        storage.persist(path)
    except Exception:
        st.toast(tr("common.sync_failed"), icon=":material/cloud_off:")


def _migrate_legacy(paths: UserPaths, legacy_root: Path) -> None:
    """Move an account dir named with the pre-digest slug to its new name.

    Runs once per account: a no-op as soon as paths.root exists. Covers both
    a dir still on local disk and one that only survives in the bucket (an
    ephemeral host after a redeploy) — bucket objects are re-keyed to the new
    dir so the next boot restores from there directly.
    """
    if paths.root.exists() or legacy_root == paths.root:
        return
    if not legacy_root.exists() and storage.enabled():
        # restore() only writes (and creates the dir) when the key exists,
        # so after this loop legacy_root exists iff the bucket had the account.
        for name in _USER_FILES:
            storage.restore(legacy_root / name)
    if not legacy_root.exists():
        return
    legacy_root.rename(paths.root)
    # Deliberately storage.persist, not _persist: a failed push must abort
    # (ensure_user_data fails the login closed) before the old key is
    # deleted, or the bucket could end up holding neither copy.
    for name in _USER_FILES:
        storage.persist(paths.root / name)  # push under the new key
        storage.persist(legacy_root / name)  # gone locally -> delete old key


def ensure_user_data(paths: UserPaths, legacy_root: Path | None = None) -> bool:
    """First login: create the account's folder and seed a starter watchlist.

    Returns True when this call seeded a brand-new account — the one moment a
    signup can be dated exactly, which mark_login() records.

    With [storage] configured, the account's files are pulled from the bucket
    first (once per process), so an ephemeral redeploy starts from the
    persisted copies instead of re-seeding. `legacy_root` is the account's
    pre-digest-slug dir; when it still exists (locally or in the bucket) it
    is renamed and re-keyed before anything is restored or seeded.

    A bucket outage here fails closed: falling through to the seeding below
    would show an empty book whose next save overwrites the account's real
    cloud data, so the session halts instead and the user retries.
    """
    try:
        if legacy_root is not None:
            _migrate_legacy(paths, legacy_root)
        paths.root.mkdir(parents=True, exist_ok=True)
        storage.restore_once(
            paths.root,
            (
                paths.watchlist,
                paths.db,
                paths.last_import,
                paths.prefs,
                paths.chat,
                paths.bank,
                paths.action,
                memory.path_for(paths.root),
            ),
        )
    except Exception:
        st.error(tr("common.storage_restore_failed"), icon=":material/cloud_off:")
        st.stop()
    if paths.watchlist.exists():
        return False
    paths.watchlist.write_text(STARTER_WATCHLIST)
    _persist(paths.watchlist)
    return True


def delete_account(paths: UserPaths) -> None:
    """Erase one account's data everywhere: bucket copies first, then disk.

    The GDPR-shaped promise on the legal page: everything under the account's
    data dir goes, cloud copies included. Bucket keys are enumerated (not just
    the fixed _USER_FILES) so nothing generated later survives. Bucket first
    and loudly: if the cloud delete fails the local copies stay too, so a
    retry still sees a consistent account instead of resurrecting the bucket
    from a half-deleted disk on the next write.

    Refuses the owner account (its "data dir" is the repo root — deleting it
    would take the CLI's own book and reference data with it) and the shared
    guest dir. Backup snapshots are immutable history and expire on their own
    schedule; the legal copy says so.
    """
    root = paths.root.resolve()
    if root in (PROJECT_ROOT.resolve(), GUEST_DIR.resolve()):
        raise ValueError("refusing to delete the owner or guest data")
    if USERS_DIR.resolve() not in root.parents:
        raise ValueError(f"not an account dir: {root}")

    if storage.enabled():
        prefix = root.relative_to(PROJECT_ROOT.resolve()).as_posix()
        for key in storage.list_keys(prefix + "/"):
            storage.delete_key(key)
    if root.exists():
        import shutil

        shutil.rmtree(root)


def is_logged_in() -> bool:
    """True when an authenticated identity with a verified email is present.

    All personal data is keyed to the email claim, so an unverified address
    must never resolve to a data dir — an IdP that skips verification would
    otherwise let anyone claim someone else's account. Google always sends
    email_verified=true for its accounts.
    """
    try:
        configured = "auth" in st.secrets
    except Exception:
        # No secrets file at all — a fresh clone, a CI checkout. Membership on
        # st.secrets *raises* there rather than answering False, and this
        # accessor is called by every page, so an unguarded read takes the
        # whole app down instead of degrading to "nobody is signed in".
        configured = False
    return bool(
        configured
        and st.user.is_logged_in
        and bool(str(getattr(st.user, "email", "") or "").strip())
        and bool(getattr(st.user, "email_verified", False))
    )


def current_email() -> str:
    """The signed-in account's email, or "" for a guest. One accessor so
    callers (and tests) never have to reach into st.user themselves."""
    return str(getattr(st.user, "email", "") or "").strip()


# ------------------------------------------------------- login accounting
# Cloud Logging keeps 30 days, so "how many accounts exist" is not a question
# the logs can answer — anyone who signed up and never came back has aged out
# of them. Each account's own prefs.json carries the two dates that can, and
# `stocks users` reads them straight out of the bucket. telemetry.bind_run
# turns the same verdict into auth.signup/auth.login events, so a signup is
# also visible in place on the log timeline.


def mark_login(paths: UserPaths, *, seeded: bool = False, email: str = "") -> str:
    """Stamp this account's first/last login; return "signup" or "login".

    `seeded` is ensure_user_data()'s verdict: True only when this run created
    the account's dir, so only then is the stamp an exact signup date. An
    account that predates this bookkeeping gets first_seen backfilled to now
    with first_seen_estimated=True and counts as a plain login — the roster
    never claims a precision it doesn't have.

    prefs.json is mirrored to the bucket on every save, so this writes at most
    once per account per day: a PUT on each sign-in would cost more than the
    metric is worth, and last_seen is only ever read at day granularity.
    """
    prefs = load_prefs(paths.prefs)
    today = datetime.now(UTC).date().isoformat()
    kind = "login"
    changed = False
    # The address, written into the account's own file so headless jobs can
    # identify it — the Telegram bot has a prefs.json and no session. The
    # free-chain allowlist (engine.free_eligible) is the caller that needs it.
    if email and prefs.get("email") != email:
        prefs["email"] = email
        changed = True
    if not prefs.get("first_seen"):
        prefs["first_seen"] = datetime.now(UTC).isoformat(timespec="seconds")
        prefs["first_seen_estimated"] = not seeded
        kind = "signup" if seeded else "login"
        changed = True
    if prefs.get("last_seen") != today:
        prefs["last_seen"] = today
        changed = True
    if changed:
        save_prefs(prefs, paths.prefs)
    return kind


def resolve_user() -> UserPaths:
    """Resolve the session's data paths without gating; call before the nav.

    Logged-in accounts get their own dir (owner → repo-root files); anonymous
    visitors get the shared guest dir so the public pages can render. Stores
    the paths in session state for the page modules.
    """
    legacy = None
    logged_in = is_logged_in()
    email = ""
    if logged_in:
        email = str(st.user.email).strip()
        owner = str(st.secrets.get("app", {}).get("owner_email", "")).strip() or None
        paths = paths_for(email, owner)
        if paths.root != PROJECT_ROOT:  # owner uses repo-root files, no slug
            legacy = USERS_DIR / _legacy_slug(email)
    else:
        paths = guest_paths()
    seeded = ensure_user_data(paths, legacy_root=legacy)
    # Streamlit reruns this on every interaction; the guard keeps the stamp
    # (and its prefs read) to the first run under a given identity, and a
    # sign-out clears it so a later sign-in is evaluated again.
    if not logged_in:
        st.session_state.pop("_login_marked", None)
        st.session_state["_login_kind"] = ""
    elif st.session_state.get("_login_marked") != email:
        st.session_state["_login_marked"] = email
        st.session_state["_login_kind"] = mark_login(paths, seeded=seeded,
                                                     email=email)
    st.session_state["user_paths"] = paths
    return paths


def login() -> None:
    """`st.login()` plus `st.stop()`, for sign-in buttons' on_click.

    st.login() only enqueues the redirect message — the run then re-renders
    the whole page while the browser is already leaving for Google, and every
    lazy-loaded frontend chunk that navigation aborts flashes a red
    "error loading dynamically imported module" box. Stopping right after the
    enqueue sends the redirect with an empty delta, so the page stands still
    until Google takes over. Safe in a callback: callbacks run in the script
    thread and StopException is the normal early-exit there too.
    """
    st.login()
    st.stop()


def require_login() -> UserPaths:
    """Auth gate for pages that touch personal data (Portfolio, Import,
    Profile) — public pages never call it.

    Renders the sign-in screen (or setup help while [auth] secrets are
    missing) and st.stop()s until an authenticated identity with an email is
    present. On success, seeds the account's data dir and stores its paths in
    session state for the page modules.
    """
    if "auth" not in st.secrets:
        st.error(tr("auth.not_configured"), icon=":material/lock:")
        st.markdown(tr("auth.setup_help"))
        st.stop()

    if not st.user.is_logged_in:
        _login_screen()
        st.stop()

    email = str(getattr(st.user, "email", "") or "").strip()
    if not email:
        st.error(tr("auth.no_email"))
        st.button(tr("common.log_out"), icon=":material/logout:", on_click=st.logout)
        st.stop()

    # Must mirror is_logged_in(): without this branch an unverified identity
    # would silently fall through resolve_user() onto the guest paths.
    if not bool(getattr(st.user, "email_verified", False)):
        st.error(tr("auth.email_unverified"))
        st.button(tr("common.log_out"), icon=":material/logout:", on_click=st.logout)
        st.stop()

    return resolve_user()


# Google's "G" mark isn't in Material Symbols, so it's drawn onto the sign-in
# button as a CSS ::before tile (white rounded square, brand-guideline style).
# Angle brackets are %-encoded: the URI is interpolated into _LOGIN_CSS, and
# DOMPurify silently drops a whole style block whose text contains a raw "<".
_GOOGLE_G_SVG = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'%3E"
    "%3Cpath fill='%234285F4' d='M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94"
    "c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z'/%3E"
    "%3Cpath fill='%2334A853' d='M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6"
    "c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19"
    "C6.51 42.62 14.62 48 24 48z'/%3E"
    "%3Cpath fill='%23FBBC05' d='M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59"
    "s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78"
    "l7.97-6.19z'/%3E"
    "%3Cpath fill='%23EA4335' d='M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85"
    "C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19"
    "C12.43 13.72 17.74 9.5 24 9.5z'/%3E"
    "%3C/svg%3E"
)

_LOGIN_CSS = f"""\
<style>
[class*="st-key-google_signin"] button::before {{
    content: "";
    flex: 0 0 auto;
    width: 1.25rem;
    height: 1.25rem;
    margin-right: 0.4rem;
    border-radius: var(--ag-radius-xs);
    /* Brand exception, not a DS neutral: Google's sign-in guidelines require
       the "G" on pure white. Declared as widgets.BRAND_GOOGLE_TILE and read
       here through the custom property — this module can't import widgets
       (widgets imports auth), and app.py emits the tokens long before the
       login gate renders. */
    background-color: var(--ag-brand-google-tile, #fff);
    background-image: url("{_GOOGLE_G_SVG}");
    background-repeat: no-repeat;
    background-position: center;
    background-size: 0.85rem;
}}
</style>
"""


def _login_screen() -> None:
    css.inject(_LOGIN_CSS)
    st.space("xlarge")
    with st.container(horizontal_alignment="center"):
        with st.container(border=True, width=420, horizontal_alignment="center"):
            st.space("xsmall")
            st.image(
                str(Path(__file__).parent / "assets" / "topstocks-logo.svg"),
                width=200,
            )
            st.caption(
                tr("auth.tagline"),
                text_alignment="center",
            )
            st.space("xsmall")
            st.markdown(
                tr("auth.signin_prompt"),
                text_alignment="center",
            )
            st.button(
                tr("common.sign_in_google"),
                type="primary",
                key="google_signin",
                on_click=login,
                width="stretch",
            )
            st.caption(
                tr("auth.browsing_public"),
                text_alignment="center",
            )
            st.space("xsmall")


# ---------------------------------------------------------------- accessors
# Set by resolve_user() in app.py (guest paths when anonymous); pages run
# after it via st.navigation.


def user_paths() -> UserPaths:
    return st.session_state["user_paths"]


def watchlist_path() -> Path:
    return user_paths().watchlist


def db_path() -> Path:
    return user_paths().db


def chat_path() -> Path:
    return user_paths().chat


# ------------------------------------------------------------- preferences


@lru_cache(maxsize=64)
def _prefs_stored(path: Path, _key: tuple[int, int] | None) -> dict:
    """The stored half of `load_prefs`, memoized on the file's stat signature.

    Keyed like `config._yaml`: `save_prefs` changes the file, which changes the
    key, so there is no invalidation call to forget. Never handed out directly
    — `load_prefs` merges a fresh dict over the defaults, because its callers
    mutate what they get back and then save it.
    """
    if _key is None:
        return {}
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return stored if isinstance(stored, dict) else {}


def load_prefs(path: Path | None = None) -> dict:
    """This account's preferences, defaults filled in.

    Read on nearly every rerun by a dozen callers (the language resolver, the
    setup card, the tour, the chat panel), so the file read and parse are
    memoized while the merge stays per-call: the result is mutable and callers
    edit it in place before `save_prefs`.
    """
    p = path or user_paths().prefs
    return {**DEFAULT_PREFS, **_prefs_stored(p, stat_key(p))}


def save_prefs(prefs: dict, path: Path | None = None) -> None:
    p = path or user_paths().prefs
    p.write_text(json.dumps(prefs, indent=2))
    _persist(p)


# ------------------------------------------------------ daily action card


def load_action(path: Path | None = None) -> dict:
    """The stored daily-action card as a raw dict ({} when there is none).

    Shaped like load_prefs: unreadable or corrupt reads as "nothing stored",
    which sends the dashboard down the regenerate path instead of an error.
    """
    p = path or user_paths().action
    try:
        out = json.loads(p.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def save_action(card: dict, path: Path | None = None) -> None:
    """Store today's card, mirrored to the bucket like every other user file.

    Worth the round trip for one small JSON: the card costs an LLM call, and
    Cloud Run recycles the container on idle — without the mirror every cold
    start would spend another unit of the free allowance on a card the account
    already has.
    """
    p = path or user_paths().action
    p.write_text(json.dumps(card, indent=2))
    _persist(p)


# ------------------------------------------------------- recent searches
# The top-bar search remembers the last few tickers the user clicked to
# explore, so refocusing the empty field can offer them again (survives
# reload — stored in prefs like every other per-user setting).


def load_recent_searches(prefs: dict | None = None) -> list[str]:
    p = prefs if prefs is not None else load_prefs()
    val = p.get("recent_searches", [])
    if not isinstance(val, list):
        return []
    return [str(t) for t in val][:RECENT_SEARCHES_MAX]


def push_recent_search(ticker: str) -> None:
    """Move `ticker` to the front of the recent list, deduped, capped."""
    t = ticker.strip().upper()
    if not t:
        return
    prefs = load_prefs()
    rest = [x for x in load_recent_searches(prefs) if x != t]
    prefs["recent_searches"] = [t, *rest][:RECENT_SEARCHES_MAX]
    save_prefs(prefs)


# ------------------------------------------------------- investor profile
# Who the assistant is advising, stated by the user (not hard-coded). Stored
# under prefs["investor_profile"] as stable enum keys (locale-independent, so
# the English system prompt stays stable whatever the UI language) plus a free
# notes field. chat_core reads it to build the assistant persona; empty ->
# chat_core falls back to its historical default line.

PROFILE_RISK = ("aggressive", "very_aggressive", "balanced", "conservative")
PROFILE_HORIZON = ("5y_plus", "3_5y", "1_3y", "under_1y")
PROFILE_FOCUS = ("tech", "em", "crypto", "dividends_value")
PROFILE_CONSTRAINTS = ("spain_tax", "us_tax", "eur", "no_leverage", "esg")

# Example tickers per declared focus, offered on the Profile page to an
# account whose watchlist does not have them yet (see focus_suggestions).
#
# Keyed on `focus` — a stated interest — and deliberately NOT on `risk`. A
# list assembled from someone's risk tolerance is a recommendation however it
# is worded, and this app is not in that business; a list assembled from "you
# said you follow emerging markets" is a shortcut for typing eight symbols,
# which is all it is meant to be. The seed already covers each area thinly, so
# these widen rather than replace, and nothing is ever removed.
#
# Tags are the English labels the starter watchlist uses, so an appended row
# lands in the same dashboard group as the seeded ones rather than starting a
# near-duplicate group.
FOCUS_EXAMPLES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "tech": (
        ("GOOGL", "Alphabet", "Tech"),
        ("AMD", "AMD", "Tech"),
        ("NOW", "ServiceNow", "Tech"),
    ),
    "em": (
        ("BABA", "Alibaba", "Emerging markets"),
        ("INFY", "Infosys", "Emerging markets"),
        ("NU", "Nu Holdings", "Emerging markets"),
    ),
    "crypto": (
        ("ETH-EUR", "Ethereum", "Crypto"),
        ("SOL-EUR", "Solana", "Crypto"),
        ("COIN", "Coinbase", "Crypto"),
    ),
    "dividends_value": (
        ("KO", "Coca-Cola", "Dividends"),
        ("PG", "Procter & Gamble", "Dividends"),
        ("ENB", "Enbridge", "Dividends"),
    ),
}

_PROFILE_DEFAULTS = {
    "risk": "aggressive",
    "horizon": "5y_plus",
    "focus": [],
    "constraints": [],
    "notes": "",
}


def load_profile(prefs: dict | None = None) -> dict:
    """The account's investor profile, defaults filled in for missing fields.

    `set` is True once the user has saved the form at least once; callers use
    it to tell a real (possibly minimal) profile from the mere defaults.
    """
    prefs = prefs if prefs is not None else load_prefs()
    stored = prefs.get("investor_profile") or {}
    return {**_PROFILE_DEFAULTS, **stored, "set": bool(stored.get("set"))}


def profile_is_set(prefs: dict | None = None) -> bool:
    prefs = prefs if prefs is not None else load_prefs()
    return bool((prefs.get("investor_profile") or {}).get("set"))


def save_profile(profile: dict) -> None:
    prefs = load_prefs()
    prefs["investor_profile"] = {**profile, "set": True}
    save_prefs(prefs)


def render_profile_form(key_prefix: str) -> dict:
    """Draw the investor-profile widgets and return the collected values.

    Shared by the Profile page and the first-login dialog; the caller renders
    its own Save button and calls save_profile(). Does not persist on its own.
    """
    cur = load_profile()
    risk = st.radio(
        tr("profile.iv_risk"),
        PROFILE_RISK,
        index=PROFILE_RISK.index(cur["risk"]) if cur["risk"] in PROFILE_RISK else 0,
        format_func=lambda k: tr(f"profile.iv_risk_{k}"),
        horizontal=True,
        key=f"{key_prefix}_risk",
    )
    horizon = st.radio(
        tr("profile.iv_horizon"),
        PROFILE_HORIZON,
        index=PROFILE_HORIZON.index(cur["horizon"])
        if cur["horizon"] in PROFILE_HORIZON
        else 0,
        format_func=lambda k: tr(f"profile.iv_horizon_{k}"),
        horizontal=True,
        key=f"{key_prefix}_horizon",
    )
    focus = st.multiselect(
        tr("profile.iv_focus"),
        PROFILE_FOCUS,
        default=[f for f in cur["focus"] if f in PROFILE_FOCUS],
        format_func=lambda k: tr(f"profile.iv_focus_{k}"),
        key=f"{key_prefix}_focus",
    )
    constraints = st.multiselect(
        tr("profile.iv_constraints"),
        PROFILE_CONSTRAINTS,
        default=[c for c in cur["constraints"] if c in PROFILE_CONSTRAINTS],
        format_func=lambda k: tr(f"profile.iv_constraints_{k}"),
        key=f"{key_prefix}_constraints",
    )
    notes = st.text_area(
        tr("profile.iv_notes"),
        value=cur["notes"],
        placeholder=tr("profile.iv_notes_ph"),
        key=f"{key_prefix}_notes",
    )
    return {
        "risk": risk,
        "horizon": horizon,
        "focus": focus,
        "constraints": constraints,
        "notes": notes.strip(),
    }


def maybe_prompt_profile() -> None:
    """First load per session: pop the investor-profile setup dialog.

    Fires once per session for a signed-in account that hasn't saved a profile
    yet; "Skip for now" just closes it (the session flag stops it re-popping),
    so it nudges again next session until the profile is filled or the user
    completes it from the Profile page. No-op otherwise.
    """
    if "auth" not in st.secrets or not is_logged_in():
        return
    if profile_is_set() or st.session_state.get("_profile_prompt_seen"):
        return
    st.session_state["_profile_prompt_seen"] = True
    # Built at call time (not @st.dialog) so the title resolves in the run's
    # active language rather than freezing at import — same as the login modal.
    st.dialog(tr("profile.iv_dialog_title"))(_profile_dialog_body)()


def _profile_dialog_body() -> None:
    st.markdown(tr("profile.iv_dialog_intro"))
    profile = render_profile_form("iv_dialog")
    save_col, skip_col = st.columns(2)
    if save_col.button(
        tr("profile.iv_save"), type="primary", key="iv_dialog_save", width="stretch"
    ):
        save_profile(profile)
        st.rerun()  # close the modal; profile_is_set() now True -> never re-pops
    if skip_col.button(tr("profile.iv_skip"), key="iv_dialog_skip", width="stretch"):
        st.rerun()  # close; the seen-flag keeps it shut for the rest of the session


# ------------------------------------------------------------- chat threads
# The assistant keeps several conversations per account — each with an id, a
# title and timestamps, one of them active — persisted like prefs so they
# survive a reload, a new session or an ephemeral redeploy, and mirrored to
# the bucket.
#
# All of them live in a single chat.json ({"version", "active",
# "conversations"}) rather than one file per thread: the whole per-account
# sync path (_USER_FILES, storage.restore_once, _persist) is built on a fixed
# tuple of paths, so a directory of threads would need its own bucket keying
# and orphan cleanup for nothing the user can see.
#
# load_chat/save_chat keep their original list-of-turns signature and act on
# the active conversation, so the Telegram bot and the headless engine never
# had to learn about threads.

CHAT_VERSION = 2
MAX_CONVERSATIONS = 50  # oldest by last use pruned first; never the active one
_TITLE_MAX = 80


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _blank_conversation(title: str = "") -> dict:
    now = _now()
    return {
        "id": f"c_{uuid.uuid4().hex[:8]}",
        "title": title,
        # False once the user renames it, so auto-titling stops overwriting.
        "title_auto": True,
        "created": now,
        "updated": now,
        "messages": [],
    }


def _empty_book() -> dict:
    conv = _blank_conversation()
    return {"version": CHAT_VERSION, "active": conv["id"], "conversations": [conv]}


def load_book(path: Path | None = None) -> dict:
    """Every conversation for the account, in the current shape.

    Never writes — a turn that fails must leave chat.json untouched (and
    absent when it never existed). A v1 file (the bare list of turns the
    single-thread assistant wrote) migrates to one conversation; anything
    missing or corrupt yields a fresh empty book.
    """
    p = path or user_paths().chat
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError, TypeError):
        data = None

    if isinstance(data, list):  # v1: one unnamed thread
        conv = _blank_conversation()
        conv["messages"] = [m for m in data if isinstance(m, dict)]
        return {"version": CHAT_VERSION, "active": conv["id"],
                "conversations": [conv]}
    if not isinstance(data, dict):
        return _empty_book()

    convs = [
        c for c in (data.get("conversations") or [])
        if isinstance(c, dict) and c.get("id")
    ]
    for c in convs:  # tolerate records written by an older/partial writer
        c.setdefault("title", "")
        c.setdefault("title_auto", True)
        c.setdefault("created", _now())
        c.setdefault("updated", c["created"])
        c["messages"] = [m for m in (c.get("messages") or []) if isinstance(m, dict)]
    if not convs:
        return _empty_book()

    active = data.get("active")
    if active not in {c["id"] for c in convs}:
        active = convs[0]["id"]
    return {"version": CHAT_VERSION, "active": active, "conversations": convs}


def _pruned(book: dict) -> dict:
    """The book capped at MAX_CONVERSATIONS, dropping least-recently-used
    threads first and never the active one."""
    convs = book["conversations"]
    if len(convs) <= MAX_CONVERSATIONS:
        return book
    ranked = sorted(convs, key=lambda c: c.get("updated") or "", reverse=True)
    keep = [c for c in ranked if c["id"] == book["active"]][:1]
    keep += [c for c in ranked if c["id"] != book["active"]][
        : MAX_CONVERSATIONS - len(keep)
    ]
    order = {c["id"]: i for i, c in enumerate(convs)}
    return {**book, "conversations": sorted(keep, key=lambda c: order[c["id"]])}


def save_book(book: dict, path: Path | None = None) -> None:
    p = path or user_paths().chat
    p.write_text(json.dumps(_pruned(book), indent=2))
    _persist(p)


def _active(book: dict) -> dict:
    """The active conversation — load_book guarantees one exists."""
    for c in book["conversations"]:
        if c["id"] == book["active"]:
            return c
    return book["conversations"][0]


def load_chat(path: Path | None = None) -> list[dict]:
    """The active conversation's turns (the historical single-thread API)."""
    return _active(load_book(path))["messages"]


def memory_path(path: Path | None = None) -> Path:
    """The account's long-term chat index, beside its chat history."""
    return (path or user_paths().chat).parent / memory.FILE


def save_chat(history: list[dict], path: Path | None = None) -> None:
    """Replace the active conversation's turns and stamp it as just used.

    Indexing rides along here rather than at the two call sites: every turn
    that reaches disk is a turn the assistant may need to recall later, and
    both surfaces (the panel and the Telegram bot) already come through this
    one function. It is idempotent and best-effort — a failed index costs a
    worse search, never a lost message."""
    book = load_book(path)
    conv = _active(book)
    conv["messages"] = list(history)
    conv["updated"] = _now()
    save_book(book, path)
    index = memory_path(path)
    if memory.remember(index, history, conv["id"]):
        _persist(index)  # only when it actually grew — most saves add nothing


def list_conversations(path: Path | None = None) -> list[dict]:
    """Conversation metadata (no message bodies), most recently used first."""
    book = load_book(path)
    metas = [
        {
            "id": c["id"], "title": c["title"], "title_auto": c["title_auto"],
            "created": c["created"], "updated": c["updated"],
            "messages": len(c["messages"]), "active": c["id"] == book["active"],
        }
        for c in book["conversations"]
    ]
    return sorted(metas, key=lambda m: m["updated"], reverse=True)


def active_conversation(path: Path | None = None) -> dict:
    """Metadata of the conversation the next turn will land in."""
    c = _active(load_book(path))
    return {k: v for k, v in c.items() if k != "messages"}


def new_conversation(path: Path | None = None, title: str = "") -> str:
    """Start (and activate) an empty conversation; returns its id.

    An active conversation that is still empty is reused, so pressing New
    repeatedly can't stack blank threads."""
    book = load_book(path)
    conv = _active(book)
    if conv["messages"]:
        conv = _blank_conversation(title)
        book["conversations"].append(conv)
    elif title:
        conv["title"] = title[:_TITLE_MAX]
    book["active"] = conv["id"]
    save_book(book, path)
    return conv["id"]


def set_active_conversation(cid: str, path: Path | None = None) -> None:
    book = load_book(path)
    if any(c["id"] == cid for c in book["conversations"]):
        book["active"] = cid
        save_book(book, path)


def rename_conversation(cid: str, title: str, path: Path | None = None) -> None:
    """User-set title — pins it, so auto-titling never overwrites it again."""
    book = load_book(path)
    for c in book["conversations"]:
        if c["id"] == cid:
            c["title"] = title.strip()[:_TITLE_MAX]
            c["title_auto"] = False
            save_book(book, path)
            return


def autotitle_conversation(cid: str, title: str, path: Path | None = None) -> None:
    """Title derived from the opening exchange; a no-op on a renamed thread."""
    book = load_book(path)
    for c in book["conversations"]:
        if c["id"] == cid and c.get("title_auto", True):
            c["title"] = title.strip()[:_TITLE_MAX]
            save_book(book, path)
            return


def delete_conversation(cid: str, path: Path | None = None) -> None:
    """Drop a conversation. Deleting the active one falls back to the most
    recently used survivor — or a fresh empty thread when it was the last."""
    book = load_book(path)
    kept = [c for c in book["conversations"] if c["id"] != cid]
    if len(kept) == len(book["conversations"]):
        return
    if not kept:
        kept = [_blank_conversation()]
    if book["active"] == cid:
        book["active"] = max(kept, key=lambda c: c["updated"])["id"]
    book["conversations"] = kept
    save_book(book, path)
    # A deleted conversation must not keep answering questions through the
    # memory index.
    index = memory_path(path)
    if memory.forget(index, cid):
        _persist(index)


def reporting_currency() -> str:
    """The currency the app reckons in for this account.

    Not a display setting any more: the ledger is replayed *in* this currency
    (every leg at its own trade-date rate), so the figures are computed in it
    rather than converted afterwards. The tax tab is the one exception — it
    follows the tax residence, which is a legal fact rather than a preference.
    """
    ccy = str(load_prefs().get("currency", "EUR")).upper()
    return ccy if ccy in CURRENCIES else "EUR"


# ---------------------------------------------------------------- watchlist


def focus_suggestions(
    profile: dict | None = None, path: Path | None = None
) -> list[dict]:
    """Example rows for the account's declared focus that it does not have yet.

    Returns `save_watchlist_entries` rows, in `FOCUS_EXAMPLES` order, with
    everything already on the watchlist filtered out — so the offer shrinks as
    it is taken up and disappears once there is nothing left to add. Empty
    whenever no focus is declared, which is what keeps this off the page for
    an account that skipped the profile.
    """
    from stocks.config import load_watchlist  # local: same reason as all_tags

    p = profile if profile is not None else load_profile()
    have = {h.ticker.upper() for h in load_watchlist(path or watchlist_path())}
    out: list[dict] = []
    for area in p.get("focus") or []:
        for ticker, name, tag in FOCUS_EXAMPLES.get(area, ()):
            if ticker.upper() in have:
                continue
            have.add(ticker.upper())  # a ticker in two areas is offered once
            out.append({"ticker": ticker, "name": name, "tags": [tag]})
    return out


def save_watchlist_entries(entries: list[dict], path: Path | None = None) -> None:
    """Rewrite the watchlist from the profile editor's rows.

    The editor covers ticker/name/favorite/shares/cost/tags; per-ticker alert
    rules and the top-level aliases map are YAML-only, so they're carried
    over untouched for tickers that survive the edit. Rows without a "tags"
    key keep their existing tags too (rows with one — even an empty list —
    set them). Rows without a ticker are dropped; tickers are upper-cased
    and de-duplicated (first row wins).
    """
    p = path or watchlist_path()
    raw = yaml_load(p.read_text()) if p.exists() else {}
    old_alerts = {
        str(item.get("ticker", "")).upper(): item.get("alerts")
        for item in (raw.get("watchlist") or [])
        if item.get("alerts")
    }
    old_tags = {
        str(item.get("ticker", "")).upper(): item.get("tags")
        for item in (raw.get("watchlist") or [])
        if item.get("tags")
    }

    items: list[dict] = []
    seen: set[str] = set()
    for e in entries:
        ticker = str(e.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        item: dict = {"ticker": ticker}
        name = str(e.get("name") or "").strip()
        if name:
            item["name"] = name
        if e.get("favorite"):
            item["favorite"] = True
        shares = e.get("shares")
        if shares:
            item["shares"] = float(shares)
        cost = e.get("cost")
        if cost:
            item["cost"] = float(cost)
        tags = e.get("tags")
        if tags is not None:
            clean = _clean_tags(tags)
            if clean:
                item["tags"] = clean
        elif ticker in old_tags:
            item["tags"] = old_tags[ticker]
        if ticker in old_alerts:
            item["alerts"] = old_alerts[ticker]
        items.append(item)

    raw["watchlist"] = items
    p.write_text(yaml_dump(raw))
    _persist(p)


# ------------------------------------------------------- favorites and tags


def _clean_tags(tags) -> list[str]:
    """Normalize a tag list: strip, drop empties, de-dup case-insensitively
    (first spelling wins), keep entry order."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        t = str(t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _update_entry(ticker: str, mutate, path: Path | None = None) -> dict:
    """Apply `mutate(entry)` to a ticker's watchlist entry, creating the entry
    (ticker only) when it isn't listed yet — favoriting or tagging a custom or
    held-only symbol adds it to the watchlist. Everything else in the YAML
    (other entries, alerts, aliases) is preserved. Returns the entry."""
    p = path or watchlist_path()
    raw = yaml_load(p.read_text()) if p.exists() else {}
    items: list[dict] = raw.get("watchlist") or []
    t = ticker.strip().upper()
    entry = next(
        (i for i in items if str(i.get("ticker", "")).upper() == t), None
    )
    if entry is None:
        entry = {"ticker": t}
        items.append(entry)
    mutate(entry)
    raw["watchlist"] = items
    p.write_text(yaml_dump(raw))
    _persist(p)
    return entry


def toggle_favorite(ticker: str, path: Path | None = None) -> bool:
    """Flip a ticker's favorite flag; returns the new state."""

    def _flip(entry: dict) -> None:
        if entry.get("favorite"):
            entry.pop("favorite", None)
        else:
            entry["favorite"] = True

    return bool(_update_entry(ticker, _flip, path).get("favorite"))


def set_favorite(ticker: str, value: bool, path: Path | None = None) -> None:
    """Set (not flip) a ticker's favorite flag — chat actions need idempotent
    semantics: "add to favorites" on an already-favorited ticker is a no-op."""

    def _set(entry: dict) -> None:
        if value:
            entry["favorite"] = True
        else:
            entry.pop("favorite", None)

    _update_entry(ticker, _set, path)


def set_tags(ticker: str, tags: list[str], path: Path | None = None) -> list[str]:
    """Replace a ticker's tags; an empty list removes the key entirely."""
    clean = _clean_tags(tags)

    def _set(entry: dict) -> None:
        if clean:
            entry["tags"] = clean
        else:
            entry.pop("tags", None)

    _update_entry(ticker, _set, path)
    return clean


def set_alerts(ticker: str, alerts: list[dict], path: Path | None = None) -> None:
    """Replace a ticker's alert rules; an empty list removes the key entirely.

    Each dict is the YAML shape config.Alert accepts: {"type": ..., and one of
    price/pct/level, optional window}. None values are dropped so the YAML
    stays clean.
    """
    clean = [
        {k: v for k, v in a.items() if v is not None and v != ""} for a in alerts
    ]
    clean = [a for a in clean if a.get("type")]

    def _set(entry: dict) -> None:
        if clean:
            entry["alerts"] = clean
        else:
            entry.pop("alerts", None)

    _update_entry(ticker, _set, path)


def add_entry(ticker: str, name: str = "", path: Path | None = None) -> None:
    """Put a ticker on the watchlist (a no-op when it's already there).

    `name` only fills a blank one — a symbol the user already labelled keeps
    its label when the assistant re-adds it."""

    def _set(entry: dict) -> None:
        if name.strip() and not entry.get("name"):
            entry["name"] = name.strip()

    _update_entry(ticker, _set, path)


def remove_entry(ticker: str, path: Path | None = None) -> None:
    """Drop a ticker from the watchlist, alerts and tags with it.

    Unlike the other mutators this never creates the entry: removing a symbol
    that isn't listed is a no-op, not an add-then-delete."""
    p = path or watchlist_path()
    if not p.exists():
        return
    raw = yaml_load(p.read_text())
    items = raw.get("watchlist") or []
    t = ticker.strip().upper()
    kept = [i for i in items if str(i.get("ticker", "")).upper() != t]
    if len(kept) == len(items):
        return
    raw["watchlist"] = kept
    p.write_text(yaml_dump(raw))
    _persist(p)


def set_position(ticker: str, shares: float | None = None,
                 cost: float | None = None, path: Path | None = None) -> None:
    """Set a ticker's held quantity and/or average cost.

    None leaves that field alone, 0 clears it — so "I hold 12 shares" can be
    recorded without inventing a cost basis. This is the watchlist fallback
    the app values when no ledger exists; an imported ledger still wins.
    """

    def _set(entry: dict) -> None:
        for field, value in (("shares", shares), ("cost", cost)):
            if value is None:
                continue
            if value:
                entry[field] = float(value)
            else:
                entry.pop(field, None)

    _update_entry(ticker, _set, path)


def all_tags(path: Path | None = None) -> list[str]:
    """Every tag used on this account's watchlist, sorted case-insensitively."""
    from stocks.config import load_watchlist

    p = path or watchlist_path()
    seen: dict[str, str] = {}
    for h in load_watchlist(p):
        for t in h.tags:
            seen.setdefault(t.lower(), t)
    return sorted(seen.values(), key=str.lower)
