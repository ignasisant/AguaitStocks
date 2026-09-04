"""Per-account data resolution: slugs, owner mapping, prefs, watchlist save."""

import re

import pytest
import yaml

from stocks.config import DATA_DIR, PROJECT_ROOT, load_watchlist
from stocks.web import auth
from stocks.web.auth import (
    DEFAULT_PREFS,
    _legacy_slug,
    all_tags,
    ensure_user_data,
    load_prefs,
    mark_login,
    paths_for,
    save_prefs,
    save_watchlist_entries,
    set_tags,
    slug,
    toggle_favorite,
)


def test_slug_is_filesystem_safe():
    s = slug("Jane.Doe+test@Gmail.com")
    assert s.startswith("jane_doe_test_gmail_com_")
    assert re.fullmatch(r"[a-z0-9_]+", s)
    assert slug("jane.doe+test@gmail.com") == s  # case/whitespace-insensitive
    assert slug("--a@b--").startswith("a_b_")


def test_slug_collision_proof():
    # Same readable base, different addresses -> different data dirs.
    assert slug("a.b@c.com") != slug("a@b.c.com")


def test_paths_for_regular_user(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    assert p.root == tmp_path / slug("jane@example.com")
    assert p.watchlist == p.root / "watchlist.yaml"
    assert p.db == p.root / "portfolio.db"
    assert p.last_import == p.root / "last_import.json"
    assert p.prefs == p.root / "prefs.json"
    assert p.chat == p.root / "chat.json"


def test_paths_for_owner_maps_to_root_files(tmp_path):
    p = paths_for("Me@Example.com", owner_email="me@example.com", users_dir=tmp_path)
    assert p.root == PROJECT_ROOT
    assert p.watchlist == PROJECT_ROOT / "watchlist.yaml"
    assert p.db == DATA_DIR / "portfolio.db"
    # Non-owner emails still land in users_dir even when an owner is set.
    other = paths_for(
        "jane@example.com", owner_email="me@example.com", users_dir=tmp_path
    )
    assert other.root == tmp_path / slug("jane@example.com")


def test_ensure_user_data_seeds_starter_watchlist(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    ensure_user_data(p)
    assert p.root.is_dir()
    holdings = load_watchlist(p.watchlist)
    assert holdings  # starter list is non-empty
    ensure_user_data(p)  # idempotent — must not overwrite
    p.watchlist.write_text("watchlist:\n  - ticker: NVDA\n")
    ensure_user_data(p)
    assert [h.ticker for h in load_watchlist(p.watchlist)] == ["NVDA"]


def test_starter_watchlist_is_a_spread_of_live_tickers_and_no_positions(tmp_path):
    """The seed exists so a first visit has data to rank, group and compare.

    The no-`shares` assertion is the important one: every figure the app draws
    for a seeded row is live market data, so nothing a brand-new account sees
    is ever a holding it does not own. A seeded `shares`/`cost` would turn the
    starter list into a fake portfolio in an app that also files tax reports.
    """
    p = tmp_path / "starter.yaml"
    p.write_text(auth.STARTER_WATCHLIST)
    holdings = load_watchlist(p)

    assert not any(h.is_position for h in holdings)
    assert not any(h.cost for h in holdings)

    tickers = [h.ticker for h in holdings]
    assert len(tickers) == len(set(tickers))
    # The screener's P/E table, the 52-week scan and the sentiment pass all
    # rank across the list; two rows gave them nothing to compare.
    assert len(tickers) >= 8
    assert all(h.name for h in holdings)  # names, or the tables read as codes

    assert any(h.favorite for h in holdings)  # the favorites expander opens
    assert any(h.tags for h in holdings)  # tag groups + earnings filter pills
    # Home's plain "Watchlist" group holds what is neither favorite nor
    # tagged; tagging every row would empty it and hide the ungrouped view.
    assert any(not h.favorite and not h.tags for h in holdings)

    assert any("-" in t for t in tickers)  # a crypto pair — the Crypto gating
    assert any("." in t for t in tickers)  # a non-US listing, for FX


def test_ensure_user_data_reports_only_the_seeding_call(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    assert ensure_user_data(p) is True  # created the account
    assert ensure_user_data(p) is False  # already there


def test_mark_login_dates_a_signup_exactly(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    seeded = ensure_user_data(p)
    assert mark_login(p, seeded=seeded) == "signup"
    prefs = load_prefs(p.prefs)
    assert prefs["first_seen"].startswith(prefs["last_seen"])  # ISO stamp, same day
    assert prefs["first_seen_estimated"] is False

    # Returning: first_seen is never restamped, and the account is not a
    # second signup.
    assert mark_login(p, seeded=False) == "login"
    assert load_prefs(p.prefs)["first_seen"] == prefs["first_seen"]


def test_mark_login_backfills_an_account_it_did_not_create(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    ensure_user_data(p)
    # No first_seen and nothing seeded this run -> the account predates the
    # bookkeeping: dated, flagged inexact, and NOT counted as a signup.
    assert mark_login(p, seeded=False) == "login"
    prefs = load_prefs(p.prefs)
    assert prefs["first_seen"]
    assert prefs["first_seen_estimated"] is True


def test_mark_login_leaves_prefs_untouched_within_the_day(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    mark_login(p, seeded=ensure_user_data(p))
    before = p.prefs.read_text()
    mark_login(p, seeded=False)  # same day: no write, so no bucket PUT
    assert p.prefs.read_text() == before


def test_mark_login_keeps_the_rest_of_prefs(tmp_path):
    p = paths_for("jane@example.com", users_dir=tmp_path)
    ensure_user_data(p)
    save_prefs({**DEFAULT_PREFS, "currency": "USD", "telegram_chat_id": 7}, p.prefs)
    mark_login(p, seeded=False)
    prefs = load_prefs(p.prefs)
    assert prefs["currency"] == "USD"
    assert prefs["telegram_chat_id"] == 7


def test_ensure_user_data_migrates_legacy_dir(tmp_path):
    email = "jane@example.com"
    p = paths_for(email, users_dir=tmp_path)
    legacy = tmp_path / _legacy_slug(email)
    legacy.mkdir(parents=True)
    (legacy / "watchlist.yaml").write_text("watchlist:\n  - ticker: NVDA\n")
    ensure_user_data(p, legacy_root=legacy)
    assert not legacy.exists()  # renamed, not copied
    assert [h.ticker for h in load_watchlist(p.watchlist)] == ["NVDA"]
    # Idempotent: once the new dir exists the legacy path is ignored.
    legacy.mkdir()
    (legacy / "watchlist.yaml").write_text("watchlist:\n  - ticker: EVIL\n")
    ensure_user_data(p, legacy_root=legacy)
    assert [h.ticker for h in load_watchlist(p.watchlist)] == ["NVDA"]


def _signed_in(monkeypatch, tmp_path, email="jane@example.com"):
    """resolve_user() against a fake Streamlit session, rooted at tmp_path."""
    # paths_for() binds users_dir as a default argument, so patching
    # auth.USERS_DIR alone would let the account land in the real data dir.
    monkeypatch.setattr(auth, "USERS_DIR", tmp_path)
    monkeypatch.setattr(
        auth, "paths_for",
        lambda addr, owner=None: paths_for(addr, owner, users_dir=tmp_path),
    )
    monkeypatch.setattr(
        auth, "guest_paths", lambda: paths_for("_guest", users_dir=tmp_path)
    )
    monkeypatch.setattr(auth, "is_logged_in", lambda: bool(email))
    monkeypatch.setattr(auth.st, "user", type("U", (), {"email": email}), raising=False)
    monkeypatch.setattr(auth.st, "secrets", {}, raising=False)
    monkeypatch.setattr(auth.st, "session_state", {}, raising=False)
    return auth.st.session_state


def test_resolve_user_stamps_a_signup_once_per_identity(monkeypatch, tmp_path):
    state = _signed_in(monkeypatch, tmp_path)
    paths = auth.resolve_user()
    assert state["_login_kind"] == "signup"
    assert state["_login_marked"] == "jane@example.com"
    stamp = paths.prefs.read_text()

    # Reruns must not re-read or rewrite prefs: prefs.json is mirrored to the
    # bucket, so an unguarded stamp would be a PUT on every interaction.
    monkeypatch.setattr(
        auth, "mark_login", lambda *a, **k: pytest.fail("restamped on rerun")
    )
    auth.resolve_user()
    assert paths.prefs.read_text() == stamp


def test_resolve_user_clears_the_verdict_on_sign_out(monkeypatch, tmp_path):
    state = _signed_in(monkeypatch, tmp_path)
    auth.resolve_user()
    monkeypatch.setattr(auth, "is_logged_in", lambda: False)
    auth.resolve_user()
    assert state["_login_kind"] == ""  # no auth.* event for a guest run
    assert "_login_marked" not in state  # a later sign-in is evaluated again


def test_resolve_user_does_not_stamp_a_guest(monkeypatch, tmp_path):
    _signed_in(monkeypatch, tmp_path, email="")
    paths = auth.resolve_user()
    assert not paths.prefs.exists()


def test_prefs_roundtrip_and_corrupt_fallback(tmp_path):
    path = tmp_path / "prefs.json"
    assert load_prefs(path) == DEFAULT_PREFS  # absent -> defaults
    save_prefs({"currency": "USD"}, path)
    assert load_prefs(path)["currency"] == "USD"
    path.write_text("{not json")
    assert load_prefs(path) == DEFAULT_PREFS


def test_save_watchlist_entries_preserves_alerts_and_aliases(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "aliases": {"RCF": "TEP.PA"},
                "watchlist": [
                    {"ticker": "NVDA", "alerts": [{"type": "drawdown", "pct": 15}]},
                    {"ticker": "AAPL", "name": "Apple"},
                ],
            }
        )
    )
    save_watchlist_entries(
        [
            {
                "ticker": "nvda",
                "name": "Nvidia",
                "favorite": True,
                "shares": 10,
                "cost": 100.0,
            },
            {"ticker": "MSFT", "name": "Microsoft"},
            {"ticker": ""},  # no ticker -> dropped
            {"ticker": "MSFT"},  # duplicate -> first row wins
        ],
        path,
    )
    raw = yaml.safe_load(path.read_text())
    assert raw["aliases"] == {"RCF": "TEP.PA"}  # untouched
    by_ticker = {i["ticker"]: i for i in raw["watchlist"]}
    assert set(by_ticker) == {"NVDA", "MSFT"}  # AAPL removed, no empties
    assert by_ticker["NVDA"]["alerts"] == [{"type": "drawdown", "pct": 15}]
    assert by_ticker["NVDA"]["favorite"] is True
    assert by_ticker["NVDA"]["shares"] == 10.0
    assert by_ticker["MSFT"].get("name") == "Microsoft"
    assert "alerts" not in by_ticker["MSFT"]

    holdings = load_watchlist(path)  # round-trips through the app loader
    assert {h.ticker for h in holdings} == {"NVDA", "MSFT"}


def test_save_watchlist_entries_tags(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        yaml.safe_dump({"watchlist": [{"ticker": "NVDA", "tags": ["semis"]}]})
    )
    save_watchlist_entries(
        [
            {"ticker": "NVDA"},  # no "tags" key -> existing tags carried over
            {"ticker": "AAPL", "tags": [" Big Tech ", "big tech", ""]},
            {"ticker": "MSFT", "tags": []},  # explicit empty -> no tags key
        ],
        path,
    )
    by_ticker = {h.ticker: h for h in load_watchlist(path)}
    assert by_ticker["NVDA"].tags == ["semis"]
    # Stripped, de-duped case-insensitively (first spelling wins), empties out.
    assert by_ticker["AAPL"].tags == ["Big Tech"]
    assert by_ticker["MSFT"].tags == []


def test_toggle_favorite_creates_entry_and_flips(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "aliases": {"RCF": "TEP.PA"},
                "watchlist": [
                    {"ticker": "NVDA", "alerts": [{"type": "drawdown", "pct": 15}]}
                ],
            }
        )
    )
    # Unlisted symbol: favoriting adds it to the watchlist.
    assert toggle_favorite("pltr", path) is True
    by_ticker = {h.ticker: h for h in load_watchlist(path)}
    assert by_ticker["PLTR"].favorite is True
    assert toggle_favorite("PLTR", path) is False
    assert not load_watchlist(path)[1].favorite
    # Neighbouring data untouched by the round-trips.
    raw = yaml.safe_load(path.read_text())
    assert raw["aliases"] == {"RCF": "TEP.PA"}
    assert raw["watchlist"][0]["alerts"] == [{"type": "drawdown", "pct": 15}]
    # Cleared flag is dropped from the YAML, not written as false.
    assert "favorite" not in raw["watchlist"][1]


def test_set_tags_and_all_tags(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(yaml.safe_dump({"watchlist": [{"ticker": "NVDA"}]}))
    assert set_tags("NVDA", ["semis", " AI ", "ai"], path) == ["semis", "AI"]
    assert set_tags("baba", ["EM"], path) == ["EM"]  # unlisted -> entry created
    assert all_tags(path) == ["AI", "EM", "semis"]  # case-insensitive sort
    assert set_tags("NVDA", [], path) == []  # clearing removes the key
    raw = yaml.safe_load(path.read_text())
    assert "tags" not in raw["watchlist"][0]
    assert all_tags(path) == ["EM"]


# ------------------------------------------------------------ account deletion


def _deletion_sandbox(monkeypatch, tmp_path):
    """auth's world rooted at tmp_path, with a dict standing in for the bucket."""
    from stocks.web import auth

    users = tmp_path / "data" / "users"
    monkeypatch.setattr(auth, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(auth, "USERS_DIR", users)
    monkeypatch.setattr(auth, "GUEST_DIR", users / "_guest")

    bucket: dict[str, bytes] = {}
    monkeypatch.setattr(auth.storage, "enabled", lambda: True)
    monkeypatch.setattr(
        auth.storage,
        "list_keys",
        lambda prefix="": sorted(k for k in bucket if k.startswith(prefix)),
    )
    monkeypatch.setattr(auth.storage, "delete_key", bucket.pop)
    return auth, users, bucket


def test_delete_account_erases_disk_and_bucket(monkeypatch, tmp_path):
    auth, users, bucket = _deletion_sandbox(monkeypatch, tmp_path)
    p = paths_for("jane@example.com", users_dir=users)
    p.root.mkdir(parents=True)
    p.watchlist.write_text("watchlist: []")
    p.prefs.write_text("{}")
    me = f"data/users/{slug('jane@example.com')}"
    other = f"data/users/{slug('bob@example.com')}"
    bucket.update({
        f"{me}/watchlist.yaml": b"x",
        f"{me}/portfolio.db": b"x",
        f"{other}/watchlist.yaml": b"bob",
        "watchlist.yaml": b"root",
    })

    auth.delete_account(p)

    assert not p.root.exists()
    # Only this account's keys are gone; the neighbour and the root survive.
    assert sorted(bucket) == [f"{other}/watchlist.yaml", "watchlist.yaml"]


def test_delete_account_refuses_owner_and_guest(monkeypatch, tmp_path):
    import pytest

    auth, users, _ = _deletion_sandbox(monkeypatch, tmp_path)
    owner = paths_for("me@x.com", owner_email="me@x.com", users_dir=users)
    with pytest.raises(ValueError):
        auth.delete_account(owner)
    guest = auth.guest_paths()
    with pytest.raises(ValueError):
        auth.delete_account(guest)
    # Nor anything outside the users dir, whatever it is named.
    stray = type(guest)(**{**guest.__dict__, "root": tmp_path / "elsewhere"})
    with pytest.raises(ValueError):
        auth.delete_account(stray)


def test_a_checkout_with_no_secrets_reads_as_signed_out(monkeypatch):
    """Membership on st.secrets *raises* when there is no secrets file at all
    — a fresh clone, a CI checkout. is_logged_in is called by every page, so
    an unguarded read takes the whole app down instead of degrading to the one
    answer that is true without an IdP configured: nobody is signed in."""
    import streamlit as st
    from streamlit.errors import StreamlitSecretNotFoundError

    class NoSecrets:
        def __contains__(self, key):
            raise StreamlitSecretNotFoundError("No secrets found.")

    monkeypatch.setattr(st, "secrets", NoSecrets())
    assert auth.is_logged_in() is False
