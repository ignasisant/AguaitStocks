#!/usr/bin/env python
"""Draw the landing page's share card and the touch icon. Run when they change.

    uv run scripts/make_og_card.py

Writes `src/stocks/web/assets/og.png` (1200x630, the size Open Graph, Twitter/X,
LinkedIn, WhatsApp and Slack all render at 1.91:1 without re-cropping) and
`apple-touch-icon.png` (180x180). Both are committed: they change only when the
brand does, and a container that generated them at boot would need the fonts.

The card carries no sentence — one image serves both the English and Spanish
page, so the copy is language-neutral tokens (FIFO, EUR, IRPF, MIT) that read
the same either way.

Fonts come from the Google Fonts repository, the same faces the pages load.
Without a network the system fallback keeps this runnable; the committed PNGs
were rendered with the real ones.
"""

from __future__ import annotations

import io
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "stocks" / "web" / "assets"

W, H = 1200, 630

# .streamlit/config.toml, and widgets.py's --ag-* tokens.
BG = (24, 22, 28)  # neutral-950
CARD = (40, 38, 45)  # neutral-900
BORDER = (59, 57, 66)  # neutral-800
TEXT = (249, 249, 250)  # neutral-50
MUTED = (130, 127, 140)  # neutral-500
PURPLE = (127, 63, 232)  # brand purple 600
PURPLE_LIGHT = (169, 142, 247)  # purple 500
GREEN = (219, 255, 210)  # success text on dark

_FONT_URLS = {
    "display": "https://raw.githubusercontent.com/google/fonts/main/ofl/epilogue/Epilogue%5Bwght%5D.ttf",
    "body": "https://raw.githubusercontent.com/google/fonts/main/ofl/instrumentsans/InstrumentSans%5Bwdth,wght%5D.ttf",
}
_SYSTEM_FALLBACK = "/System/Library/Fonts/Supplemental/Arial.ttf"


def _load(kind: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    """A font at `size`, from Google Fonts if reachable, else the system one."""
    data = _CACHE.get(kind)
    if data is None:
        try:
            with urllib.request.urlopen(_FONT_URLS[kind], timeout=20) as r:
                data = r.read()
        except OSError as exc:
            print(f"  ! {kind} font unavailable ({exc}); using the system face")
            data = Path(_SYSTEM_FALLBACK).read_bytes()
        _CACHE[kind] = data
    font = ImageFont.truetype(io.BytesIO(data), size)
    if weight is not None:
        try:
            font.set_variation_by_axes([weight])
        except (OSError, AttributeError):
            pass  # static fallback face — no axes to set
    return font


_CACHE: dict[str, bytes] = {}


def _glow(img: Image.Image) -> None:
    """A purple bloom behind the wordmark — the landing's hero, in one gesture.

    Drawn as concentric translucent ellipses on an overlay rather than with a
    blur filter: fewer pixels touched, and the falloff is explicit.
    """
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    cx, cy = 250, 210
    for i in range(26, 0, -1):
        r = i * 26
        alpha = int(3 + (26 - i) * 0.9)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*PURPLE, alpha))
    img.alpha_composite(overlay)


def _mark(d: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    """The brand tile: rounded purple square with an upward tick."""
    d.rounded_rectangle((x, y, x + size, y + size), radius=size // 4, fill=PURPLE)
    pad = size * 0.24
    pts = [
        (x + pad, y + size - pad),
        (x + size * 0.42, y + size * 0.52),
        (x + size * 0.6, y + size * 0.66),
        (x + size - pad, y + pad),
    ]
    d.line(pts, fill=(255, 255, 255), width=max(3, size // 14), joint="curve")


def _chips(d: ImageDraw.ImageDraw, labels: list[str], x: int, y: int) -> None:
    """Language-neutral capability chips along the bottom."""
    font = _load("body", 26, weight=600)
    for label in labels:
        w = int(d.textlength(label, font=font))
        d.rounded_rectangle((x, y, x + w + 44, y + 56), radius=28, fill=CARD,
                            outline=BORDER, width=1)
        d.text((x + 22, y + 15), label, font=font, fill=MUTED)
        x += w + 44 + 14


def _sparkline(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    """A portfolio curve in a card — the right half, so the layout is not blank.

    Fixed sample points rather than random ones: the card is committed to the
    repo, so it has to render identically on every run.
    """
    series = [0.10, 0.18, 0.14, 0.30, 0.26, 0.44, 0.52, 0.47, 0.68, 0.75, 0.92]
    d.rounded_rectangle((x, y, x + w, y + h), radius=24, fill=CARD,
                        outline=BORDER, width=1)
    label = _load("body", 22, weight=500)
    d.text((x + 26, y + 18), "Portfolio, EUR", font=label, fill=MUTED)

    plot_x, plot_y = x + 26, y + 54
    plot_w, plot_h = w - 52, h - 78
    pts = [
        (plot_x + plot_w * i / (len(series) - 1), plot_y + plot_h * (1 - v))
        for i, v in enumerate(series)
    ]
    # Baseline first, then the curve over it — the same order the app's charts
    # paint in, so the grid never sits on top of the data.
    d.line((plot_x, plot_y + plot_h, plot_x + plot_w, plot_y + plot_h),
           fill=BORDER, width=1)
    d.line(pts, fill=PURPLE_LIGHT, width=5, joint="curve")
    last = pts[-1]
    d.ellipse((last[0] - 8, last[1] - 8, last[0] + 8, last[1] + 8), fill=GREEN)


def build_card() -> Image.Image:
    img = Image.new("RGBA", (W, H), (*BG, 255))
    _glow(img)
    d = ImageDraw.Draw(img)

    _mark(d, 80, 96, 88)
    d.text((196, 104), "TopStocks", font=_load("display", 84, weight=800), fill=TEXT)

    d.text(
        (82, 250),
        "Your real return, in euros.",
        font=_load("display", 62, weight=700),
        fill=TEXT,
    )
    d.text(
        (82, 330),
        "Broker statement in. FIFO positions, EUR P/L at the ECB rate,",
        font=_load("body", 34, weight=400),
        fill=MUTED,
    )
    d.text(
        (82, 376),
        "portfolio risk and your Spanish IRPF figures out.",
        font=_load("body", 34, weight=400),
        fill=MUTED,
    )

    # One number, in the app's own colour language: the point of the product.
    num_font = _load("display", 46, weight=700)
    d.text((82, 452), "+47.7%", font=num_font, fill=GREEN)
    d.text(
        (82 + int(d.textlength("+47.7%", font=num_font)) + 18, 464),
        "vs SPY, in EUR",
        font=_load("body", 28, weight=500),
        fill=MUTED,
    )

    _chips(d, ["FIFO", "EUR", "IRPF", "MIT licence"], 82, 534)
    # Bottom right: the one clear corner. Level with the chips, clear of the
    # headline above and the copy to its left.
    _sparkline(d, 730, 446, 390, 144)

    # A purple hairline along the top edge, matching the landing's own rules.
    d.rectangle((0, 0, W, 5), fill=PURPLE_LIGHT)
    return img.convert("RGB")


def build_icon() -> Image.Image:
    """180x180 touch icon: the tile alone, on the page background."""
    size = 180
    img = Image.new("RGBA", (size, size), (*BG, 255))
    d = ImageDraw.Draw(img)
    _mark(d, 22, 22, size - 44)
    return img.convert("RGB")


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    card = ASSETS / "og.png"
    icon = ASSETS / "apple-touch-icon.png"
    build_card().save(card, "PNG", optimize=True)
    build_icon().save(icon, "PNG", optimize=True)
    for p in (card, icon):
        print(f"  wrote {p.relative_to(ROOT)} ({p.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
