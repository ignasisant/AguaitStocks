"""memory: the long-term chat index — what gets stored, found and forgotten."""

import pytest

from stocks.chat import memory

pytestmark = pytest.mark.skipif(
    not memory.available(),
    reason="memory needs model2vec + sqlite-vec; the chat degrades without them",
)

_T1 = [
    {"role": "user",
     "content": "Estoy pensando en recortar mi posición en ASML, las reservas "
                "salieron flojas y ya pesa el 30% de mi cartera"},
    {"role": "assistant",
     "content": "El riesgo real es la concentración: con el 30% de la cartera "
                "un solo aviso de resultados mueve todo tu patrimonio"},
]
_T2 = [
    {"role": "user",
     "content": "what do you think about buying more Nvidia here, it has run a "
                "lot this year and I only hold a small position"},
    {"role": "assistant",
     "content": "Adding after a 200% run means paying for certainty you did "
                "not have before; size it so a 40% drawdown changes nothing"},
]


@pytest.fixture
def index(tmp_path):
    path = tmp_path / memory.FILE
    memory.remember(path, _T1, "t1")
    memory.remember(path, _T2, "t2")
    return path


# ------------------------------------------------------------------ storing


def test_remember_indexes_the_turns_worth_keeping(tmp_path):
    path = tmp_path / memory.FILE
    assert memory.remember(path, _T1, "t1") == 2
    assert path.exists()


def test_remember_is_idempotent(index):
    # Callers re-index the whole thread after every answer; storing the same
    # turn twice would double it in every future search.
    assert memory.remember(index, _T1, "t1") == 0


def test_only_the_new_turns_cost_anything(index):
    grown = _T1 + [{"role": "user",
                    "content": "y si en vez de recortar ASML compro más ahora "
                               "que ha caído tanto desde los máximos?"}]
    assert memory.remember(index, grown, "t1") == 1


def test_small_talk_is_not_a_memory(tmp_path):
    chatter = [{"role": "user", "content": "ok"},
               {"role": "assistant", "content": "gracias"},
               {"role": "user", "content": "y eso?"}]
    assert memory.remember(tmp_path / memory.FILE, chatter, "t1") == 0


def test_turns_that_are_not_the_conversation_are_skipped(tmp_path):
    system = [{"role": "system", "content": "x" * 200}]
    assert memory.remember(tmp_path / memory.FILE, system, "t1") == 0


def test_a_long_turn_is_indexed_up_to_its_share(tmp_path):
    path = tmp_path / memory.FILE
    memory.remember(path, [{"role": "user", "content": "palabra " * 5000}], "t1")
    got = memory.recall(path, "palabra")
    assert got and len(got[0].text) <= memory.MAX_CHARS


def test_the_same_thread_in_two_accounts_never_mixes(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    memory.remember(a, _T1, "t1")
    memory.remember(b, _T2, "t1")
    assert "ASML" in memory.recall(a, "ASML")[0].text
    assert not [m for m in memory.recall(b, "ASML") if "ASML" in m.text]


# ------------------------------------------------------------------ finding


def test_recall_finds_the_turn_by_its_words(index):
    got = memory.recall(index, "¿qué decidí sobre ASML?")
    assert got and "ASML" in got[0].text


def test_recall_finds_a_turn_that_shares_no_words(index):
    # The point of the embedding half: no term here appears in the note.
    got = memory.recall(index, "how much of one holding is too much")
    assert any("concentración" in m.text for m in got)


def test_recall_ignores_accents_the_user_did_not_type(index):
    assert memory.recall(index, "concentracion de la cartera")


def test_recall_skips_the_conversation_in_progress(index):
    threads = {m.thread for m in memory.recall(index, "ASML", exclude_thread="t1")}
    assert "t1" not in threads


def test_recall_ranks_best_first(index):
    got = memory.recall(index, "Nvidia")
    assert [m.rank for m in got] == sorted(m.rank for m in got)


def test_recall_respects_the_limit(index):
    assert len(memory.recall(index, "cartera position", limit=1)) == 1


def test_recall_without_an_index_is_empty(tmp_path):
    assert memory.recall(tmp_path / "never-written.db", "anything") == []


def test_recall_of_nothing_searches_nothing(index):
    assert memory.recall(index, "   ") == []


# ------------------------------------------------------------- fts queries


@pytest.mark.parametrize("raw", [
    '¿qué decidí sobre "ASML"?',
    "AND OR NOT NEAR",
    "BRK.B vs BRK-B",
    "50% -- drop",
])
def test_a_question_never_becomes_an_fts_syntax_error(index, raw):
    # FTS5 treats quotes, hyphens and its own keywords as syntax; a raw
    # question is a crash waiting for the first user who types one.
    memory.recall(index, raw)  # must not raise


def test_the_fts_query_is_the_words_the_user_typed():
    assert memory.fts_query("¿qué tal ASML?") == '"asml" OR "qué" OR "tal"'
    assert memory.fts_query("!!! ---") == ""


# ---------------------------------------------------------------- forgetting


def test_forget_drops_one_threads_memories(index):
    assert memory.forget(index, "t1") == 2
    assert {m.thread for m in memory.recall(index, "position cartera")} == {"t2"}


def test_forget_of_an_unknown_thread_is_harmless(index):
    assert memory.forget(index, "nope") == 0


def test_a_forgotten_turn_can_be_remembered_again(index):
    memory.forget(index, "t1")
    assert memory.remember(index, _T1, "t1") == 2


# ------------------------------------------------------------- degradation


def test_nothing_is_indexed_when_the_model_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "available", lambda: False)
    assert memory.remember(tmp_path / memory.FILE, _T1, "t1") == 0
    assert memory.recall(tmp_path / memory.FILE, "ASML") == []


def test_a_broken_embedder_does_not_break_the_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "embed", lambda texts: None)
    assert memory.remember(tmp_path / memory.FILE, _T1, "t1") == 0


def test_an_unwritable_index_does_not_break_the_conversation(tmp_path):
    blocked = tmp_path / "file.txt"
    blocked.write_text("not a database")
    assert memory.remember(blocked, _T1, "t1") == 0
    assert memory.recall(blocked, "ASML") == []
