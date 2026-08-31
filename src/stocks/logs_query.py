"""Query the production logs from the terminal (`stocks logs ...`).

Cloud Run ships the container's stdout/stderr to Cloud Logging, where it is
kept for 30 days in the _Default bucket. This module is the read side: it
builds a Logging filter, shells out to `gcloud logging read`, and renders the
entries as one compact line each — plus `stats` (counts and latency
percentiles per event) and `export` (a JSONL snapshot on disk, for keeping a
bad day past the retention window or grinding through it offline).

`gcloud` does the auth, so there is no key to manage: whoever is logged in with
`gcloud auth login` and can read the project's logs can run these.

Every command accepts `--file` to read an exported JSONL snapshot instead of
calling Cloud Logging, so the same rendering works with no network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from stocks.config import DATA_DIR

PROJECT = os.getenv("STOCKS_GCP_PROJECT", "topstocks-507209")
SERVICE = os.getenv("STOCKS_GCP_SERVICE", "topstocks")
LOGS_DIR = DATA_DIR / "logs"

# The per-request access log. It is one entry per HTTP call — including
# Streamlit's health checks and websocket polling — so it drowns everything
# else unless asked for explicitly (`--http`).
_REQUEST_LOG = "run.googleapis.com%2Frequests"


class LogsError(RuntimeError):
    """gcloud is missing, unauthenticated, or refused the query."""


# --------------------------------------------------------------------- filters


def build_filter(
    *,
    service: str = SERVICE,
    project: str = PROJECT,
    level: str | None = None,
    event: str | None = None,
    user: str | None = None,
    grep: str | None = None,
    http: bool = False,
    revision: str | None = None,
    extra: str | None = None,
) -> str:
    """Assemble a Cloud Logging filter string from the CLI's options."""
    parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{service}"',
    ]
    if http:
        parts.append(f'logName="projects/{project}/logs/{_REQUEST_LOG}"')
    else:
        parts.append(f'logName!="projects/{project}/logs/{_REQUEST_LOG}"')
        # Admin API calls (deploys, IAM edits) are logged against the same
        # resource and land as ERROR whenever a console call 403s. They are not
        # the app talking, so they never belong in an app-error view.
        parts.append('NOT logName:"cloudaudit"')
    if level:
        parts.append(f"severity>={level.upper()}")
    if event:
        parts.append(f'jsonPayload.event="{event}"')
    if user:
        parts.append(f'jsonPayload.user="{user}"')
    if revision:
        parts.append(f'resource.labels.revision_name="{revision}"')
    if grep:
        # Free-text lines land in textPayload, ours in jsonPayload.message; a
        # user searching for a phrase means either.
        esc = grep.replace('"', '\\"')
        parts.append(f'(textPayload:"{esc}" OR jsonPayload.message:"{esc}")')
    if extra:
        parts.append(f"({extra})")
    return " AND ".join(parts)


# ----------------------------------------------------------------------- fetch


def read(
    log_filter: str,
    *,
    project: str = PROJECT,
    freshness: str = "1h",
    limit: int = 100,
) -> list[dict]:
    """Run `gcloud logging read` and return the entries, newest first."""
    if shutil.which("gcloud") is None:
        raise LogsError(
            "gcloud not found — install the Google Cloud CLI and run "
            "`gcloud auth login` to read production logs."
        )
    cmd = [
        "gcloud", "logging", "read", log_filter,
        "--project", project,
        "--limit", str(limit),
        "--freshness", freshness,
        "--order", "desc",
        "--format", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise LogsError((proc.stderr or proc.stdout).strip()[:2000])
    return json.loads(proc.stdout or "[]")


def read_file(path: Path) -> list[dict]:
    """Entries from a JSONL snapshot written by `stocks logs export`."""
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def export(entries: Sequence[dict], out: Path | None = None) -> Path:
    """Write entries as JSONL. Default name carries the export timestamp."""
    if out is None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = LOGS_DIR / f"topstocks-{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return out


# ------------------------------------------------------------------- rendering

# Bookkeeping we render in the fixed columns or never want in the k=v tail.
_TAIL_SKIP = frozenset(
    {"message", "event", "severity", "logger", "stack_trace", "@type", "revision",
     "logging.googleapis.com/sourceLocation", "ok"}
)


def payload(entry: dict) -> dict:
    """The structured body of an entry, whatever shape it arrived in."""
    if "jsonPayload" in entry:
        return entry["jsonPayload"]
    if "textPayload" in entry:
        return {"message": entry["textPayload"]}
    if "httpRequest" in entry:
        r = entry["httpRequest"]
        return {
            "message": f"{r.get('requestMethod', '?')} {r.get('requestUrl', '?')}",
            "status": r.get("status"),
            "latency": r.get("latency"),
            "ip": r.get("remoteIp"),
        }
    return {"message": json.dumps(entry.get("protoPayload", ""))[:200]}


# Fixed-width severity so the columns line up; Cloud Logging's own names are
# too long to sit in a terminal line next to the message.
_SEV = {"DEFAULT": "log", "DEBUG": "DEBUG", "INFO": "INFO", "NOTICE": "NOTE",
        "WARNING": "WARN", "ERROR": "ERROR", "CRITICAL": "CRIT", "ALERT": "ALERT",
        "EMERGENCY": "EMERG"}


def _local_time(ts: str) -> str:
    try:
        return (
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%m-%d %H:%M:%S")
        )
    except (ValueError, TypeError):
        return (ts or "")[:19]


def format_entry(entry: dict, *, show_trace: bool = False) -> str:
    """One line per entry: time, severity, event/logger, message, then k=v."""
    body = payload(entry)
    raw_sev = entry.get("severity") or "DEFAULT"
    sev = _SEV.get(raw_sev, raw_sev[:5])
    name = body.get("event") or body.get("logger") or ""
    msg = str(body.get("message", "")).split("\n")[0][:160]
    tail = " ".join(
        f"{k}={v}"
        for k, v in body.items()
        if k not in _TAIL_SKIP and v not in (None, "")
    )
    line = f"{_local_time(entry.get('timestamp', ''))} {sev:<5} {name:<26} {msg}"
    if tail:
        line = f"{line}  {tail}"
    if show_trace and body.get("stack_trace"):
        line = f"{line}\n{body['stack_trace'].rstrip()}"
    return line


def render(entries: Iterable[dict], *, show_trace: bool = False) -> str:
    """Oldest first — reading a timeline top-to-bottom beats newest-first."""
    rows = list(entries)[::-1]
    return "\n".join(format_entry(e, show_trace=show_trace) for e in rows)


# --------------------------------------------------------------------- summary


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile; statistics.quantiles needs n>=2."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def stats(entries: Iterable[dict], *, by: str = "event") -> list[dict]:
    """Per-group counts, error counts and latency percentiles.

    Groups on `jsonPayload.<by>` (default the event name); entries without
    that field are grouped under "-" so free-text noise stays visible rather
    than silently dropping out of the totals.
    """
    groups: dict[str, dict] = {}
    for entry in entries:
        body = payload(entry)
        key = str(body.get(by) or "-")
        g = groups.setdefault(key, {"key": key, "count": 0, "errors": 0, "ms": []})
        g["count"] += 1
        if (entry.get("severity") or "") in ("ERROR", "CRITICAL"):
            g["errors"] += 1
        ms = body.get("duration_ms")
        if isinstance(ms, (int, float)):
            g["ms"].append(float(ms))
    out = []
    for g in groups.values():
        ms = g.pop("ms")
        out.append({
            **g,
            "p50_ms": round(_pct(ms, 0.50)) if ms else None,
            "p95_ms": round(_pct(ms, 0.95)) if ms else None,
            "max_ms": round(max(ms)) if ms else None,
        })
    return sorted(out, key=lambda g: g["count"], reverse=True)


def usage(entries: Iterable[dict]) -> dict:
    """Product-usage rollup out of the ops log — who used what, per day.

    Counts on the events the app already emits (no extra tracking): a
    "user" is any entry carrying the pseudonymous account slug, a "run" is
    one page render, a chat turn is one chat.request, feedback is the
    sidebar widget. Guests show up under the "guest"/"-" slugs like
    everywhere else in the logs.
    """
    days: dict[str, dict] = {}
    pages: dict[str, int] = {}
    all_users: set[str] = set()
    for entry in entries:
        body = payload(entry)
        day = str(entry.get("timestamp", ""))[:10] or "-"
        d = days.setdefault(
            day, {"day": day, "users": set(), "runs": 0, "chat": 0, "feedback": 0}
        )
        user = str(body.get("user") or "")
        if user and user != "-":
            d["users"].add(user)
            all_users.add(user)
        event = body.get("event")
        if event == "page.render":
            d["runs"] += 1
            page = str(body.get("page") or "-")
            pages[page] = pages.get(page, 0) + 1
        elif event == "chat.request":
            d["chat"] += 1
        elif event == "feedback":
            d["feedback"] += 1
    rows = [
        {**d, "users": len(d["users"])}
        for _, d in sorted(days.items())
        if d["day"] != "-" or d["runs"] or d["chat"]
    ]
    top_pages = sorted(pages.items(), key=lambda kv: kv[1], reverse=True)
    return {"days": rows, "pages": top_pages, "total_users": len(all_users)}


def render_usage(summary: dict) -> str:
    if not summary["days"]:
        return "(no entries in range)"
    head = f"{'day':<12} {'users':>6} {'page runs':>10} {'chat':>6} {'feedback':>9}"
    lines = [head, "-" * len(head)]
    for d in summary["days"]:
        lines.append(
            f"{d['day']:<12} {d['users']:>6} {d['runs']:>10} "
            f"{d['chat']:>6} {d['feedback']:>9}"
        )
    lines.append(f"\nunique users in range: {summary['total_users']}")
    if summary["pages"]:
        lines.append("\npage runs:")
        for page, count in summary["pages"]:
            lines.append(f"  {page:<28} {count:>6}")
    return "\n".join(lines)


def render_stats(rows: Sequence[dict], *, by: str = "event") -> str:
    if not rows:
        return "(no entries in range)"
    head = f"{by:<32} {'count':>7} {'errors':>7} {'p50':>8} {'p95':>8} {'max':>8}"
    lines = [head, "-" * len(head)]
    for r in rows:
        def ms(v):
            return "-" if v is None else f"{v}ms"
        lines.append(
            f"{r['key'][:32]:<32} {r['count']:>7} {r['errors']:>7} "
            f"{ms(r['p50_ms']):>8} {ms(r['p95_ms']):>8} {ms(r['max_ms']):>8}"
        )
    return "\n".join(lines)
