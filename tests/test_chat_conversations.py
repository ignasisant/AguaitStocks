"""Multi-conversation chat storage (stocks.web.auth chat threads).

The assistant keeps several threads per account inside a single chat.json.
What matters here: the v1 single-thread file still loads, load never writes,
load_chat/save_chat keep acting on the active thread (the Telegram bot and
the headless engine call them unchanged), and the housekeeping — reuse of an
empty thread, deletion fallbacks, the cap — never leaves the book without an
active conversation.
"""

import json

import pytest

from stocks.web import auth


@pytest.fixture
def chat(tmp_path):
    return tmp_path / "chat.json"


# ------------------------------------------------------------------ loading


def test_missing_file_is_one_empty_thread_and_writes_nothing(chat):
    book = auth.load_book(chat)
    assert len(book["conversations"]) == 1
    assert book["active"] == book["conversations"][0]["id"]
    assert auth.load_chat(chat) == []
    assert not chat.exists()  # a read must never create the file


def test_corrupt_file_degrades_to_an_empty_thread(chat):
    chat.write_text("{not json")
    assert auth.load_chat(chat) == []
    assert len(auth.list_conversations(chat)) == 1


def test_v1_flat_list_migrates_to_one_conversation(chat):
    turns = [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}]
    chat.write_text(json.dumps(turns))

    assert auth.load_chat(chat) == turns
    convs = auth.list_conversations(chat)
    assert len(convs) == 1 and convs[0]["messages"] == 2
    assert convs[0]["active"] and convs[0]["title"] == ""


def test_v1_migration_only_persists_on_the_next_write(chat):
    chat.write_text(json.dumps([{"role": "user", "content": "hi"}]))
    auth.load_chat(chat)
    assert isinstance(json.loads(chat.read_text()), list)  # untouched

    auth.save_chat([{"role": "user", "content": "hi again"}], chat)
    saved = json.loads(chat.read_text())
    assert saved["version"] == auth.CHAT_VERSION
    assert len(saved["conversations"]) == 1


def test_unknown_active_id_falls_back_to_a_real_thread(chat):
    auth.save_chat([{"role": "user", "content": "a"}], chat)
    book = json.loads(chat.read_text())
    book["active"] = "c_gone"
    chat.write_text(json.dumps(book))

    assert auth.active_conversation(chat)["id"] == book["conversations"][0]["id"]


# ------------------------------------------------------------- active thread


def test_save_and_load_roundtrip_on_the_active_thread(chat):
    turns = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    auth.save_chat(turns, chat)
    assert auth.load_chat(chat) == turns


def test_save_chat_only_touches_the_active_thread(chat):
    auth.save_chat([{"role": "user", "content": "first"}], chat)
    first = auth.active_conversation(chat)["id"]
    second = auth.new_conversation(chat)
    auth.save_chat([{"role": "user", "content": "second"}], chat)

    auth.set_active_conversation(first, chat)
    assert auth.load_chat(chat) == [{"role": "user", "content": "first"}]
    auth.set_active_conversation(second, chat)
    assert auth.load_chat(chat) == [{"role": "user", "content": "second"}]


# -------------------------------------------------------------- new / switch


def test_new_conversation_reuses_an_empty_active_thread(chat):
    first = auth.new_conversation(chat)
    assert auth.new_conversation(chat) == first  # nothing said yet — same thread
    assert len(auth.list_conversations(chat)) == 1


def test_new_conversation_forks_once_the_thread_has_turns(chat):
    auth.save_chat([{"role": "user", "content": "hi"}], chat)
    first = auth.active_conversation(chat)["id"]
    second = auth.new_conversation(chat)

    assert second != first
    assert auth.active_conversation(chat)["id"] == second
    assert auth.load_chat(chat) == []
    assert {c["id"] for c in auth.list_conversations(chat)} == {first, second}


def test_set_active_ignores_an_unknown_id(chat):
    current = auth.new_conversation(chat)
    auth.set_active_conversation("c_nope", chat)
    assert auth.active_conversation(chat)["id"] == current


def test_list_is_ordered_by_last_use(chat):
    auth.save_chat([{"role": "user", "content": "old"}], chat)
    older = auth.active_conversation(chat)["id"]
    newer = auth.new_conversation(chat)
    auth.save_chat([{"role": "user", "content": "new"}], chat)

    # Same-second timestamps would make the order arbitrary — nudge the old one.
    book = auth.load_book(chat)
    for c in book["conversations"]:
        if c["id"] == older:
            c["updated"] = "2020-01-01T00:00:00+00:00"
    auth.save_book(book, chat)

    assert [c["id"] for c in auth.list_conversations(chat)] == [newer, older]


# ------------------------------------------------------------------- titles


def test_autotitle_names_an_unnamed_thread(chat):
    cid = auth.new_conversation(chat)
    auth.autotitle_conversation(cid, "NVDA valuation", chat)
    assert auth.active_conversation(chat)["title"] == "NVDA valuation"


def test_rename_pins_the_title_against_autotitling(chat):
    cid = auth.new_conversation(chat)
    auth.rename_conversation(cid, "  My thread  ", chat)
    auth.autotitle_conversation(cid, "Something else", chat)

    conv = auth.active_conversation(chat)
    assert conv["title"] == "My thread" and conv["title_auto"] is False


def test_titles_are_length_capped(chat):
    cid = auth.new_conversation(chat)
    auth.rename_conversation(cid, "x" * 500, chat)
    assert len(auth.active_conversation(chat)["title"]) == auth._TITLE_MAX


# ------------------------------------------------------------------- delete


def test_delete_active_falls_back_to_the_most_recent_survivor(chat):
    auth.save_chat([{"role": "user", "content": "keep"}], chat)
    keeper = auth.active_conversation(chat)["id"]
    doomed = auth.new_conversation(chat)

    auth.delete_conversation(doomed, chat)
    assert auth.active_conversation(chat)["id"] == keeper
    assert auth.load_chat(chat) == [{"role": "user", "content": "keep"}]


def test_deleting_the_last_thread_leaves_a_fresh_empty_one(chat):
    auth.save_chat([{"role": "user", "content": "bye"}], chat)
    only = auth.active_conversation(chat)["id"]
    auth.delete_conversation(only, chat)

    convs = auth.list_conversations(chat)
    assert len(convs) == 1 and convs[0]["id"] != only
    assert auth.load_chat(chat) == []


def test_delete_ignores_an_unknown_id(chat):
    auth.new_conversation(chat)
    before = auth.list_conversations(chat)
    auth.delete_conversation("c_nope", chat)
    assert auth.list_conversations(chat) == before


# ---------------------------------------------------------------------- cap


def test_cap_prunes_least_recently_used_and_keeps_the_active_one(chat):
    book = auth.load_book(chat)
    book["conversations"] = [
        {"id": f"c_{i:04d}", "title": str(i), "title_auto": True,
         "created": "2020-01-01T00:00:00+00:00",
         "updated": f"2020-01-01T00:00:{i:02d}+00:00",
         "messages": [{"role": "user", "content": str(i)}]}
        for i in range(auth.MAX_CONVERSATIONS + 5)
    ]
    book["active"] = "c_0000"  # the oldest — must survive anyway
    auth.save_book(book, chat)

    kept = {c["id"] for c in auth.list_conversations(chat)}
    assert len(kept) == auth.MAX_CONVERSATIONS
    assert "c_0000" in kept
    assert f"c_{auth.MAX_CONVERSATIONS + 4:04d}" in kept  # newest kept
    assert "c_0001" not in kept  # next-oldest pruned first
