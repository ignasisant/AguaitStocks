"""Observability: structured logs that Cloud Logging can index and query.

Production runs on Cloud Run, which already ships every line the container
writes to stdout/stderr into Cloud Logging — but as free text, so the only way
to answer "which page is slow" or "how often does the free LLM chain fall
through" was to eyeball a wall of prints. This module turns those lines into
one JSON object per event, which Cloud Run parses into structured fields:

    {"severity": "INFO", "message": "page.render", "event": "page.render",
     "duration_ms": 812, "page": "Cartera", "user": "a_b_c_com_1f2e3d4c"}

Anything under a top-level key lands in `jsonPayload.<key>`, so the query side
(``stocks logs``, see stocks.logs_query) can filter on it directly instead of
substring-matching prose.

Usage:

    from stocks import obs

    obs.setup()                                  # once, at process start
    obs.bind(user=..., page=...)                 # ambient fields for this run
    obs.event("chat.request", provider="claude") # a point-in-time fact
    with obs.timed("page.render", page=title):   # adds duration_ms + ok
        ...
    with obs.swallow("logo.mirror", ticker=t):   # log instead of silent pass
        ...

Local runs get a compact human line instead of JSON. Nothing here imports
Streamlit or any third-party package: the CLI, the workers and the dashboard
all share one setup.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

# Cloud Run sets these on every instance; their presence is how we tell prod
# from a laptop without a flag of our own.
SERVICE = os.getenv("K_SERVICE", "")
REVISION = os.getenv("K_REVISION", "")
IN_CLOUD_RUN = bool(SERVICE)

log = logging.getLogger("stocks")

# Python level -> the severity strings Cloud Logging understands.
_SEVERITY = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

# Error Reporting only groups an entry when it carries this @type and a message
# whose body is a real stack trace.
_ERROR_EVENT_TYPE = (
    "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
)

# Everything logging puts on a record by itself; whatever else a caller passed
# through `extra=` is ours to promote into the payload.
_STD_ATTRS = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName
    relativeCreated stack_info taskName thread threadName""".split()
)

# Libraries that log a line per HTTP call or per retry; at INFO they bury the
# app's own events (and on Cloud Run, cost money to store).
_NOISY = ("botocore", "boto3", "s3transfer", "urllib3", "httpx", "httpcore",
          "matplotlib", "PIL", "asyncio", "watchdog", "peewee")

# None rather than {} — a mutable ContextVar default is shared across every
# context that never set it, so one bind() would leak into all of them.
_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "stocks_obs_ctx", default=None
)
_configured = False


# ------------------------------------------------------------------ formatters


def _fields() -> dict:
    """The ambient context of the current thread/task (never None)."""
    return _ctx.get() or {}


def _extras(record: logging.LogRecord) -> dict:
    return {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS}


class CloudLoggingFormatter(logging.Formatter):
    """One JSON object per line, in the shape Cloud Run's agent destructures."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict = {
            "severity": _SEVERITY.get(record.levelno, record.levelname),
            "logger": record.name,
        }
        payload.update(_fields())
        payload.update(_extras(record))
        if record.exc_info:
            # Error Reporting parses the traceback out of `message`, so it has
            # to be inline; the separate field keeps it queryable on its own.
            trace = self.formatException(record.exc_info)
            payload["stack_trace"] = trace
            message = f"{message}\n{trace}"
            if record.levelno >= logging.ERROR:
                # Only real errors should open an Error Reporting group; a
                # swallowed-and-degraded path logs its trace at WARNING and
                # must not page anyone.
                payload["@type"] = _ERROR_EVENT_TYPE
        payload["message"] = message
        payload["logging.googleapis.com/sourceLocation"] = {
            "file": record.pathname,
            "line": str(record.lineno),
            "function": record.funcName,
        }
        if REVISION:
            payload["revision"] = REVISION
        # default=str so a stray Path/Decimal/datetime never costs us the line.
        return json.dumps(payload, default=str, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Human-readable dev output: `12:04:31 WARN stocks.web.llm msg k=v`."""

    def format(self, record: logging.LogRecord) -> str:
        fields = {**_fields(), **_extras(record)}
        fields.pop("event", None)  # already the message
        tail = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
        line = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname[:5]:<5} "
            f"{record.name} {record.getMessage()}"
        )
        if tail:
            line = f"{line}  {tail}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


# ----------------------------------------------------------------------- setup


def setup(level: str | int | None = None, *, force: bool = False) -> None:
    """Install the root handler. Idempotent — safe on every Streamlit rerun.

    Level comes from STOCKS_LOG_LEVEL (default INFO); set it to DEBUG on a
    revision to turn up detail without a code change.
    """
    global _configured
    if _configured and not force:
        return
    lvl = level or os.getenv("STOCKS_LOG_LEVEL", "INFO")
    if isinstance(lvl, str):
        lvl = getattr(logging, lvl.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter() if IN_CLOUD_RUN else PlainFormatter())
    handler.set_name("stocks-obs")

    root = logging.getLogger()
    for h in list(root.handlers):
        if h.get_name() == "stocks-obs":
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(lvl)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)  # DeprecationWarning et al become log records
    _configured = True
    if IN_CLOUD_RUN:
        # One line per cold start — how you tell "the app is slow" from "the app
        # keeps restarting". Skipped locally, where it would only prepend noise
        # to every CLI command's output.
        event(
            "process.start",
            service=SERVICE,
            revision=REVISION or None,
            python=sys.version.split()[0],
            pid=os.getpid(),
        )


# --------------------------------------------------------------------- context


def bind(**fields) -> None:
    """Merge ambient fields into every subsequent record on this thread/task."""
    _ctx.set({**_fields(), **{k: v for k, v in fields.items() if v is not None}})


def unbind(*names: str) -> None:
    _ctx.set({k: v for k, v in _fields().items() if k not in names})


def current() -> dict:
    """The fields bound right now (a copy)."""
    return dict(_fields())


@contextmanager
def context(**fields) -> Iterator[None]:
    """Bind fields for the duration of a block, then restore what was there."""
    token = _ctx.set({**_fields(), **fields})
    try:
        yield
    finally:
        _ctx.reset(token)


def new_id() -> str:
    """Short opaque id for correlating the records of one session/request."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------- events


def event(name: str, *, level: int = logging.INFO, **fields) -> None:
    """Record one named fact. `name` is both the message and `jsonPayload.event`."""
    log.log(level, name, extra={"event": name, **fields})


def warn(name: str, **fields) -> None:
    event(name, level=logging.WARNING, **fields)


def error(name: str, exc: BaseException | None = None, **fields) -> None:
    """An error worth paging through later; `exc` attaches the stack trace."""
    log.error(name, exc_info=exc, extra={"event": name, **_exc_fields(exc), **fields})


def _exc_fields(exc: BaseException | None) -> dict:
    if exc is None:
        return {}
    return {"error_type": type(exc).__name__, "error": str(exc)[:500]}


@contextmanager
def timed(
    name: str,
    *,
    passthrough: tuple[type[BaseException], ...] = (),
    **fields,
) -> Iterator[dict]:
    """Time a block and log it with `duration_ms` and `ok`.

    Yields a mutable dict: anything added to it inside the block joins the
    record (row counts, cache hits, the model that actually answered).
    Exceptions are logged as errors and re-raised — this measures, it does not
    swallow.

    `passthrough` names exception types that are control flow rather than
    failure (Streamlit's st.stop()/st.rerun() raise, and a page that stops
    early is a normal run): they are timed as successes and re-raised
    untouched.
    """
    extra: dict = dict(fields)
    started = time.perf_counter()

    def ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        yield extra
    except BaseException as exc:  # noqa: BLE001 — logged, then re-raised
        if passthrough and isinstance(exc, passthrough):
            log.info(name, extra={"event": name, "duration_ms": ms(), "ok": True,
                                  "stopped": True, **extra})
            raise
        log.error(
            name,
            exc_info=exc,
            extra={"event": name, "duration_ms": ms(), "ok": False,
                   **_exc_fields(exc), **extra},
        )
        raise
    else:
        log.info(name, extra={"event": name, "duration_ms": ms(), "ok": True, **extra})


@contextmanager
def swallow(name: str, *, level: int = logging.WARNING, **fields) -> Iterator[dict]:
    """`except Exception: pass`, but the exception leaves a trace.

    For the genuinely optional paths (a logo mirror, a nice-to-have enrichment)
    where failing loudly would be worse than degrading. Everything else should
    use `timed` and let the error propagate.
    """
    extra: dict = dict(fields)
    try:
        yield extra
    except Exception as exc:  # noqa: BLE001 — that is the whole point
        log.log(
            level, name, exc_info=exc,
            extra={"event": name, "ok": False, **_exc_fields(exc), **extra},
        )
