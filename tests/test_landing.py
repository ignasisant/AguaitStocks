"""The landing's CTA parameters, number formatting and markup.

`AppTest` cannot cover the parameter half: it does not deliver query parameters
to app.py at all (verified at the script's first executable line), which is also
why the pre-existing `?ticker=` deep-link handler is untested there.
`consume_params()` is pure enough to exercise directly, so it is.

The markup half needs no Streamlit runtime either — the page is built as a
string, outside any script run. See test_landing_static.py for the document
that wraps it and test_server.py for the routes that serve it.
"""

import re
from contextlib import ExitStack

import pytest

from stocks.web import landing


class _Params(dict):
    """Stand-in for st.query_params — dict plus the .get()/del we rely on."""


@pytest.fixture
def gate(monkeypatch):
    """Anonymous visitor, auth configured, no parameters yet."""
    params, state, logins = _Params(), {}, []
    monkeypatch.setattr(landing.st, "query_params", params)
    monkeypatch.setattr(landing.st, "session_state", state)
    monkeypatch.setattr(landing.st, "secrets", {"auth": {"client_id": "x"}})
    monkeypatch.setattr(landing.st, "login", lambda *a, **k: logins.append(True))
    monkeypatch.setattr(landing.auth, "is_logged_in", lambda: False)
    return params, state, logins


def test_no_parameters_is_a_no_op(gate):
    """A plain app load must not touch the session or start a login."""
    params, state, logins = gate
    landing.consume_params()
    assert (dict(params), state, logins) == ({}, {}, [])


def test_signin_param_starts_the_oidc_round_trip(gate):
    params, _, logins = gate
    params[landing.PARAM_SIGNIN] = "1"
    landing.consume_params()
    assert logins == [True], "st.login() should have been called exactly once"


def test_signin_param_is_ignored_once_signed_in(gate, monkeypatch):
    """The parameter can survive the redirect; a second login would loop."""
    params, _, logins = gate
    monkeypatch.setattr(landing.auth, "is_logged_in", lambda: True)
    params[landing.PARAM_SIGNIN] = "1"
    landing.consume_params()
    assert logins == []


def test_signin_param_is_ignored_without_auth_configured(gate, monkeypatch):
    params, _, logins = gate
    monkeypatch.setattr(landing.st, "secrets", {})
    params[landing.PARAM_SIGNIN] = "1"
    landing.consume_params()
    assert logins == []


def test_guest_param_is_cleared_from_the_url(gate):
    params, _, logins = gate
    params[landing.PARAM_GUEST] = "1"
    landing.consume_params()
    assert landing.PARAM_GUEST not in params, "param must be cleared from the URL"
    assert logins == [], "browsing as a guest must not start a login"


def test_ticker_deep_link_passes_straight_through(gate):
    """Shared ?ticker= URLs are handed out by the app and must be left alone."""
    params, state, logins = gate
    params["ticker"] = "AAPL"
    landing.consume_params()
    assert params["ticker"] == "AAPL"
    assert (state, logins) == ({}, [])


@pytest.mark.parametrize("lang", ["es", "en"])
def test_lang_param_overrides_the_run_language(gate, lang):
    params, state, _ = gate
    params["lang"] = lang
    landing.consume_params()
    assert state["active_lang"] == lang


def test_lang_param_survives_for_the_next_rerun(gate):
    """app.py re-resolves the language every rerun; the parameter re-applies it."""
    params, _, _ = gate
    params["lang"] = "es"
    landing.consume_params()
    assert params["lang"] == "es"


def test_unknown_lang_param_is_ignored(gate):
    params, state, _ = gate
    params["lang"] = "klingon"
    landing.consume_params()
    assert "active_lang" not in state


# ------------------------------------------------------------------ numbers


@pytest.fixture
def as_lang():
    """Enter landing.render_language(lang, jurisdiction) for the test.

    The jurisdiction defaults to the language's own — English pitches the US
    rules, Spanish the Spanish ones — so a test that cares about the country
    passes it explicitly.
    """
    with ExitStack() as stack:

        def _set(lang, jurisdiction=None):
            stack.enter_context(landing.render_language(lang, jurisdiction))

        yield _set


@pytest.mark.parametrize(
    ("lang", "amount", "signed", "expected"),
    [
        ("en", 48230, False, "€48,230"),
        ("es", 48230, False, "48.230 €"),
        ("en", 13254, True, "+€13,254"),
        ("es", 13254, True, "+13.254 €"),
        ("en", -612, True, "−€612"),
        ("es", -612, True, "−612 €"),
        ("en", 407, False, "€407"),
        ("es", 1234567, False, "1.234.567 €"),
    ],
)
def test_currency_follows_the_reader(as_lang, lang, amount, signed, expected):
    """Separators follow the language; the symbol follows the jurisdiction."""
    as_lang(lang, "ES")
    assert landing._money(amount, signed=signed) == expected


@pytest.mark.parametrize(
    ("lang", "amount", "signed", "expected"),
    [
        ("en", 48230, False, "$48,230"),
        ("es", 48230, False, "48.230 $"),
        ("en", -612, True, "−$612"),
    ],
)
def test_a_us_reader_gets_dollars(as_lang, lang, amount, signed, expected):
    as_lang(lang, "US")
    assert landing._money(amount, signed=signed) == expected


def test_english_pitches_the_us_rules_and_spanish_the_spanish_ones(as_lang):
    as_lang("en")
    assert landing.active_jurisdiction() == "US"
    assert landing._symbol() == "$"
    as_lang("es")
    assert landing.active_jurisdiction() == "ES"
    assert landing._symbol() == "€"


def test_jurisdiction_scoped_copy_falls_back_to_the_neutral_string(as_lang):
    as_lang("en", "US")
    # Overridden for the US pitch…
    assert landing.jur_key("landing.hero_title") == "landing.us_hero_title"
    # …but the shared strings have one version.
    assert landing.jur_key("landing.faq_q1") == "landing.faq_q1"
    assert landing.jur_key("common.sign_in_google") == "common.sign_in_google"


@pytest.mark.parametrize(
    ("lang", "value", "signed", "expected"),
    [
        ("en", 47.7, True, "+47.7%"),
        ("es", 47.7, True, "+47,7 %"),
        ("en", -22.6, True, "−22.6%"),
        ("es", -22.6, True, "−22,6 %"),
        ("en", 3.1, False, "3.1%"),
        ("es", 3.1, False, "3,1 %"),
    ],
)
def test_percent_follows_the_reader(as_lang, lang, value, signed, expected):
    as_lang(lang)
    assert landing._pct(value, signed=signed) == expected


def test_decimals_use_the_local_separator(as_lang):
    as_lang("en")
    assert landing._dec(1.0982, 4) == "1.0982"
    as_lang("es")
    assert landing._dec(1.0982, 4) == "1,0982"


def test_row_count_is_singular_or_plural(as_lang):
    as_lang("en")
    assert landing._rows(1) == "1 row"
    assert landing._rows(214) == "214 rows"


# ------------------------------------------------------------------- mobile


def _style_body(block: str) -> str:
    """The CSS text inside a <style> wrapper."""
    assert block.startswith("<style>") and block.endswith("</style>")
    return block[len("<style>") : -len("</style>")]


@pytest.mark.parametrize("rules", ["_MOBILE_RULES", "_TINY_CSS", "_BASE_CSS"])
def test_no_less_than_anywhere_in_the_css(rules):
    """DOMPurify drops a whole style element when its text holds a "<"."""
    assert "<" not in getattr(landing, rules)


def test_bar_script_holds_no_less_than_either():
    body = landing._BAR_JS.split("<script>")[1].split("</script>")[0]
    assert "<" not in body


def test_stylesheet_is_one_block_with_the_breakpoints_in_order():
    css = _style_body(landing._CSS)
    assert "<" not in css
    assert css.count("{") == css.count("}")
    mobile = css.index("@media (max-width: 640px)")
    tiny = css.index("@media (max-width: 380px)")
    # base rules first, then the phone block, then the small-phone type tweaks —
    # each layer has to be able to override the one before it
    assert css.index(".ag-l-wrap {") < mobile < tiny


def test_mobile_rules_are_gated_by_width_in_the_stylesheet():
    css = _style_body(landing._CSS)
    after_breakpoint = css[css.index("@media (max-width: 640px)") :]
    assert ".ag-l-mbar {" in after_breakpoint
    # ...and the bar is hidden by default, outside any query
    assert ".ag-l-mbar { display: none; }" in landing._BASE_CSS


def test_user_agent_override_reapplies_the_same_rules_at_900():
    css = _style_body(landing._mobile_css())
    assert css.startswith("@media (max-width: 900px) {")
    assert landing._MOBILE_RULES in css
    # the small-phone block rides along, and stays last so it still wins
    assert css.index("@media (max-width: 380px)") > css.index(landing._MOBILE_RULES)


def test_mobile_bar_carries_the_sign_in_parameter():
    bar = landing._mobile_bar()
    assert bar.startswith('<div class="ag-l-mbar">')
    assert f"?{landing.PARAM_SIGNIN}=1" in bar


# -------------------------------------------------------------------- markup


@pytest.fixture
def body(as_lang):
    def _build(lang="en"):
        as_lang(lang)
        return landing.page_body()

    return _build


def test_the_english_page_argues_the_us_rules(body):
    """The pitch is a country's case, so the copy has to be that country's."""
    html = body("en")
    assert "IRS" in html and "IRC 1091" in html
    assert "Modelo 720" not in html and "LIRPF" not in html
    assert "$" in html


def test_the_spanish_page_argues_the_spanish_ones(body):
    html = body("es")
    assert "Modelo 720" in html and "33.5.f" in html
    assert "IRC 1091" not in html


def test_the_page_is_one_element_with_every_section(body):
    html = body()
    assert html.startswith('<div class="ag-l">') and html.endswith("</div>")
    for marker in (
        "ag-l-bar",      # top bar
        "ag-l-hero",
        "ag-l-broker",   # broker list
        "ag-l-provcard", # provenance
        "ag-l-trustgrid",
        "ag-l-q",        # FAQ
        "ag-l-final",
        "ag-l-foot",
        "ag-l-mbar",     # the phone CTA bar ships on every request
    ):
        assert marker in html, f"section missing: {marker}"


def test_every_cta_leaves_the_landing_for_the_app(body):
    """The CTAs must point at the app, not back at the page they are on.

    `server.py` routes `/` to the landing precisely when there is no query
    parameter, so a relative `?signin=1` from `/es/` would land on the Spanish
    landing again instead of the app.
    """
    # &amp; because the href is escaped for the attribute it sits in
    pairs = (("en", "/?signin=1"), ("es", "/?signin=1&amp;lang=es"))
    for lang, expected in pairs:
        html = body(lang)
        assert f'href="{expected}"' in html
        assert 'href="?signin=1"' not in html, "relative CTA would stay on the page"


def test_the_language_toggle_stays_inside_the_jurisdiction(as_lang):
    """A reader on the Spanish-tax page wants that page in English, not the
    US-tax one — so the switch keeps the country and changes the language."""
    as_lang("en", "US")
    en_us = landing.page_body()
    assert f'href="{landing.PATH_ES_US}"' in en_us
    assert '<span class="on">EN</span>' in en_us
    assert f'href="{landing.PATH_ES}"' not in en_us
    assert "?lang=" not in en_us.replace("&lang=es", ""), "no ?lang= toggle links"

    as_lang("en", "ES")
    assert f'href="{landing.PATH_ES}"' in landing.page_body()


def test_the_jurisdiction_toggle_keeps_the_language(as_lang):
    as_lang("es", "ES")
    es_es = landing.page_body()
    # The other country, same language — from the tax panel and the footer.
    assert f'class="ag-l-jurswitch" href="{landing.PATH_ES_US}"' in es_es
    assert es_es.count(f'href="{landing.PATH_ES_US}"') >= 2
    # …while the language switch on the same page keeps the Spanish rules.
    assert f'href="{landing.PATH_EN_ES}"' in es_es

    as_lang("en", "US")
    en_us = landing.page_body()
    assert f'class="ag-l-jurswitch" href="{landing.PATH_EN_ES}"' in en_us


def test_the_brand_mark_is_absolute(body):
    """/es/ is a directory deeper, so a relative asset URL would 404 there."""
    assert f'src="{landing.ASSET_BASE}topstocks-icon.svg"' in body()


def test_the_copy_actually_changes_language(body):
    assert "Your real return" in body("en")
    assert "Tu rentabilidad real" in body("es")


def test_the_faq_renders_every_question_the_structured_data_claims(body):
    assert body().count("<details class=\"ag-l-q\"") == landing.FAQ_COUNT


def test_no_grid_track_can_outgrow_its_container():
    """`minmax(Npx, 1fr)` does not shrink below N — it overflows and gets clipped.

    Streamlit's main container hides horizontal overflow, so a 400px minimum
    track inside a 370px phone viewport cut the hero card off at the right edge
    rather than scrolling. Every auto-fit minimum is written min(Npx, 100%).
    """
    bare = re.findall(r"minmax\(\d+px", landing._CSS)
    assert not bare, f"unclamped grid minimums: {bare}"
