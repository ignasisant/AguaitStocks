"""The legal documents themselves (stocks.web.legal) — the routes that serve
them are covered in test_server.py."""

from __future__ import annotations

import pytest

from stocks.web import legal


@pytest.mark.parametrize("doc", ["privacy", "terms"])
@pytest.mark.parametrize("lang", ["en", "es"])
def test_each_document_is_complete_html(doc, lang):
    html = legal.document(doc, lang)
    assert html.startswith("<!doctype html>")
    assert f'<html lang="{lang}">' in html
    assert legal.LAST_UPDATED in html
    assert 'href="/"' in html  # a way back out


def test_an_unknown_language_falls_back_to_english():
    assert 'lang="en"' in legal.document("terms", "fr")


def test_the_terms_state_the_disclaimer_in_both_languages():
    assert "not investment advice" in legal.document("terms", "en").lower()
    assert "asesoramiento de inversión" in legal.document("terms", "es").lower()


def test_the_privacy_policy_names_the_deletion_path():
    for lang in ("en", "es"):
        html = legal.document("privacy", lang)
        assert "ts_app" in html  # the one first-party cookie, by name
        assert "Delete account" in html or "Eliminar cuenta" in html


def test_the_documents_cross_link_and_keep_the_language():
    en = legal.document("privacy", "en")
    assert f'href="{legal.PATH_TERMS}"' in en
    es = legal.document("privacy", "es")
    assert f'href="{legal.PATH_TERMS}?lang=es"' in es


def test_the_legal_pages_carry_a_focus_ring():
    assert ":focus-visible" in legal.document("privacy", "en")
