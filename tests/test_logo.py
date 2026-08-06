"""Logo domain parsing and same-origin mirroring — pure, no network."""

import pytest

import stocks.data.logo as logo_mod
from stocks.data.logo import domain_from_website


@pytest.mark.parametrize(
    ("website", "expected"),
    [
        ("https://www.apple.com", "apple.com"),
        ("http://microsoft.com/", "microsoft.com"),
        ("https://investor.nvidia.com/home", "investor.nvidia.com"),
        ("nvidia.com", "nvidia.com"),
        ("www.tesla.com", "tesla.com"),
        ("", None),
        (None, None),
    ],
)
def test_domain_from_website(website, expected):
    assert domain_from_website(website) == expected


def test_mirror_logo_downloads_once_then_serves_from_disk(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(logo_mod, "logo_url", lambda t: "https://x/AAPL.png")

    def fake_get(url, **kw):
        calls.append(url)
        return b"png-bytes", "image/png"

    monkeypatch.setattr(logo_mod, "get_bytes_and_type", fake_get)

    assert logo_mod.mirror_logo("aapl", tmp_path) == "AAPL.png"
    assert (tmp_path / "AAPL.png").read_bytes() == b"png-bytes"
    # Second call finds the file — the logo host is contacted exactly once.
    assert logo_mod.mirror_logo("AAPL", tmp_path) == "AAPL.png"
    assert len(calls) == 1


def test_mirror_logo_unresolved_and_failed_download(tmp_path, monkeypatch):
    monkeypatch.setattr(logo_mod, "logo_url", lambda t: None)
    assert logo_mod.mirror_logo("ZZZZ", tmp_path) is None

    monkeypatch.setattr(logo_mod, "logo_url", lambda t: "https://x/y.png")

    def boom(url, **kw):
        raise OSError("network down")

    monkeypatch.setattr(logo_mod, "get_bytes_and_type", boom)
    assert logo_mod.mirror_logo("ZZZZ", tmp_path) is None
    assert not any(tmp_path.iterdir())  # nothing half-written


def test_mirror_brand_downloads_once_under_brand_stem(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        logo_mod, "brand_logo_url", lambda d: f"https://logo.clearbit.com/{d}"
    )

    def fake_get(url, **kw):
        calls.append(url)
        return b"png-bytes", "image/png"

    monkeypatch.setattr(logo_mod, "get_bytes_and_type", fake_get)

    name = logo_mod.mirror_brand("trading212", "trading212.com", tmp_path)
    assert name == "brand-trading212.png"
    # Second call serves from disk; brand stem can't collide with a ticker.
    assert logo_mod.mirror_brand("trading212", "trading212.com", tmp_path) == name
    assert len(calls) == 1


def test_mirror_brand_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(logo_mod, "brand_logo_url", lambda d: None)
    assert logo_mod.mirror_brand("generic", "example.com", tmp_path) is None


def test_brand_logo_url_cached_with_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(logo_mod, "LOGO_CACHE", tmp_path / "logos.json")
    checks = []

    def fake_ok(url):
        checks.append(url)
        return "clearbit" in url

    monkeypatch.setattr(logo_mod, "_url_ok", fake_ok)

    url = logo_mod.brand_logo_url("degiro.com")
    assert url == "https://logo.clearbit.com/degiro.com"
    # Second call answers from the on-disk cache under the brand: prefix.
    assert logo_mod.brand_logo_url("degiro.com") == url
    assert len(checks) == 1
    cache = logo_mod._load_cache()
    assert cache["brand:degiro.com"] == url
