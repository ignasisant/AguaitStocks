"""The routing in front of the Streamlit app.

`/` is shared: the landing for a visitor who has neither a query parameter nor
the app cookie, the app for everyone else. That rule is the whole design (see
`stocks.web.server`), and it is the one thing here that could go wrong in a way
nobody notices — a leak in either direction either hides the app from returning
users or hides the pitch from Google.

The Streamlit app itself is stubbed with a catch-all route. Booting the real one
would need a runtime, a websocket and a secrets file; what these tests are about
is which requests reach it at all, and what the response carries when they do.
"""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from stocks.web import landing_static, server

STUB = "STREAMLIT-APP"


@pytest.fixture
def client(monkeypatch):
    """server.py's routes and gate, with a stub standing in for Streamlit."""
    landing_static.document.cache_clear()
    server._gzipped.cache_clear()
    # No secrets file in the test environment; make the override explicit so a
    # developer's own [app] public_url cannot change the expected URLs.
    monkeypatch.setattr(server, "secret", lambda *a, **k: "")

    async def stub(request):
        return PlainTextResponse(STUB, media_type="text/html")

    app = Starlette(
        routes=[*server.routes, Route("/{path:path}", stub, methods=["GET", "POST"])],
        middleware=[
            Middleware(server.SecurityHeaders),
            Middleware(server.LandingGate),
        ],
    )
    return TestClient(app, base_url="https://topstocks.example")


# ------------------------------------------------------------------ the gate


def test_a_first_visit_gets_the_landing(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'property="og:title"' in r.text
    assert STUB not in r.text


def test_a_crawler_gets_the_landing(client):
    """Crawlers send no cookies, so they always see the page a visitor sees."""
    r = client.get("/", headers={"User-Agent": "Googlebot/2.1"})
    assert 'rel="canonical"' in r.text


def test_a_cta_click_goes_to_the_app(client):
    r = client.get("/?signin=1")
    assert r.text == STUB


def test_a_ticker_deep_link_goes_to_the_app(client):
    r = client.get("/?ticker=AAPL")
    assert r.text == STUB


def test_the_app_response_marks_the_browser_and_the_next_visit_skips_the_pitch(client):
    assert client.get("/?guest=1").text == STUB
    assert client.cookies.get(server.APP_COOKIE) == "1"
    # cookie now on the client — a bare "/" is a returning visitor
    assert client.get("/").text == STUB


def test_the_landing_can_still_be_asked_for(client):
    client.cookies.set(server.APP_COOKIE, "1")
    assert 'property="og:title"' in client.get("/?landing=1").text


def test_a_post_to_the_root_is_never_the_landing(client):
    assert client.post("/").text == STUB


def test_app_pages_are_untouched_by_the_gate(client):
    assert client.get("/portfolio").text == STUB


# --------------------------------------------------------------- index policy


def test_the_app_is_marked_noindex(client):
    """A JavaScript shell over somebody's positions has no business ranking."""
    for path in ("/portfolio", "/?signin=1", "/_stcore/health"):
        assert client.get(path).headers["x-robots-tag"] == "noindex, nofollow"


@pytest.mark.parametrize("path", ["/", "/es/", "/robots.txt", "/sitemap.xml"])
def test_the_marketing_pages_are_indexable(client, path):
    assert "x-robots-tag" not in client.get(path).headers


def test_the_landing_is_revalidated_but_the_spanish_page_is_cacheable(client):
    en = client.get("/")
    assert en.headers["cache-control"] == "no-cache"
    assert "Cookie" in en.headers["vary"]
    es = client.get("/es/")
    assert es.headers["cache-control"] == "public, max-age=300"


# ------------------------------------------------------------------- spanish


def test_the_spanish_page_is_served_in_spanish(client):
    r = client.get("/es/")
    assert r.status_code == 200
    assert 'lang="es"' in r.text
    assert "Tu rentabilidad real" in r.text


def test_the_unslashed_spanish_url_redirects_once_and_permanently(client):
    r = client.get("/es", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/es/"


# --------------------------------------------------------- robots and sitemap


def test_robots_is_generated_for_the_host_that_answered(client):
    r = client.get("/robots.txt", headers={"X-Forwarded-Host": "topstocks.dev"})
    assert r.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://topstocks.dev/sitemap.xml" in r.text


def test_the_forwarded_scheme_wins_over_the_socket(client):
    """Cloud Run terminates TLS; the container itself sees plain HTTP."""
    r = client.get(
        "/robots.txt",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "topstocks.dev"},
    )
    assert "https://topstocks.dev" in r.text


def test_a_spoofed_host_cannot_get_into_the_canonical_url(client):
    r = client.get("/", headers={"X-Forwarded-Host": "evil.example/../x"})
    assert "evil.example" not in r.text


def test_the_sitemap_is_xml_with_both_pages(client):
    r = client.get("/sitemap.xml")
    assert r.headers["content-type"].startswith("application/xml")
    assert "<loc>https://topstocks.example/</loc>" in r.text
    assert "<loc>https://topstocks.example/es/</loc>" in r.text
    assert "<lastmod>" in r.text


# -------------------------------------------------------------------- assets


def test_the_brand_mark_is_served(client):
    r = client.get("/lp/topstocks-icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")
    assert "max-age" in r.headers["cache-control"]


def test_the_share_card_is_served(client):
    r = client.get("/lp/og.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


@pytest.mark.parametrize("name", ["..%2fapp.py", "%2e%2e/app.py", "nope.svg"])
def test_the_asset_mount_serves_nothing_outside_itself(client, name):
    assert client.get(f"/lp/{name}").status_code == 404


@pytest.mark.parametrize("name", ["../app.py", "../../../etc/passwd", "sub/../../x"])
def test_the_asset_handler_refuses_to_escape_its_directory(name):
    """Checked against the handler directly: an HTTP client normalises `..`
    out of the path before it is ever sent, so the client cannot express this."""

    class _Req:
        path_params = {"path": name}

    assert asyncio.run(server.asset(_Req())).status_code == 404


# ---------------------------------------------------------------- compression


def test_the_document_is_compressed_when_the_client_takes_it(client):
    plain = client.get("/", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers

    # TestClient transparently decodes, so the check is on the header and on
    # the fact that the decoded body is the same page.
    gzipped = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert gzipped.headers["content-encoding"] == "gzip"
    assert gzipped.text == plain.text
    assert "Accept-Encoding" in gzipped.headers["vary"]


# --------------------------------------------------------- one canonical host


@pytest.fixture
def pinned_client(monkeypatch):
    """The same app, with `[app] public_url` pinned to one origin."""
    landing_static.document.cache_clear()
    server._gzipped.cache_clear()
    monkeypatch.setattr(server, "secret", lambda *a, **k: "https://topstocks.example")

    async def stub(request):
        return PlainTextResponse(STUB, media_type="text/html")

    app = Starlette(
        routes=[*server.routes, Route("/{path:path}", stub, methods=["GET", "POST"])],
        middleware=[Middleware(server.LandingGate)],
    )
    return TestClient(app, base_url="https://alias.run.app")


def test_a_stray_hostname_is_redirected_to_the_canonical_one(pinned_client):
    r = pinned_client.get("/es/", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://topstocks.example/es/"


def test_the_redirect_keeps_the_query_string(pinned_client):
    r = pinned_client.get("/ticker?ticker=AAPL", follow_redirects=False)
    assert r.headers["location"] == "https://topstocks.example/ticker?ticker=AAPL"


def test_a_live_session_is_not_redirected_out_from_under_itself(pinned_client):
    # Moving a websocket or an XHR mid-session breaks the page the visitor is
    # already looking at; the document redirect is what moves them.
    assert pinned_client.get("/_stcore/health").status_code == 200


def test_the_canonical_host_itself_is_served_not_redirected(monkeypatch):
    monkeypatch.setattr(server, "secret", lambda *a, **k: "https://topstocks.example")

    async def stub(request):
        return PlainTextResponse(STUB, media_type="text/html")

    app = Starlette(
        routes=[*server.routes, Route("/{path:path}", stub, methods=["GET", "POST"])],
        middleware=[Middleware(server.LandingGate)],
    )
    client = TestClient(app, base_url="https://topstocks.example")
    assert client.get("/es/", follow_redirects=False).status_code == 200


def test_without_a_public_url_every_host_is_served_as_is(client):
    assert client.get("/es/", follow_redirects=False).status_code == 200


# ------------------------------------------------------------- real not-founds


def test_an_unknown_path_is_a_404_not_the_app_shell(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404
    assert STUB not in r.text
    assert r.headers["X-Robots-Tag"] == "noindex"


@pytest.mark.parametrize(
    "path",
    ["/portfolio", "/ticker", "/screener", "/earnings", "/profile",
     "/import_transactions", "/home", "/oauth2callback", "/_stcore/health",
     "/media/abc", "/component/x/y", "/app/static/logo.png", "/auth/login",
     "/manifest.json", "/favicon.png"],
)
def test_everything_that_is_really_served_survives_the_gate(client, path):
    assert client.get(path).status_code == 200


def test_a_trailing_slash_on_a_real_page_is_not_a_404(client):
    assert client.get("/portfolio/").status_code == 200


def test_the_page_list_comes_from_the_app_pages_directory():
    # A page added to app_pages/ must not need a second edit here to be
    # reachable — that drift is exactly what would 404 a live page.
    from stocks.web import seo

    assert "/portfolio" in seo.app_page_paths()
    assert all(server._is_known_path(p) for p in seo.app_page_paths())


def test_the_oidc_callback_is_never_bounced_to_another_host(pinned_client):
    # Google sends the browser to the exact URI registered with it; finishing
    # that round trip on a different origin is how a login silently breaks.
    r = pinned_client.get("/oauth2callback?code=abc&state=xyz", follow_redirects=False)
    assert r.status_code == 200


# ------------------------------------------------------------------ liveness


def test_healthz_answers_without_touching_the_app(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["cache-control"] == "no-store"
    # An uptime probe has not "been to the app": no returning-visitor cookie,
    # or the checks themselves would flip what `/` serves to real monitors.
    assert "set-cookie" not in r.headers


def test_status_reports_revision_uptime_and_storage(client, monkeypatch):
    monkeypatch.setenv("K_REVISION", "topstocks-00042-abc")
    r = client.get("/status")
    body = r.json()
    assert body["status"] == "ok"
    assert body["revision"] == "topstocks-00042-abc"
    assert body["uptime_s"] >= 0
    assert body["storage"] is False  # no [storage] in the test env
    assert "set-cookie" not in r.headers


# --------------------------------------------------------------- legal pages


def test_the_legal_pages_are_served_in_both_languages(client):
    for doc in ("privacy", "terms"):
        en = client.get(f"/legal/{doc}")
        assert en.status_code == 200
        assert 'lang="en"' in en.text
        es = client.get(f"/legal/{doc}?lang=es")
        assert 'lang="es"' in es.text
        assert "set-cookie" not in en.headers


def test_the_terms_lead_with_the_investment_disclaimer(client):
    assert "not investment advice" in client.get("/legal/terms").text.lower()


def test_an_unknown_legal_doc_is_a_404(client):
    assert client.get("/legal/nonsense").status_code == 404


def test_the_landing_links_the_legal_pages(client):
    html = client.get("/").text
    assert 'href="/legal/privacy"' in html
    assert 'href="/legal/terms"' in html
    assert 'href="/legal/privacy?lang=es"' in client.get("/es/").text


# ---------------------------------------------------------- security headers


def test_every_response_carries_the_baseline_headers(client):
    for path in ("/", "/portfolio", "/legal/privacy", "/no-such-page"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "SAMEORIGIN"
        assert h["content-security-policy"] == "frame-ancestors 'self'"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in h["permissions-policy"]


def test_hsts_is_set_on_tls_and_not_on_local_http(client, monkeypatch):
    assert "strict-transport-security" in client.get("/").headers

    monkeypatch.setattr(server, "secret", lambda *a, **k: "")

    async def stub(request):
        return PlainTextResponse(STUB, media_type="text/html")

    plain = TestClient(
        Starlette(
            routes=[Route("/{path:path}", stub, methods=["GET"])],
            middleware=[Middleware(server.SecurityHeaders)],
        ),
        base_url="http://localhost:8501",
    )
    # Teaching a dev browser to refuse http://localhost would outlive the run.
    assert "strict-transport-security" not in plain.get("/").headers
