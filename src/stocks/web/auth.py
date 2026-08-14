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
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
import yaml

from stocks import storage
from stocks.config import DATA_DIR, PROJECT_ROOT, WATCHLIST_FILE
from stocks.web.i18n import t as tr

USERS_DIR = DATA_DIR / "users"
GUEST_DIR = USERS_DIR / "_guest"

CURRENCIES = ("EUR", "USD", "GBP", "CHF")
CURRENCY_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF "}
RECENT_SEARCHES_MAX = 5
DEFAULT_PREFS = {  # language None = auto (browser); picker_sort_by None = default order
    "currency": "EUR",
    "language": None,
    "picker_sort_by": None,
    "recent_searches": [],  # tickers clicked from the top-bar search, newest first
    # Telegram notifications: chat_id is set by the Profile linking flow; the
    # toggles only take effect once it is. The cron (notify/fanout.py) reads
    # these headless straight from prefs.json.
    "telegram_chat_id": None,
    "notify_digest": True,
    "notify_alerts": True,
}

STARTER_WATCHLIST = """\
# Personal watchlist — managed from the Profile page.
#
# Per-entry fields:
#   favorite: true   -> pinned to top of the dashboard + quick-access buttons
#   shares: 12       -> makes it a real position (portfolio weights by value)
#   cost: 145.30     -> average buy price/share, for unrealised P/L
watchlist:
  - ticker: AAPL
    name: Apple
  - ticker: MSFT
    name: Microsoft
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
        )
    d = users_dir / slug(email)
    return UserPaths(
        root=d,
        watchlist=d / "watchlist.yaml",
        db=d / "portfolio.db",
        last_import=d / "last_import.json",
        prefs=d / "prefs.json",
        chat=d / "chat.json",
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
    )


_USER_FILES = (
    "watchlist.yaml", "portfolio.db", "last_import.json", "prefs.json", "chat.json",
)


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
    for name in _USER_FILES:
        storage.persist(paths.root / name)  # push under the new key
        storage.persist(legacy_root / name)  # gone locally -> delete old key


def ensure_user_data(paths: UserPaths, legacy_root: Path | None = None) -> None:
    """First login: create the account's folder and seed a starter watchlist.

    With [storage] configured, the account's files are pulled from the bucket
    first (once per process), so an ephemeral redeploy starts from the
    persisted copies instead of re-seeding. `legacy_root` is the account's
    pre-digest-slug dir; when it still exists (locally or in the bucket) it
    is renamed and re-keyed before anything is restored or seeded.
    """
    if legacy_root is not None:
        _migrate_legacy(paths, legacy_root)
    paths.root.mkdir(parents=True, exist_ok=True)
    storage.restore_once(
        paths.root,
        (paths.watchlist, paths.db, paths.last_import, paths.prefs, paths.chat),
    )
    if not paths.watchlist.exists():
        paths.watchlist.write_text(STARTER_WATCHLIST)
        storage.persist(paths.watchlist)


def is_logged_in() -> bool:
    """True when an authenticated identity with a verified email is present.

    All personal data is keyed to the email claim, so an unverified address
    must never resolve to a data dir — an IdP that skips verification would
    otherwise let anyone claim someone else's account. Google always sends
    email_verified=true for its accounts.
    """
    return (
        "auth" in st.secrets
        and st.user.is_logged_in
        and bool(str(getattr(st.user, "email", "") or "").strip())
        and bool(getattr(st.user, "email_verified", False))
    )


def resolve_user() -> UserPaths:
    """Resolve the session's data paths without gating; call before the nav.

    Logged-in accounts get their own dir (owner → repo-root files); anonymous
    visitors get the shared guest dir so the public pages can render. Stores
    the paths in session state for the page modules.
    """
    legacy = None
    if is_logged_in():
        email = str(st.user.email).strip()
        owner = str(st.secrets.get("app", {}).get("owner_email", "")).strip() or None
        paths = paths_for(email, owner)
        if paths.root != PROJECT_ROOT:  # owner uses repo-root files, no slug
            legacy = USERS_DIR / _legacy_slug(email)
    else:
        paths = guest_paths()
    ensure_user_data(paths, legacy_root=legacy)
    st.session_state["user_paths"] = paths
    return paths


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
    border-radius: 4px;
    background-color: #fff;
    background-image: url("{_GOOGLE_G_SVG}");
    background-repeat: no-repeat;
    background-position: center;
    background-size: 0.85rem;
}}
</style>
"""


def _login_screen() -> None:
    st.html(_LOGIN_CSS)
    st.space("xlarge")
    with st.container(horizontal_alignment="center"):
        with st.container(border=True, width=420, horizontal_alignment="center"):
            st.space("xsmall")
            st.image(
                str(Path(__file__).parent / "assets" / "aguait-logo.svg"),
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
                on_click=st.login,
                width="stretch",
            )
            st.caption(
                tr("auth.browsing_public"),
                text_alignment="center",
            )
            st.space("xsmall")


def maybe_prompt_login() -> None:
    """First load only: pop a dismissible modal inviting Google sign-in.

    Fires once per session and only for anonymous visitors while [auth] is
    configured. Any exit — the skip button, the X, ESC or a click outside —
    leaves them on the public guest view; the sidebar sign-in entry stays for
    later. The seen-flag is set before the dialog opens so it never re-pops on
    the fragment reruns the modal itself triggers.
    """
    if "auth" not in st.secrets or is_logged_in():
        return
    if st.session_state.get("_login_prompt_seen"):
        return
    st.session_state["_login_prompt_seen"] = True
    # Build the dialog at call time (not via @st.dialog) so the title resolves
    # in the run's active language rather than freezing at import.
    st.dialog(tr("auth.welcome_title"))(_login_dialog_body)()


def _login_dialog_body() -> None:
    st.html(_LOGIN_CSS)
    st.image(
        str(Path(__file__).parent / "assets" / "aguait-logo.svg"),
        width=180,
    )
    st.markdown(tr("auth.signin_prompt"))
    # Distinct key: on a first anonymous visit to a require_login page, this
    # modal and _login_screen render in the same run — a shared key crashes
    # with StreamlitDuplicateElementKey. The Google-G CSS matches both keys.
    st.button(
        tr("common.sign_in_google"),
        type="primary",
        key="google_signin_modal",
        on_click=st.login,
        width="stretch",
    )
    if st.button(tr("auth.continue_guest"), key="login_skip", width="stretch"):
        st.rerun()  # close the modal; the seen-flag keeps it from re-opening
    st.caption(tr("auth.browsing_public"))


# ---------------------------------------------------------------- accessors
# Set by resolve_user() in app.py (guest paths when anonymous); pages run
# after it via st.navigation.


def user_paths() -> UserPaths:
    return st.session_state["user_paths"]


def watchlist_path() -> Path:
    return user_paths().watchlist


def db_path() -> Path:
    return user_paths().db


def last_import_path() -> Path:
    return user_paths().last_import


def chat_path() -> Path:
    return user_paths().chat


# ------------------------------------------------------------- preferences


def load_prefs(path: Path | None = None) -> dict:
    p = path or user_paths().prefs
    try:
        return {**DEFAULT_PREFS, **json.loads(p.read_text())}
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_PREFS)


def save_prefs(prefs: dict, path: Path | None = None) -> None:
    p = path or user_paths().prefs
    p.write_text(json.dumps(prefs, indent=2))
    storage.persist(p)


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
PROFILE_CONSTRAINTS = ("spain_tax", "eur", "no_leverage", "esg")

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


# ---------------------------------------------------------------- chat thread
# The assistant conversation is persisted per account (like prefs) so it
# survives a reload, a new session, or an ephemeral redeploy — and is mirrored
# to the bucket. One thread per account, stored as a list of {role, content}.


def load_chat(path: Path | None = None) -> list[dict]:
    p = path or user_paths().chat
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def save_chat(history: list[dict], path: Path | None = None) -> None:
    p = path or user_paths().chat
    p.write_text(json.dumps(history, indent=2))
    storage.persist(p)


def display_currency() -> str:
    ccy = str(load_prefs().get("currency", "EUR")).upper()
    return ccy if ccy in CURRENCIES else "EUR"


# ---------------------------------------------------------------- watchlist


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
    raw = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
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
    p.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    storage.persist(p)


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
    raw = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
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
    p.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    storage.persist(p)
    return entry


def toggle_favorite(ticker: str, path: Path | None = None) -> bool:
    """Flip a ticker's favorite flag; returns the new state."""

    def _flip(entry: dict) -> None:
        if entry.get("favorite"):
            entry.pop("favorite", None)
        else:
            entry["favorite"] = True

    return bool(_update_entry(ticker, _flip, path).get("favorite"))


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


def all_tags(path: Path | None = None) -> list[str]:
    """Every tag used on this account's watchlist, sorted case-insensitively."""
    from stocks.config import load_watchlist

    p = path or watchlist_path()
    seen: dict[str, str] = {}
    for h in load_watchlist(p):
        for t in h.tags:
            seen.setdefault(t.lower(), t)
    return sorted(seen.values(), key=str.lower)
