"""Login gate and per-user data resolution for the web app.

Authentication is Streamlit-native OIDC (st.login / st.user) configured in
.streamlit/secrets.toml under [auth] — see .streamlit/secrets.example.toml.

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

import json
import re
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
import yaml

from stocks.config import DATA_DIR, PROJECT_ROOT, WATCHLIST_FILE

USERS_DIR = DATA_DIR / "users"
GUEST_DIR = USERS_DIR / "_guest"

CURRENCIES = ("EUR", "USD", "GBP", "CHF")
CURRENCY_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF "}
DEFAULT_PREFS = {"currency": "EUR"}

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


def slug(email: str) -> str:
    """Filesystem-safe directory name for an account email."""
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
        )
    d = users_dir / slug(email)
    return UserPaths(
        root=d,
        watchlist=d / "watchlist.yaml",
        db=d / "portfolio.db",
        last_import=d / "last_import.json",
        prefs=d / "prefs.json",
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
    )


def ensure_user_data(paths: UserPaths) -> None:
    """First login: create the account's folder and seed a starter watchlist."""
    paths.root.mkdir(parents=True, exist_ok=True)
    if not paths.watchlist.exists():
        paths.watchlist.write_text(STARTER_WATCHLIST)


def is_logged_in() -> bool:
    """True when an authenticated identity with an email is present."""
    return (
        "auth" in st.secrets
        and st.user.is_logged_in
        and bool(str(getattr(st.user, "email", "") or "").strip())
    )


def resolve_user() -> UserPaths:
    """Resolve the session's data paths without gating; call before the nav.

    Logged-in accounts get their own dir (owner → repo-root files); anonymous
    visitors get the shared guest dir so the public pages can render. Stores
    the paths in session state for the page modules.
    """
    if is_logged_in():
        email = str(st.user.email).strip()
        owner = str(st.secrets.get("app", {}).get("owner_email", "")).strip() or None
        paths = paths_for(email, owner)
    else:
        paths = guest_paths()
    ensure_user_data(paths)
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
        st.error("Authentication is not configured.", icon=":material/lock:")
        st.markdown(
            "Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml` "
            "and fill in the Google OAuth client — see the *Login* section of the "
            "README. Portfolio features stay locked until then."
        )
        st.stop()

    if not st.user.is_logged_in:
        _login_screen()
        st.stop()

    email = str(getattr(st.user, "email", "") or "").strip()
    if not email:
        st.error(
            "Your identity provider returned no email address, and the app keys "
            "all personal data to it. Sign in with an account that shares one."
        )
        st.button("Log out", icon=":material/logout:", on_click=st.logout)
        st.stop()

    return resolve_user()


# Google's "G" mark isn't in Material Symbols, so it's drawn onto the sign-in
# button as a CSS ::before tile (white rounded square, brand-guideline style).
_GOOGLE_G_SVG = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'>"
    "<path fill='%234285F4' d='M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94"
    "c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z'/>"
    "<path fill='%2334A853' d='M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6"
    "c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19"
    "C6.51 42.62 14.62 48 24 48z'/>"
    "<path fill='%23FBBC05' d='M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59"
    "s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78"
    "l7.97-6.19z'/>"
    "<path fill='%23EA4335' d='M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85"
    "C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19"
    "C12.43 13.72 17.74 9.5 24 9.5z'/>"
    "</svg>"
)

_LOGIN_CSS = f"""\
<style>
.st-key-google_signin button::before {{
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
                str(Path(__file__).parent / "assets" / "atalaya-logo.svg"),
                width=200,
            )
            st.caption(
                "Watchlist, portfolio and valuation dashboard.",
                text_alignment="center",
            )
            st.space("xsmall")
            st.markdown(
                "Sign in to use this page. Your watchlist, portfolio ledger "
                "and preferences are private to your account.",
                text_alignment="center",
            )
            st.button(
                "Sign in with Google",
                type="primary",
                key="google_signin",
                on_click=st.login,
                width="stretch",
            )
            st.caption(
                ":material/public: Browsing the market pages needs no login.",
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


def last_import_path() -> Path:
    return user_paths().last_import


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


def all_tags(path: Path | None = None) -> list[str]:
    """Every tag used on this account's watchlist, sorted case-insensitively."""
    from stocks.config import load_watchlist

    p = path or watchlist_path()
    seen: dict[str, str] = {}
    for h in load_watchlist(p):
        for t in h.tags:
            seen.setdefault(t.lower(), t)
    return sorted(seen.values(), key=str.lower)
