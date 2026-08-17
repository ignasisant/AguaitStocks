#!/bin/sh
# Container boot: materialize Streamlit secrets from the environment, then
# launch the dashboard.
#
# STREAMLIT_SECRETS_TOML — full contents of .streamlit/secrets.toml ([auth],
# [app], [storage], [chat], [free_llm], [telegram]). Container hosts inject
# secrets as env vars while st.secrets only reads files, so we write it out
# here. Left unset, the app boots without secrets (auth setup screen), or
# with a mounted .streamlit/secrets.toml when running docker locally.
set -eu
cd "$(dirname "$0")/.."

if [ -n "${STREAMLIT_SECRETS_TOML:-}" ]; then
    mkdir -p .streamlit
    umask 077
    printf '%s\n' "$STREAMLIT_SECRETS_TOML" > .streamlit/secrets.toml
    umask 022
fi

exec .venv/bin/streamlit run src/stocks/web/app.py \
    --server.port "${PORT:-8501}" \
    --server.address 0.0.0.0
