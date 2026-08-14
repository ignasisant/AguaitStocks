"""Command-line interface: `stocks update | alerts | dashboard`."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC
from pathlib import Path


def cmd_update(args: argparse.Namespace) -> None:
    from stocks.config import tickers as watchlist_tickers
    from stocks.data.fetch import fetch_many, save_history

    tickers = watchlist_tickers()
    frames = fetch_many(tickers, period=args.period)  # one bulk download
    for t in tickers:
        df = frames.get(t)
        if df is None:
            print(f"{t}: no data")
            continue
        path = save_history(t, df)
        print(f"{t}: {len(df)} rows -> {path}")


def cmd_alerts(args: argparse.Namespace) -> None:
    if args.all_users:
        from stocks.notify.fanout import run_alerts_fanout

        status = run_alerts_fanout()
        if not status:
            print("no subscribers with alerts")
            return
        for label, result in status.items():
            print(f"{label}: {result}")
        return

    from stocks.notify.alerts import check_all

    lines = [str(h) for h in check_all()]

    if args.earnings_days:
        from stocks.data.earnings import upcoming

        for e in upcoming(within_days=args.earnings_days):
            lines.append(f"EARNINGS {e.ticker} in {e.days_until}d ({e.date})")

    if not lines:
        print("no alerts triggered")
        return

    if args.deliver:
        from stocks.notify.deliver import deliver

        status = deliver(lines, subject="Stock alerts")
        print("\ndelivery: " + ", ".join(f"{k}={v}" for k, v in status.items()))
    else:
        for line in lines:
            print(f"ALERT {line}")


def cmd_digest(args: argparse.Namespace) -> None:
    if args.all_users:
        from stocks.notify.digest import run_digest_fanout

        status = run_digest_fanout(dry_run=args.dry_run)
        if not status:
            print("no digest subscribers")
            return
        for label, result in status.items():
            print(f"{label}: {result}")
        return

    # Single-user smoke path: owner root files, env TELEGRAM_CHAT_ID channel.
    import os

    from stocks.config import DATA_DIR, WATCHLIST_FILE
    from stocks.notify.digest import compute_digest_data, render_digest

    data = compute_digest_data(WATCHLIST_FILE, DATA_DIR / "portfolio.db")
    text = render_digest(data, "en")
    if args.dry_run or not os.getenv("TELEGRAM_CHAT_ID"):
        print(text)
        return
    from stocks.notify import telegram

    telegram.send_message(text, os.environ["TELEGRAM_CHAT_ID"], parse_mode="HTML")
    print("digest sent")


def _parse_kv(items: list[str] | None) -> list[tuple[str, float]]:
    """Parse repeated 'metric=value' filter args into (metric, value) pairs."""
    pairs = []
    for item in items or []:
        key, sep, val = item.partition("=")
        if not sep:
            raise SystemExit(f"filter must be metric=value, got {item!r}")
        pairs.append((key.strip(), float(val)))
    return pairs


def cmd_screen(args: argparse.Namespace) -> None:
    from stocks.analysis.screener import (
        DEFAULT_COLUMNS,
        Filter,
        apply_filters,
        fetch_metrics_many,
        format_frame,
        metrics_frame,
        rank,
    )
    from stocks.config import tickers as watchlist_tickers

    metrics = fetch_metrics_many(watchlist_tickers())
    df = metrics_frame(metrics)

    filters = [Filter(k, "min", v) for k, v in _parse_kv(args.min)]
    filters += [Filter(k, "max", v) for k, v in _parse_kv(args.max)]
    df = apply_filters(df, filters)

    if args.sort:
        df = rank(df, args.sort, ascending=args.asc)
    if args.top:
        df = df.head(args.top)
    if not args.all:
        df = df[[c for c in DEFAULT_COLUMNS if c in df.columns]]

    if df.empty:
        print("no tickers pass the screen")
        return
    print(format_frame(df).to_string())
    print(f"\n{len(df)} tickers. Percent thresholds are fractions (0.15 = 15%).")


def cmd_earnings(args: argparse.Namespace) -> None:
    from stocks.data.earnings import upcoming

    events = upcoming(within_days=args.days)
    if not events:
        print(f"no earnings in the next {args.days} days")
        return
    print(f"Upcoming earnings (next {args.days} days):")
    for e in events:
        print(f"  {e.date}  T-{e.days_until:>3}d  {e.ticker}")


def cmd_portfolio(args: argparse.Namespace) -> None:
    from stocks.analysis.portfolio import (
        effective_positions,
        top_n_weight,
    )

    rep = _portfolio_report(args.period)
    weights = rep.weights
    print(f"Portfolio ({len(weights)} names, {args.period} window)\n")

    print("Risk:")
    print(f"  Annualised return : {rep.cagr * 100:6.1f}%")
    print(f"  Annualised vol    : {rep.volatility * 100:6.1f}%")
    print(f"  Max drawdown      : {rep.max_drawdown * 100:6.1f}%")
    for b in rep.bench_returns:
        print(f"  Beta vs {b:<4}      : {rep.beta_vs(b):6.2f}")

    print("\nConcentration:")
    print(f"  Top 5 weight      : {top_n_weight(weights, 5) * 100:6.1f}%")
    print(f"  Effective names   : {effective_positions(weights):6.1f}")

    for key in ("sector", "country", "currency"):
        alloc = rep.allocation(key)
        print(f"\nAllocation by {key}:")
        for label, w in alloc.items():
            print(f"  {label:<28} {w * 100:5.1f}%")


def _portfolio_report(period: str):
    from stocks.analysis.portfolio import analyze

    return analyze(period=period)


def cmd_search(args: argparse.Namespace) -> None:
    from stocks.data.edgar import search_companies

    matches = search_companies(" ".join(args.query), limit=args.limit)
    if not matches:
        print("no matches in the SEC ticker map (US listings only)")
        return
    for ticker, name in matches:
        print(f"{ticker:8s} {name}")


def cmd_favorites(args: argparse.Namespace) -> None:
    from stocks.config import favorites

    favs = favorites()
    if not favs:
        print("no favorites (set `favorite: true` on a watchlist entry)")
        return
    for h in favs:
        print(f"⭐ {h.ticker:8s} {h.name}")


def cmd_tv(args: argparse.Namespace) -> None:
    from stocks.data.tradingview import (
        DEFAULT_TIMEFRAMES,
        INTRADAY_INTERVALS,
        consensus,
        consensus_multi,
    )

    ticker = args.ticker.upper()
    miss = (
        f"{ticker}: no TradingView data "
        "(map non-US names under `tv:` in watchlist.yaml; "
        "install with `pip install 'stocks[tv]'`)"
    )

    def fmt(c) -> str:
        return (
            f"  {c.interval:<4} {c.recommendation or 'n/a':<11} "
            f"score {c.score:+.2f}  (buy {c.buy} / neu {c.neutral} / sell {c.sell})"
            f"  MA {c.ma or '-'} OSC {c.osc or '-'}"
        )

    if args.multi:
        frames = consensus_multi(ticker)
        if not frames:
            print(miss)
            return
        print(f"{ticker} TradingView consensus (multi-timeframe):")
        for iv in DEFAULT_TIMEFRAMES:
            if iv in frames:
                print(fmt(frames[iv]))
        return

    c = consensus(ticker, interval=args.interval)
    if c is None:
        print(miss)
        return
    tag = "intraday" if args.interval in INTRADAY_INTERVALS else "consensus"
    print(f"{ticker} TradingView {tag} @ {args.interval}:")
    print(fmt(c))


def cmd_dashboard(args: argparse.Namespace) -> None:
    app = Path(__file__).parent / "web" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app)], check=False)


def cmd_fundamentals(args: argparse.Namespace) -> None:
    from stocks.analysis.fundamentals import comparables_table
    from stocks.analysis.screener import fetch_metrics_many

    tickers = [args.ticker.upper()]
    tickers += [p.strip().upper() for p in args.peers.split(",") if p.strip()]
    metrics = fetch_metrics_many(tickers)  # concurrent, order-preserving
    print(comparables_table(metrics).to_string())

    if args.eur:
        from stocks.data.fx import usd_eur

        rate, as_of = usd_eur()
        print(f"\nFX USD->EUR (ECB spot, {as_of}): {rate:.4f}")

    if args.check:
        from stocks.data.edgar import cross_check

        print("\nSEC EDGAR cross-check (latest 10-K, USD):")
        for t, m in zip(tickers, metrics, strict=True):
            try:
                facts = cross_check(t)
            except Exception as exc:
                print(f"  {t}: EDGAR unavailable ({exc}) — set EDGAR_USER_AGENT in .env")
                continue
            rev = facts["revenue"]
            if rev is None:
                print(f"  {t}: not found on EDGAR (non-US filer?)")
                continue
            end, val = rev
            print(f"  {t}: revenue {val / 1e9:,.1f}B (FY end {end})", end="")
            ni = facts["net_income"]
            if ni:
                print(f", net income {ni[1] / 1e9:,.1f}B", end="")
            print(f"  [yfinance net margin {m.get('net_margin')}]")

    print(
        "\nnote: yfinance loads, EDGAR verifies. PEG is consensus-level "
        "data — cross-check before use."
    )


def cmd_value(args: argparse.Namespace) -> None:
    from stocks.analysis.valuation import summarize

    ticker = args.ticker.upper()
    data = _valuation_gather(ticker, args)
    price = data["price"]
    cons = data["consensus"]

    if cons is not None and cons.target_mean is not None:
        rmean = f"{cons.rating_mean:.2f}" if cons.rating_mean is not None else "n/a"
        print(f"=== {ticker} analyst consensus [consensus — cross-check] ===")
        print(f"  rating:        {cons.rating or 'n/a'} (mean {rmean})")
        print(f"  price target:  {cons.target_low:.2f} / {cons.target_mean:.2f} / "
              f"{cons.target_high:.2f} (low/mean/high)")
        if cons.target_upside is not None:
            print(f"  target upside: {_ret(cons.target_upside)} vs price {price:.2f}")
        print(f"  next-FY EPS:   {cons.eps_next_fy} "
              f"(growth {_ret(cons.eps_growth_next_fy)})")
        print(f"  next-FY rev:   growth {_ret(cons.rev_growth_next_fy)}\n")

    inp = data["inputs"]
    if inp is None:
        print(f"{ticker}: no DCF — free cash flow or share count unavailable "
              "(negative/missing FCF is common for early-growth names).")
        return

    base_growth = args.growth if args.growth is not None else data["base_growth"]
    if base_growth is None:
        raise SystemExit(
            "no growth starting point (no consensus, no 5y CAGR) — pass --growth"
        )

    exit_note = f" · exit x{args.exit_multiple:g}" if args.exit_multiple else ""
    print(f"=== {ticker} DCF fair value [derived — assumptions are yours] ===")
    print(f"  FCF0 {inp.fcf0 / 1e9:,.2f}B · shares {inp.shares / 1e9:,.2f}B · "
          f"net cash {inp.net_cash / 1e9:,.2f}B")
    print(f"  discount {inp.discount_rate * 100:.1f}% · "
          f"terminal {inp.terminal_growth * 100:.1f}% · horizon {inp.years}y · "
          f"base growth {base_growth * 100:.1f}%{exit_note}\n")

    summary = summarize(
        inp, price, base_growth, spread=args.spread, exit_multiple=args.exit_multiple
    )
    results, cases = summary["results"], summary["cases"]

    print(f"  {'':5s} {'growth':>8s} {'fair val':>10s} "
          f"{'upside':>9s} {'ann.':>8s} {'term%':>7s}")
    for name in ("bear", "base", "bull"):
        r = results[name]
        tot = (r.fair_value / price - 1) if price else None
        can_ann = price and r.fair_value > 0
        ann = (r.fair_value / price) ** (1 / inp.years) - 1 if can_ann else None
        print(f"  {name:5s} {cases[name] * 100:>7.1f}% {r.fair_value:>10.2f} "
              f"{_ret(tot):>9s} {_ret(ann):>8s} {r.terminal_weight * 100:>6.0f}%")

    if not price:
        print("\n(no cached price — run `stocks update` for return/upside figures)")
    else:
        weighted = summary["weighted"]
        print(f"\n  probability-weighted (25/50/25): "
              f"fair value {weighted['fair_value']:.2f}, "
              f"total {_ret(weighted['total'])}, "
              f"annualized {_ret(weighted['annualized'])}")
        print(f"  margin of safety (base): {_ret(summary['mos'])}")
        implied = summary["implied"]
        if implied is not None:
            print(f"  reverse-DCF: price implies {implied * 100:.1f}% constant FCF "
                  f"growth — beatable? compare to base {base_growth * 100:.1f}%")

    print("\nnote: derived scaffold — terminal value dominates, so treat the "
          "terminal% column as a confidence gauge. Not advice.")


def _ret(x: float | None) -> str:
    """Signed percent string for a return/growth fraction; 'n/a' when missing."""
    from stocks.formatting import pct

    return pct(x, signed=True)


def _valuation_gather(ticker: str, args: argparse.Namespace) -> dict:
    from stocks.analysis.valuation import gather

    return gather(
        ticker,
        discount_rate=args.discount,
        terminal_growth=args.terminal_growth,
        years=args.years,
    )


def cmd_tx(args: argparse.Namespace) -> None:
    from stocks.portfolio.ledger import Transaction, add, all_transactions, import_csv

    if args.tx_command == "add":
        tx = Transaction(
            date=args.date,
            ticker=args.ticker,
            action=args.action,
            quantity=args.qty,
            price=args.price,
            currency=args.currency,
            fee=args.fee,
            note=args.note,
        )
        tx_id = add(tx)
        print(f"added #{tx_id}: {tx.date} {tx.ticker} {tx.action} {tx.quantity}@{tx.price}")
    elif args.tx_command == "import":
        n = import_csv(Path(args.file))
        print(f"imported {n} transactions from {args.file}")
    elif args.tx_command == "revolut":
        _tx_revolut(Path(args.file), commit=args.commit)
    else:  # list
        txs = all_transactions()
        if not txs:
            print("no transactions (add with `stocks tx add ...`)")
            return
        for t in txs:
            print(
                f"#{t.id:<4} {t.date}  {t.ticker:8s} {t.action:9s} "
                f"{t.quantity:>10.4f} @ {t.price:>10.2f} {t.currency} fee {t.fee:.2f}"
            )


def _tx_revolut(path: Path, *, commit: bool) -> None:
    """Parse + validate a Revolut statement (CSV or PDF); write only on --commit."""
    from datetime import datetime

    from stocks.portfolio import last_import, revolut, revolut_pdf
    from stocks.portfolio.ledger import add_many, all_transactions
    from stocks.portfolio.validate import known_tickers, validate

    if path.suffix.lower() == ".pdf":
        result = revolut_pdf.parse_pdf(path)
    else:
        result = revolut.parse_csv(path.read_text(encoding="utf-8-sig"))

    v = validate(result, all_transactions(), known=known_tickers())
    print(f"{path.name}: {v.summary}, {len(result.skipped)} skipped by design")
    for c in v.rejected:
        why = "; ".join(i.message for i in c.errors)
        print(f"  🚫 {c.tx.date} {c.tx.ticker:8s} {c.tx.action:9s} — {why}")
    for c in v.flagged:
        why = "; ".join(i.message for i in c.warnings)
        print(f"  ⚠️  {c.tx.date} {c.tx.ticker:8s} {c.tx.action:9s} — {why}")
    if not commit:
        print("dry run — pass --commit to write the importable rows to the ledger")
        return
    ids = add_many(v.importable)
    last_import.save(
        last_import.ImportRecord(
            filename=path.name,
            imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tx_ids=ids,
        )
    )
    print(f"committed {len(ids)} transactions; ledger now holds {len(all_transactions())}")


def cmd_positions(args: argparse.Namespace) -> None:
    from stocks.analysis.portfolio import market_values_eur
    from stocks.portfolio.ledger import all_transactions
    from stocks.portfolio.positions import build

    positions, _ = build(all_transactions())
    if not positions:
        print("no open positions")
        return
    values = market_values_eur(positions)  # concurrent price+FX lookups
    print(f"{'TICKER':8s} {'QTY':>10s} {'COST EUR':>12s} {'VALUE EUR':>12s} {'P/L EUR':>12s}")
    total_cost = total_value = 0.0
    for p in positions:
        value = values.get(p.ticker)
        total_cost += p.cost_eur
        vstr = f"{value:>12,.0f}" if value is not None else f"{'n/a':>12s}"
        plstr = f"{value - p.cost_eur:>12,.0f}" if value is not None else f"{'n/a':>12s}"
        if value is not None:
            total_value += value
        print(f"{p.ticker:8s} {p.quantity:>10.4f} {p.cost_eur:>12,.0f} {vstr} {plstr}")
    print("-" * 58)
    print(f"{'TOTAL':8s} {'':>10s} {total_cost:>12,.0f} {total_value:>12,.0f} "
          f"{total_value - total_cost:>12,.0f}")


def cmd_realized(args: argparse.Namespace) -> None:
    from stocks.portfolio.ledger import all_transactions
    from stocks.portfolio.positions import build

    _, realized = build(all_transactions())
    if args.year:
        realized = [s for s in realized if int(s.sell_date[:4]) == args.year]
    if not realized:
        print("no realized sales")
        return
    for s in realized:
        print(
            f"{s.ticker:8s} buy {s.buy_date} sell {s.sell_date} "
            f"qty {s.quantity:.4f}  cost {s.cost_eur:,.0f}  "
            f"proceeds {s.proceeds_eur:,.0f}  gain {s.gain_eur:>+,.0f} EUR"
        )
    print(f"\ntotal realized gain: {sum(s.gain_eur for s in realized):>+,.0f} EUR")


def cmd_tax(args: argparse.Namespace) -> None:
    from collections import defaultdict

    from stocks.analysis.portfolio import market_values_eur
    from stocks.portfolio.ledger import all_transactions
    from stocks.portfolio.positions import build
    from stocks.portfolio.tax_es import fiscal_year, modelo_720_flag

    txs = all_transactions()
    positions, realized = build(txs)
    buy_dates: dict[str, list[str]] = defaultdict(list)
    for t in txs:
        if t.action == "buy":
            buy_dates[t.ticker].append(t.date)

    ty = fiscal_year(realized, args.year, buy_dates)
    print(f"=== IRPF savings base — FY {ty.year} ===")
    print(f"realized gains:        {ty.realized_gain_eur:>12,.0f} EUR")
    print(f"realized losses:       {ty.realized_loss_eur:>12,.0f} EUR")
    if ty.deferred_loss_eur:
        print(f"  of which deferred:   {ty.deferred_loss_eur:>12,.0f} EUR (2-month rule)")
    print(f"deductible losses:     {ty.deductible_loss_eur:>12,.0f} EUR")
    print(f"net taxable base:      {ty.net_taxable_eur:>12,.0f} EUR")
    print(f"estimated tax:         {ty.estimated_tax_eur:>12,.0f} EUR")
    if ty.carryforward_loss_eur:
        print(f"loss carryforward:     {ty.carryforward_loss_eur:>12,.0f} EUR (4 years)")

    values = market_values_eur(positions)  # concurrent price+FX lookups
    foreign = sum(values.get(p.ticker) or p.cost_eur for p in positions)
    print(f"\n{modelo_720_flag(foreign).message}")
    print("\nnote: planning aid, not tax advice. Verify with your gestor / Renta.")


def cmd_dividends(args: argparse.Namespace) -> None:
    from stocks.portfolio.dividends import by_year
    from stocks.portfolio.ledger import all_transactions

    years = by_year(all_transactions())
    if args.year:
        years = {y: d for y, d in years.items() if y == args.year}
    if not years:
        print("no dividends recorded")
        return
    for yr in sorted(years):
        d = years[yr]
        print(f"=== dividends {yr} ===")
        print(f"  gross:       {d.gross_eur:>10,.0f} EUR")
        print(f"  withheld:    {d.withheld_eur:>10,.0f} EUR")
        print(f"  net:         {d.net_eur:>10,.0f} EUR")
        print(f"  creditable:  {d.creditable_eur:>10,.0f} EUR (Spain double-tax credit)")
        print(f"  reclaimable: {d.reclaimable_eur:>10,.0f} EUR (from source country)")


def cmd_report(args: argparse.Namespace) -> None:
    from datetime import date

    from stocks.analysis.report import gather, render_report
    from stocks.config import load_watchlist

    ticker = args.ticker.upper()
    peers = [p.strip().upper() for p in args.peers.split(",") if p.strip()]
    name = next(
        (h.name for h in load_watchlist() if h.ticker.upper() == ticker), ""
    )

    data = gather(ticker, peers, eur=args.eur)
    md = render_report(
        ticker=ticker,
        name=name,
        metrics=data["metrics"],
        peers=data["peers"],
        edgar=data["edgar"],
        technicals=data["technicals"],
        as_of=date.today().isoformat(),
        fx=data["fx"],
    )

    out = Path(args.out) if args.out else Path(f"{ticker}_analysis.md")
    out.write_text(md)
    print(f"{ticker}: analysis scaffold -> {out}")

    if args.pdf:
        _to_pdf(out)


def _to_pdf(md_path: Path) -> None:
    """Convert Markdown to PDF via pandoc if present; otherwise say so."""
    import shutil

    if shutil.which("pandoc") is None:
        print("pandoc not found — skipping PDF (install pandoc, or open the .md).")
        return
    pdf_path = md_path.with_suffix(".pdf")
    result = subprocess.run(
        ["pandoc", str(md_path), "-o", str(pdf_path)], capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"           PDF -> {pdf_path}")
    else:
        print(f"pandoc failed: {result.stderr.strip()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stocks", description="Stock tracking toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_update = sub.add_parser("update", help="fetch and cache price history")
    p_update.add_argument("--period", default="1y", help="yfinance period, e.g. 1y, 6mo")
    p_update.set_defaults(func=cmd_update)

    p_alerts = sub.add_parser("alerts", help="check watchlist alerts (price/RSI/etc)")
    p_alerts.add_argument(
        "--deliver", action="store_true",
        help="send hits via configured channels (Telegram/email)",
    )
    p_alerts.add_argument(
        "--earnings-days", type=int, default=0, metavar="N",
        help="also remind about earnings within N days",
    )
    p_alerts.add_argument(
        "--all-users", action="store_true",
        help="cron mode: evaluate every Telegram-linked account and message each",
    )
    p_alerts.set_defaults(func=cmd_alerts)

    p_digest = sub.add_parser(
        "digest", help="daily portfolio digest (value, moves, earnings) via Telegram"
    )
    p_digest.add_argument(
        "--all-users", action="store_true",
        help="cron mode: send every Telegram-linked account its own digest",
    )
    p_digest.add_argument(
        "--dry-run", action="store_true",
        help="print the rendered digest(s) instead of sending",
    )
    p_digest.set_defaults(func=cmd_digest)

    p_screen = sub.add_parser("screen", help="rank/filter the whole watchlist by KPIs")
    p_screen.add_argument("--sort", help="metric key to rank by, e.g. roic, pe_ttm")
    p_screen.add_argument("--asc", action="store_true", help="force ascending sort")
    p_screen.add_argument("--top", type=int, help="keep only the top N rows")
    p_screen.add_argument(
        "--min", action="append", metavar="KEY=VAL",
        help="keep rows with metric >= value (repeatable)",
    )
    p_screen.add_argument(
        "--max", action="append", metavar="KEY=VAL",
        help="keep rows with metric <= value (repeatable)",
    )
    p_screen.add_argument("--all", action="store_true", help="show every KPI column")
    p_screen.set_defaults(func=cmd_screen)

    p_earn = sub.add_parser("earnings", help="upcoming earnings across the watchlist")
    p_earn.add_argument("--days", type=int, default=30, help="look-ahead window in days")
    p_earn.set_defaults(func=cmd_earnings)

    p_port = sub.add_parser("portfolio", help="portfolio analytics: allocation & risk")
    p_port.add_argument("--period", default="1y", help="return window, e.g. 6mo, 1y, 2y")
    p_port.set_defaults(func=cmd_portfolio)

    p_find = sub.add_parser(
        "search", help="find tickers by symbol or company name (SEC map, US listings)"
    )
    p_find.add_argument("query", nargs="+", help='e.g. "bank of america" or BAC')
    p_find.add_argument("--limit", type=int, default=10, help="max matches (default 10)")
    p_find.set_defaults(func=cmd_search)

    p_fav = sub.add_parser("favorites", help="list favorite (starred) tickers")
    p_fav.set_defaults(func=cmd_favorites)

    p_tv = sub.add_parser(
        "tv", help="TradingView technical consensus (BUY/NEUTRAL/SELL) for a ticker"
    )
    p_tv.add_argument("ticker", help="ticker, e.g. AAPL (non-US: map under `tv:`)")
    p_tv.add_argument(
        "--interval", default="1d",
        help="timeframe: 1m/5m/15m/30m/1h/2h/4h/1d/1W/1M (default 1d)",
    )
    p_tv.add_argument(
        "--multi", action="store_true",
        help="read multiple timeframes (1h, 1d, 1W) in one call",
    )
    p_tv.set_defaults(func=cmd_tv)

    p_dash = sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    p_dash.set_defaults(func=cmd_dashboard)

    p_fund = sub.add_parser("fundamentals", help="print fundamental KPIs")
    p_fund.add_argument("ticker", help="main ticker, e.g. AAPL")
    p_fund.add_argument(
        "--peers", default="", help="comma-separated peer tickers for comps table"
    )
    p_fund.add_argument("--eur", action="store_true", help="print USD->EUR ECB spot rate")
    p_fund.add_argument(
        "--check", action="store_true", help="cross-check vs SEC EDGAR 10-K facts"
    )
    p_fund.set_defaults(func=cmd_fundamentals)

    # --- portfolio: transactions -> FIFO positions, realized gains, ES tax ---
    p_tx = sub.add_parser("tx", help="manage the transaction ledger")
    tx_sub = p_tx.add_subparsers(dest="tx_command", required=True)

    p_add = tx_sub.add_parser("add", help="record one transaction")
    p_add.add_argument("date", help="ISO date YYYY-MM-DD")
    p_add.add_argument("ticker")
    p_add.add_argument("action", choices=sorted({"buy", "sell", "dividend", "fee", "split"}))
    p_add.add_argument("--qty", type=float, default=0.0, help="shares (split: ratio)")
    p_add.add_argument("--price", type=float, default=0.0,
                       help="per-share native ccy (dividend: gross total)")
    p_add.add_argument("--currency", default="USD")
    p_add.add_argument("--fee", type=float, default=0.0,
                       help="commission (dividend: tax withheld)")
    p_add.add_argument("--note", default="")

    p_imp = tx_sub.add_parser("import", help="bulk import from CSV")
    p_imp.add_argument("file", help="CSV: date,ticker,action,quantity,price,currency,fee,note")

    p_rev = tx_sub.add_parser(
        "revolut", help="parse + validate a Revolut statement (CSV or PDF)"
    )
    p_rev.add_argument("file", help="Revolut trading account statement (.csv or .pdf)")
    p_rev.add_argument(
        "--commit", action="store_true",
        help="write importable rows to the ledger (default: dry-run preview)",
    )

    tx_sub.add_parser("list", help="print all transactions")
    p_tx.set_defaults(func=cmd_tx)

    p_pos = sub.add_parser("positions", help="open positions + unrealized P/L (EUR)")
    p_pos.set_defaults(func=cmd_positions)

    p_real = sub.add_parser("realized", help="realized sales (FIFO, EUR)")
    p_real.add_argument("--year", type=int, help="filter to one calendar year")
    p_real.set_defaults(func=cmd_realized)

    p_tax = sub.add_parser("tax", help="Spanish IRPF savings-base summary")
    p_tax.add_argument("--year", type=int, required=True, help="fiscal year, e.g. 2025")
    p_tax.set_defaults(func=cmd_tax)

    p_div = sub.add_parser("dividends", help="dividend income + withholding (EUR)")
    p_div.add_argument("--year", type=int, help="filter to one calendar year")
    p_div.set_defaults(func=cmd_dividends)

    p_rep = sub.add_parser(
        "report", help="generate the 7-section analysis scaffold (Markdown)"
    )
    p_rep.add_argument("ticker", help="main ticker, e.g. AAPL")
    p_rep.add_argument(
        "--peers", default="", help="comma-separated peer tickers for the comps table"
    )
    p_rep.add_argument("--eur", action="store_true", help="include USD->EUR spot rate")
    p_rep.add_argument("--out", help="output path (default TICKER_analysis.md)")
    p_rep.add_argument(
        "--pdf", action="store_true", help="also render PDF via pandoc if installed"
    )
    p_rep.set_defaults(func=cmd_report)

    p_val = sub.add_parser(
        "value", help="DCF + reverse-DCF fair value with bull/base/bear scenarios"
    )
    p_val.add_argument("ticker", help="ticker to value, e.g. AAPL")
    p_val.add_argument("--growth", type=float,
                       help="base annual FCF growth (default: consensus EPS growth)")
    p_val.add_argument("--spread", type=float, default=0.05,
                       help="+/- growth for bull/bear (default 0.05 = 5pp)")
    p_val.add_argument("--discount", type=float, default=0.10,
                       help="discount rate / required return (default 0.10)")
    p_val.add_argument("--terminal-growth", type=float, default=0.025,
                       dest="terminal_growth",
                       help="perpetual growth for the Gordon terminal (default 0.025)")
    p_val.add_argument("--years", type=int, default=5,
                       help="explicit forecast horizon (default 5)")
    p_val.add_argument("--exit-multiple", type=float, dest="exit_multiple",
                       help="use a terminal FCF exit multiple instead of Gordon growth")
    p_val.set_defaults(func=cmd_value)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
