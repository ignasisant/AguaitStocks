"""Command-line interface: `stocks update | alerts | dashboard`."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def cmd_update(args: argparse.Namespace) -> None:
    from stocks.config import load_watchlist
    from stocks.data.fetch import fetch_history, save_history

    for h in load_watchlist():
        df = fetch_history(h.ticker, period=args.period)
        path = save_history(h.ticker, df)
        print(f"{h.ticker}: {len(df)} rows -> {path}")


def cmd_alerts(args: argparse.Namespace) -> None:
    from stocks.notify.alerts import check_all

    hits = check_all()
    if not hits:
        print("no alerts triggered")
        return
    for hit in hits:
        print(f"ALERT {hit}")


def cmd_dashboard(args: argparse.Namespace) -> None:
    app = Path(__file__).parent / "web" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app)], check=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stocks", description="Stock tracking toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_update = sub.add_parser("update", help="fetch and cache price history")
    p_update.add_argument("--period", default="1y", help="yfinance period, e.g. 1y, 6mo")
    p_update.set_defaults(func=cmd_update)

    p_alerts = sub.add_parser("alerts", help="check watchlist price alerts")
    p_alerts.set_defaults(func=cmd_alerts)

    p_dash = sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    p_dash.set_defaults(func=cmd_dashboard)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
