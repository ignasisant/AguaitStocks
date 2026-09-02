"""The landing page as a standalone HTML document.

`landing` builds the markup, `seo` builds the metadata, and this module is the
document that puts them together — the thing `server.py` writes to the socket
for `GET /` and `GET /es/`.

Why a document at all, when the same markup used to render inside the app: a
Streamlit page cannot own its `<head>`. No script run can set a title, a
description, a canonical URL or an Open Graph image, because by the time the
script runs the browser has already been served Streamlit's own shell. It also
cannot be read without JavaScript, and the shell has to boot a websocket before
the first pixel of copy appears. Serving the bytes directly fixes all of that at
once: the copy is in the response, the metadata is in the head, and the app
starts only when a visitor asks for it.

Two details that follow from being static:

* **Everything is inline.** One request, no render-blocking asset of our own —
  the tokens, the stylesheet and the reveal script all ship in the document.
  The only external request is the font stylesheet, preconnected in `seo.head`.
* **The phone check moved into the page.** The app version asked
  `widgets.is_mobile()` on the server; a cached static document cannot, so the
  User-Agent block ships disabled (`media="not all"`) and a one-line script
  enables it for phone User-Agents before the first paint. The width-driven
  640px block in the stylesheet is unaffected and does the real work.
"""

from __future__ import annotations

from functools import lru_cache

from stocks.web import landing, seo
from stocks.web.i18n import DEFAULT_LANG, LANGUAGES
from stocks.web.widgets import ds_vars_css

# The reset Streamlit used to provide. Only what the design assumes: no page
# gutter, and the page colour painted over the full viewport (`.ag-l` colours
# itself, but a short page would otherwise show white below the footer).
_RESET = """
<style>
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--ag-surface-page);
    color: var(--ag-text-primary);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
</style>
"""

# Flip the User-Agent block on for phones. Inline and blocking, in the head,
# ahead of the markup: a class toggled after first paint would reflow the page
# in front of the reader. Wrapped in a try/catch it can't need, because a throw
# here would take the rest of the head's parsing with it.
_UA_SCRIPT = """
<script>
  try {
    if (/Mobi|Android|iP(hone|od|ad)/i.test(navigator.userAgent)) {
      var s = document.getElementById("ag-ua-mobile");
      if (s) s.media = "all";
    }
  } catch (e) {}
</script>
"""


def _styles() -> str:
    """Design tokens, the reset, the stylesheet, then the phone override.

    Source order is the cascade: `landing.ua_mobile_rules()` repeats the phone
    block at a wider breakpoint and has to be able to win, so it goes last.
    """
    return (
        ds_vars_css()
        + _RESET
        + landing.stylesheet()
        + f'<style id="ag-ua-mobile" media="not all">{landing.ua_mobile_rules()}</style>'
        + _UA_SCRIPT
    )


@lru_cache(maxsize=8)
def document(lang: str, base_url: str, jurisdiction: str | None = None) -> str:
    """The full page for `lang`, with absolute URLs under `base_url`.

    Cached: the markup is a pure function of the language, the host and the tax
    jurisdiction it argues from, all fixed for the life of a deployment, and
    the alternative is rebuilding ~90KB of string per request. `maxsize` leaves
    room for the languages times the handful of hostnames one service answers
    on. `jurisdiction` defaults to the language's own (landing.render_language).
    """
    code = lang if lang in LANGUAGES else DEFAULT_LANG
    with landing.render_language(code, jurisdiction):
        body = landing.page_body()
        script = landing.bar_script()
        head = seo.head(code, base_url, extra_styles=_styles())
    return (
        "<!doctype html>"
        f'<html lang="{code}">'
        f"<head>{head}</head>"
        f"<body>{body}{script}</body>"
        "</html>"
    )
