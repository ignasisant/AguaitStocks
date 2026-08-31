"""The landing as a standalone HTML document.

The point of serving the page as bytes instead of rendering it in Streamlit is
that a crawler and a first-time visitor get the whole thing in one response.
These checks are the ones that would silently undo that: markup that needs
JavaScript to appear, an asset that costs a second round trip, or a head that
lost its metadata.
"""

import re
from html import escape

import pytest

from stocks.web import landing, landing_static, seo

BASE = "https://topstocks.example"


@pytest.fixture(autouse=True)
def _fresh_cache():
    """`document` is lru_cached for the server; tests want it rebuilt."""
    landing_static.document.cache_clear()
    yield
    landing_static.document.cache_clear()


@pytest.mark.parametrize("lang", ["en", "es"])
def test_it_is_a_complete_document(lang):
    html = landing_static.document(lang, BASE)
    assert html.startswith("<!doctype html>")
    assert f'<html lang="{lang}">' in html
    assert "<head>" in html and "</head>" in html
    assert html.endswith("</body></html>")


@pytest.mark.parametrize("lang", ["en", "es"])
def test_the_copy_is_in_the_response_not_behind_a_script(lang):
    html = landing_static.document(lang, BASE)
    with landing.render_language(lang):
        title = landing.tr("landing.hero_title")
    # Compared on the fragment before the first comma: the headline is escaped
    # into the markup, and its apostrophes would not match verbatim.
    assert title.split(",")[0] in html


@pytest.mark.parametrize("lang", ["en", "es"])
def test_the_head_carries_the_metadata_and_the_styles(lang):
    html = landing_static.document(lang, BASE)
    head = html[: html.index("</head>")]
    assert f'rel="canonical" href="{BASE}{seo.path_for(lang)}"' in head
    assert 'type="application/ld+json"' in head
    assert "--ag-surface-page" in head, "design tokens must be inlined"
    assert ".ag-l-wrap {" in head, "page stylesheet must be inlined"


def test_the_only_external_request_is_the_font_stylesheet():
    """Anything else would be a round trip before the page can paint."""
    html = landing_static.document("en", BASE)
    sheets = re.findall(r'<link rel="stylesheet"[^>]+href="([^"]+)"', html)
    # escaped, because the font URL's "&" separators sit in an attribute
    assert set(sheets) == {escape(seo.FONTS_HREF, quote=True)}
    assert not re.search(r"<script[^>]+src=", html), "no external script"
    assert "<img" not in html or "src=\"/lp/" in html, "images stay same-origin"


def test_the_font_stylesheet_does_not_block_the_first_paint():
    """A blocking link to a second origin holds the whole page hostage.

    `media="print"` takes it off the critical path and the onload flip applies
    it as soon as it lands; the noscript copy is the blocking fallback.
    """
    html = landing_static.document("en", BASE)
    href = escape(seo.FONTS_HREF, quote=True)
    assert f'<link rel="preload" as="style" href="{href}">' in html
    assert f'href="{href}" media="print" onload=' in html
    assert f'<noscript><link rel="stylesheet" href="{href}"></noscript>' in html
    # Every blocking stylesheet in the document must be an inline <style>.
    blocking = re.findall(r'<link rel="stylesheet"(?![^>]*media="print")[^>]*>', html)
    assert all("noscript" in html[max(0, html.index(tag) - 10):html.index(tag)]
               for tag in blocking)


def test_the_phone_override_ships_disabled_with_the_script_that_enables_it():
    html = landing_static.document("en", BASE)
    assert '<style id="ag-ua-mobile" media="not all">' in html
    assert 'getElementById("ag-ua-mobile")' in html
    assert "navigator.userAgent" in html
    # Disabled block after the stylesheet it overrides, script after the block.
    assert html.index(".ag-l-wrap {") < html.index('id="ag-ua-mobile"')
    assert html.index('id="ag-ua-mobile"') < html.index("navigator.userAgent")


def test_the_body_background_is_painted_by_the_document():
    """`.ag-l` colours itself; a short viewport would show white underneath."""
    html = landing_static.document("en", BASE)
    assert "min-height: 100vh" in html
    assert "background: var(--ag-surface-page)" in html


def test_every_design_token_the_page_uses_is_defined_in_the_document():
    """The app used to emit the tokens; the document has to carry them itself.

    A `var(--ag-x)` with no definition falls back to nothing — the element just
    loses its colour, silently, on a page nobody reruns in development.
    """
    used = set(re.findall(r"var\((--ag-[a-z0-9-]+)", landing.stylesheet()))
    used |= set(re.findall(r"var\((--ag-[a-z0-9-]+)", landing.ua_mobile_rules()))
    document = landing_static.document("en", BASE)
    missing = [token for token in used if f"{token}:" not in document]
    assert not missing, f"undefined design tokens: {sorted(missing)}"


def test_every_font_the_css_asks_for_is_actually_loaded():
    """config.toml loads the app's faces; a static document loads its own."""
    css = landing.stylesheet()
    families = set(re.findall(r"font-family: '([^']+)'", css))
    for family in families:
        if family in ("sans-serif", "monospace"):
            continue
        assert family.replace(" ", "+") in seo.FONTS_HREF, f"{family} never loaded"


def test_exactly_one_h1():
    html = landing_static.document("en", BASE)
    assert len(re.findall(r"<h1[ >]", html)) == 1


def test_the_reveal_script_is_the_last_thing_in_the_body():
    html = landing_static.document("en", BASE)
    body = html[html.index("<body>") : html.index("</body>")]
    assert body.rstrip().endswith("</script>")
    assert "IntersectionObserver" in body


def test_the_languages_produce_different_documents():
    assert landing_static.document("en", BASE) != landing_static.document("es", BASE)


def test_an_unknown_language_falls_back_to_english():
    assert landing_static.document("kl", BASE) == landing_static.document("en", BASE)


def test_the_render_language_does_not_leak():
    """A ContextVar left set would make the next request answer in Spanish."""
    landing_static.document("es", BASE)
    assert landing.active_language() == "en"


def test_keyboard_focus_ring_ships_with_the_page():
    """The UA default ring is near-invisible on the dark surface; the page
    carries an explicit :focus-visible ring for keyboard users."""
    html = landing_static.document("en", BASE)
    assert ":focus-visible" in html
    assert "prefers-reduced-motion" in html  # the animated bar can be stilled
