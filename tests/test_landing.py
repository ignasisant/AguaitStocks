"""Landing gate and its number formatting — no Streamlit runtime.

`AppTest` cannot cover this: it does not deliver query parameters to app.py at
all (verified at the script's first executable line), which is also why the
pre-existing `?ticker=` deep-link handler is untested there. The gate is pure
enough to exercise directly, so it is.
"""

import re

import pytest

from stocks.web import landing


class _Params(dict):
    """Stand-in for st.query_params — dict plus the .get()/del we rely on."""


@pytest.fixture
def gate(monkeypatch):
    """Anonymous visitor, auth configured, nothing seen yet."""
    params, state, logins = _Params(), {}, []
    monkeypatch.setattr(landing.st, "query_params", params)
    monkeypatch.setattr(landing.st, "session_state", state)
    monkeypatch.setattr(landing.st, "secrets", {"auth": {"client_id": "x"}})
    monkeypatch.setattr(landing.st, "login", lambda *a, **k: logins.append(True))
    monkeypatch.setattr(landing.auth, "is_logged_in", lambda: False)
    monkeypatch.setattr(landing, "active_language", lambda: "en")
    return params, state, logins


def test_anonymous_first_visit_shows_landing(gate):
    assert landing.should_show() is True


def test_signed_in_visitor_never_sees_it(gate, monkeypatch):
    monkeypatch.setattr(landing.auth, "is_logged_in", lambda: True)
    assert landing.should_show() is False


def test_hidden_when_auth_is_not_configured(gate, monkeypatch):
    monkeypatch.setattr(landing.st, "secrets", {})
    assert landing.should_show() is False


def test_ticker_deep_link_wins(gate):
    """Shared ?ticker= URLs are handed out by the app and must still work."""
    params, _, _ = gate
    params["ticker"] = "AAPL"
    assert landing.should_show() is False


def test_blank_ticker_does_not_count_as_a_deep_link(gate):
    params, _, _ = gate
    params["ticker"] = "   "
    assert landing.should_show() is True


def test_guest_param_dismisses_clears_and_sticks(gate):
    params, state, _ = gate
    params[landing.PARAM_GUEST] = "1"
    assert landing.should_show() is False
    assert landing.PARAM_GUEST not in params, "param must be cleared from the URL"
    assert state["_landing_seen"] is True
    # and it stays dismissed on the next run, with no parameter left
    assert landing.should_show() is False


def test_signin_param_starts_the_oidc_round_trip(gate):
    params, _, logins = gate
    params[landing.PARAM_SIGNIN] = "1"
    assert landing.should_show() is False
    assert logins == [True], "st.login() should have been called exactly once"


@pytest.mark.parametrize("lang", ["es", "en"])
def test_lang_param_overrides_the_run_language(gate, lang):
    params, state, _ = gate
    params["lang"] = lang
    landing.should_show()
    assert state["active_lang"] == lang


def test_unknown_lang_param_is_ignored(gate):
    params, state, _ = gate
    params["lang"] = "klingon"
    landing.should_show()
    assert "active_lang" not in state


# ------------------------------------------------------------------ numbers


@pytest.fixture
def as_lang(monkeypatch):
    def _set(lang):
        monkeypatch.setattr(landing, "active_language", lambda: lang)

    return _set


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
    as_lang(lang)
    assert landing._eur(amount, signed=signed) == expected


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


@pytest.fixture
def rendered(monkeypatch):
    """render_landing() with st.html captured instead of emitted."""

    def _render(*, mobile: bool):
        blocks: list[str] = []
        monkeypatch.setattr(
            landing.st, "html", lambda body, **kw: blocks.append(body)
        )
        monkeypatch.setattr(landing, "is_mobile", lambda: mobile)
        monkeypatch.setattr(landing, "active_language", lambda: "en")
        monkeypatch.setattr(landing, "_static_logo_src", lambda name: "/x.svg")
        landing.render_landing()
        return blocks

    return _render


def test_the_bar_and_its_script_ship_on_every_request(rendered):
    blocks = rendered(mobile=False)
    assert '<div class="ag-l-mbar">' in blocks[-2], "bar missing from the markup"
    assert blocks[-1] == landing._BAR_JS, "reveal script must follow the markup"


def test_the_user_agent_override_ships_only_for_phones(rendered):
    assert landing._mobile_css() in rendered(mobile=True)
    assert landing._mobile_css() not in rendered(mobile=False)


def test_no_grid_track_can_outgrow_its_container():
    """`minmax(Npx, 1fr)` does not shrink below N — it overflows and gets clipped.

    Streamlit's main container hides horizontal overflow, so a 400px minimum
    track inside a 370px phone viewport cut the hero card off at the right edge
    rather than scrolling. Every auto-fit minimum is written min(Npx, 100%).
    """
    bare = re.findall(r"minmax\(\d+px", landing._CSS)
    assert not bare, f"unclamped grid minimums: {bare}"
