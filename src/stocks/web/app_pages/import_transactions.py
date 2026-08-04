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

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from stocks.portfolio import last_import, platforms
from stocks.portfolio.ledger import add_many, all_transactions, clear, delete_many
from stocks.portfolio.validate import known_tickers, validate
from stocks.web import auth
from stocks.web.widgets import ticker_table_html

# Imports write the personal ledger — no anonymous access.
auth.require_login()

st.title("Import transactions")

# Everything on this page reads/writes the session user's own book.
paths = auth.user_paths()

st.caption(
    "Pick the platform, then upload its statement. Rows are parsed and "
    "validated; nothing is written until you commit. Duplicates against the "
    "ledger are flagged, not removed — wipe first for a clean re-import of an "
    "overlapping export."
)

ledger = all_transactions(paths.db)
if st.session_state.pop("imports_cleared", False):
    st.toast("All imports cleared — the ledger is empty.", icon=":material/delete_forever:")

with st.container(horizontal=True, vertical_alignment="center"):
    st.metric("Transactions currently in ledger", len(ledger))
    with st.popover(
        "Clear all imports", icon=":material/delete_forever:", disabled=not ledger
    ):
        st.markdown(
            f"Delete **all {len(ledger)} transactions** from your ledger? "
            "Positions, realized gains and tax reports all derive from it, so "
            "the portfolio resets to empty — as if nothing was ever imported. "
            "This cannot be undone."
        )
        if st.button("Delete everything", type="primary", icon=":material/delete_forever:"):
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
    st.html(
        ticker_table_html(
            frame,
            fmt=_TX_FMT,
            ticker_col="ticker" if rich else None,
            left_cols=_TX_LEFT + ("warnings", "errors"),
        )
    )


platform = st.segmented_control(
    "Importing from",
    platforms.PLATFORMS,
    format_func=lambda p: p.label,
    default=platforms.PLATFORMS[0],
    required=True,  # clicking the active segment must not deselect it
)

# Key the uploader by platform so switching platforms drops the staged file —
# a statement must never be parsed by another platform's parser.
uploaded = st.file_uploader(
    f"{platform.label} statement ({', '.join(t.upper() for t in platform.file_types)})",
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
    st.subheader("Last import")
    when = datetime.fromisoformat(record.imported_at).strftime("%Y-%m-%d %H:%M UTC")
    st.markdown(
        f"**{record.filename}** ({platforms.by_key(record.platform).label}) — "
        f"{len(record.tx_ids)} transactions committed {when}"
        + (" (ledger wiped first)" if record.wiped else "")
    )

    batch_ids = set(record.tx_ids)
    still_in_ledger = [t for t in ledger if t.id in batch_ids]
    if len(still_in_ledger) < len(record.tx_ids):
        st.caption(
            f"{len(record.tx_ids) - len(still_in_ledger)} of those rows are no "
            "longer in the ledger (deleted or wiped since)."
        )
    if still_in_ledger:
        with st.expander(f"Imported rows still in ledger ({len(still_in_ledger)})"):
            _tx_table(_tx_frame(still_in_ledger))

    with st.container(horizontal=True):
        if st.button(
            f"Clear last import ({len(still_in_ledger)} rows)",
            icon=":material/delete:",
            disabled=not still_in_ledger,
        ):
            delete_many(record.tx_ids, paths.db)
            last_import.forget(paths.last_import)
            st.rerun()
        if st.button("Dismiss record (keep transactions)", icon=":material/close:"):
            last_import.forget(paths.last_import)
            st.rerun()
    st.caption(
        "**Clear last import** deletes exactly these rows from the ledger — no "
        "re-upload needed. **Dismiss** only forgets this note; the ledger stays."
    )
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
        f"No rows parsed — is this a {platform.label} statement? {platform.hint}"
    )
    st.stop()

# The wipe decision must precede validation: duplicate flags, split-ratio
# derivation and the oversell replay all read the prior ledger. Validating
# against rows that are about to be wiped rejects sells whose "missing" buys
# are merely doubled, and makes real split ratios underivable.
wipe = st.checkbox("Wipe ledger before importing (clean re-import)")

with st.spinner("Validating tickers, dates and quantities…"):
    validation = validate(
        result,
        [] if wipe else ledger,
        known=known_tickers(paths.watchlist, paths.db),
        lookup=_ticker_exists,
    )

st.subheader(f"Preview — {validation.summary}")

importable = validation.importable
if importable:
    _tx_table(_tx_frame(importable))
else:
    st.warning("No importable rows survived validation.")

if validation.flagged:
    st.warning(f"{len(validation.flagged)} rows import with warnings — review:")
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
    st.error(f"{len(validation.rejected)} rows rejected — quarantined, not imported:")
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
    st.caption(
        "Fix these in the export, or add them by hand from a terminal with the "
        "bundled CLI: `uv run stocks tx add <date> <ticker> <action> --qty … "
        "--price …` (run `uv run stocks tx add --help` in the repo folder)."
    )

if result.skipped:
    with st.expander(
        f"Skipped rows ({len(result.skipped)}) — by design, nothing lost"
    ):
        st.dataframe(pd.DataFrame(result.skipped), hide_index=True)
        if platform.key == "revolut":
            st.caption(
                "Cash movements, rewards and dividend-tax corrections don't affect "
                "positions. Splits are auto-resolved when the ratio is derivable; "
                "underivable ones stay here — add those manually."
            )
        else:
            st.caption(
                "Each row lists why it couldn't become a transaction — fix the "
                "CSV and re-upload, or add those rows manually."
            )

st.divider()
if st.button("Commit to ledger", type="primary", disabled=not importable):
    if wipe:
        clear(paths.db)
    ids = add_many(importable, paths.db)
    last_import.save(
        last_import.ImportRecord(
            filename=uploaded.name,
            imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            tx_ids=ids,
            wiped=wipe,
            platform=platform.key,
        ),
        paths.last_import,
    )
    st.success(
        f"Imported {len(ids)} transactions. "
        f"Ledger now holds {len(all_transactions(paths.db))}."
    )
    st.caption(
        "Committed rows persist in the ledger across reloads — no need to "
        "re-upload. Open **Portfolio** for positions & tax, or **Overview** to "
        "see your buys/sells on the price chart."
    )
