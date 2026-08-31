"""Per-account data resolution: slugs, owner mapping, prefs, watchlist save."""

import re

import yaml

from stocks.config import DATA_DIR, PROJECT_ROOT, load_watchlist
from stocks.web.auth import (
    DEFAULT_PREFS,
    _legacy_slug,
    all_tags,
    ensure_user_data,
    load_prefs,
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
