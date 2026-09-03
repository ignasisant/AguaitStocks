"""Search and social metadata for the landing pages.

Worth testing rather than eyeballing: none of it is visible on the page, so a
dropped canonical, a stale hreflang or an over-long title fails silently and
only shows up as a ranking or an ugly link preview weeks later. The length
budgets and the JSON-LD/FAQ agreement are the checks that would otherwise need
an external validator.
"""

import json
import re
from xml.etree import ElementTree

import pytest

from stocks.web import landing, markup, seo
from stocks.web.i18n import LANGUAGES, translate
from stocks.web.landing import (
    ASSET_BASE,
    FAQ_COUNT,
    PATH_EN_ES,
    PATH_ES,
    PATH_ES_US,
)

BASE = "https://topstocks.example"

# Search results truncate a title past ~60 characters and a description past
# ~160. Both are hand-written copy, so the only way they stay inside the budget
# is a test that fails when they don't.
TITLE_MAX = 60
DESCRIPTION_MAX = 160


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
@pytest.mark.parametrize("jur", ["ES", "US"])
def test_title_and_description_fit_a_search_result(lang, jur):
    """Every variant's own copy, not just the default pair's."""
    for key, cap in (("seo_title", TITLE_MAX), ("seo_description", DESCRIPTION_MAX)):
        assert len(translate(landing.jur_key(f"landing.{key}", jur), lang)) <= cap


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_head_carries_the_page_identity(lang):
    head = seo.head(lang, BASE)
    # The head argues the language's own jurisdiction, so the copy it carries
    # is that variant's — the override where one exists, the shared string
    # where it doesn't. jur_key is the same resolution the page itself uses.
    jur = landing.jurisdiction_for(lang)
    title = translate(landing.jur_key("landing.seo_title", jur), lang)
    description = translate(landing.jur_key("landing.seo_description", jur), lang)

    assert f"<title>{markup.esc(title)}</title>" in head
    assert f'<meta name="description" content="{markup.esc(description)}">' in head
    assert f'<link rel="canonical" href="{BASE}{seo.path_for(lang)}">' in head
    assert '<meta charset="utf-8">' in head
    assert "width=device-width" in head


def test_canonical_differs_per_language():
    assert f'href="{BASE}/"' in seo.head("en", BASE)
    assert f'href="{BASE}{PATH_ES}"' in seo.head("es", BASE)
    # And the cross variants are addressable on their own URLs.
    assert (
        f'<link rel="canonical" href="{BASE}{PATH_EN_ES}">'
        in seo.head("en", BASE, jurisdiction="ES")
    )
    assert (
        f'<link rel="canonical" href="{BASE}{PATH_ES_US}">'
        in seo.head("es", BASE, jurisdiction="US")
    )


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_both_languages_declare_each_other_and_a_default(lang):
    head = seo.head(lang, BASE)
    # The pair is per jurisdiction: the English US-tax page's Spanish
    # alternate is the Spanish US-tax page, not the Spanish-tax one (different
    # content, and declaring it an alternate would be a lie to the crawler).
    expected = {
        "en": ("/", PATH_EN_ES),
        "es": (PATH_ES_US, PATH_ES),
    }
    jur = "US" if lang == "en" else "ES"
    for code, paths in expected.items():
        path = paths[0] if jur == "US" else paths[1]
        assert f'<link rel="alternate" hreflang="{code}" href="{BASE}{path}">' in head
    default = "/" if jur == "US" else PATH_EN_ES
    assert (
        f'<link rel="alternate" hreflang="x-default" href="{BASE}{default}">' in head
    )


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_share_card_is_absolute_and_sized(lang):
    """A relative or unsized og:image is the classic broken link preview."""
    head = seo.head(lang, BASE)
    assert f'property="og:image" content="{BASE}{ASSET_BASE}og.png"' in head
    assert f'property="og:image:width" content="{seo.OG_IMAGE_W}"' in head
    assert f'property="og:image:height" content="{seo.OG_IMAGE_H}"' in head
    assert 'name="twitter:card" content="summary_large_image"' in head
    assert f'name="twitter:image" content="{BASE}{ASSET_BASE}og.png"' in head


def test_og_locale_is_a_locale_not_a_language_code():
    assert 'property="og:locale" content="es_ES"' in seo.head("es", BASE)
    assert 'property="og:locale:alternate" content="en_US"' in seo.head("es", BASE)


def test_the_page_asks_to_be_indexed_with_a_large_preview():
    head = seo.head("en", BASE)
    assert 'name="robots" content="index, follow, max-image-preview:large"' in head


def test_extra_styles_land_last():
    """The caller controls CSS order; the metadata must not sit after it."""
    head = seo.head("en", BASE, extra_styles="<style>.x{}</style>")
    assert head.endswith("<style>.x{}</style>")


def test_unknown_language_falls_back_to_english():
    assert seo.head("klingon", BASE) == seo.head("en", BASE)


def test_fonts_are_preconnected_before_they_are_requested():
    head = seo.head("en", BASE)
    assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in head
    assert head.index("preconnect") < head.index('rel="stylesheet"')


# ------------------------------------------------------------- structured data


def _graph(lang: str) -> list[dict]:
    block = seo.json_ld(lang, BASE)
    body = block[block.index(">") + 1 : block.rindex("</script>")]
    return json.loads(body)["@graph"]


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_structured_data_is_valid_json_with_the_three_nodes(lang):
    types = {node["@type"] for node in _graph(lang)}
    assert types == {"WebSite", "SoftwareApplication", "FAQPage"}


def test_structured_data_never_closes_the_script_early():
    """A literal "</script>" in the JSON would end the element mid-object."""
    block = seo.json_ld("en", BASE)
    body = block[block.index(">") + 1 : block.rindex("</script>")]
    # Nothing in today's copy needs escaping; the invariant is what matters,
    # because one "<" added to a FAQ answer would otherwise break the element.
    assert "<" not in body


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_faq_structured_data_matches_the_visible_section(lang):
    """Marking up questions the page does not show is how FAQ results get lost."""
    faq = next(n for n in _graph(lang) if n["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) == FAQ_COUNT
    first = faq["mainEntity"][0]
    assert first["name"] == translate("landing.faq_q1", lang)
    assert first["acceptedAnswer"]["text"] == translate("landing.faq_a1", lang)


def test_the_app_is_declared_free():
    app = next(n for n in _graph("en") if n["@type"] == "SoftwareApplication")
    assert app["isAccessibleForFree"] is True
    assert app["offers"]["price"] == 0
    assert app["applicationCategory"] == "FinanceApplication"


# -------------------------------------------------------------------- robots


def test_robots_allows_the_landing_and_disallows_the_app():
    body = seo.robots_txt(BASE)
    assert "User-agent: *" in body
    # "/" cannot be blanket-disallowed: it is the landing for anyone without
    # the app cookie, which includes every crawler.
    assert "Allow: /$" in body
    for path in (PATH_ES, PATH_EN_ES, PATH_ES_US):
        assert f"Allow: {path}" in body
    for private in ("/portfolio", "/profile", "/import_transactions", "/_stcore/"):
        assert f"Disallow: {private}" in body
    assert f"Sitemap: {BASE}/sitemap.xml" in body


def _robots_group(agent: str) -> list[str]:
    """The rules a crawler calling itself `agent` would apply."""
    out, active = [], False
    for line in seo.robots_txt(BASE).splitlines():
        if line.lower().startswith("user-agent:"):
            active = line.split(":", 1)[1].strip() == agent
        elif active and line.strip():
            out.append(line.strip())
    return out


def test_robots_never_disallows_the_landing_itself():
    # Scoped to the "*" group: the AI-crawler groups below it *do* say
    # "Disallow: /", and that is the point of them.
    assert "Disallow: /" not in _robots_group("*"), "would delist the landing page"


def test_ai_crawlers_are_turned_away_from_everything():
    for agent in seo.AI_CRAWLERS:
        assert _robots_group(agent) == ["Disallow: /"], agent


def test_a_search_crawler_is_still_welcome():
    # Google-Extended is the Gemini-training token and blocking it must not
    # touch Googlebot, which is what puts the landing in search results.
    assert "Google-Extended" in seo.AI_CRAWLERS
    assert "Googlebot" not in seo.AI_CRAWLERS
    assert "Applebot" not in seo.AI_CRAWLERS  # only Applebot-Extended is blocked
    assert _robots_group("Googlebot") == []  # falls through to the "*" group


# ------------------------------------------------------------------- sitemap


def test_sitemap_is_well_formed_and_lists_every_variant():
    xml = seo.sitemap_xml(BASE, lastmod="2026-08-31")
    root = ElementTree.fromstring(xml)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall(".//s:loc", ns)]
    assert locs == [
        f"{BASE}/", f"{BASE}{PATH_ES}", f"{BASE}{PATH_EN_ES}", f"{BASE}{PATH_ES_US}"
    ]
    assert all(e.text == "2026-08-31" for e in root.findall(".//s:lastmod", ns))


def test_every_sitemap_url_carries_the_full_alternate_set():
    """Including itself — the spec requires the self-referencing xhtml:link."""
    xml = seo.sitemap_xml(BASE)
    root = ElementTree.fromstring(xml)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
          "xhtml": "http://www.w3.org/1999/xhtml"}
    for url in root.findall("s:url", ns):
        langs = {e.get("hreflang") for e in url.findall("xhtml:link", ns)}
        assert langs == {"en", "es", "x-default"}


def test_sitemap_without_a_lastmod_omits_the_element():
    assert "<lastmod>" not in seo.sitemap_xml(BASE)


def test_absolute_urls_have_no_double_slash():
    """A trailing slash on the base plus a leading slash on the path."""
    xml = seo.sitemap_xml(BASE + "/")
    assert not re.search(r"https://[^\"<]*//", xml)


def test_the_share_card_declares_its_type():
    # Slack and LinkedIn will skip an image they have to sniff.
    assert '<meta property="og:image:type" content="image/png">' in seo.head("en", BASE)


def test_robots_disallows_every_app_page_it_can_see():
    body = seo.robots_txt(BASE)
    for path in seo.app_page_paths():
        assert f"Disallow: {path}" in body
    for prefix in seo.APP_PREFIXES:
        assert f"Disallow: {prefix}" in body


def test_robots_leaves_the_favicon_and_manifest_crawlable():
    # Blocking a favicon is how a search result loses its icon; neither file is
    # content, and both already carry noindex.
    body = seo.robots_txt(BASE)
    assert "Disallow: /favicon.png" not in body
    assert "Disallow: /manifest.json" not in body


def test_the_page_paths_track_the_app_pages_directory():
    paths = seo.app_page_paths()
    assert "/portfolio" in paths and "/ticker" in paths
    assert not any(p.startswith("/_") for p in paths)
