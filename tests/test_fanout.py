"""Multi-user notification fan-out (stocks.notify.fanout)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from stocks.notify import fanout, narrative
from stocks.notify import state as state_mod


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Silence the optional LLM lines by default.

    Not politeness: a developer with [free_llm] secrets configured otherwise
    runs the whole fan-out against real backends, which costs minutes per run
    and spends the shared daily pot. Tests that want a line opt back in.
    """
    monkeypatch.setattr(narrative, "alerts_line", lambda *a, **kw: None)
    monkeypatch.setattr(narrative, "highlight", lambda *a, **kw: None)


def _user(tmp_path, slug, prefs, watchlist=None):
    root = tmp_path / "users" / slug
    root.mkdir(parents=True)
    (root / "prefs.json").write_text(json.dumps(prefs))
    if watchlist:
        (root / "watchlist.yaml").write_text(watchlist)
    return root


@pytest.fixture
def local(monkeypatch, tmp_path):
    """Local-filesystem discovery (storage disabled), rooted at tmp_path."""
    from stocks import storage

    monkeypatch.setattr(storage, "_cached", {"config": None})
    monkeypatch.setattr(fanout, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(fanout, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fanout, "WATCHLIST_FILE", tmp_path / "watchlist.yaml")
    return tmp_path


LINKED = {"telegram_chat_id": 111, "language": "es"}


def test_iter_filters_on_chat_id_and_toggle(local):
    _user(local, "jane_ab12cd34", LINKED)
    _user(local, "bob_ef56ab78", {"telegram_chat_id": 222, "notify_digest": False})
    _user(local, "carol_11223344", {"currency": "EUR"})  # never linked
    _user(local, "_guest", LINKED)  # guests never notified

    digest_users = fanout.iter_notify_users("digest")
    assert [u.label for u in digest_users] == ["jane_ab12cd34"]
    assert digest_users[0].chat_id == 111
    assert digest_users[0].lang == "es"

    alert_users = fanout.iter_notify_users("alerts")  # bob only disabled digest
    assert [u.label for u in alert_users] == ["bob_ef56ab78", "jane_ab12cd34"]


def test_iter_all_users_includes_unlinked(local):
    _user(local, "jane_ab12cd34", LINKED)
    _user(local, "carol_11223344", {"currency": "EUR"})  # never linked

    users = fanout.iter_all_users()
    assert [u.label for u in users] == ["carol_11223344", "jane_ab12cd34"]
    # chat.json path sits next to prefs.json for slug accounts
    assert users[0].chat_path == local / "users" / "carol_11223344" / "chat.json"


def test_iter_accounts_reports_registration_dates(local):
    _user(local, "jane_ab12cd34", {**LINKED, "first_seen": "2026-01-05T10:00:00+00:00",
                                   "last_seen": "2026-02-01"})
    _user(local, "carol_11223344", {"first_seen": "2026-03-09T08:00:00+00:00",
                                    "last_seen": "2026-03-09",
                                    "first_seen_estimated": True})
    _user(local, "_guest", {"first_seen": "2020-01-01T00:00:00+00:00"})
    (local / "prefs.json").write_text(json.dumps({"first_seen": "2025-12-01T00:00:00Z"}))

    rows = {r["label"]: r for r in fanout.iter_accounts()}
    assert set(rows) == {"jane_ab12cd34", "carol_11223344", "owner"}  # no _guest
    assert rows["jane_ab12cd34"]["last_seen"] == "2026-02-01"
    assert rows["jane_ab12cd34"]["telegram"] is True
    assert rows["carol_11223344"]["estimated"] is True
    assert rows["jane_ab12cd34"]["estimated"] is False


def test_iter_accounts_tolerates_prefs_without_dates(local):
    _user(local, "jane_ab12cd34", {"currency": "EUR"})
    (row,) = fanout.iter_accounts()
    assert row == {"label": "jane_ab12cd34", "first_seen": "", "last_seen": "",
                   "estimated": False, "telegram": False}


def test_owner_chat_path_is_data_chat_json(local):
    (local / "prefs.json").write_text(json.dumps({"telegram_chat_id": 999}))
    (owner,) = fanout.iter_all_users()
    assert owner.chat_path == local / "chat.json"


def test_owner_root_prefs_is_an_entry(local):
    (local / "prefs.json").write_text(json.dumps({"telegram_chat_id": 999}))
    users = fanout.iter_notify_users("digest")
    assert [u.label for u in users] == ["owner"]
    assert users[0].watchlist == fanout.WATCHLIST_FILE
    assert users[0].db == local / "portfolio.db"
    assert users[0].state_path == local / "alerts_state.json"


def test_bucket_discovery_restores_files(monkeypatch, tmp_path):
    """Slugs come from bucket keys; user files are pulled before reading."""
    from stocks import storage

    from .test_storage import FakeClient

    client = FakeClient()
    client.objects["data/users/jane_ab12cd34/prefs.json"] = json.dumps(LINKED).encode()
    client.objects["data/users/jane_ab12cd34/watchlist.yaml"] = b"watchlist: []\n"
    monkeypatch.setattr(storage, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        storage,
        "_cached",
        {
            "config": {
                "bucket": "b", "access_key_id": "k", "secret_access_key": "s",
                "endpoint_url": "", "region": "auto",
            },
            "client": client,
        },
    )
    monkeypatch.setattr(fanout, "USERS_DIR", tmp_path / "data" / "users")
    monkeypatch.setattr(fanout, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(fanout, "WATCHLIST_FILE", tmp_path / "watchlist.yaml")

    users = fanout.iter_notify_users("digest")
    assert [u.label for u in users] == ["jane_ab12cd34"]
    assert (tmp_path / "data" / "users" / "jane_ab12cd34" / "prefs.json").exists()
    assert users[0].watchlist.read_text() == "watchlist: []\n"


WATCHLIST_WITH_ALERT = """\
watchlist:
  - ticker: NVDA
    alerts:
      - type: above
        price: 100
"""


def _frame(closes):
    return pd.DataFrame({"Close": closes})


def test_run_alerts_fanout_sends_once_then_dedupes(local, monkeypatch):
    _user(local, "jane_ab12cd34", LINKED, watchlist=WATCHLIST_WITH_ALERT)
    monkeypatch.setattr(
        fanout, "_fetch_frames",
        lambda tickers, max_workers=4: {"NVDA": _frame([90, 150])},
    )
    sent = []
    monkeypatch.setattr(
        fanout.telegram,
        "send_message",
        lambda text, chat_id, parse_mode=None: sent.append((text, chat_id)),
    )

    status = fanout.run_alerts_fanout()
    assert status == {"jane_ab12cd34": "sent 1"}
    (text, chat_id), = sent
    assert chat_id == 111
    assert "NVDA" in text and "above 100" in text
    assert "Alertas de precio" in text  # user's language (es)

    # Second run an hour later: condition still true -> suppressed by state.
    status2 = fanout.run_alerts_fanout()
    assert status2 == {"jane_ab12cd34": "no hits"}
    assert len(sent) == 1


def test_run_alerts_fanout_blocked_user_skipped(local, monkeypatch):
    root = _user(local, "jane_ab12cd34", LINKED, watchlist=WATCHLIST_WITH_ALERT)
    monkeypatch.setattr(
        fanout, "_fetch_frames",
        lambda tickers, max_workers=4: {"NVDA": _frame([90, 150])},
    )

    def blocked(text, chat_id, parse_mode=None):
        raise fanout.telegram.TelegramBlocked("blocked")

    monkeypatch.setattr(fanout.telegram, "send_message", blocked)
    assert fanout.run_alerts_fanout() == {"jane_ab12cd34": "blocked"}
    assert state_mod.is_blocked(state_mod.load_state(root / "alerts_state.json"))
    # Next run never re-attempts delivery.
    assert fanout.run_alerts_fanout() == {"jane_ab12cd34": "skipped: blocked"}


def test_run_alerts_fanout_isolates_user_errors(local, monkeypatch):
    _user(local, "jane_ab12cd34", LINKED, watchlist=WATCHLIST_WITH_ALERT)
    _user(local, "bob_ef56ab78", {"telegram_chat_id": 222},
          watchlist=WATCHLIST_WITH_ALERT)
    monkeypatch.setattr(
        fanout, "_fetch_frames",
        lambda tickers, max_workers=4: {"NVDA": _frame([90, 150])},
    )
    calls = []

    def flaky(text, chat_id, parse_mode=None):
        if chat_id == 222:
            raise RuntimeError("boom")
        calls.append(chat_id)

    monkeypatch.setattr(fanout.telegram, "send_message", flaky)
    status = fanout.run_alerts_fanout()
    assert status["jane_ab12cd34"] == "sent 1"
    assert status["bob_ef56ab78"].startswith("error:")
    assert calls == [111]


def test_alert_note_is_appended_when_the_llm_answers(local, monkeypatch):
    _user(local, "jane_ab12cd34", LINKED, watchlist=WATCHLIST_WITH_ALERT)
    monkeypatch.setattr(
        fanout, "_fetch_frames",
        lambda tickers, max_workers=4: {"NVDA": _frame([90, 150])},
    )
    seen = []
    monkeypatch.setattr(
        narrative, "alerts_line",
        lambda hits, prefs, lang: seen.append((len(hits), lang)) or "Semis running.",
    )
    sent = []
    monkeypatch.setattr(
        fanout.telegram, "send_message",
        lambda text, chat_id, parse_mode=None: sent.append(text),
    )

    assert fanout.run_alerts_fanout() == {"jane_ab12cd34": "sent 1"}
    assert seen == [(1, "es")]  # one call, the account's language
    assert "NVDA" in sent[0] and "💡 Semis running." in sent[0]


def test_alert_note_absent_never_blocks_the_alert(local, monkeypatch):
    _user(local, "jane_ab12cd34", LINKED, watchlist=WATCHLIST_WITH_ALERT)
    monkeypatch.setattr(
        fanout, "_fetch_frames",
        lambda tickers, max_workers=4: {"NVDA": _frame([90, 150])},
    )
    monkeypatch.setattr(narrative, "alerts_line", lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(
        fanout.telegram, "send_message",
        lambda text, chat_id, parse_mode=None: sent.append(text),
    )

    assert fanout.run_alerts_fanout() == {"jane_ab12cd34": "sent 1"}
    assert "💡" not in sent[0]


def test_no_hits_never_calls_the_llm(local, monkeypatch):
    _user(local, "jane_ab12cd34", LINKED, watchlist=WATCHLIST_WITH_ALERT)
    monkeypatch.setattr(
        fanout, "_fetch_frames",
        lambda tickers, max_workers=4: {"NVDA": _frame([90, 50])},  # below 100
    )
    calls = []
    monkeypatch.setattr(
        narrative, "alerts_line", lambda *a, **kw: calls.append(1) or "x"
    )
    monkeypatch.setattr(
        fanout.telegram, "send_message",
        lambda text, chat_id, parse_mode=None: None,
    )
    assert fanout.run_alerts_fanout() == {"jane_ab12cd34": "no hits"}
    assert calls == []


# ------------------------------------------------- digest highlight memory


def _digest_env(local, monkeypatch, highlight):
    """One linked account, a fixed digest, and `highlight` as the LLM."""
    from datetime import date

    from stocks.notify import digest

    _user(local, "jane_ab12cd34", LINKED)
    monkeypatch.setattr(
        digest, "compute_digest_data",
        lambda watchlist, db, base: digest.DigestData(
            date=date(2026, 9, 3), total=1000.0, day=(10.0, 0.01)
        ),
    )
    monkeypatch.setattr(narrative, "highlight", highlight)
    return digest


def test_digest_highlight_is_remembered_for_the_next_run(local, monkeypatch):
    seen = []

    def highlight(data, prefs, lang, recent=None):
        seen.append(list(recent or []))
        return f"Line {len(seen)}."

    digest = _digest_env(local, monkeypatch, highlight)
    monkeypatch.setattr(
        fanout.telegram, "send_message", lambda text, chat_id, parse_mode=None: None
    )

    assert digest.run_digest_fanout() == {"jane_ab12cd34": "sent"}
    assert digest.run_digest_fanout() == {"jane_ab12cd34": "sent"}
    assert digest.run_digest_fanout() == {"jane_ab12cd34": "sent"}
    # Each run sees everything delivered before it, oldest first.
    assert seen == [[], ["Line 1."], ["Line 1.", "Line 2."]]


def test_undelivered_highlight_is_not_remembered(local, monkeypatch):
    """A line the user never saw must not be treated as already said."""
    seen = []

    def highlight(data, prefs, lang, recent=None):
        seen.append(list(recent or []))
        return "Only line."

    digest = _digest_env(local, monkeypatch, highlight)

    def blocked(text, chat_id, parse_mode=None):
        raise fanout.telegram.TelegramBlocked("blocked")

    monkeypatch.setattr(fanout.telegram, "send_message", blocked)
    assert digest.run_digest_fanout() == {"jane_ab12cd34": "blocked"}
    assert state_mod.recent_highlights(
        state_mod.load_state(local / "users" / "jane_ab12cd34" / "alerts_state.json")
    ) == []


def test_digest_without_a_highlight_writes_no_state(local, monkeypatch):
    digest = _digest_env(local, monkeypatch, lambda *a, **kw: None)
    monkeypatch.setattr(
        fanout.telegram, "send_message", lambda text, chat_id, parse_mode=None: None
    )
    assert digest.run_digest_fanout() == {"jane_ab12cd34": "sent"}
    assert not (local / "users" / "jane_ab12cd34" / "alerts_state.json").exists()
