#!/usr/bin/env bash
# One-time GCP budget alert for the project — the cost backstop for Cloud Run
# (min-instances, egress) and everything else billed to it.
#
# Usage:
#   gcloud billing accounts list        # find the billing account ID
#   ./scripts/setup_budget.sh 0X0X0X-0X0X0X-0X0X0X [amount] [currency]
#
# Defaults: 15 EUR/month, emails at 50% / 90% / 100% of budget to the billing
# account admins (Cloud Billing's default recipients — add more people under
# Billing > Budgets & alerts in the console if needed).

set -euo pipefail

PROJECT="${STOCKS_GCP_PROJECT:-topstocks-507209}"
BILLING_ACCOUNT="${1:-}"
AMOUNT="${2:-15}"
CURRENCY="${3:-EUR}"

[ -n "$BILLING_ACCOUNT" ] || {
    echo "usage: $0 <billing-account-id> [amount] [currency]" >&2
    echo "       (gcloud billing accounts list)" >&2
    exit 1
}

if gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
    --format 'value(displayName)' 2>/dev/null | grep -qx "topstocks-budget"; then
    echo "budget exists: topstocks-budget"
    exit 0
fi

gcloud billing budgets create \
    --billing-account="$BILLING_ACCOUNT" \
    --display-name="topstocks-budget" \
    --budget-amount="${AMOUNT}${CURRENCY}" \
    --filter-projects="projects/$PROJECT" \
    --threshold-rule=percent=0.5 \
    --threshold-rule=percent=0.9 \
    --threshold-rule=percent=1.0

echo "created: topstocks-budget (${AMOUNT} ${CURRENCY}/month, alerts at 50/90/100%)"
