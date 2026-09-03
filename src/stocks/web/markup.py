"""Primitives for turning raw strings into something a destination accepts.

Most of the app renders through Streamlit widgets, which escape for us. The
landing page, the legal documents and the SEO head build markup by hand and
hand it to `st.html` / the static writer — and there `esc` is the only thing
between page copy and an injection. It lived three times over, once per
module, which is two chances too many for it to drift.

`slug` is the same idea one layer over: a Streamlit widget key may not carry
whatever a ticker or a broker code happens to contain.

Keep this module free of Streamlit: the static landing writer and the SEO
head render outside a script run.
"""

from __future__ import annotations

import html
import re


def esc(value: object) -> str:
    """HTML-escape anything bound for hand-built markup, quotes included.

    `quote=True` is not optional here: these callers interpolate into
    attributes (`content="…"`, `href="…"`) as often as into text.
    """
    return html.escape(str(value), quote=True)


def slug(s: str) -> str:
    """A string safe to build a Streamlit widget key from."""
    return re.sub(r"\W+", "_", s)
