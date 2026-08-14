#!/usr/bin/env bash
# Push every secret the notifications workflow needs to the GitHub repo, and
# print the [telegram] block to paste into Streamlit Cloud's secrets.
#
# Usage:
#   cp .notify_secrets.env.example .notify_secrets.env   # then paste values
#   ./scripts/setup_notify_secrets.sh
#
# Reads:
#   .notify_secrets.env      TELEGRAM_* + STOCKS_STORAGE_* (you paste these)
#   .streamlit/secrets.toml  [chat] enc_key + [free_llm] keys (harvested)
#
# Requires: gh CLI authenticated (`gh auth login`), python3.11+ (tomllib).

set -euo pipefail
cd "$(dirname "$0")/.."

REPO="ignasisant/AguaitStocks"
ENV_FILE=".notify_secrets.env"

[ -f "$ENV_FILE" ] || {
    echo "error: $ENV_FILE not found — cp .notify_secrets.env.example $ENV_FILE and fill it in" >&2
    exit 1
}
gh auth status > /dev/null 2>&1 || {
    echo "error: gh CLI not authenticated — run: gh auth login" >&2
    exit 1
}

# Parse KEY=value lines ourselves — tolerant of toml-style pastes with spaces
# around '=' and surrounding quotes, which would break a plain `source`.
while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*([A-Z_]+)[[:space:]]*=[[:space:]]*(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    val="${BASH_REMATCH[2]}"
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    export "$key=$val"
done < "$ENV_FILE"

for var in TELEGRAM_BOT_TOKEN STOCKS_STORAGE_ENDPOINT_URL STOCKS_STORAGE_BUCKET \
           STOCKS_STORAGE_ACCESS_KEY_ID STOCKS_STORAGE_SECRET_ACCESS_KEY; do
    [ -n "${!var:-}" ] || { echo "error: $var is empty in $ENV_FILE" >&2; exit 1; }
done

# Harvest from .streamlit/secrets.toml so the Actions cron matches the deploy.
harvest() { # harvest <section> <key>
    python3 - "$1" "$2" << 'EOF'
import sys, tomllib
with open(".streamlit/secrets.toml", "rb") as f:
    print(tomllib.load(f).get(sys.argv[1], {}).get(sys.argv[2], ""), end="")
EOF
}

CHAT_ENC_KEY="$(harvest chat enc_key)"
FREE_LLM_GROQ="$(harvest free_llm groq)"
FREE_LLM_CEREBRAS="$(harvest free_llm cerebras)"
FREE_LLM_OPENROUTER="$(harvest free_llm openrouter)"

set_secret() { # set_secret <name> <value>
    if [ -n "$2" ]; then
        printf '%s' "$2" | gh secret set "$1" -R "$REPO"
        echo "  set $1"
    else
        echo "  skipped $1 (empty)"
    fi
}

echo "Pushing GitHub Actions secrets to $REPO:"
set_secret TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
set_secret STOCKS_STORAGE_ENDPOINT_URL "$STOCKS_STORAGE_ENDPOINT_URL"
set_secret STOCKS_STORAGE_BUCKET "$STOCKS_STORAGE_BUCKET"
set_secret STOCKS_STORAGE_ACCESS_KEY_ID "$STOCKS_STORAGE_ACCESS_KEY_ID"
set_secret STOCKS_STORAGE_SECRET_ACCESS_KEY "$STOCKS_STORAGE_SECRET_ACCESS_KEY"
set_secret CHAT_ENC_KEY "$CHAT_ENC_KEY"
set_secret FREE_LLM_GROQ "$FREE_LLM_GROQ"
set_secret FREE_LLM_CEREBRAS "$FREE_LLM_CEREBRAS"
set_secret FREE_LLM_OPENROUTER "$FREE_LLM_OPENROUTER"

cat << EOF

Done. Now paste this into Streamlit Cloud -> app -> Settings -> Secrets
(keep everything already there, add this section):

[telegram]
bot_token = "$TELEGRAM_BOT_TOKEN"
bot_username = "${TELEGRAM_BOT_USERNAME:-}"

Then:
  1. Reboot the Streamlit app so it picks up the new secrets.
  2. Profile page -> Connect Telegram -> press Start in Telegram.
  3. GitHub -> Actions -> notifications -> Run workflow -> digest.
     Message should land in Telegram within ~2 min.
EOF
