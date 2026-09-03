"""Jurisdiction plumbing for the web layer: which country, which words.

The Portfolio page renders whatever the active jurisdiction's `TaxPeriod`
returns (`kpis()`, `notes()`, reporting flags) — it never branches on a country
code. That works because catalog keys follow one convention:

    portfolio.<code>_<name>   e.g. portfolio.us_estimated_tax_help
    portfolio.<name>          the neutral fallback

`key()` picks the most specific string that exists, so shipping a new
jurisdiction means adding a module under `stocks.portfolio.tax` plus whatever
`portfolio.<code>_*` copy reads better than the neutral default. Nothing here
decides tax; it decides wording, currency symbols and where the setting lives.
"""

from __future__ import annotations

from dataclasses import replace

import streamlit as st

from stocks.portfolio import tax
from stocks.web import auth, i18n

# prefs.json keys. tax_residence None = auto (from the browser locale's region).
PREF_RESIDENCE = "tax_residence"
PREF_FILING_STATUS = "tax_filing_status"
PREF_OTHER_INCOME = "tax_other_income"
PREF_NIIT = "tax_niit"
PREF_CHURCH_TAX = "tax_church_rate"
PREF_SUBNATIONAL = "tax_subnational_rate"

AUTO = "auto"


def region_of(locale: str | None) -> str | None:
    """Region subtag of a browser locale: 'en-US' -> 'US', 'es' -> None."""
    if not locale:
        return None
    parts = str(locale).replace("_", "-").split("-")
    return parts[1].upper() if len(parts) > 1 and len(parts[1]) == 2 else None


def resolve_code(prefs: dict | None = None) -> str:
    """Active jurisdiction: Profile preference > browser region > default.

    Mirrors how the language resolves, with one difference: an unknown region
    lands on Spain rather than on nothing, because the ledger has to be taxed
    under *some* set of rules and this app's home jurisdiction is Spain.
    """
    p = prefs if prefs is not None else auth.load_prefs()
    stored = p.get(PREF_RESIDENCE)
    if stored and stored != AUTO:
        return tax.normalize(stored)
    region = region_of(getattr(st.context, "locale", None))
    if region and region in tax.JURISDICTIONS:
        return region
    return tax.DEFAULT_CODE


def settings(prefs: dict | None = None) -> tax.TaxSettings:
    """The filer's bracket inputs, straight from prefs.json."""
    p = prefs if prefs is not None else auth.load_prefs()
    try:
        income = float(p.get(PREF_OTHER_INCOME) or 0.0)
    except (TypeError, ValueError):
        income = 0.0
    try:
        church = float(p.get(PREF_CHURCH_TAX) or 0.0)
    except (TypeError, ValueError):
        church = 0.0
    try:
        subnational = float(p.get(PREF_SUBNATIONAL) or 0.0)
    except (TypeError, ValueError):
        subnational = 0.0
    return tax.TaxSettings(
        filing_status=str(p.get(PREF_FILING_STATUS) or "single"),
        other_income=income,
        include_niit=bool(p.get(PREF_NIIT)),
        church_tax_rate=church,
        subnational_rate=subnational,
    )


def with_funds(settings: tax.TaxSettings, tickers) -> tax.TaxSettings:
    """`settings` with the fund tickers among `tickers` classified.

    Germany exempts 30% of an equity fund's result, so the engine has to know
    which holdings are funds — and it must not guess: an unclassified book is
    computed without the exemption and says so. The classification comes from
    the learned quoteType cache (data.funds), never a live fetch, so a cold
    cache degrades to "not classified" instead of blocking the page.
    """
    from stocks.data.funds import is_fund

    return replace(
        settings,
        fund_tickers=frozenset(
            t.upper() for t in tickers if is_fund(t, fetch=False)
        ),
    )


def jurisdiction(prefs: dict | None = None) -> tax.Jurisdiction:
    return tax.get(resolve_code(prefs))


# ------------------------------------------------------------------- wording


def key(code: str, name: str) -> str:
    """`portfolio.<code>_<name>` when that string exists, else the neutral one."""
    specific = f"portfolio.{code.lower()}_{name}"
    return specific if i18n.has(specific) else f"portfolio.{name}"


def t(code: str, name: str, /, **kwargs) -> str:
    """Translate a tax string for `code`, most specific wording first."""
    return i18n.t(key(code, name), **kwargs)


def label(code: str) -> str:
    """The jurisdiction's own name, e.g. "Spain (IRPF)" — for selectors."""
    return i18n.t(f"profile.tax_residence_{code.lower()}")


def symbol(currency: str) -> str:
    return auth.CURRENCY_SYMBOL.get(currency.upper(), f"{currency} ")


def money(value: float, currency: str, *, signed: bool = False) -> str:
    """A whole-unit amount with its currency symbol: "€1,240", "$-3,000"."""
    fmt = "+,.0f" if signed else ",.0f"
    return f"{symbol(currency)}{value:{fmt}}"


def flag_caption(code: str, flag: tax.ReportingFlag, currency: str) -> str:
    """One localized line for a reporting threshold, crossed or not.

    Wording comes from `portfolio.<code>_flag_<name>` (plus a `_reportable` /
    `_ok` variant for the inner clause). A jurisdiction that ships a flag with
    no copy of its own still renders, through the neutral `flag_default` set —
    a threshold nobody worded is better shown generically than as a raw key.
    """
    name = f"flag_{flag.name}"
    if not (
        i18n.has(f"portfolio.{code.lower()}_{name}") or i18n.has(f"portfolio.{name}")
    ):
        name = "flag_default"
    inner = t(
        code,
        f"{name}_reportable" if flag.reportable else f"{name}_ok",
        val=money(flag.total_value, currency),
        threshold=money(flag.threshold, currency),
    )
    return t(code, name, message=inner)
