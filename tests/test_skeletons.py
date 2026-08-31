"""Skeleton shapes, slot lifecycle, and the two invariants they replace.

The visual result can't be asserted here, but the rules that keep it correct
can: every shape stays inside the styled wrapper, the style block never trips
the DOMPurify trap, a slot is always resolved (a stranded shimmer is the one
way this can look broken), and no spinner survives anywhere in the web layer.
"""

import re
from pathlib import Path

import pytest

from stocks.web import skeletons

WEB = Path(__file__).resolve().parents[1] / "src" / "stocks" / "web"

# One representative call per shape, with the kwargs the pages actually pass.
SHAPES = [
    ("text", {"lines": 4}),
    ("metrics", {"n": 4}),
    ("metrics", {"n": (4, 3)}),
    ("chart", {"height": 380, "legend": True}),
    ("chart", {"shape": "bars", "bars": 30}),
    ("chart", {"shape": "pie"}),
    ("chart", {"shape": "heatmap", "cells": 6}),
    ("table", {"rows": 5, "cols": 4}),
    ("rows", {"rows": 8}),
    ("cards", {"n": 3, "lines": 6}),
    ("calendar", {"weeks": 4, "cols": 5, "cell": 62}),
    ("pills", {"n": 5}),
]


@pytest.mark.parametrize(("kind", "kw"), SHAPES)
def test_every_shape_is_a_tagged_shimmer(kind, kw):
    out = skeletons.html(kind, **kw)
    # The wrapper carries the palette and the animation; a block outside it
    # renders as an invisible transparent box.
    assert out.startswith('<div class="topstocks-sk')
    assert 'class="skb' in out
    assert out.count("<div") == out.count("</div>")


@pytest.mark.parametrize(("kind", "kw"), SHAPES)
def test_shapes_are_deterministic(kind, kw):
    """Bar heights and line widths come from fixed cycles, not a random draw —
    a rerun must repaint the same silhouette, not reshuffle it."""
    assert skeletons.html(kind, **kw) == skeletons.html(kind, **kw)


@pytest.mark.parametrize(("kind", "kw"), SHAPES)
def test_title_option_prepends_exactly_one_heading_bar(kind, kw):
    """`title` is the only knob that draws a heading — a shape that also drew
    its own would stack two bars over one subheader."""
    assert "sk-ctitle" not in skeletons.html(kind, **kw)
    assert skeletons.html(kind, title=True, **kw).count("sk-ctitle") == 1


def test_unknown_kind_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="unknown skeleton"):
        skeletons.html("chart-ish")


def test_style_block_has_no_left_angle_bracket():
    """DOMPurify silently drops a whole style block whose text contains one,
    taking every skeleton's palette and animation with it (same trap the base
    style block in app.py is annotated for)."""
    body = skeletons.CSS.split("<style>")[1].split("</style>")[0]
    assert "<" not in body


def test_every_class_the_shapes_emit_is_styled():
    """An unstyled block is a transparent nothing — the skeleton renders as a
    blank gap and reads as a broken page, not a loading one."""
    classes = set()
    for kind, kw in SHAPES:
        markup = skeletons.html(kind, title=True, **kw)
        for attr in re.findall(r'class="([^"]+)"', markup):
            classes.update(attr.split())
    # Anchored so a renamed rule (.sk-donut -> .sk-donut-x) reads as missing
    # rather than as a substring hit.
    unstyled = {
        c for c in classes
        if not re.search(rf"\.{re.escape(c)}(?![-\w])", skeletons.CSS)
    }
    assert not unstyled


def test_every_design_token_the_css_reads_is_defined():
    """The palette, radii and rules come from widgets' `--ag-*` custom
    properties. A var() naming a token that table doesn't emit resolves to
    nothing: no shimmer gradient, no borders, invisible skeletons."""
    from stocks.web.widgets import ds_vars_css

    defined = set(re.findall(r"--ag-([a-z0-9-]+):", ds_vars_css()))
    used = set(re.findall(r"var\(--ag-([a-z0-9-]+)\)", skeletons.CSS))
    assert used and used <= defined


def test_css_is_injected_once_at_the_entry_point():
    """Shapes assume the style block is already on the page; nothing injects
    it per call, so app.py has to."""
    assert "st.html(skeletons.CSS)" in (WEB / "app.py").read_text()


def test_table_falls_back_to_dense_rows_on_phones(monkeypatch):
    """ticker_table_html renders a wide grid on desktop and two-line rows on
    phones — the placeholder has to fork the same way or the swap relayouts."""
    monkeypatch.setattr(skeletons, "_mobile", lambda: True)
    assert skeletons.html("table", rows=3, cols=4) == skeletons.html("rows", rows=3)
    monkeypatch.setattr(skeletons, "_mobile", lambda: False)
    assert "sk-trow" in skeletons.html("table", rows=3, cols=4)


# --------------------------------------------------------------- slot lifecycle


class FakePlaceholder:
    """Stand-in for the st.empty() a slot draws into."""

    def __init__(self):
        self.content = None
        self.emptied = False

    def html(self, markup):
        self.content = markup

    def container(self, **kw):
        self.content = ("container", kw)
        return self

    def empty(self):
        self.emptied = True
        self.content = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_st(monkeypatch):
    holder = FakePlaceholder()
    fake = type("st", (), {"empty": staticmethod(lambda: holder)})
    monkeypatch.setattr(skeletons, "st", fake)
    return holder


def test_reserve_draws_the_shimmer_and_leaves_the_slot_open(fake_st):
    box = skeletons.reserve("metrics", n=3)
    assert "topstocks-sk" in fake_st.content
    assert not box.resolved


def test_container_and_clear_both_resolve(fake_st):
    box = skeletons.reserve()
    box.container(border=True)
    assert box.resolved and fake_st.content == ("container", {"border": True})

    box2 = skeletons.reserve()
    box2.clear()
    assert box2.resolved and fake_st.emptied


def test_slot_clears_a_placeholder_nobody_filled(fake_st):
    """An early return past the fill is the realistic bug — a fetch that came
    back empty leaving the shimmer sweeping forever."""
    with skeletons.slot("chart"):
        pass
    assert fake_st.emptied


def test_slot_leaves_filled_content_alone(fake_st):
    with skeletons.slot("chart") as box:
        box.container()
    assert not fake_st.emptied


def test_slot_clears_when_the_body_raises(fake_st):
    with pytest.raises(RuntimeError), skeletons.slot("chart"):
        raise RuntimeError("throttled")
    assert fake_st.emptied


def test_no_spinner_survives_in_the_web_layer():
    """The whole point: loading state is a skeleton in the shape of what's
    coming, everywhere. A spinner reintroduced anywhere fails here."""
    offenders = []
    for path in sorted(WEB.rglob("*.py")):
        src = path.read_text()
        if "st.spinner(" in src:
            offenders.append(f"{path.name}: st.spinner")
        # show_spinner=False is the required form; a string or True is a spinner.
        for match in re.finditer(r"show_spinner=(?!False)(\S+)", src):
            offenders.append(f"{path.name}: show_spinner={match.group(1)}")
    assert not offenders
