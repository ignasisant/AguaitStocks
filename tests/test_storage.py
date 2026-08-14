"""S3-compatible persistence layer (stocks.storage) and its write hooks."""

from __future__ import annotations

import io

import pytest

from stocks import storage
from stocks.portfolio import last_import, ledger


class NoSuchKey(Exception):
    pass


class FakeExceptions:
    NoSuchKey = NoSuchKey


class FakeClient:
    """Minimal in-memory stand-in for boto3's S3 client."""

    exceptions = FakeExceptions

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, Bucket, Key, Body):
        self.calls.append(("put", Key))
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete", Key))
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                client.calls.append(("list", Prefix))
                keys = sorted(k for k in client.objects if k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys]} if keys else {}

        return Paginator()


@pytest.fixture
def bucket(monkeypatch, tmp_path):
    """Enable storage against a fake client, rooted at tmp_path."""
    client = FakeClient()
    monkeypatch.setattr(storage, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        storage,
        "_cached",
        {
            "config": {
                "bucket": "b",
                "access_key_id": "k",
                "secret_access_key": "s",
                "endpoint_url": "",
                "region": "auto",
            },
            "client": client,
        },
    )
    monkeypatch.setattr(storage, "_restored", set())
    return client


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(storage, "_cached", {"config": None})


# ------------------------------------------------------------------ storage


def test_disabled_is_noop(disabled, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    assert not storage.enabled()
    storage.persist(f)  # must not raise or need a client
    assert storage.restore(f) is False


def test_persist_uploads_repo_relative_key(bucket, tmp_path):
    f = tmp_path / "data" / "users" / "jane" / "prefs.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"currency": "USD"}')
    storage.persist(f)
    assert bucket.objects["data/users/jane/prefs.json"] == f.read_bytes()


def test_persist_missing_file_deletes_remote(bucket, tmp_path):
    key = "data/last_import.json"
    bucket.objects[key] = b"{}"
    storage.persist(tmp_path / "data" / "last_import.json")
    assert key not in bucket.objects


def test_persist_outside_repo_root_skips(bucket, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "f.txt"
    other.write_text("x")
    storage.persist(other)
    assert bucket.calls == []


def test_restore_overwrites_local(bucket, tmp_path):
    bucket.objects["watchlist.yaml"] = b"watchlist: []\n"
    f = tmp_path / "watchlist.yaml"
    f.write_text("stale local")
    assert storage.restore(f) is True
    assert f.read_bytes() == b"watchlist: []\n"


def test_restore_missing_remote(bucket, tmp_path):
    f = tmp_path / "nope.json"
    assert storage.restore(f) is False
    assert not f.exists()


def test_restore_once_runs_once_per_group(bucket, tmp_path):
    bucket.objects["u/a.txt"] = b"a"
    group = tmp_path / "u"
    files = (group / "a.txt",)
    storage.restore_once(group, files)
    storage.restore_once(group, files)
    assert bucket.calls.count(("get", "u/a.txt")) == 1


def test_restore_once_retries_after_failure(bucket, tmp_path, monkeypatch):
    group = tmp_path / "u"
    bucket.objects["u/a.txt"] = b"a"
    real, state = storage.restore, {"fail": True}

    def flaky(path):
        if state["fail"]:
            raise OSError("net down")
        return real(path)

    monkeypatch.setattr(storage, "restore", flaky)
    with pytest.raises(OSError):
        storage.restore_once(group, (group / "a.txt",))
    state["fail"] = False  # group must not be cached as restored by the failed try
    storage.restore_once(group, (group / "a.txt",))
    assert (group / "a.txt").read_bytes() == b"a"


def test_restore_dir_pulls_flat_pool_once(bucket, tmp_path):
    d = tmp_path / "src/stocks/web/static/logos"
    bucket.objects["src/stocks/web/static/logos/AAPL.png"] = b"a"
    bucket.objects["src/stocks/web/static/logos/brand-groq.png"] = b"g"
    bucket.objects["src/stocks/web/static/logos/nested/x.png"] = b"n"  # skipped
    storage.restore_dir(d)
    assert (d / "AAPL.png").read_bytes() == b"a"
    assert (d / "brand-groq.png").read_bytes() == b"g"
    assert not (d / "nested").exists()
    calls_after_first = list(bucket.calls)
    storage.restore_dir(d)  # second touch: no bucket round-trip
    assert bucket.calls == calls_after_first


def test_restore_dir_local_wins(bucket, tmp_path):
    d = tmp_path / "logos"
    d.mkdir()
    (d / "AAPL.png").write_bytes(b"fresh")
    bucket.objects["logos/AAPL.png"] = b"stale"
    storage.restore_dir(d)
    assert (d / "AAPL.png").read_bytes() == b"fresh"


def test_restore_dir_disabled_is_noop(disabled, tmp_path):
    storage.restore_dir(tmp_path / "logos")
    assert not (tmp_path / "logos").exists()


def test_mirror_logo_persists_and_restores(bucket, tmp_path, monkeypatch):
    """One successful mirror survives an 'ephemeral reboot' via the bucket."""
    import stocks.data.logo as logo_mod

    d = tmp_path / "src/stocks/web/static/logos"
    monkeypatch.setattr(logo_mod, "logo_url", lambda t: "https://x/AAPL.png")
    monkeypatch.setattr(
        logo_mod, "get_bytes_and_type", lambda url, **kw: (b"png", "image/png")
    )
    assert logo_mod.mirror_logo("AAPL", d) == "AAPL.png"
    assert bucket.objects["src/stocks/web/static/logos/AAPL.png"] == b"png"

    # "Reboot": static dir wiped, restore memo reset, source unresolvable —
    # the bucket copy alone brings the logo back.
    for f in d.iterdir():
        f.unlink()
    monkeypatch.setattr(storage, "_restored", set())
    monkeypatch.setattr(logo_mod, "logo_url", lambda t: None)
    assert logo_mod.mirror_logo("AAPL", d) == "AAPL.png"
    assert (d / "AAPL.png").read_bytes() == b"png"


# -------------------------------------------------------------- write hooks


def test_ledger_writes_persist(bucket, tmp_path):
    db = tmp_path / "data" / "users" / "jane" / "portfolio.db"
    db.parent.mkdir(parents=True)
    key = "data/users/jane/portfolio.db"

    tx_id = ledger.add(ledger.Transaction("2026-01-02", "AAPL", "buy", 1, 100), db)
    assert key in bucket.objects
    snapshot = bucket.objects[key]

    ledger.delete(tx_id, db)
    assert bucket.objects[key] != snapshot  # re-uploaded after the delete
    assert ledger.all_transactions(db) == []


def test_last_import_save_and_forget_persist(bucket, tmp_path):
    p = tmp_path / "data" / "users" / "jane" / "last_import.json"
    p.parent.mkdir(parents=True)
    key = "data/users/jane/last_import.json"

    last_import.save(last_import.ImportRecord("s.csv", "2026-01-02T00:00:00Z", [1]), p)
    assert key in bucket.objects
    last_import.forget(p)
    assert key not in bucket.objects


def test_ensure_user_data_restores_before_seeding(bucket, tmp_path):
    from stocks.web import auth

    paths = auth.paths_for("jane@example.com", users_dir=tmp_path / "data" / "users")
    key = f"data/users/{auth.slug('jane@example.com')}/watchlist.yaml"
    bucket.objects[key] = b"watchlist:\n- ticker: NVDA\n"

    auth.ensure_user_data(paths)
    assert "NVDA" in paths.watchlist.read_text()  # restored copy, not the starter


def test_ensure_user_data_seeds_and_persists_new_account(bucket, tmp_path):
    from stocks.web import auth

    paths = auth.paths_for("new@example.com", users_dir=tmp_path / "data" / "users")
    auth.ensure_user_data(paths)
    assert "AAPL" in paths.watchlist.read_text()
    key = f"data/users/{auth.slug('new@example.com')}/watchlist.yaml"
    assert key in bucket.objects


def test_ensure_user_data_migrates_legacy_bucket_objects(bucket, tmp_path):
    # An account whose data predates the digest-suffixed slug and survives
    # only in the bucket (ephemeral host): objects are restored, the dir is
    # renamed, and every object is re-keyed to the new slug.
    from stocks.web import auth

    users = tmp_path / "data" / "users"
    email = "jane@example.com"
    paths = auth.paths_for(email, users_dir=users)
    legacy = users / auth._legacy_slug(email)
    legacy_key = f"data/users/{auth._legacy_slug(email)}/portfolio.db"
    bucket.objects[legacy_key] = b"ledgerbytes"

    auth.ensure_user_data(paths, legacy_root=legacy)
    assert paths.db.read_bytes() == b"ledgerbytes"
    assert not legacy.exists()
    assert legacy_key not in bucket.objects  # old key deleted
    new_key = f"data/users/{auth.slug(email)}/portfolio.db"
    assert bucket.objects[new_key] == b"ledgerbytes"


def test_list_keys_filters_by_prefix(bucket):
    bucket.objects["data/users/jane_ab12cd34/prefs.json"] = b"{}"
    bucket.objects["data/users/bob_ef56ab78/prefs.json"] = b"{}"
    bucket.objects["data/prefs.json"] = b"{}"
    keys = storage.list_keys("data/users/")
    assert keys == [
        "data/users/bob_ef56ab78/prefs.json",
        "data/users/jane_ab12cd34/prefs.json",
    ]


def test_list_keys_disabled_returns_empty(disabled):
    assert storage.list_keys("data/") == []
