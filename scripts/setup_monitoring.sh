#!/usr/bin/env bash
# One-time Cloud Monitoring setup for the Cloud Run service: an uptime check
# on /healthz, an alert when it fails, and an alert on ERROR-severity app logs.
#
# Usage:
#   ./scripts/setup_monitoring.sh you@example.com
#
# Idempotent-ish: each resource is looked up by display name first and skipped
# when it already exists, so re-running after a partial failure is safe.
#
# Requires: gcloud authenticated on the project (gcloud auth login), with the
# Monitoring Editor role. Project/service/region match the deployed app; the
# STOCKS_GCP_* variables override them (same ones stocks/logs_query.py reads).

set -euo pipefail

PROJECT="${STOCKS_GCP_PROJECT:-topstocks-507209}"
SERVICE="${STOCKS_GCP_SERVICE:-topstocks}"
REGION="${STOCKS_GCP_REGION:-europe-west1}"
EMAIL="${1:-}"

[ -n "$EMAIL" ] || { echo "usage: $0 <alert-email>" >&2; exit 1; }

HOST="$(gcloud run services describe "$SERVICE" --project "$PROJECT" \
    --region "$REGION" --format 'value(status.url)' | sed 's|https://||')"
[ -n "$HOST" ] || { echo "error: Cloud Run service $SERVICE not found" >&2; exit 1; }
echo "service host: $HOST"

# --- notification channel (email) --------------------------------------------
CHANNEL="$(gcloud beta monitoring channels list --project "$PROJECT" \
    --filter "displayName='stocks-alerts' AND type='email'" \
    --format 'value(name)' | head -1)"
if [ -z "$CHANNEL" ]; then
    CHANNEL="$(gcloud beta monitoring channels create --project "$PROJECT" \
        --display-name "stocks-alerts" --type email \
        --channel-labels "email_address=$EMAIL" --format 'value(name)')"
    echo "created channel: $CHANNEL"
else
    echo "channel exists: $CHANNEL"
fi

# --- uptime check on /healthz -------------------------------------------------
if gcloud monitoring uptime list-configs --project "$PROJECT" \
    --format 'value(displayName)' | grep -qx "stocks-healthz"; then
    echo "uptime check exists"
else
    gcloud monitoring uptime create "stocks-healthz" \
        --project "$PROJECT" \
        --resource-type uptime-url \
        --resource-labels "host=$HOST,project_id=$PROJECT" \
        --protocol https --path /healthz --port 443 \
        --period 5 --timeout 10
    echo "created uptime check"
fi

# --- alert policies (JSON in infra/monitoring/) --------------------------------
cd "$(dirname "$0")/.."
for f in infra/monitoring/*.json; do
    NAME="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['displayName'])" "$f")"
    if gcloud alpha monitoring policies list --project "$PROJECT" \
        --filter "displayName='$NAME'" --format 'value(name)' | grep -q .; then
        echo "policy exists: $NAME"
        continue
    fi
    # Inject the service name and the channel at create time.
    python3 - "$f" "$SERVICE" > /tmp/policy.json <<'EOF'
import json, sys
policy = json.load(open(sys.argv[1]))
text = json.dumps(policy).replace("__SERVICE__", sys.argv[2])
print(text)
EOF
    gcloud alpha monitoring policies create --project "$PROJECT" \
        --policy-from-file /tmp/policy.json \
        --notification-channels "$CHANNEL"
    echo "created policy: $NAME"
done

echo
echo "Done. Test: pause the service or curl a bad deploy, or run"
echo "  uv run stocks logs errors --since 1h"
