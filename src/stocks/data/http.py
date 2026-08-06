"""Shared HTTP helpers — stdlib urllib with a User-Agent and timeout.

Every data source in this package (EDGAR, frankfurter, FMP, logos) is a plain
GET with a UA header; this is the single copy of that boilerplate.
"""

from __future__ import annotations

import json
import urllib.error
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


def probe_image(url: str, *, user_agent: str = DEFAULT_UA, timeout: float = 6) -> str:
    """Whether `url` serves an image, as seen from THIS host.

    "ok"      — 200 with an image content type.
    "dead"    — definitively not an image: hard 404/410, or a 2xx serving
                something else (an HTML placeholder page).
    "blocked" — inconclusive: 403/429/5xx, timeout or network error. Logo
                CDNs routinely reject datacenter IPs, so callers must treat
                this as "unknown from here", never as "gone".
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            return "ok" if ctype.startswith("image") else "dead"
    except urllib.error.HTTPError as e:
        return "dead" if e.code in (404, 410) else "blocked"
    except Exception:
        return "blocked"
