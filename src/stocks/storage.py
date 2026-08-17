"""Optional S3-compatible persistence for user data (Cloudflare R2, S3, MinIO).

Deploy targets with ephemeral filesystems (containers, Streamlit Community
Cloud) lose data/users/ and the imported ledgers on every restart or
redeploy. When a bucket is configured, every user-data write is mirrored to
it (persist) and each account's files are pulled back on first access after
a boot (restore_user). Unconfigured, every function is a no-op, so local
dev, tests and the CLI keep working on the plain filesystem.

Configuration — [storage] in .streamlit/secrets.toml, or the equivalent
STOCKS_STORAGE_* environment variables (env wins):

    [storage]
    endpoint_url      = "https://<account-id>.r2.cloudflarestorage.com"
    bucket            = "aguait-user-data"
    access_key_id     = "..."
    secret_access_key = "..."
    # region = "auto"        # default; use e.g. "eu-west-1" for AWS S3

Object keys are the file paths relative to the repo root (e.g.
"data/users/jane_example_com/portfolio.db", or "watchlist.yaml" for the
owner account), so one bucket mirrors both the per-user dirs and the
owner's repo-root book.

Consistency model: single app container. On the first touch of an account
per process the bucket copy wins (the local checkout only has git-seeded or
no files); from then on the local file is authoritative and every write
pushes, so the bucket is always current for the next boot.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from stocks.config import PROJECT_ROOT

_ENV_PREFIX = "STOCKS_STORAGE_"
_lock = threading.Lock()
_restored: set[str] = set()
_cached: dict[str, object] = {}


def _secrets_section() -> dict:
    """[storage] from .streamlit/secrets.toml, {} when unavailable.

    Imported lazily so the CLI never pays for (or requires) streamlit.
    """
    try:
        import streamlit as st

        return dict(st.secrets.get("storage", {}))
    except Exception:
        return {}


def _config() -> dict | None:
    """Resolved storage settings, or None when persistence is off."""
    if "config" not in _cached:
        section = {
            k: str(os.environ.get(_ENV_PREFIX + k.upper()) or v).strip()
            for k, v in (
                {
                    "endpoint_url": "",
                    "bucket": "",
                    "access_key_id": "",
                    "secret_access_key": "",
                    "region": "auto",
                }
                | _secrets_section()
            ).items()
        }
        needed = ("bucket", "access_key_id", "secret_access_key")
        _cached["config"] = section if all(section.get(k) for k in needed) else None
    return _cached["config"]  # type: ignore[return-value]


def enabled() -> bool:
    return _config() is not None


def _client():
    """One boto3 S3 client per process."""
    if "client" not in _cached:
        try:
            import boto3
        except ImportError as e:  # configured but dependency missing: fail loud
            raise RuntimeError(
                "[storage] is configured but boto3 is not installed — "
                "run `uv sync` (boto3 is a main dependency)."
            ) from e
        cfg = _config() or {}
        _cached["client"] = boto3.client(
            "s3",
            endpoint_url=cfg.get("endpoint_url") or None,
            region_name=cfg.get("region") or "auto",
            aws_access_key_id=cfg["access_key_id"],
            aws_secret_access_key=cfg["secret_access_key"],
        )
    return _cached["client"]


def _key(path: Path) -> str | None:
    """Bucket key for a local file: its path relative to the repo root.

    None (skip) for paths outside the repo — nothing personal lives there.
    """
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return None


def persist(path: Path) -> None:
    """Mirror one local write to the bucket (delete when the file is gone).

    Call after the local write has fully committed (file closed / connection
    committed). No-op when storage is unconfigured. Errors propagate: a
    silent persist failure would look saved and still vanish on restart.
    """
    if not enabled():
        return
    key = _key(path)
    if key is None:
        return
    cfg = _config() or {}
    if path.exists():
        _client().put_object(Bucket=cfg["bucket"], Key=key, Body=path.read_bytes())
    else:
        _client().delete_object(Bucket=cfg["bucket"], Key=key)


def restore(path: Path) -> bool:
    """Pull one file from the bucket, overwriting local. True when it existed."""
    if not enabled():
        return False
    key = _key(path)
    if key is None:
        return False
    cfg = _config() or {}
    client = _client()
    try:
        obj = client.get_object(Bucket=cfg["bucket"], Key=key)
    except client.exceptions.NoSuchKey:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(obj["Body"].read())
    return True


def restore_dir(directory: Path) -> None:
    """Pull every bucket object directly under `directory` the first time it
    is touched this process.

    For flat pools of generated files whose names aren't known up front
    (mirrored logos: the extension depends on what the host served); fixed
    -name user data uses restore_once. Existing local files win — a running
    process has fresher mirrors than the bucket. Nested keys are skipped.
    """
    if not enabled():
        return
    prefix = _key(directory)
    if prefix is None:
        return
    tag = f"dir:{directory.resolve()}"
    with _lock:
        if tag in _restored:
            return
        _restored.add(tag)
    cfg = _config() or {}
    client = _client()
    try:
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=cfg["bucket"], Prefix=prefix + "/"
        )
        for page in pages:
            for obj in page.get("Contents", []):
                name = obj["Key"].removeprefix(prefix + "/")
                if not name or "/" in name or name in (".", ".."):
                    continue
                dest = directory / name
                if dest.exists():
                    continue
                body = client.get_object(Bucket=cfg["bucket"], Key=obj["Key"])["Body"]
                directory.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body.read())
    except Exception:
        with _lock:  # let the next touch retry instead of caching a half-restore
            _restored.discard(tag)
        raise


def read_key(key: str) -> bytes | None:
    """One object's bytes by bucket key, or None (missing / storage off).

    For bucket-only data with no local mirror (the Telegram update queue);
    file-backed data goes through restore().
    """
    if not enabled():
        return None
    cfg = _config() or {}
    client = _client()
    try:
        return client.get_object(Bucket=cfg["bucket"], Key=key)["Body"].read()
    except client.exceptions.NoSuchKey:
        return None


def delete_key(key: str) -> None:
    """Delete one object by bucket key. No-op when storage is unconfigured."""
    if not enabled():
        return
    cfg = _config() or {}
    _client().delete_object(Bucket=cfg["bucket"], Key=key)


def list_keys(prefix: str = "") -> list[str]:
    """All bucket keys under `prefix` ([] when storage is unconfigured).

    Used by headless jobs to enumerate accounts (data/users/<slug>/...)
    without a local checkout of the user data.
    """
    if not enabled():
        return []
    cfg = _config() or {}
    keys: list[str] = []
    pages = _client().get_paginator("list_objects_v2").paginate(
        Bucket=cfg["bucket"], Prefix=prefix
    )
    for page in pages:
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


def restore_once(group: Path, files: tuple[Path, ...]) -> None:
    """Restore a group of files the first time `group` is touched this process.

    After that the local copies are authoritative (every write persists), so
    reruns and later sessions skip the bucket round-trips.
    """
    if not enabled():
        return
    tag = str(group.resolve())
    with _lock:
        if tag in _restored:
            return
        _restored.add(tag)
    try:
        for f in files:
            restore(f)
    except Exception:
        with _lock:  # let the next touch retry instead of caching a half-restore
            _restored.discard(tag)
        raise
