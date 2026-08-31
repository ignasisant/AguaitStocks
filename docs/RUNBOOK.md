# Runbook

What to do when TopStocks misbehaves in production. Prod is Cloud Run:
project `topstocks-507209`, region `europe-west1`, service `topstocks`
(staging: `topstocks-staging`). Alerts come from the policies in
`infra/monitoring/` (created by `scripts/setup_monitoring.sh`).

First three commands of any incident:

```bash
curl -s https://<service-url>/status        # revision, uptime, storage on?
uv run stocks logs errors --since 2h        # what is actually failing
uv run stocks logs stats --since 2h         # which events, how slow
```

A suspiciously **young `uptime_s`** in `/status` during an incident means the
container is crash-looping — go straight to "Bad deploy / rollback".

## Alert: `/healthz` down (uptime check)

The service isn't answering at all.

1. `gcloud run services describe topstocks --region europe-west1` — look at
   the Ready condition and the serving revision.
2. Revision unhealthy right after a deploy → roll back (below).
3. No recent deploy → check quota/billing (budget email?) and Cloud Run
   outages, then `uv run stocks logs tail --since 30m` for boot errors
   (a bad `STREAMLIT_SECRETS_TOML` version kills every page at import).

## Alert: ERROR logs

1. `uv run stocks logs errors --since 2h` — the stack traces are inline.
2. `uv run stocks logs stats --since 2h --by event` — one page or everything?
3. One user affected? `uv run stocks logs tail --user <slug> --since 2h` gives
   their timeline; the slug is also the data dir name under `data/users/`.

Known shapes:

- **yfinance / market data**: Yahoo throttles datacenter IPs. The app already
  degrades (banner + cached sections keep rendering); a burst of
  `YFRateLimitError` is weather, not an incident. Only act if it never
  recovers: consider raising cache TTLs or reducing fetch fan-out.
- **Storage/R2 errors**: logins fail closed ("storage restore failed") rather
  than showing empty books. Check Cloudflare status; nothing to fix app-side —
  sessions recover on the next run once R2 answers.
- **Free-LLM chain exhausted**: `llm.free.answered` disappears,
  `llm.free.global_cap` events appear, users see the cap message. Raise
  `FREE_LLM_GLOBAL_DAILY_CAP` (env overlay wins over secrets) or wait for the
  UTC midnight reset.

## Bad deploy / rollback

```bash
gcloud run revisions list --service topstocks --region europe-west1
gcloud run services update-traffic topstocks --region europe-west1 \
  --to-revisions <last-good-revision>=100
```

Rollback is traffic-only and instant; the bad revision keeps existing. Fix
forward on a branch, let CI go green, then `./scripts/deploy.sh prod`.

## Data: corrupt or lost user data

Backups are daily bucket snapshots (see README "Backups & restore").

```bash
uv run stocks backup list
uv run stocks backup restore <stamp> --only data/users/<slug>/   # one account
uv run stocks backup restore <stamp>                             # everything
```

Then **redeploy or restart the service** — a running container's local files
win over the bucket and would re-push the bad copies. Verify before and after
with the account's timeline: `uv run stocks logs tail --user <slug>`.

## Secrets rotation

Prod reads `STREAMLIT_SECRETS_TOML` from Secret Manager `topstocks-secrets`,
**pinned to a version number**. Rotating anything (OIDC client, R2 keys,
LLM keys, `[chat] enc_key`):

1. `gcloud secrets versions add topstocks-secrets --data-file=<new toml>`
2. Redeploy pointing at it:
   `./scripts/deploy.sh prod --secret topstocks-secrets:<new-version>`
3. Never paste the local dev `secrets.toml` — its `redirect_uri` is
   localhost and breaks Google login for everyone.

Rotating R2 keys also needs the four `STOCKS_STORAGE_*` GitHub Actions
secrets updated (Terraform manages them: `cd infra && terraform apply`), or
the notify/backup crons start failing.

## Crons (GitHub Actions)

Digest/alerts (`notify.yml`), Telegram chat (`telegram_chat.yml`), snapshots
(`backup.yml`). A red run emails the repo owner. They are stateless: fix the
cause and `gh workflow run <name>` to re-run; a missed digest is not worth
backfilling.

## Cost spike (budget alert email)

1. Cloud Console → Billing → Reports, group by SKU. The usual suspects:
   Cloud Run instance time (did `min-instances` change? traffic spike?) and
   egress.
2. `uv run stocks logs usage --since 7d` — real users, or a crawler/abuser?
   A single hammering IP shows up in `--http` access logs.
3. Emergency lever: `./scripts/deploy.sh prod --min-instances 0` (accepts
   cold starts) and/or lower `--max-instances` in the script.
