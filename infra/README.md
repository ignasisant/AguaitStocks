# infra/ — Terraform for AguaitStocks

Manages the parts of the deployment that have Terraform providers:

| Piece | Managed here | Notes |
|---|---|---|
| Cloudflare R2 bucket | yes | persistent storage behind `stocks.storage` |
| GitHub Actions secrets | yes | everything `.github/workflows/notify.yml` reads; replaces `scripts/setup_notify_secrets.sh` |
| App secrets on the VM | no | no API — the app reads `deploy/secrets.toml`, see `deploy_secrets_reminder` output |
| Telegram bot | no | @BotFather; never set a webhook (Profile linking polls `getUpdates`) |
| Google OIDC client | no | Google Cloud console, OAuth consent screen |
| R2 S3 credential pair | yes | `cloudflare_account_token` scoped to the bucket; S3 creds derived as access_key_id = token id, secret = `sha256(token value)` |

## Usage

```sh
cd infra
cp terraform.tfvars.example terraform.tfvars   # fill in values (git-ignored)
terraform init
```

Existing bucket (the live deploy already uses one) — import it, don't let
Terraform create a duplicate:

```sh
terraform import cloudflare_r2_bucket.stocks <account_id>/<bucket_name>/default
```

Existing repo secrets need no import — `terraform apply` overwrites them
idempotently.

```sh
terraform plan
terraform apply
```

After apply, print the block for the deploy's `secrets.toml`:

```sh
terraform output deploy_secrets_reminder
```

## State is secret

`terraform.tfstate` holds every secret in plaintext (`sensitive = true` only
hides values from CLI output, not from state). Local state is git-ignored via
`infra/.gitignore`. If you move to a remote backend, use one with encryption
at rest and restricted access.
