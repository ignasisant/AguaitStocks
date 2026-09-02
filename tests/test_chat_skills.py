"""Skill library loading, classifier-output parsing and prompt assembly — pure,
no network, no streamlit."""

import pytest

from stocks.web import chat_skills

EXPECTED_IDS = {
    "tech", "energy", "value", "financials", "healthcare", "crypto",
    "emerging-markets", "dividend", "growth-momentum", "technical", "macro",
    "earnings-review", "portfolio-risk", "spain-tax", "bear-case", "etfs",
}


def test_catalog_loads_all_skills():
    skills = chat_skills.catalog()
    assert {s.id for s in skills} == EXPECTED_IDS
    for s in skills:
        assert s.name and s.description and s.body, s.id


def test_skills_block_empty_without_ids():
    assert chat_skills.skills_block([]) == ""
    assert chat_skills.skills_block(["not-a-skill"]) == ""


def test_skills_block_contains_name_and_body():
    block = chat_skills.skills_block(["tech"])
    tech = next(s for s in chat_skills.catalog() if s.id == "tech")
    assert f"## {tech.name}" in block
    assert tech.body in block


def test_skills_block_order_is_stable():
    """Same set in any order -> byte-identical text (prompt-cache friendly)."""
    assert chat_skills.skills_block(["value", "tech"]) == chat_skills.skills_block(
        ["tech", "value"]
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"skills": ["tech", "value"]}', ["tech", "value"]),
        ('```json\n{"skills": ["energy"]}\n```', ["energy"]),
        ('Sure! {"skills": ["macro"]} fits best.', ["macro"]),
        ('{"skills": []}', []),
        ('{"skills": ["nope", "tech"]}', ["tech"]),  # unknown ids dropped
        ('{"skills": [42, "tech"]}', ["tech"]),  # non-strings dropped
        ("I would pick tech and spain-tax here", ["tech", "spain-tax"]),  # fallback
        ("total garbage", []),
        ("", []),
        ('{"skills": ["tech", "tech", "value"]}', ["tech", "value"]),  # dedupe
    ],
)
def test_parse_skill_ids(raw, expected):
    assert chat_skills.parse_skill_ids(raw) == expected


def test_parse_skill_ids_caps_at_limit():
    raw = '{"skills": ["tech", "value", "macro", "energy"]}'
    assert len(chat_skills.parse_skill_ids(raw)) == chat_skills.MAX_AUTO
    assert chat_skills.parse_skill_ids(raw, limit=3) == ["tech", "value", "macro"]


class _FakeProvider:
    classifier_model = "cheap-model"

    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc
        self.calls = []

    def complete(self, api_key, model, system, messages):
        self.calls.append((api_key, model, system, messages))
        if self.exc:
            raise self.exc
        return self.reply


def test_classify_returns_parsed_ids():
    p = _FakeProvider(reply='{"skills": ["earnings-review"]}')
    assert chat_skills.classify(p, "k", "how was the NVDA quarter?") == [
        "earnings-review"
    ]
    api_key, model, system, messages = p.calls[0]
    assert (api_key, model) == ("k", "cheap-model")
    assert "- earnings-review:" in system  # catalog rides in the system prompt
    assert "NVDA" in messages[0]["content"]


def test_classify_none_on_provider_error():
    p = _FakeProvider(exc=RuntimeError("boom"))
    assert chat_skills.classify(p, "k", "anything") is None


def test_classify_empty_on_garbage_reply():
    p = _FakeProvider(reply="no json here")
    assert chat_skills.classify(p, "k", "hello") == []
