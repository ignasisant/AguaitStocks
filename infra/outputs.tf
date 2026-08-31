output "r2_bucket" {
  description = "R2 bucket name."
  value       = cloudflare_r2_bucket.stocks.name
}

output "r2_endpoint_url" {
  description = "S3-compatible endpoint for this account's R2."
  value       = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
}

output "github_secrets_set" {
  description = "Actions secrets managed on the repo."
  value       = sort(keys(github_actions_secret.notify))
}

# The app reads secrets from a file on the VM (deploy/secrets.toml, bind-mounted
# to /app/.streamlit/secrets.toml) — nothing to manage via a provider.
# This block mirrors what the app needs (see .streamlit/secrets.toml).
output "deploy_secrets_reminder" {
  description = "Blocks to paste into the deploy's secrets.toml (deploy/secrets.toml on the VM). Values marked <...> are not managed here."
  sensitive   = true
  value       = <<-EOT
    [storage]
    endpoint_url = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
    bucket = "${cloudflare_r2_bucket.stocks.name}"
    access_key_id = "${local.r2_access_key_id}"
    secret_access_key = "${local.r2_secret_access_key}"

    [telegram]
    bot_token = "${var.telegram_bot_token}"
    bot_username = "<bot @handle, no @>"

    # Also keep existing [auth] (Google OIDC), [chat], [free_llm] sections.
  EOT
}
