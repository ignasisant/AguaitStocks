"""Import transactions from a broker statement into the ledger.

The user picks the source platform (see portfolio/platforms.py for the
registry — Revolut CSV/PDF, generic ledger-format CSV, …); parsing is
delegated to that platform, everything downstream is shared.

Flow: pick platform -> upload -> parse (no writes) -> validate ->
tiered preview -> commit.

The preview separates rows into three tiers so a bad export can't corrupt
cost basis silently:

* importable — parsed clean; committed on button press
* warnings   — importable but flagged (unknown ticker, possible duplicate)
* rejected   — failed validation (future date, oversell, malformed ticker);
  quarantined, never committed. Fix the export or add them manually.

Rows the parser skips by design (cash movements, fees, tax corrections) are
listed separately; stock splits are auto-resolved to a ratio when the held
quantity at the split date makes the ratio unambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from stocks.portfolio import last_import, platforms
from stocks.portfolio.ledger import add_many, all_transactions, clear, delete_many
from stocks.portfolio.validate import known_tickers, validate
from stocks.web import auth, skeletons
from stocks.web.i18n import t as tr
from stocks.web.widgets import (
    brand_logo,
    data_table,
    is_mobile,
    stacked_table_html,
    ticker_table_html,
)

# Imports write the personal ledger — no anonymous access.
auth.require_login()

st.title(tr("import.title"))

# Everything on this page reads/writes the session user's own book.
paths = auth.user_paths()

st.caption(tr("import.intro_caption"))

ledger = all_transactions(paths.db)
if st.session_state.pop("imports_cleared", False):
    st.toast(tr("import.toast_cleared"), icon=":material/delete_forever:")

with st.container(horizontal=True, vertical_alignment="center"):
    st.metric(tr("import.metric_in_ledger"), len(ledger))
    with st.popover(
        tr("import.clear_all_imports"),
        icon=":material/delete_forever:",
        disabled=not ledger,
    ):
        st.markdown(tr("import.clear_all_confirm", n=len(ledger)))
        if st.button(
            tr("import.delete_everything"),
            type="primary",
            icon=":material/delete_forever:",
        ):
            clear(paths.db)
            last_import.forget(paths.last_import)
            st.session_state["imports_cleared"] = True
            st.rerun()


def _tx_frame(txs) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": t.date, "ticker": t.ticker, "action": t.action,
            "quantity": t.quantity, "price": t.price, "fee": t.fee,
            "currency": t.currency, "note": t.note,
        }
        for t in txs
    )


# Shared Positions-style table look for the previews.
_TX_FMT = {"quantity": "{:,.4f}", "price": "{:,.2f}", "fee": "{:,.2f}"}
_TX_LEFT = ("date", "action", "currency", "note")


def _tx_table(frame: pd.DataFrame, *, rich: bool = True) -> None:
    # rich=False skips the logo/name lookup — rejected rows carry malformed
    # symbols, and resolving each one costs a network round-trip.
    # Eight columns pan off a phone. With a resolvable symbol the preview goes
    # dense (quantity + price on the right, date/action/notes on the wrapping
    # dim line); rejected rows have no symbol to hang a dense row off, so they
    # stack as label/value cards instead.
    if not rich and is_mobile():
        st.html(stacked_table_html(frame, title="ticker", fmt=_TX_FMT))
        return
    st.html(
        ticker_table_html(
            frame,
            fmt=_TX_FMT,
            ticker_col="ticker" if rich else None,
            left_cols=_TX_LEFT + ("warnings", "errors"),
            mobile={
                "value": "quantity",
                "delta": "price",
                "sub": ("date", "action")
                + tuple(
                    c for c in ("note", "warnings", "errors") if c in frame.columns
                ),
                "wrap": True,
            },
        )
    )


def _platform_option_md(key: str) -> str:
    """Segmented-control label: brand logo (markdown image) + name."""
    p = platforms.by_key(key)
    src = brand_logo(p.key, p.domain)
    img = f"![{p.label}]({src}) " if src else ""
    return f"{img}{p.label}"


# Options are keys, not Platform objects: Streamlit's default-value check
# converts a dataclass default via its dataframe logic (exploding it into
# field values), which raises "default not part of the options".
platform = platforms.by_key(
    st.segmented_control(
        tr("import.importing_from"),
        [p.key for p in platforms.PLATFORMS],
        format_func=_platform_option_md,
        default=platforms.PLATFORMS[0].key,
        required=True,  # clicking the active segment must not deselect it
    )
)

# Key the uploader by platform so switching platforms drops the staged file —
# a statement must never be parsed by another platform's parser.
uploaded = st.file_uploader(
    tr(
        "import.uploader_label",
        platform=platform.label,
        types=", ".join(t.upper() for t in platform.file_types),
    ),
    type=list(platform.file_types),
    key=f"upload_{platform.key}",
)
if uploaded is None:
    record = last_import.load(paths.last_import)
    if record is None:
        st.info(platform.hint)
        st.stop()

    # Committed imports live in the ledger (SQLite) — nothing to re-upload.
    # Show what the last commit did and offer to undo exactly that batch.
    st.subheader(tr("import.last_import"))
    when = datetime.fromisoformat(record.imported_at).strftime("%Y-%m-%d %H:%M UTC")
    st.markdown(
        tr(
            "import.last_import_summary",
            filename=record.filename,
            platform=platforms.by_key(record.platform).label,
            n=len(record.tx_ids),
            when=when,
        )
        + (tr("import.ledger_wiped_suffix") if record.wiped else "")
    )

    batch_ids = set(record.tx_ids)
    still_in_ledger = [t for t in ledger if t.id in batch_ids]
    if len(still_in_ledger) < len(record.tx_ids):
        st.caption(
            tr("import.rows_no_longer", n=len(record.tx_ids) - len(still_in_ledger))
        )
    if still_in_ledger:
        with st.expander(tr("import.imported_rows_still", n=len(still_in_ledger))):
            _tx_table(_tx_frame(still_in_ledger))

    with st.container(horizontal=True):
        if st.button(
            tr("import.clear_last_import", n=len(still_in_ledger)),
            icon=":material/delete:",
            disabled=not still_in_ledger,
        ):
            delete_many(record.tx_ids, paths.db)
            last_import.forget(paths.last_import)
            st.rerun()
        if st.button(tr("import.dismiss_record"), icon=":material/close:"):
            last_import.forget(paths.last_import)
            st.rerun()
    st.caption(tr("import.last_import_help"))
    st.stop()


@st.cache_data(ttl=86400, show_spinner=False)
def _ticker_exists(ticker: str) -> bool | None:
    """Live yfinance existence check for symbols the EDGAR map doesn't know."""
    try:
        import yfinance as yf

        return bool(yf.Ticker(ticker).fast_info.get("lastPrice"))
    except Exception:
        return None  # network down ≠ ticker invalid


result = platform.parse(uploaded.name, uploaded.getvalue())

if not result.transactions and not result.skipped:
    st.error(
        tr("import.no_rows_parsed", platform=platform.label, hint=platform.hint)
    )
    st.stop()

# The wipe decision must precede validation: duplicate flags, split-ratio
# derivation and the oversell replay all read the prior ledger. Validating
# against rows that are about to be wiped rejects sells whose "missing" buys
# are merely doubled, and makes real split ratios underivable.
wipe = st.checkbox(tr("import.wipe_checkbox"))

# Validation re-derives split ratios and replays every sell against the prior
# ledger, and looks unknown symbols up live — seconds on a full statement. The
# preview shimmers as the table it is about to become, heading included, so
# the commit button below keeps its place on the page.
_preview = skeletons.reserve("table", rows=6, cols=5, title=True)
validation = validate(
    result,
    [] if wipe else ledger,
    known=known_tickers(paths.watchlist, paths.db),
    lookup=_ticker_exists,
)

importable = validation.importable
with _preview.container():
    st.subheader(tr("import.preview", summary=validation.summary))
    if importable:
        _tx_table(_tx_frame(importable))
    else:
        st.warning(tr("import.no_importable"))

if validation.flagged:
    st.warning(tr("import.rows_with_warnings", n=len(validation.flagged)))
    _tx_table(
        pd.DataFrame(
            {
                "date": c.tx.date, "ticker": c.tx.ticker, "action": c.tx.action,
                "quantity": c.tx.quantity, "price": c.tx.price,
                "warnings": "; ".join(i.message for i in c.warnings),
            }
            for c in validation.flagged
        )
    )

if validation.rejected:
    st.error(tr("import.rows_rejected", n=len(validation.rejected)))
    _tx_table(
        pd.DataFrame(
            {
                "date": c.tx.date, "ticker": c.tx.ticker, "action": c.tx.action,
                "quantity": c.tx.quantity, "price": c.tx.price,
                "errors": "; ".join(i.message for i in c.errors),
            }
            for c in validation.rejected
        ),
        rich=False,
    )
    st.caption(tr("import.rejected_help"))

if result.skipped:
    with st.expander(tr("import.skipped_rows", n=len(result.skipped))):
        data_table(pd.DataFrame(result.skipped), hide_index=True)
        if platform.key == "revolut":
            st.caption(tr("import.skipped_caption_revolut"))
        else:
            st.caption(tr("import.skipped_caption_generic"))

# Attribution. Every broker parser stamps its own name as the note's first
# word, which is what the Fees and Custody views read the book by
# (fees.broker_of); a generic ledger CSV can come from anywhere and stamps
# nothing, so its origin is asked for here and written in on commit. Required:
# a batch imported unattributed shows up in those views under whatever its
# notes happened to start with, and is tedious to fix afterwards.
origin = platforms.detected_broker(importable)
if importable and not origin:
    origin = st.selectbox(
        tr("import.broker"),
        platforms.broker_options(),
        index=None,
        format_func=lambda k: (
            tr("import.broker_other") if k == platforms.OTHER
            else platforms.broker_label(k)
        ),
        placeholder=tr("import.broker_pick"),
        accept_new_options=True,  # naming the real broker beats "other"
        help=tr("import.broker_help"),
    ) or ""

st.divider()
if st.button(tr("import.commit_button"), type="primary",
             disabled=not importable or not origin):
    if wipe:
        clear(paths.db)
    ids = add_many(platforms.stamp_broker(importable, origin), paths.db)
    last_import.save(
        last_import.ImportRecord(
            filename=uploaded.name,
            imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tx_ids=ids,
            wiped=wipe,
            platform=platform.key,
        ),
        paths.last_import,
    )
    st.success(
        tr(
            "import.commit_success",
            n=len(ids),
            total=len(all_transactions(paths.db)),
        )
    )
    st.caption(tr("import.commit_help"))
