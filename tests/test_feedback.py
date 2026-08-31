"""The feedback store (stocks.web.feedback) — submission files and read-back.

The sidebar widget itself is Streamlit chrome; what must not break silently is
the storage contract: a submission survives as a JSON file (mirrored to the
bucket) that `stocks feedback` can read back, including bucket-only copies a
fresh checkout has never seen.
"""

from __future__ import annotations

import json

import pytest

from stocks import storage
from stocks.web import feedback


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(storage, "_cached", {"config": None})  # bucket off
    monkeypatch.setattr(feedback, "active_language", lambda: "en")
    return tmp_path / "feedback"


def test_submit_writes_one_json_file_per_submission(sandbox):
    p = feedback.submit("The chart is upside down", "bug", page="Cartera")
    data = json.loads(p.read_text())
    assert data["kind"] == "bug"
    assert data["text"] == "The chart is upside down"
    assert data["page"] == "Cartera"
    assert data["user"] == "guest"  # no session -> anonymous
    assert p.parent == sandbox


def test_submit_clamps_kind_and_length(sandbox):
    p = feedback.submit("x" * (feedback.MAX_CHARS + 500), "exploit")
    data = json.loads(p.read_text())
    assert data["kind"] == "other"
    assert len(data["text"]) == feedback.MAX_CHARS


def test_stored_merges_local_and_bucket_and_sorts(sandbox, monkeypatch):
    feedback.submit("local one", "idea")
    bucket = {
        "data/feedback/2020-01-01T00-00-00Z-aaaaaa.json": json.dumps(
            {"ts": "2020-01-01T00-00-00Z", "kind": "bug", "text": "old, bucket-only"}
        ).encode(),
        "data/feedback/not-json.txt": b"ignored",
    }
    monkeypatch.setattr(
        storage, "list_keys",
        lambda prefix="": sorted(k for k in bucket if k.startswith(prefix)),
    )
    monkeypatch.setattr(storage, "read_key", bucket.get)
    items = feedback.stored()
    assert len(items) == 2
    assert items[0]["text"] == "old, bucket-only"  # oldest first
    assert items[1]["text"] == "local one"


def test_stored_is_empty_when_nothing_anywhere(sandbox):
    assert feedback.stored() == []
