# Aguait Stocks

<img src="src/stocks/web/assets/aguait-logo.svg" alt="Aguait Stocks logo" width="270">

**Aguait Stocks** is a personal equity tracking toolkit: fetch market
prices, compute technical indicators, run price alerts, and browse a visual
analytics dashboard.

## Quickstart — first 10 minutes

```bash
uv sync                                        # 1. install (creates .venv)
#    2. create .streamlit/secrets.toml with the Google OAuth client
#       — see "Login (web app)" below
uv run stocks dashboard                        # 3. opens the app in the browser
```

Market pages (Home, Ticker, Screener, Earnings, Valuation) work signed-out
with a starter watchlist; **sign in with Google** for everything personal.
Then:

4. **Profile** page — add the tickers you follow (new accounts start with
   Apple + Microsoft as examples). Home, Ticker, Screener and Earnings work
   from the watchlist alone.
5. **Import** page — drop a Revolut account-statement CSV to fill the
   transaction ledger (every row is previewed before committing).
6. **Portfolio** page — positions, EUR P/L, risk and Spanish tax now derive
   from the ledger automatically.

Prefer the terminal? Skip the OAuth setup: copy
[`watchlist.example.yaml`](watchlist.example.yaml) to `watchlist.yaml`
(git-ignored — it holds your personal positions) and edit it, then
`uv run stocks update && uv run stocks alerts`. The full command set is
under [Usage](#usage).

## Stack

- **[uv](https://docs.astral.sh/uv/)** — Python + dependency manager (Python 3.12)
- **yfinance** — price data (no API key needed)
- **pandas** — data wrangling
- **Streamlit + Plotly** — the dashboard / "website"
- **BYOK LLMs** (Claude / ChatGPT / Gemini SDKs, optional) — the portfolio-aware
  chat assistant; see [AI assistant](#ai-assistant--chat-with-your-portfolio)
- **pytest + ruff** — tests and linting

## Setup

```bash
uv sync            # create .venv and install everything
```

### Login (web app)

Browsing the market pages (Home, Ticker, Screener, Earnings, Valuation) is
public — anonymous visitors get a shared read-only starter watchlist under
`data/users/_guest/`. Google sign-in (Streamlit-native OIDC: `st.login` /
`st.user`) is required only for everything personal: the Portfolio, Import
and Profile pages, plus the favorite/tag/watchlist-editing actions. Each
Google account gets its own private data under `data/users/<slug>/` —
watchlist, portfolio ledger, last-import record and preferences — keyed by
the verified account email. The optional `[app].owner_email` account maps to
the repo-root `watchlist.yaml` and `data/portfolio.db` instead, so it shares
one book with the (single-user) CLI. Broker-code `aliases` stay global in
the root `watchlist.yaml`, falling back to the tracked
`watchlist.example.yaml` while that file doesn't exist.

Configure:

1. Create a Google OAuth client (Web application) at
   <https://console.cloud.google.com/apis/credentials> with redirect URI
   `http://localhost:8501/oauth2callback`.
2. Create `.streamlit/secrets.toml` with an `[auth]` section holding
   `client_id`, `client_secret`, `redirect_uri` and a random `cookie_secret`
   (`python -c "import secrets; print(secrets.token_hex(32))"`).
3. `uv run stocks dashboard` — the portfolio pages show the sign-in screen
   until the secrets are in place; the market pages work regardless.

`secrets.toml` is git-ignored; never commit it. When deploying, add the
deployed URL + `/oauth2callback` to both the Google client and
`redirect_uri`.

### Persistent user data (deploys)

Hosts like Streamlit Community Cloud and most containers have an
**ephemeral filesystem**: `data/users/` and every imported ledger vanish on
restart or redeploy. To keep them, point the app at any S3-compatible
bucket (Cloudflare R2 free tier is plenty) via the `[storage]` section of
`secrets.toml` or the equivalent `STOCKS_STORAGE_*` env vars.

With a bucket configured, every write (watchlist edits, statement imports,
prefs) is mirrored to it immediately, and each account's files are pulled
back the first time it's touched after a boot. Object keys mirror the local
paths (`data/users/<slug>/portfolio.db`, plus the owner's repo-root
`watchlist.yaml` / `data/portfolio.db`). Unconfigured — the default for
local dev — everything stays plain files, no bucket or boto3 credentials
needed.

The bucket holds **every** account's ledgers and watchlists, so scope its
credentials tightly: use an API token limited to that one bucket with
object read/write only (on R2: *Object Read & Write* on the specific
bucket), never an account-level key. Keep the bucket private (no public
access / dev URL) and enable object versioning so a bad write can be
rolled back.

## Usage

```bash
uv run stocks update      # fetch + cache price history for the watchlist
uv run stocks alerts      # print any triggered price alerts
uv run stocks dashboard   # launch the Streamlit dashboard in the browser
uv run stocks search bank of america   # find tickers by name or symbol (SEC map)

# fundamental KPIs + comps table (+ EUR spot, + SEC EDGAR cross-check)
uv run stocks fundamentals AAPL --peers MSFT,GOOGL --eur --check

# 7-section analysis scaffold -> AAPL_analysis.md (quant auto-filled, judgment prompted)
uv run stocks report AAPL --peers MSFT,GOOGL --eur --pdf

# DCF + reverse-DCF fair value, bull/base/bear (growth prefilled from consensus)
uv run stocks value AAPL                          # consensus block + scenario table
uv run stocks value NVDA --discount 0.12 --years 7 --spread 0.08
uv run stocks value MSFT --growth 0.10 --exit-multiple 22   # relative terminal

# --- watchlist-wide analysis & monitoring ---
uv run stocks screen --sort roic --min roic=0.15 --max pe_ttm=40 --top 15
uv run stocks earnings --days 30              # upcoming earnings across the book
uv run stocks portfolio --period 1y           # allocation, concentration, risk, betas
uv run stocks alerts --deliver --earnings-days 7   # send hits + earnings via Telegram/email

# --- portfolio: transactions -> FIFO positions, realized gains, Spanish tax ---
uv run stocks tx add 2024-01-15 AAPL buy --qty 10 --price 185 --currency USD --fee 1
uv run stocks tx import trades.csv   # bulk load (our schema: date,ticker,action,quantity,price,currency,fee,note)
uv run stocks positions              # open positions + unrealized P/L (EUR)
uv run stocks realized --year 2025   # FIFO-matched realized sales (EUR)
uv run stocks tax --year 2025        # IRPF savings-base summary + estimated tax
uv run stocks dividends --year 2025  # dividend income + foreign withholding (EUR)
```

Edit the tickers you follow in `watchlist.yaml` (copy
[`watchlist.example.yaml`](watchlist.example.yaml) to create it — the real
file is git-ignored because it carries your positions).

## AI assistant — chat with your portfolio

A slide-in assistant panel is reachable from every page (the ✨ launcher pinned
top-right, signed-in users only). It's not a generic chatbot bolted on: **every
message carries a live snapshot of your real book and your current view**, so
you can ask "am I too concentrated in tech?", "which position is dragging me
today?", or "does NVDA still fit my thesis at this weight?" and get an answer
grounded in *your* numbers, not generic advice.

What the model sees on each turn (assembled in
[`chat_core.py`](src/stocks/web/chat_core.py) `_system_prompt`):

- **Your live holdings** — the same frame the Portfolio page shows: shares,
  EUR value, cost, unrealised P/L (% and EUR), portfolio weight and today's
  move per name, plus the total book value and P/L. Sourced from the imported
  FIFO ledger valued at live prices (cached per account); falls back to the
  watchlist's `shares`/`cost` when no ledger exists yet.
- **Your watchlist** — tickers you follow but don't hold, so it can reason
  about candidates too.
- **Your current view** — which page you're on and the ticker in focus, so a
  question like "is this one expensive?" resolves to what's on screen.
- **A fixed persona** — a concise investing assistant briefed that you're an
  aggressive long-term (5y+) investor; it gives analysis and trade-offs, flags
  what needs your own judgement, and does **not** pose as a licensed advisor.

Only the signed-in account's own data is read (`auth.db_path` /
`auth.watchlist_path`); nothing crosses between users.

### Bring your own key (multi-provider)

The assistant is **BYOK** — you supply your own API key and pay your own
provider bill; the app ships no keys and makes no calls on its own account
(unless the deploy opts into the free chain below). Three BYOK providers ship
in the registry ([`llm.py`](src/stocks/web/llm.py)), each with a model picker:

| Provider | Models | Get a key |
|---|---|---|
| **Claude** (default) | Opus 4.8, Sonnet 5, Haiku 4.5 | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| **ChatGPT** | GPT-5, GPT-4o, GPT-4o-mini | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Gemini** | Flash / Pro (rolling `-latest`) | [aistudio.google.com](https://aistudio.google.com/apikey) |

Each provider's SDK is imported lazily, so a provider only appears when its
package is installed — a missing optional dependency disables that one entry
instead of breaking the panel. Answers **stream** token-by-token; SDK errors
are classified into friendly messages (invalid key / no credits / API error)
in your language.

### Free assistant, no user key (`[free_llm]`)

Optionally, the deploy can offer a keyless **Aguait AI** provider: a chain of
free-tier backends billed to *operator* keys in `secrets.toml`. Users get chat
with zero setup; when a backend answers with a rate-limit (or any error before
its first token), the chain hops to the next one, and only errors out once
every configured backend is exhausted.

```toml
[free_llm]
# Configure any subset; fallback order is groq -> cerebras -> openrouter.
groq = "gsk_..."          # console.groq.com/keys
cerebras = "csk-..."      # cloud.cerebras.ai
openrouter = "sk-or-..."  # openrouter.ai/settings/keys (:free models)
# Optional per-backend model override (a retired free model is a config fix):
# groq_model = "llama-3.3-70b-versatile"
# Per-account daily message allowance (default 30):
# daily_cap = 30
```

When configured, Aguait AI is listed first and becomes the default for
accounts that never picked a provider; the BYOK entries stay available in the
same selector. Each account gets a **daily message cap** (`daily_cap`, counted
in its prefs) so one user can't drain the shared quota. Mind the fine print:
free tiers route your prompts — including the portfolio snapshot — through the
chosen vendors; check each vendor's data policy, or leave `[free_llm]` unset
to stay strictly BYOK.

### Key storage & privacy

- Your key stays in session by default. Tick **Remember** and it's encrypted
  (Fernet) and persisted for **15 days**, then auto-expires. Rotating the
  server's `[chat].enc_key` invalidates every stored key on the spot.
- Key storage is **account-scoped** (prefs `<pid>_key_enc`), so multiple users
  never share a key. **Forget** wipes it from session and prefs immediately.
- **Chat history persists per account** (one thread per watchlist, mirrored to
  disk / your S3 bucket) so it survives reloads, new sessions, and ephemeral
  redeploys — see [Persistent user data](#persistent-user-data-deploys).
- Enabling encrypted "Remember" needs `[chat].enc_key` in `secrets.toml`
  (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
  Without it the assistant still works — keys just aren't remembered across
  sessions.

### Upload your Revolut transactions (dashboard)

The ledger is the single source of truth for your book. The fastest way to fill
it: launch the dashboard (`uv run stocks dashboard`), open the **📥 Import** page,
and drop a **Revolut** account-statement CSV (Revolut → Stocks → statement, CSV).
Every parsed row is previewed before anything is written; press **Commit** to load
it. Re-importing an overlapping export duplicates rows, so tick *Wipe ledger first*
for a clean re-import.

From there the **📊 Portfolio** page derives everything from those transactions
(FIFO): open positions & EUR P/L, allocation & risk (EUR-weighted), realized
gains + IRPF savings-base tax, and dividends.

Notes on the Revolut parser (`src/stocks/portfolio/revolut.py`): buy/sell/dividend
rows import; cash top-ups/withdrawals, fees and **stock splits** are skipped (the
statement gives the resulting share count, not the split ratio — add splits by hand
with `stocks tx add … split --qty <ratio>`). Dividends import as gross with 0
withholding, since Revolut's CSV doesn't break out withholding tax — edit the fee
on those rows if you want the double-tax credit computed.

## Fundamental KPIs & data sources

Every KPI is tagged with a reliability level and a verification source
(see `KPI_SOURCES` in `src/stocks/analysis/fundamentals.py`):

- **fact** — verifiable in a primary source (SEC EDGAR 10-K/10-Q)
- **consensus** — analyst/market aggregate (forward P/E, PEG)
- **derived** — computed here from statements (ROIC, FCF yield, CAGRs)

Loading vs verification are separate concerns:

| Purpose | Source | Key |
|---|---|---|
| Load prices + fundamentals | yfinance | none |
| Verify facts (primary, US filers) | SEC EDGAR companyfacts API | none (`EDGAR_USER_AGENT` in `.env`) |
| Spot FX EUR (ES tax basis) | frankfurter.dev (ECB rates) | none |
| Manual cross-check: 10y ratios | stockanalysis.com | — |
| Manual cross-check: 15-20y trends | macrotrends.net | — |
| Manual cross-check: consensus/comps | Koyfin / TIKR | — |

Known caveats baked into the code: yfinance PEG is unreliable (flagged in
dashboard + CLI output); missing data renders as `n/a`, never invented.

KPI set: P/E (TTM/fwd), P/B, EV/EBITDA, EV/Sales, ROE, ROIC, margins
(gross/op/net), FCF + FCF yield, net debt/EBITDA, cash conversion (FCF/NI),
5y CAGRs (revenue, net income, FCF) and diluted-share dilution (SBC proxy).

## Forward valuation (DCF + reverse-DCF)

`stocks value` (💰 dashboard page) turns fundamentals into a forward fair value
without a spreadsheet — the piece a tool like TIKR charges for:

- **DCF** projects free cash flow over an explicit horizon and discounts it,
  with a Gordon-growth terminal (`--terminal-growth`) or a relative
  `--exit-multiple`. Every result reports `terminal %` — the share of value
  from the terminal — as a confidence gauge (high = shaky).
- **Bull / base / bear** from a base growth `±spread`, plus a probability-weighted
  (25/50/25) fair value and margin of safety.
- **Reverse-DCF** (`implied_growth`) inverts the model: the constant FCF growth
  the *current price* already assumes. Compare it to your base to judge whether
  the market's baked-in expectation is beatable — the useful question for a
  growth-tilted book.
- **Consensus prefill** (`stocks.data.estimates`): analyst price targets, rating
  split, and next-FY EPS/revenue growth seed the base case. All **consensus**
  level — cross-check before acting; missing coverage degrades to `n/a`.

The math (`stocks.analysis.valuation`) is pure and unit-tested offline; only
`gather` touches the network. Inputs (growth, discount, terminal, horizon) are
yours to own — the output is **derived**, only as good as the assumptions.

## Portfolio analytics, screener & alerts

Decision-support layer over the watchlist — dashboard pages (Streamlit
`st.navigation` multipage, under `web/app_pages/`) and matching CLI commands:

- **Portfolio analytics** (`stocks portfolio`, 📊 page): allocation by
  sector / geography / currency, concentration (top-5 weight, effective number
  of names via 1/HHI), annualised return & volatility, max drawdown, beta vs
  SPY / QQQ / EEM, and a return-correlation heatmap. Add `shares:` (and optional
  `cost:`) in `watchlist.yaml` for market-value weighting and unrealised P/L;
  without them the book is equal-weighted so the risk view still works.
- **Screener** (`stocks screen`, 🔎 page): rank and filter the whole watchlist
  by any KPI — cheap P/E, high ROIC, high FCF yield, low leverage. Repeatable
  `--min KEY=VAL` / `--max KEY=VAL` (percent metrics are fractions: `0.15` = 15%).
- **Earnings calendar** (`stocks earnings`, 📅 page): next report date per name,
  sorted soonest-first, windowed by `--days`.
- **Upgraded alerts + delivery** (`stocks alerts`): beyond `above`/`below`,
  rules for `pct_move`, `drawdown`, `rsi_below`/`rsi_above`, `sma_cross`, and
  `high_52w`/`low_52w`. `--deliver` pushes hits (and, with `--earnings-days N`,
  earnings reminders) to any channel configured in `.env` — Telegram and/or
  email over SMTP; unconfigured channels are skipped, so it degrades to console.

Pure logic (screening, portfolio math, alert evaluation, earnings-date
selection) is unit-tested offline; only the fetchers touch the network.

## Portfolio, FIFO & Spanish tax

Positions and tax derive from one source of truth: a transaction ledger in
SQLite (`data/portfolio.db`, gitignored — it's your private book). Record
`buy | sell | dividend | fee | split` events; everything else is computed.

- **FIFO** (art. 37 LIRPF): sales match oldest lots first. Acquisition cost
  includes buy commissions; proceeds are net of sell commissions.
- **EUR at transaction date**: every leg converts to EUR at the ECB rate for
  its date (frankfurter.dev, cached in `data/fx_history.json`; weekends resolve
  to the prior business day). This is the Hacienda basis, not spot.
- **IRPF savings base**: progressive brackets (19/21/23/27/28%); losses net
  against gains; net loss carries forward 4 years.
- **Regla de los 2 meses** (art. 33.5.f): a loss is auto-deferred when
  homogeneous shares are repurchased within 2 months of the sale.
- **Dividends**: foreign withholding split into the Spain-creditable part
  (treaty cap ~15%) and the reclaimable excess (e.g. French over-withholding).
- **Modelo 720 flag**: warns when foreign holdings clear the 50.000 EUR line.

Planning aid, not tax advice. All tax/FIFO logic is pure and unit-tested
(`tests/test_ledger.py`, `tests/test_tax_es.py`); FX is injectable so tests run
offline.

## Layout

```
src/stocks/
  config.py            paths, data model (Alert/Holding), watchlist loader
  data/fetch.py        yfinance download + CSV cache
  data/fundamentals.py yfinance snapshot + annual statements
  data/edgar.py        SEC EDGAR companyfacts (primary-source cross-check)
  data/fx.py           ECB FX (frankfurter.dev): spot + cached historical
  data/earnings.py     upcoming earnings dates (yfinance) + look-ahead window
  portfolio/ledger.py       SQLite transaction ledger (+ CSV import)
  portfolio/positions.py    FIFO lot matching -> positions + realized gains
  portfolio/tax_es.py       Spanish IRPF savings base, 2-month rule, 720 flag
  portfolio/dividends.py    dividend income + foreign withholding (EUR)
  analysis/indicators  SMA, EMA, RSI, returns
  analysis/fundamentals.py  KPI computation + KPI_SOURCES source-of-truth map
  analysis/portfolio.py     allocation, concentration, vol/beta/drawdown, corr
  analysis/screener.py      cross-sectional KPI rank/filter over the watchlist
  notify/alerts.py     evaluate alerts vs price history (price/RSI/drawdown/cross/52w)
  notify/deliver.py    push alerts to Telegram / email (env-gated, console fallback)
  web/app.py           Streamlit entry point (st.navigation + page config/CSS + global ticker picker)
  web/auth.py          Google OIDC login gate + per-account data paths/prefs
  web/chat_core.py     portfolio-aware assistant panel: context + BYOK key + conversation
  web/llm.py           multi-provider LLM registry (Claude/ChatGPT/Gemini), streaming + error map
  web/app_pages/       pages: Home, Ticker, Portfolio, Screener, Earnings, Valuation, Import, Profile
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

- [x] Portfolio positions + P/L tracking (FIFO ledger + Spanish tax)
- [x] Portfolio analytics (allocation, concentration, beta/vol/drawdown, benchmarks)
- [x] Watchlist screener (rank/filter by KPIs)
- [x] Earnings calendar + reminders
- [x] Alert upgrades (%move, drawdown, RSI, SMA cross, 52w) + Telegram/email delivery
- [x] Portfolio-aware AI assistant (BYOK Claude/ChatGPT/Gemini, live-book context)
- [ ] Desktop / push (mobile) notifications on alert hits
- [ ] Scheduled daily fetch (cron / launchd)
- [ ] More indicators (MACD, Bollinger)
- [ ] Backtesting simple strategies
