# Telegram webhook Worker

Receives the bot's webhook POSTs from Telegram, stores each update in the R2
bucket under `data/tg_updates/`, and fires a `repository_dispatch`
(`telegram-update`) so the GitHub Actions job answers it
(`.github/workflows/telegram_chat.yml` → `stocks telegram-chat`).

The Worker holds **no** bot token, storage keys or LLM keys — only the
webhook secret and a GitHub PAT. R2 is reached through a native binding.

## Deploy

1. Create a fine-grained GitHub PAT: repo `ignasisant/AguaitStocks`,
   permission **Contents: read and write** (that's what `repository_dispatch`
   requires). Set a long expiry — replace it when it lapses.
2. Check `wrangler.toml`: `bucket_name` must equal the app's
   `STOCKS_STORAGE_BUCKET`.
3. From this directory:

   ```sh
   wrangler deploy
   wrangler secret put WEBHOOK_SECRET   # e.g. python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   wrangler secret put GITHUB_PAT
   ```

4. Point the bot at the Worker (same secret value):

   ```sh
   TELEGRAM_WEBHOOK_SECRET=<value> uv run stocks telegram-chat \
       --set-webhook https://aguait-telegram-webhook.<account>.workers.dev
   ```

5. Send the bot a message; watch the `telegram chat` workflow run.

## Rollback

```sh
uv run stocks telegram-chat --delete-webhook
```

restores `getUpdates` polling (and the pre-webhook Profile linking flow).
Queued objects left under `data/tg_updates/` are drained by the next
`stocks telegram-chat` run, or can be deleted from the bucket.
