#!/usr/bin/env bash
# Deploy to Cloud Run — staging by default, prod behind a CI check + confirm.
#
# Usage:
#   ./scripts/deploy.sh                    # -> topstocks-staging
#   ./scripts/deploy.sh prod               # -> topstocks (gated)
#   ./scripts/deploy.sh prod --min-instances 0    # accept cold starts, save €
#   ./scripts/deploy.sh staging --secret topstocks-secrets-staging:3
#
# Both services are source deploys of this checkout (same as the manual
# command in the runbook). Settings the flags below don't mention are
# preserved from the service's current configuration — including the
# STREAMLIT_SECRETS_TOML secret binding, which is pinned to a Secret Manager
# *version*: publishing a new secret version does nothing until a deploy (or
# --secret here) points at it.
#
# Staging caveat: Google OIDC redirects to the exact URI in the secret, so
# login on staging works only with a staging secret whose [auth] redirect_uri
# is the staging URL (and that URI registered on the OAuth client). Everything
# outside login works with the prod secret.
#
# Requires: gcloud authed on the project; gh CLI (prod gate) authenticated.

set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${STOCKS_GCP_PROJECT:-topstocks-507209}"
REGION="${STOCKS_GCP_REGION:-europe-west1}"

ENV="${1:-staging}"
shift || true

MIN_INSTANCES=""
SECRET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --min-instances) MIN_INSTANCES="$2"; shift 2 ;;
        --secret) SECRET="$2"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
done

case "$ENV" in
    prod)
        SERVICE="${STOCKS_GCP_SERVICE:-topstocks}"
        # min 1 keeps one instance warm: Streamlit's cold start (container
        # boot + first session) is seconds, long enough to lose a visitor.
        MIN_INSTANCES="${MIN_INSTANCES:-1}"
        # One replica, not three: a Streamlit session lives in the instance
        # that holds its websocket, and the file-upload PUT is a separate HTTP
        # request. Cloud Run's session affinity is best-effort, so with more
        # than one instance the upload regularly lands on the wrong replica
        # and fails with "Invalid session_id" (a red file chip in the chat and
        # Import pages). Concurrency is 80 — one instance is plenty here.
        MAX_INSTANCES=1
        ;;
    staging)
        SERVICE="${STOCKS_GCP_SERVICE:-topstocks}-staging"
        MIN_INSTANCES="${MIN_INSTANCES:-0}" # scale-to-zero: staging is free when idle
        MAX_INSTANCES=1
        ;;
    *) echo "usage: $0 [staging|prod] [--min-instances N] [--secret NAME:VER]" >&2
       exit 1 ;;
esac

if [ "$ENV" = "prod" ]; then
    SHA="$(git rev-parse HEAD)"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "error: uncommitted changes — prod deploys ship exactly one commit" >&2
        exit 1
    fi
    # The CI gate: this commit must have a green `ci` run on GitHub.
    STATUS="$(gh run list --workflow ci --commit "$SHA" \
        --json conclusion --jq '.[0].conclusion' 2>/dev/null || echo none)"
    if [ "$STATUS" != "success" ]; then
        echo "error: no green CI run for $SHA (found: $STATUS)." >&2
        echo "Push the commit and wait for ci.yml, or check: gh run list" >&2
        exit 1
    fi
    printf "Deploy %s to PRODUCTION (%s)? [y/N] " "${SHA:0:10}" "$SERVICE"
    read -r reply
    case "$reply" in y|Y|yes) ;; *) echo "aborted"; exit 1 ;; esac
fi

ARGS=(
    run deploy "$SERVICE"
    --source .
    --project "$PROJECT"
    --region "$REGION"
    --port 8080
    --session-affinity           # Streamlit needs sticky sessions
    --min-instances "$MIN_INSTANCES"
    --max-instances "$MAX_INSTANCES"
)
if [ -n "$SECRET" ]; then
    ARGS+=(--update-secrets "STREAMLIT_SECRETS_TOML=$SECRET")
fi

echo "gcloud ${ARGS[*]}"
gcloud "${ARGS[@]}"

echo
gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --format 'value(status.url)'
echo "Smoke: curl <url>/healthz && curl <url>/status"
