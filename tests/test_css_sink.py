"""No stylesheet may reach Streamlit's shared style sink.

A style-ONLY `st.html()` is hoisted out of the page into one shared container
(`_RootContainer.EVENT`) whose children are addressed by position, and a
fragment rerun writes that container from the cursor it captured when the
fragment was declared. The slot it lands on belongs to whichever stylesheet
the full script run put there — which is how the assistant's launcher, and
later the whole assistant panel, lost `position: fixed` and fell into the page
flow. `stocks.web.css.inject` keeps every block in the flow instead; this
guards the rule so a plain `st.html(_SOME_CSS)` can never creep back.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from streamlit.elements.html import _html_only_style_tags

from stocks.web import css
from stocks.web.widgets import ds_vars_css

WEB = Path("src/stocks/web")


def test_inject_payload_is_never_style_only():
    """Both shapes `inject` accepts stay in the page flow."""
    for block in ("body {color: red;}", "<style>body {color: red;}</style>"):
        payload = css._MARKER + css._HIDE + (
            block if "<style" in block else f"<style>{block}</style>"
        )
        assert not _html_only_style_tags(payload)


def test_injected_css_hides_its_own_container():
    """In-flow means an element container; it must cost no space."""
    assert "display:none" in css._HIDE
    assert "ts-inline-css" in css._MARKER


def _top_level_strings(tree: ast.Module) -> dict[str, str]:
    out = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _resolve(arg: ast.expr, consts: dict[str, str]) -> str | None:
    """The argument's string value, when it can be known without running the app.

    Pages are scripts — importing one renders it — so module-level constants are
    read from source rather than by import. `ds_vars_css()` is the one call that
    returns a bare stylesheet, and it is safe to call.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
        try:
            module = importlib.import_module(f"stocks.web.{arg.value.id}")
        except ImportError:
            return None
        value = getattr(module, arg.attr, None)
        return value if isinstance(value, str) else None
    if (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Name)
        and arg.func.id == "ds_vars_css"
        and not arg.args
    ):
        return ds_vars_css()
    return None


@pytest.mark.parametrize("path", sorted(WEB.rglob("*.py")), ids=lambda p: str(p))
def test_no_style_only_st_html(path: Path):
    tree = ast.parse(path.read_text())
    consts = _top_level_strings(tree)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "html"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "st"
            and node.args
        ):
            continue
        body = _resolve(node.args[0], consts)
        if body is None:
            continue
        assert not _html_only_style_tags(body), (
            f"{path}:{node.lineno} passes style-only content to st.html(); "
            "use stocks.web.css.inject() so it keeps its own delta path"
        )


def test_the_guard_would_catch_a_regression():
    """The check above is only worth its lines if it fails on the bad shape."""
    assert _html_only_style_tags("<style>body {color: red;}</style>")

def test_markup_helpers_never_degenerate_to_style_only():
    """The `st.html()` calls this guard cannot resolve statically are the
    markup builders (KPI grids, ticker tables). Empty input is the one shape
    that could leave a builder emitting its stylesheet alone — which would put
    it in the shared sink after all."""
    import pandas as pd

    from stocks.web.widgets import kpi_grid_html, stacked_table_html, ticker_table_html

    empty = pd.DataFrame()
    assert not _html_only_style_tags(kpi_grid_html([]))
    assert not _html_only_style_tags(ticker_table_html(empty))
    assert not _html_only_style_tags(stacked_table_html(empty, title="ticker"))
