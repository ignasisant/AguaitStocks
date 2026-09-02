"""ASGI entry point: the static marketing site in front of the Streamlit app.

Run: uv run stocks dashboard   (or: uv run streamlit run src/stocks/web/server.py)

`streamlit run` detects the module-level `st.App` and serves it directly, so
this is one process on one port — the app exactly as before, plus a handful of
plain Starlette routes it now shares the server with:

    /              the landing (English), or the app for a visitor who has one
    /es/           the landing (Spanish)
    /lp/*          the landing's own assets — brand mark, share card
    /robots.txt    crawl rules, generated for whatever host we answer on
    /sitemap.xml   both landing URLs, cross-linked by hreflang
    /livez         liveness probe for uptime checks (scripts/setup_monitoring.sh)
    /healthz       the same probe, for local runs and the Docker HEALTHCHECK
    /legal/*       privacy policy and terms of use (static, bilingual)
    everything else    Streamlit: /portfolio, /ticker, /_stcore/…, /oauth2callback

Why `/` is shared rather than the app moving to a prefix: the app's default
page is served at the root by Streamlit and nothing can move it (`st.Page`
ignores `url_path` for the default page), so putting the landing anywhere else
would mean `server.baseUrlPath` — which rewrites every app URL, invalidates
every bookmark, and changes the OIDC redirect URI registered with Google. The
gate below avoids all of that: a request for `/` gets the landing only when it
carries no query parameter and no `ts_app` cookie, which is exactly the state
of a first-time visitor and of every crawler. Any CTA click arrives with a
parameter, and the cookie keeps returning visitors going straight to the app.

Crawlers never send cookies, so the pages Google sees at `/` and `/es/` are the
pages a first-time human sees. The app routes are marked `noindex` on the way
out, because they are a JavaScript shell over somebody's positions.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import streamlit as st
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Route

from stocks import obs
from stocks.secrets_env import secret
from stocks.web import landing, landing_static, legal, ratelimit, seo
from stocks.web.landing import (
    ASSET_BASE,
    LANDING_PATHS,
    PATH_EN,
    PATH_ES,
    variant_for,
)

_HERE = Path(__file__).parent
_ASSETS = _HERE / "assets"

# Marks a browser that has already been handed the app. Set on every app
# response, read on `/` alone: without it that request is a first visit and
# gets the pitch, with it the visitor goes where they left off. Not a session
# and not a login — the app decides who is signed in, this only decides which
# document `/` returns.
APP_COOKIE = "ts_app"
_APP_COOKIE_MAX_AGE = 400 * 24 * 3600  # the ceiling Chrome will honour

# Forces the landing even for a returning visitor: `/?landing=1`. Anything else
# in the query string means "take me to the app".
PARAM_LANDING = "landing"

# The marketing site's own paths — deliberately excluding `/`, which is only
# the landing when the gate says so. Anything not listed here is the app, and
# gets stamped noindex on the way out. The probes and the legal pages ride
# along: they are public documents that must not set the app cookie (an uptime
# probe or a privacy-page reader has not "been to the app").
_MARKETING_PREFIXES = (
    *(p for p in LANDING_PATHS if p != PATH_EN),
    ASSET_BASE, "/robots.txt", "/sitemap.xml", "/livez", "/healthz",
    "/status", "/legal/",
)

# A Host header ends up inside canonical and Open Graph URLs, so it is checked
# before it is echoed: hostname characters and an optional port, nothing else.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d+)?$")

_HTML = "text/html; charset=utf-8"

# Paths the canonical-host redirect must leave alone: Streamlit's transport
# (an in-flight session moves with it and breaks) and the OIDC return (the
# hostname is registered with Google and is not ours to change mid-flight).
_NEVER_REDIRECT = ("/_stcore/", "/oauth2callback")


def _is_marketing_path(path: str) -> bool:
    """True for a path that only ever serves the marketing site.

    `/` is not one of them: the gate answers it with the landing directly, so
    anything that reaches this check for `/` came back from the app and is
    treated as such — which is what marks the browser as a returning visitor.
    """
    return any(path.startswith(p) for p in _MARKETING_PREFIXES)


def public_origin() -> str | None:
    """The one origin this site is meant to be reached at, if it is configured.

    `[app] public_url` (or `APP_PUBLIC_URL`). Unset, every hostname the service
    answers on is its own self-canonicalizing copy of the site — Cloud Run hands
    out more than one by default, so this is not hypothetical. Set, it is both
    the base for every absolute URL and the target every other hostname is
    redirected to.
    """
    override = secret("APP_PUBLIC_URL", "app", "public_url")
    return override.rstrip("/") if override else None


def base_url(request: Request) -> str:
    """Absolute origin for this request, e.g. `https://topstocks.example`.

    Canonical, hreflang, Open Graph and the sitemap all need absolute URLs, and
    this service is deployed at whatever hostname it is given (a `*.run.app`
    URL today), so the origin is derived per request rather than configured.
    Cloud Run terminates TLS and forwards the original scheme and host, which
    is why the forwarded headers win over the socket's own view.

    `[app] public_url` (or `APP_PUBLIC_URL`) overrides the lot — set it once a
    real domain is in front, so a request that reaches the container by its
    internal hostname still emits the public one.
    """
    if origin := public_origin():
        return origin

    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip() or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        or request.headers.get("host", "").strip()
    )
    if not _HOST_RE.match(host or ""):
        host = request.url.netloc
    return f"{scheme}://{host}"


@lru_cache(maxsize=16)
def _gzipped(lang: str, origin: str, jurisdiction: str) -> bytes:
    """The landing document, pre-compressed once per variant and host.

    Streamlit's own gzip middleware sits *inside* this module's, and the gate
    answers before reaching it, so compressing here is what keeps a ~90KB
    document from going out uncompressed. mtime is zeroed to keep the bytes
    reproducible.
    """
    body = landing_static.document(lang, origin, jurisdiction).encode("utf-8")
    return gzip.compress(body, compresslevel=9, mtime=0)


def landing_response(
    request: Request, lang: str, jurisdiction: str | None = None
) -> Response:
    """The landing document for one variant, gzipped when the client takes it."""
    origin = base_url(request)
    accepts_gzip = "gzip" in request.headers.get("accept-encoding", "").lower()
    jur = jurisdiction or landing.jurisdiction_for(lang)
    root = request.url.path == PATH_EN

    headers = {"Vary": "Accept-Encoding" + (", Cookie" if root else "")}
    if root:
        # `/` answers with two different documents depending on the cookie, so
        # it must be revalidated rather than reused from the browser cache —
        # otherwise the click that sets the cookie would still land on the
        # landing. Every other variant has a path of its own, no cookie split,
        # and can simply be cached.
        headers["Cache-Control"] = "no-cache"
    else:
        headers["Cache-Control"] = "public, max-age=300"

    if accepts_gzip:
        headers["Content-Encoding"] = "gzip"
        return Response(
            _gzipped(lang, origin, jur), media_type=_HTML, headers=headers
        )
    return Response(
        landing_static.document(lang, origin, jur),
        media_type=_HTML,
        headers=headers,
    )


def _wants_landing(request: Request) -> bool:
    """True when this request for `/` should get the pitch instead of the app.

    No query parameter and no app cookie — a first-time visitor, or a crawler,
    which sends neither. `?landing=1` asks for it explicitly.
    """
    if request.method not in ("GET", "HEAD"):
        return False
    if PARAM_LANDING in request.query_params:
        return True
    if request.query_params:
        return False  # a CTA click, a ?ticker= deep link, an OIDC return
    return request.cookies.get(APP_COOKIE) != "1"


def _request_origin(request: Request) -> str:
    """The origin this request actually arrived on, ignoring the override."""
    scheme = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        or request.url.scheme
    )
    host = (
        request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        or request.headers.get("host", "").strip()
    )
    if not _HOST_RE.match(host or ""):
        host = request.url.netloc
    return f"{scheme}://{host}"


def canonical_redirect(request: Request) -> str | None:
    """Where this request should have gone, when it arrived on a stray host.

    One site, one hostname: without this, every alias Cloud Run answers on
    serves a full copy of the landing that canonicalizes to itself, which is
    duplicate content in the most literal sense. GET/HEAD only, and never for
    `/_stcore/` — redirecting a live websocket or an XHR would break the
    session a visitor is already in rather than move it. The OIDC callback is
    exempt for the same reason from the other end: Google sends the browser to
    the exact URI registered with it, and bouncing that response to another
    hostname lands the login on an origin the round trip did not start on.
    """
    if request.method not in ("GET", "HEAD"):
        return None
    origin = public_origin()
    if origin is None or origin == _request_origin(request):
        return None
    if request.url.path.startswith(_NEVER_REDIRECT):
        return None
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{origin}{request.url.path}{query}"


def _is_known_path(path: str) -> bool:
    """True for a path something actually serves.

    Streamlit's static mount answers anything it does not recognise with the
    app shell and a 200, which makes every typo and every stale link a soft 404
    — indexed as nothing, reported in Search Console as a problem, and crawled
    forever. The set below is the whole surface: this module's own pages, the
    app's pages (derived from `app_pages/`, so a new one needs no edit here),
    and Streamlit's own endpoints.
    """
    path = path.rstrip("/") or "/"
    if path == PATH_EN or _is_marketing_path(path + "/") or _is_marketing_path(path):
        return True
    if path in seo.app_page_paths() or path in seo.APP_PATHS:
        return True
    return any(path.startswith(pre) or path + "/" == pre for pre in seo.APP_PREFIXES)


def not_found(request: Request) -> Response:
    """A real 404, with just enough page to be worth landing on."""
    home = PATH_ES if request.url.path.startswith(PATH_ES) else PATH_EN
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="robots" content="noindex">'
        "<title>404 — TopStocks</title>"
        f'<style>body{{background:{seo.THEME_COLOR};color:#fff;font:16px/1.6 '
        "system-ui,sans-serif;display:grid;place-content:center;height:100vh;"
        "margin:0;text-align:center}a{color:#fff}</style></head><body>"
        f'<h1>404</h1><p>Nothing at this address. <a href="{home}">TopStocks</a></p>'
        "</body></html>"
    )
    return Response(body, status_code=404, media_type=_HTML,
                    headers={"X-Robots-Tag": "noindex"})


# Requests one client may make per window before it is turned away. Counted
# per document, not per asset: one page load pulls dozens of Streamlit bundles
# and every websocket frame is a chat message, so metering those would either
# lock out a normal first visit or have to be set so high it meters nothing.
# Documents are the expensive part anyway — a landing render, an app shell.
CLIENT_MAX_DOCS = 60
CLIENT_WINDOW_S = 60

# Not metered: the transport Streamlit needs to keep a session alive (the app
# has its own per-account limit on what arrives over it, see
# web/ratelimit.py's use in chat_core), the mirrored logos and landing assets,
# and the probes an uptime monitor hits on a schedule.
_UNMETERED = ("/_stcore/", "/static/", ASSET_BASE, "/livez", "/healthz",
              "/favicon", "/app/static/")

# How many proxies sit in front of this process. Cloud Run's frontend appends
# its own hop to X-Forwarded-For, so the client is the entry before the last.
# Behind a second proxy (a CDN in front of Cloud Run) it is two before, and
# with no proxy at all the socket peer is the client.
TRUSTED_PROXY_HOPS = 1


def _trusted_hops() -> int:
    try:
        return max(0, int(os.environ.get("TRUSTED_PROXY_HOPS",
                                         TRUSTED_PROXY_HOPS)))
    except ValueError:
        return TRUSTED_PROXY_HOPS


def client_ip(request: Request) -> str:
    """The caller's address as far as it can be trusted.

    X-Forwarded-For is client-supplied up to the first proxy that appends to
    it, so only the entries our own infrastructure wrote mean anything: with
    one trusted hop, the last entry is Cloud Run's frontend and the one before
    it is what that frontend saw. Everything to the left of that a client can
    write itself.

    A determined attacker still has as many "addresses" as it has real ones,
    which is why this is a speed bump in front of the account-level limits,
    not the thing keeping anyone honest.
    """
    parts = [p.strip() for p in
             request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    hops = _trusted_hops()
    if parts and hops and len(parts) > hops:
        return parts[-(hops + 1)]
    if parts and not hops:
        return parts[0]
    return request.client.host if request.client else "unknown"


class ClientThrottle(BaseHTTPMiddleware):
    """Per-client burst limit on document requests, outermost in the stack.

    The service runs at --max-instances 1, so one container answers everyone:
    a script hammering `/` does not just cost egress, it takes the app away
    from real users. The app's own limiter (web/ratelimit.py) only starts
    after a Google sign-in, which leaves everything before the login unmetered
    — this is that half.

    Fails open. A limiter that starts refusing traffic because of a bug in
    itself is worse than the flood it was added to stop.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _UNMETERED):
            return await call_next(request)
        try:
            key = f"http::{client_ip(request)}"
            allowed = ratelimit.allow(key, max_events=CLIENT_MAX_DOCS,
                                      window_s=CLIENT_WINDOW_S)
        except Exception:
            return await call_next(request)
        if not allowed:
            wait = ratelimit.retry_after(key, window_s=CLIENT_WINDOW_S)
            obs.warn("http.throttled", path=path, retry_after=wait)
            return Response("Too many requests\n", status_code=429,
                            media_type="text/plain; charset=utf-8",
                            headers={"Retry-After": str(max(1, wait))})
        return await call_next(request)


class SecurityHeaders(BaseHTTPMiddleware):
    """Baseline hardening headers on every response, marketing and app alike.

    Deliberately not a full Content-Security-Policy: Streamlit's shell relies
    on inline scripts/styles and a websocket, so a source allowlist would
    either break the app or be wide enough to mean nothing. What is set here
    is the uncontroversial floor:

    * `nosniff` — responses execute as their declared type only.
    * `frame-ancestors 'self'` (+ the legacy X-Frame-Options) — nobody frames
      the app on another origin to clickjack a logged-in session. Streamlit's
      own component iframes are same-origin and unaffected.
    * a tight Referrer-Policy — app URLs can carry tickers and view state;
      other origins get the origin, not the path.
    * HSTS, only when the request already arrived on TLS (Cloud Run
      terminates it and forwards the scheme): a plain-HTTP local dev run
      must not teach the browser to refuse http://localhost.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy",
                     "camera=(), microphone=(), geolocation=()")
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        if (proto or request.url.scheme) == "https":
            h.setdefault("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
        return response


class LandingGate(BaseHTTPMiddleware):
    """Serves the landing at `/`, and keeps the app out of search indexes.

    Middleware rather than a route because `/` has to be able to fall through:
    Streamlit owns that path for its default page and there is no way to hand a
    request back to a route once it has been claimed.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (target := canonical_redirect(request)) is not None:
            return RedirectResponse(target, status_code=301)
        if path == PATH_EN and _wants_landing(request):
            return landing_response(request, "en")
        if not _is_known_path(path):
            return not_found(request)

        response = await call_next(request)

        if not _is_marketing_path(path):
            # Everything that is not the marketing site is the app: a shell
            # over private positions, or transport. Out of the index, and the
            # browser is now known to have been there.
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            if request.cookies.get(APP_COOKIE) != "1":
                response.set_cookie(
                    APP_COOKIE,
                    "1",
                    max_age=_APP_COOKIE_MAX_AGE,
                    path="/",
                    httponly=True,
                    samesite="lax",
                )
        return response


async def variant_landing(request: Request) -> Response:
    """Any landing path but `/`: the language and jurisdiction it stands for.

    One handler for all of them — the path *is* the variant (landing.VARIANTS),
    so another pair needs a route and no new code.
    """
    lang, jur = variant_for(request.url.path) or ("es", "ES")
    return landing_response(request, lang, jur)


async def variant_redirect(request: Request) -> Response:
    """`/es` -> `/es/`: one canonical address per variant, permanently."""
    path = request.url.path
    target = next(
        (p for p in LANDING_PATHS if p.rstrip("/") == path.rstrip("/")), PATH_ES
    )
    return RedirectResponse(target, status_code=301)


def _landing_sources() -> list[Path]:
    """The files whose content *is* the landing page."""
    sources = [_HERE / "landing.py", _HERE / "landing_static.py", _HERE / "seo.py"]
    return sources + sorted(_HERE.glob("locales/*/landing.json"))


def _git_lastmod(sources: list[Path]) -> str | None:
    """The newest commit date among `sources`, when this is a git checkout.

    Preferred over mtime because mtime is a property of the filesystem, not of
    the content: a fresh clone (or any CI build) stamps every file with the
    checkout time and would have the sitemap claim the copy changed today.
    Deployed images carry no `.git`, so this returns None there and the mtime
    path below takes over — correct, because the source upload preserves the
    times the files really had.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", *[str(p) for p in sources]],
            cwd=_HERE, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    date = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    return date


@lru_cache(maxsize=1)
def _lastmod() -> str:
    """The landing's own modification date, for the sitemap.

    The markup, the copy and the metadata are the page, so their newest change
    is the honest answer — and it beats a build timestamp (which would claim a
    change on every redeploy) or a hard-coded date (which would go stale
    silently). Git first, mtime second; see `_git_lastmod`.
    """
    sources = _landing_sources()
    if (from_git := _git_lastmod(sources)) is not None:
        return from_git
    stamps = [p.stat().st_mtime for p in sources if p.exists()]
    newest = max(stamps) if stamps else 0
    return datetime.fromtimestamp(newest, UTC).strftime("%Y-%m-%d")


async def robots(request: Request) -> Response:
    return Response(
        seo.robots_txt(base_url(request)),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def sitemap(request: Request) -> Response:
    return Response(
        seo.sitemap_xml(base_url(request), lastmod=_lastmod()),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def healthz(request: Request) -> Response:
    """Liveness for uptime checks: the ASGI stack answers, nothing deeper.

    Served at both /livez and /healthz. /livez is the one to probe from
    outside: on Cloud Run a request for /healthz is answered by Google's
    frontend with its own 404 and never reaches the container (no
    x-cloud-trace-context on the response, no entry in the request log),
    so the route below is unreachable in production. /healthz is kept
    because it does work everywhere else — local runs, the Docker
    HEALTHCHECK, any other host.

    Deliberately no storage or market-data round trip — this runs once a
    minute from several regions, and a dependency blip should page through
    the error-rate alert (which sees the real user impact), not by taking
    the whole service "down" in the uptime check.
    """
    return Response(
        '{"status":"ok"}',
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


_BOOTED = datetime.now(UTC)


async def status(request: Request) -> Response:
    """`/status` — a shade more than liveness, still zero round trips.

    What it adds over `/healthz`: which revision is answering, how long this
    container has been up (a suspiciously young uptime during an incident
    means crash-looping), and whether persistence is configured. Deliberately
    no storage or market-data probe — this must stay cheap enough to curl in
    a loop mid-incident. See docs/RUNBOOK.md.
    """
    from stocks import storage

    body = {
        "status": "ok",
        "revision": os.getenv("K_REVISION", "dev"),
        "uptime_s": int((datetime.now(UTC) - _BOOTED).total_seconds()),
        "storage": storage.enabled(),
    }
    return Response(
        json.dumps(body),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


async def legal_page(request: Request) -> Response:
    doc = request.path_params["doc"]
    if doc not in ("privacy", "terms"):
        return not_found(request)
    lang = request.query_params.get("lang", "en")
    return Response(
        legal.document(doc, lang),
        media_type=_HTML,
        headers={"Cache-Control": "public, max-age=3600", "Vary": "Accept-Encoding"},
    )


async def asset(request: Request) -> Response:
    """`/lp/<file>` — the landing's brand mark and share card.

    Its own mount rather than Streamlit's `app/static`: that one is served at a
    path relative to wherever the app document lives, which `/es/` is one
    segment away from, and these files have to resolve from both pages.
    """
    name = request.path_params.get("path", "")
    target = (_ASSETS / name).resolve()
    if _ASSETS.resolve() not in target.parents or not target.is_file():
        return Response("Not found", status_code=404, media_type="text/plain")
    return FileResponse(
        target, headers={"Cache-Control": "public, max-age=3600"}
    )


routes = [
    # Every landing variant but `/`, which the gate answers so Streamlit can
    # keep owning that path (see LandingGate).
    *(
        Route(p, variant_landing, methods=["GET", "HEAD"])
        for p in LANDING_PATHS
        if p != PATH_EN
    ),
    *(
        Route(p.rstrip("/"), variant_redirect, methods=["GET", "HEAD"])
        for p in LANDING_PATHS
        if p != PATH_EN
    ),
    Route("/robots.txt", robots, methods=["GET", "HEAD"]),
    Route("/sitemap.xml", sitemap, methods=["GET", "HEAD"]),
    Route("/livez", healthz, methods=["GET", "HEAD"]),
    Route("/healthz", healthz, methods=["GET", "HEAD"]),
    Route("/status", status, methods=["GET", "HEAD"]),
    Route("/legal/{doc:str}", legal_page, methods=["GET", "HEAD"]),
    Route(f"{ASSET_BASE}{{path:path}}", asset, methods=["GET", "HEAD"]),
]

# The script path is absolute on purpose: `streamlit run` resolves a relative
# one against this module, but an external ASGI server (or a test importing
# this module) would resolve it against the working directory instead.
app = st.App(
    str(_HERE / "app.py"),
    routes=routes,
    # First is outermost: the security headers wrap everything, including the
    # gate's own short-circuit responses (landing, redirects, 404s).
    middleware=[Middleware(ClientThrottle), Middleware(SecurityHeaders),
                Middleware(LandingGate)],
)
