"""Alert dedupe state machine (stocks.notify.state)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from stocks.config import Alert
from stocks.notify import state as st_mod

NOW = datetime(2026, 8, 14, 14, 0, 0)
MISSING = Path("/nonexistent/alerts_state.json")


# ------------------------------------------------------------- fingerprint


def test_fingerprint_covers_threshold_kinds():
    assert st_mod.fingerprint("nvda", Alert("above", price=190.0)) == (
        "NVDA|above|price=190|w="
    )
    assert st_mod.fingerprint("TTD", Alert("rsi_below", level=30.0, window=14)) == (
        "TTD|rsi_below|level=30|w=14"
    )
    assert st_mod.fingerprint("SHOP", Alert("pct_move", pct=5.0)) == (
        "SHOP|pct_move|pct=5|w="
    )
    assert st_mod.fingerprint("AAPL", Alert("high_52w")) == "AAPL|high_52w||w="


def test_fingerprint_stable_for_same_rule():
    a, b = Alert("below", price=95.5), Alert("below", price=95.5)
    assert st_mod.fingerprint("MELI", a) == st_mod.fingerprint("MELI", b)


# ------------------------------------------------------------- should_send


def test_rising_edge_sends_once_then_suppresses():
    state = st_mod.load_state(MISSING)
    fp = "NVDA|above|price=190|w="
    assert st_mod.should_send(state, fp, fired=True, now=NOW) is True
    an_hour = NOW + timedelta(hours=1)
    assert st_mod.should_send(state, fp, fired=True, now=an_hour) is False


def test_cooldown_expiry_resends():
    state = st_mod.load_state(MISSING)
    fp = "X|above|price=1|w="
    st_mod.should_send(state, fp, fired=True, now=NOW)
    later = NOW + timedelta(hours=25)
    assert st_mod.should_send(state, fp, fired=True, now=later) is True


def test_clear_rearms_edge():
    state = st_mod.load_state(MISSING)
    fp = "X|above|price=1|w="
    st_mod.should_send(state, fp, fired=True, now=NOW)
    an_hour = NOW + timedelta(hours=1)
    assert st_mod.should_send(state, fp, fired=False, now=an_hour) is False
    # condition fires again right after clearing: new edge, still in cooldown window
    assert st_mod.should_send(state, fp, fired=True, now=NOW + timedelta(hours=2)) is True


def test_not_fired_never_sends():
    state = st_mod.load_state(MISSING)
    assert st_mod.should_send(state, "A|above|price=1|w=", fired=False, now=NOW) is False


# ------------------------------------------------------------ load / save


def test_load_missing_and_corrupt_tolerated(tmp_path):
    assert st_mod.load_state(tmp_path / "none.json")["alerts"] == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert st_mod.load_state(bad)["alerts"] == {}
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text('["list"]')
    assert st_mod.load_state(wrong_shape)["alerts"] == {}


def test_save_prunes_stale_fingerprints(tmp_path, monkeypatch):
    persisted = []
    monkeypatch.setattr(st_mod.storage, "persist", persisted.append)
    path = tmp_path / "alerts_state.json"
    state = st_mod.load_state(path)
    st_mod.should_send(state, "KEEP|above|price=1|w=", fired=True, now=NOW)
    st_mod.should_send(state, "GONE|below|price=2|w=", fired=True, now=NOW)
    st_mod.save_state(state, path, active_fingerprints={"KEEP|above|price=1|w="})
    reloaded = st_mod.load_state(path)
    assert list(reloaded["alerts"]) == ["KEEP|above|price=1|w="]
    assert persisted == [path]


def test_blocked_flag_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(st_mod.storage, "persist", lambda p: None)
    path = tmp_path / "alerts_state.json"
    state = st_mod.load_state(path)
    assert not st_mod.is_blocked(state)
    st_mod.mark_blocked(state, NOW)
    st_mod.save_state(state, path)
    assert st_mod.is_blocked(st_mod.load_state(path))
