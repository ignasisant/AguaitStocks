"""One rule for every stylesheet this app injects: keep it in the page flow.

Streamlit 1.60 hoists a style-ONLY `st.html()` out of the page and into a
single shared sink (`_RootContainer.EVENT`, see `streamlit/elements/html.py`
— issue #9388, so a stylesheet costs no vertical space). That sink addresses
its children BY POSITION, and a fragment rerun restores the delta cursors it
captured when the fragment was *declared* (`runtime/fragment.py`: the
`cursors_snapshot` deepcopy). So the slot a fragment writes on rerun is the
slot the full script run had handed to whatever stylesheet came next — and
that stylesheet is silently replaced.

Two real outages came out of that: the search dropdown's logo rules
overwriting the assistant launcher's stylesheet (the fixed top-right FAB fell
into the page flow), then the same collision landing on the assistant panel
itself — the whole conversation rendered inline above the page instead of in
the fixed right rail.

`inject()` sidesteps the sink entirely: a marker span makes the payload
"not style-only", so it keeps its own delta path in the main container like
any other element, and the first rule hides that element container so it
costs no height and no flex gap. A `<style>` inside a `display: none`
subtree still applies — style elements are never laid out.

The payload must contain no raw "<" beyond the style tags themselves:
DOMPurify's mXSS guard drops a whole style block whose text holds one (no
error, no console warning). Reword comments, and percent-encode SVG data
URIs (`%3Csvg`).
"""

from __future__ import annotations

import streamlit as st

# Marker + the rule that hides the container the marker sits in. Emitted with
# every block (idempotent — the rule is the same one every time).
_MARKER = '<span class="ts-inline-css"></span>'
_HIDE = (
    "<style>"
    '[data-testid="stElementContainer"]:has(.ts-inline-css)'
    "{display:none !important;}"
    "</style>"
)


def inject(block: str) -> None:
    """Emit a stylesheet that stays where the script wrote it.

    `block` is either bare CSS or a complete `<style>...</style>` block (both
    shapes exist across the app); a bare one is wrapped here. Never call
    `st.html()` with style-only content directly — see the module docstring.
    """
    css = block if "<style" in block.lower() else f"<style>{block}</style>"
    st.html(_MARKER + _HIDE + css)
