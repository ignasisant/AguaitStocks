# stocks

Personal stock tracking toolkit — fetch prices, compute indicators, run price
alerts, and browse a visual dashboard.

## Stack

- **[uv](https://docs.astral.sh/uv/)** — Python + dependency manager (Python 3.12)
- **yfinance** — price data (no API key needed)
- **pandas** — data wrangling
- **Streamlit + Plotly** — the dashboard / "website"
- **pytest + ruff** — tests and linting

## Setup

```bash
uv sync            # create .venv and install everything
```

## Usage

```bash
uv run stocks update      # fetch + cache price history for the watchlist
uv run stocks alerts      # print any triggered price alerts
uv run stocks dashboard   # launch the Streamlit dashboard in the browser
```

Edit the tickers you follow in [`watchlist.yaml`](watchlist.yaml).

## Layout

```
src/stocks/
  config.py            paths, data model (Alert/Holding), watchlist loader
  data/fetch.py        yfinance download + CSV cache
  analysis/indicators  SMA, EMA, RSI, returns
  notify/alerts.py     evaluate alerts vs latest price
  web/app.py           Streamlit dashboard
  cli.py               `stocks` command
scripts/update_prices.py   standalone refresh (cron-friendly)
tests/                     smoke tests (no network)
data/                      cached CSVs (gitignored)
notebooks/                 scratch analysis
watchlist.yaml             stocks I follow + alert thresholds
```

## Dev

```bash
uv run pytest      # tests
uv run ruff check  # lint
uv run ruff format # format
```

## Roadmap

- [ ] Portfolio positions + P/L tracking
- [ ] Desktop / email / push notifications on alert hits
- [ ] Scheduled daily fetch (cron / launchd)
- [ ] More indicators (MACD, Bollinger)
- [ ] Backtesting simple strategies
