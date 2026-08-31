"""Bucket snapshots and their restore path (stocks.backup).

Everything runs against an in-memory stand-in for the S3 client — the point
is the key arithmetic (what gets copied where, what prune removes, what a
scoped restore touches), not boto3.
"""

from __future__ import annotations

import io

import pytest

from stocks import backup, storage


class NoSuchKey(Exception):
    pass


class FakeExceptions:
    NoSuchKey = NoSuchKey


class FakeClient:
    exceptions = FakeExceptions

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def copy_object(self, Bucket, Key, CopySource):
        src = CopySource["Key"]
        if src not in self.objects:
            raise NoSuchKey(src)
        self.objects[Key] = self.objects[src]

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for k in client.objects if k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys]} if keys else {}

        return Paginator()


@pytest.fixture
def bucket(monkeypatch):
    client = FakeClient()
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
    client.objects = {
        "watchlist.yaml": b"root",
        "data/users/jane_x/portfolio.db": b"jane-db",
        "data/users/jane_x/prefs.json": b"jane-prefs",
        "data/users/bob_y/portfolio.db": b"bob-db",
    }
    return client


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(storage, "_cached", {"config": None})


def test_run_snapshots_every_live_key(bucket):
    stamp, count = backup.run()
    assert count == 4
    assert bucket.objects[f"backups/{stamp}/watchlist.yaml"] == b"root"
    assert (
        bucket.objects[f"backups/{stamp}/data/users/jane_x/portfolio.db"] == b"jane-db"
    )
    assert backup.snapshots() == [stamp]


def test_a_second_run_does_not_snapshot_the_snapshots(bucket):
    s1, c1 = backup.run()
    s2, c2 = backup.run()
    assert c1 == c2 == 4  # backups/ excluded from the live tree
    assert f"backups/{s2}/backups/{s1}/watchlist.yaml" not in bucket.objects


def test_prune_keeps_the_newest_and_drops_whole_snapshots(bucket):
    for i in range(3):
        bucket.objects[f"backups/2026-0{i + 1}-01T00-00-00Z/watchlist.yaml"] = b"old"
    removed = backup.prune(keep=2)
    assert removed == ["2026-01-01T00-00-00Z"]
    assert "backups/2026-01-01T00-00-00Z/watchlist.yaml" not in bucket.objects
    assert "backups/2026-03-01T00-00-00Z/watchlist.yaml" in bucket.objects


def test_prune_refuses_to_delete_everything(bucket):
    with pytest.raises(ValueError):
        backup.prune(keep=0)


def test_restore_copies_a_snapshot_back_over_the_live_keys(bucket):
    stamp, _ = backup.run()
    bucket.objects["data/users/jane_x/portfolio.db"] = b"corrupted"
    restored = backup.restore(stamp)
    assert restored == 4
    assert bucket.objects["data/users/jane_x/portfolio.db"] == b"jane-db"


def test_restore_can_be_scoped_to_one_account(bucket):
    stamp, _ = backup.run()
    bucket.objects["data/users/jane_x/portfolio.db"] = b"corrupted"
    bucket.objects["data/users/bob_y/portfolio.db"] = b"bob-new"
    restored = backup.restore(stamp, only="data/users/jane_x/")
    assert restored == 2  # jane's two files, nothing else
    assert bucket.objects["data/users/jane_x/portfolio.db"] == b"jane-db"
    assert bucket.objects["data/users/bob_y/portfolio.db"] == b"bob-new"


def test_restore_never_deletes_live_keys_the_snapshot_lacks(bucket):
    stamp, _ = backup.run()
    bucket.objects["data/users/new_z/prefs.json"] = b"newer-account"
    backup.restore(stamp)
    assert bucket.objects["data/users/new_z/prefs.json"] == b"newer-account"


def test_restore_of_an_unknown_stamp_refuses(bucket):
    with pytest.raises(ValueError, match="no snapshot"):
        backup.restore("2020-01-01T00-00-00Z")


def test_everything_refuses_without_storage(disabled):
    for call in (backup.run, backup.prune, lambda: backup.restore("x")):
        with pytest.raises(RuntimeError, match="not configured"):
            call()
