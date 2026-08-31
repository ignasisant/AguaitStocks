# --- Provider credentials ------------------------------------------------------

variable "cloudflare_api_token" {
  description = "Cloudflare API token with Account > Workers R2 Storage: Edit AND Account > Account API Tokens: Edit (the latter to mint the bucket-scoped S3 token). Dashboard -> My Profile -> API Tokens."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account id (dashboard sidebar, or any zone's overview page)."
  type        = string
}

variable "github_token" {
  description = "GitHub PAT with repo + secrets scope (classic: repo; fine-grained: Secrets read/write on the repo)."
  type        = string
  sensitive   = true
}

variable "github_owner" {
  description = "GitHub owner of the repository."
  type        = string
  default     = "ignasisant"
}

variable "github_repo" {
  description = "Repository name (no owner prefix)."
  type        = string
  default     = "AguaitStocks"
}

# --- R2 -------------------------------------------------------------------------

variable "r2_bucket_name" {
  description = "R2 bucket holding user data dirs + static/logos restore dir."
  type        = string
  default     = "aguait-user-data"
}

variable "r2_location" {
  description = "R2 location hint. WEUR = Western Europe."
  type        = string
  default     = "WEUR"
}

# --- Notification secrets --------------------------------------------------------

variable "telegram_bot_token" {
  description = "Telegram bot token from @BotFather. Never configure a webhook on this bot — the Profile linking flow polls getUpdates."
  type        = string
  sensitive   = true
}

variable "chat_enc_key" {
  description = "Optional. Fernet key, must equal Streamlit [chat] enc_key — lets the digest use BYOK LLM keys. Empty = secret not created."
  type        = string
  sensitive   = true
  default     = ""
}

variable "free_llm_groq" {
  description = "Optional. Groq API key for the free-chain digest highlight. Empty = secret not created."
  type        = string
  sensitive   = true
  default     = ""
}

variable "free_llm_cerebras" {
  description = "Optional. Cerebras API key for the free-chain digest highlight. Empty = secret not created."
  type        = string
  sensitive   = true
  default     = ""
}

variable "free_llm_openrouter" {
  description = "Optional. OpenRouter API key for the free-chain digest highlight. Empty = secret not created."
  type        = string
  sensitive   = true
  default     = ""
}
