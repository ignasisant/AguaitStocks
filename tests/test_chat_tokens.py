"""tokens: context budget counting, truncation and the fit-to-budget trim."""

import pytest

from stocks.chat import tokens


def _msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


# ------------------------------------------------------------------- count


def test_count_grows_with_the_text():
    assert tokens.count("") == 0
    assert tokens.count("hello") < tokens.count("hello " * 50)


def test_count_messages_charges_for_the_framing():
    one = _msgs(("user", "hi"))
    assert tokens.count_messages(one) > tokens.count("hi")


def test_count_falls_back_to_characters_without_tiktoken(monkeypatch):
    monkeypatch.setattr(tokens, "_encoder", lambda: None)
    text = "a" * 360
    assert tokens.count(text) == pytest.approx(360 / tokens._CHARS_PER_TOKEN, rel=0.1)


def test_count_survives_an_encoder_that_blows_up(monkeypatch):
    class _Broken:
        def encode(self, *a, **kw):
            raise RuntimeError("bpe file is corrupt")

    monkeypatch.setattr(tokens, "_encoder", lambda: _Broken())
    assert tokens.count("some text here") > 0  # counting never raises


def test_count_does_not_choke_on_special_token_text():
    # A pasted transcript can contain the literal "<|endoftext|>"; tiktoken
    # raises on it unless special tokens are disallowed rather than parsed.
    assert tokens.count("<|endoftext|> and more") > 0


# ---------------------------------------------------------------- truncate


def test_truncate_leaves_a_short_text_alone():
    assert tokens.truncate("short enough", 100) == "short enough"


def test_truncate_cuts_the_tail_and_says_so():
    got = tokens.truncate("question at the top " + "filler " * 2000, 100)
    assert got.startswith("question at the top")  # the user's words survive
    assert got.endswith(tokens.TRIM_MARK)
    assert tokens.count(got) <= 100


def test_truncate_with_no_room_is_just_the_mark():
    assert tokens.truncate("anything", 0) == tokens.TRIM_MARK.strip()


# --------------------------------------------------------------------- fit


def test_fit_leaves_a_small_thread_untouched():
    msgs = _msgs(("user", "hi"), ("assistant", "hello"), ("user", "and now?"))
    assert tokens.fit(msgs, budget=1000) is msgs  # same list, no copying


def test_fit_drops_the_oldest_turns_first():
    msgs = _msgs(("user", "oldest " * 200), ("assistant", "mid " * 200),
                 ("user", "newest " * 10))
    got = tokens.fit(msgs, budget=120)
    assert got[-1]["content"].startswith("newest")
    assert len(got) < len(msgs)
    assert tokens.count_messages(got) <= 120


def test_fit_keeps_the_thread_opening_on_a_user_turn():
    # Anthropic rejects a history that starts with an assistant reply.
    msgs = _msgs(("user", "q1 " * 300), ("assistant", "a1 " * 5),
                 ("user", "q2 " * 5), ("assistant", "a2 " * 5),
                 ("user", "q3 " * 5))
    got = tokens.fit(msgs, budget=80)
    assert got[0]["role"] == "user"


def test_fit_charges_the_system_prompt_against_the_budget():
    msgs = _msgs(("user", "one " * 50), ("assistant", "two " * 50),
                 ("user", "three " * 10))
    roomy = tokens.fit(msgs, budget=160)
    tight = tokens.fit(msgs, system="persona " * 100, budget=160)
    assert len(tight) < len(roomy)


def test_fit_truncates_the_newest_turn_rather_than_dropping_it():
    # The newest turn is the question (plus whatever augmentation stapled on),
    # so it is truncated, never dropped — an empty request is not an answer.
    msgs = _msgs(("user", "old " * 100),
                 ("user", "my actual question " + "web extract " * 2000))
    got = tokens.fit(msgs, budget=150)
    assert len(got) == 1
    assert got[0]["content"].startswith("my actual question")
    assert got[0]["content"].endswith(tokens.TRIM_MARK)
    assert tokens.count_messages(got) <= 150


def test_fit_does_not_mutate_the_callers_messages():
    msgs = _msgs(("user", "huge " * 5000))
    before = msgs[0]["content"]
    tokens.fit(msgs, budget=50)
    assert msgs[0]["content"] == before


def test_fit_of_nothing_is_nothing():
    assert tokens.fit([], budget=10) == []
