terraform {
  required_version = ">= 1.6"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

provider "github" {
  token = var.github_token
  owner = var.github_owner
}

# --- Cloudflare R2: persistent storage behind stocks.storage ------------------
#
# Container filesystems are ephemeral (rebuilds/redeploys wipe them); user
# data dirs are synced to this bucket (see src/stocks/storage.py). The same bucket is read by the
# notifications cron in .github/workflows/notify.yml.
#
# Existing bucket? Import instead of recreating (jurisdiction segment required):
#   terraform import cloudflare_r2_bucket.stocks <account_id>/<bucket_name>/default

resource "cloudflare_r2_bucket" "stocks" {
  account_id = var.cloudflare_account_id
  name       = var.r2_bucket_name
  location   = var.r2_location
}

# --- R2 S3 credentials: account token scoped to the bucket --------------------
#
# Cloudflare derives S3-compatible credentials from an API token:
#   access_key_id     = token id
#   secret_access_key = sha256(token value)
# so the dashboard "Manage R2 API Tokens" step is not needed. The provider
# credential (var.cloudflare_api_token) must carry "Account API Tokens: Edit"
# on top of R2 edit for this resource to be creatable.

resource "cloudflare_account_token" "r2" {
  account_id = var.cloudflare_account_id
  name       = "stocks-r2-${cloudflare_r2_bucket.stocks.name}"

  policies = [{
    effect = "allow"
    permission_groups = [{
      # "Workers R2 Storage Bucket Item Write" — object read/write, this bucket only.
      id = "2efd5506f9c8494dacb1fa10a3e7d5b6"
    }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.stocks.name}" = "*"
    })
  }]
}

locals {
  r2_access_key_id     = cloudflare_account_token.r2.id
  r2_secret_access_key = sha256(cloudflare_account_token.r2.value)
}

# --- GitHub Actions secrets for the notifications workflow --------------------
#
# Mirrors scripts/setup_notify_secrets.sh: required storage/Telegram secrets
# plus optional LLM keys. Empty optional values are skipped (not created),
# matching the script's "skipped (empty)" behavior — notify.yml treats them
# as optional.

locals {
  required_secrets = {
    STOCKS_STORAGE_ENDPOINT_URL      = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
    STOCKS_STORAGE_BUCKET            = cloudflare_r2_bucket.stocks.name
    STOCKS_STORAGE_ACCESS_KEY_ID     = local.r2_access_key_id
    STOCKS_STORAGE_SECRET_ACCESS_KEY = local.r2_secret_access_key
    TELEGRAM_BOT_TOKEN               = var.telegram_bot_token
  }

  optional_secrets = {
    CHAT_ENC_KEY        = var.chat_enc_key
    FREE_LLM_GROQ       = var.free_llm_groq
    FREE_LLM_CEREBRAS   = var.free_llm_cerebras
    FREE_LLM_OPENROUTER = var.free_llm_openrouter
  }

  actions_secrets = merge(
    local.required_secrets,
    { for k, v in local.optional_secrets : k => v if v != "" },
  )
}

resource "github_actions_secret" "notify" {
  # Values derived from sensitive vars mark the whole map sensitive, which
  # for_each rejects (instance keys would leak). Keys here are just secret
  # names; plaintext_value is still masked by the provider's own sensitive
  # schema. Requires TF >= 1.7 (nonsensitive() no longer errors on
  # already-plain values).
  for_each    = nonsensitive(local.actions_secrets)
  repository  = var.github_repo
  secret_name = each.key
  value       = each.value
}
