# TopStocks — container image for any container host. The live deploy runs it
# on an Oracle Always Free VM behind Caddy (see deploy/). Serves the Streamlit
# dashboard on $PORT (default 8501).
#
# Secrets: bind-mount the real .streamlit/secrets.toml (what deploy/ does), or
# set STREAMLIT_SECRETS_TOML to its full contents and the entrypoint writes it
# to disk at boot. [storage] may alternatively come in as individual
# STOCKS_STORAGE_* env vars (see src/stocks/storage.py).

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# HF Spaces run the container as UID 1000 regardless of USER; everything under
# /app must belong to it (data/ price caches, static/logos mirror, the
# secrets.toml bootstrap, R2 restores of watchlist.yaml + data/users/).
# Create + own /app as root FIRST: the classic (non-BuildKit) Docker builder
# used by Cloud Build makes WORKDIR dirs root-owned, so uv sync's .venv write
# would fail with EACCES unless /app is chowned to appuser beforehand.
RUN useradd -m -u 1000 appuser && mkdir -p /app && chown appuser:appuser /app
USER appuser
ENV HOME=/home/appuser
WORKDIR /app

# Dependency layer first: code edits reuse the resolved-lockfile layer.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser . .
# Editable install of the project itself (default): stocks.config.PROJECT_ROOT
# resolves to /app, so data/ and watchlist.yaml live next to the source.
RUN uv sync --frozen --no-dev

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD \
    python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
