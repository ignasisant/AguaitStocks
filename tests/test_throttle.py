"""The per-client burst limit in front of the Streamlit app."""

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from stocks.web import ratelimit, server


@pytest.fixture(autouse=True)
def empty_counters():
    ratelimit._events.clear()
    yield
    ratelimit._events.clear()


@pytest.fixture
def client():
    async def stub(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/{path:path}", stub)],
                    middleware=[Middleware(server.ClientThrottle)])
    return TestClient(app, base_url="https://topstocks.example")


def _request(headers=None, client=("10.0.0.1", 1234), path="/"):
    scope = {"type": "http", "method": "GET", "path": path, "client": client,
             "headers": [(k.lower().encode(), v.encode())
                         for k, v in (headers or {}).items()],
             "query_string": b"", "scheme": "https",
             "server": ("topstocks.example", 443)}
    return Request(scope)


# ------------------------------------------------------------------ the ip


def test_the_client_is_the_hop_before_our_own_frontend(monkeypatch):
    # Cloud Run appends its frontend to X-Forwarded-For, so the real caller is
    # the entry before the last one.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    r = _request({"x-forwarded-for": "203.0.113.7, 169.254.1.1"})
    assert server.client_ip(r) == "203.0.113.7"


def test_a_forged_prefix_cannot_move_the_client(monkeypatch):
    # A client writing its own X-Forwarded-For only adds entries to the left
    # of the ones our infrastructure appended; those are what get read.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    forged = _request({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 203.0.113.7, 169.254.1.1"})
    assert server.client_ip(forged) == "203.0.113.7"


def test_two_proxies_are_configurable(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    r = _request({"x-forwarded-for": "203.0.113.7, 198.51.100.9, 169.254.1.1"})
    assert server.client_ip(r) == "203.0.113.7"


def test_without_a_proxy_the_socket_peer_is_the_client(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
    assert server.client_ip(_request()) == "10.0.0.1"


def test_a_short_header_falls_back_to_the_peer(monkeypatch):
    # One entry with one trusted hop means the frontend's own address is all
    # there is; guessing the client from it would be wrong.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    assert server.client_ip(_request({"x-forwarded-for": "169.254.1.1"})) == "10.0.0.1"


def test_a_nonsense_hop_count_does_not_break_the_read(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "not a number")
    assert server.client_ip(_request({"x-forwarded-for": "203.0.113.7, 169.254.1.1"}))


# ---------------------------------------------------------------- throttling


def _get(client, path="/", ip="203.0.113.7"):
    return client.get(path, headers={"x-forwarded-for": f"{ip}, 169.254.1.1"})


def test_a_normal_visit_is_never_touched(client):
    for _ in range(server.CLIENT_MAX_DOCS):
        assert _get(client).status_code == 200


def test_a_flood_is_turned_away_with_a_retry_after(client):
    for _ in range(server.CLIENT_MAX_DOCS):
        _get(client)
    r = _get(client)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= server.CLIENT_WINDOW_S


def test_one_flooder_does_not_lock_out_everyone_else(client):
    for _ in range(server.CLIENT_MAX_DOCS + 5):
        _get(client, ip="203.0.113.7")
    assert _get(client, ip="198.51.100.4").status_code == 200


def test_the_transport_streamlit_needs_is_not_metered(client):
    # Every websocket frame is a chat message; metering them here would fight
    # the per-account limit that already covers them, and drop live sessions.
    for _ in range(server.CLIENT_MAX_DOCS * 2):
        assert _get(client, path="/_stcore/stream").status_code == 200


def test_assets_are_not_metered(client):
    for _ in range(server.CLIENT_MAX_DOCS * 2):
        assert _get(client, path="/app/static/logo.png").status_code == 200


def test_probes_are_not_metered(client):
    for _ in range(server.CLIENT_MAX_DOCS * 2):
        assert _get(client, path="/livez").status_code == 200


def test_a_broken_limiter_lets_traffic_through(client, monkeypatch):
    # Fail open: a limiter refusing traffic because of its own bug is worse
    # than the flood it was added to stop.
    monkeypatch.setattr(ratelimit, "allow",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _get(client).status_code == 200
