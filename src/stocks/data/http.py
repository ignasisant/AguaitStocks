"""Shared HTTP helpers — stdlib urllib with a User-Agent and timeout.

Every data source in this package (EDGAR, frankfurter, FMP, logos) is a plain
GET with a UA header; this is the single copy of that boilerplate.
"""

from __future__ import annotations

import json
import urllib.request

DEFAULT_UA = "stocks-toolkit"


def get_bytes(url: str, *, user_agent: str = DEFAULT_UA, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, *, user_agent: str = DEFAULT_UA, timeout: float = 30) -> dict:
    return json.loads(get_bytes(url, user_agent=user_agent, timeout=timeout))


def get_bytes_and_type(
    url: str, *, user_agent: str = DEFAULT_UA, timeout: float = 30
) -> tuple[bytes, str]:
    """GET returning (body, content type) — for callers that store the body
    under a type-derived file extension (e.g. the logo mirror)."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def url_is_image(url: str, *, user_agent: str = DEFAULT_UA, timeout: float = 6) -> bool:
    """True if url returns a 200 with an image content type."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            return resp.status == 200 and ctype.startswith("image")
    except Exception:
        return False
