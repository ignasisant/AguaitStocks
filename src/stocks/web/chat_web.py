"""Web search for the assistant panel (web/chat_core.py).

Keyless like the free chain: DuckDuckGo via the ``ddgs`` package, no API key
and no per-search billing. A *planner* call through the provider's cheapest
model (the same pattern as the skill auto-router) decides per message whether
the answer needs fresh information from the web and emits at most MAX_QUERIES
search queries; the hits are appended to the outgoing copy of the user's
message — not the system prompt, so provider prompt caches stay warm — and the
system prompt (chat_core._system_prompt) tells the model to ground on them and
cite URLs.

The top hits are then *opened*: their article text (not DDG's two-sentence
snippet) is what reaches the model, which is the difference between citing a
headline and citing what the page actually says. Boilerplate removal is
trafilatura's (optional dep, lxml fallback below) — with only _PAGE_CHARS of
each page reaching the prompt, nav and cookie walls are budget stolen from
the numbers. Links the user pastes skip
the planner entirely and are always read.

Everything degrades to "no web": a missing ddgs install, a search error or an
empty result set all yield [], and the answer proceeds on the model's own
knowledge plus the app context. A planner *failure* is the one case that does
not silently drop the web — a keyword heuristic (`heuristic_queries`) takes
over, so a dead classifier model costs relevance, not internet access.

Streamlit-free so it stays trivially testable, like chat_skills/chat_actions.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import field_validator

from stocks import obs
from stocks.chat import structured

if TYPE_CHECKING:
    from stocks.web.llm import Provider

MAX_QUERIES = 2  # searches the planner may request per message
MAX_RESULTS = 6  # hits injected into the prompt, across all queries
_PER_QUERY = 4  # hits fetched per query before the global cap
_SNIPPET_CHARS = 320  # per-hit body text kept in the prompt

# Reading the pages, not just the result list. A DDG snippet is two sentences
# of whatever the crawler indexed — enough to know a page exists, never enough
# to answer "what did they guide to?". The top hits are therefore fetched and
# stripped to text, which is the difference between citing a headline and
# citing the article.
READ_PAGES = 3  # result pages actually opened per message
_PAGE_CHARS = 1800  # article text kept per page
_MIN_ARTICLE_CHARS = 200  # shorter than this and an extraction counts as a miss
_PAGE_TIMEOUT = 6.0  # seconds per page; the batch runs concurrently
_MAX_PAGE_BYTES = 2_000_000  # stop reading a stream that big — it is not an article
_BROWSER_UA = (  # news sites 403 the toolkit's own UA
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


def available() -> bool:
    """True when the ddgs package is installed (search is keyless)."""
    return importlib.util.find_spec("ddgs") is not None


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str
    text: str = ""  # article body when the page was read (see `read_pages`)

    @property
    def body(self) -> str:
        """What goes in the prompt: the article when we have it, else DDG's
        snippet."""
        return self.text or self.snippet


# ------------------------------------------------------------- planner

_PLANNER_SYSTEM = (
    "You decide whether answering the latest message in a stock-tracker chat "
    "needs fresh information from the web — news, prices beyond the app "
    "context, current events, recent filings or releases, or anything likely "
    "newer than the model's training data. Reply with ONLY a JSON object of "
    f'the form {{"queries": [...]}} holding at most {MAX_QUERIES} web search '
    "queries — or an empty list when the message needs none (greetings, app "
    "questions, the user's own positions, math, long-settled facts). Write "
    "queries in the language most likely to find good sources (usually "
    "English), include tickers or company names, and put the current year in "
    "time-sensitive queries. No prose, no code fences."
)


class QueryPlan(structured.Contract):
    """The planner's contract: at most MAX_QUERIES cleaned search queries.

    The list is cleaned rather than rejected — a stray non-string or a
    duplicate is the model being sloppy inside a shape it got right, and a
    second call would buy nothing. A missing or non-list "queries" *is* a
    rejection: that is the model answering a different question. Capping is
    the caller's, so a caller asking for a different limit still gets one.
    """

    queries: list[str]

    @field_validator("queries", mode="before")
    @classmethod
    def _clean(cls, v):
        if not isinstance(v, list):
            return v  # not a list -> let the type error reject it
        out: list[str] = []
        for q in v:
            if not isinstance(q, str):
                continue
            q = " ".join(q.split())[:200]
            if q and q not in out:
                out.append(q)
        return out


def parse_queries(raw: str, limit: int = MAX_QUERIES) -> list[str]:
    """Search queries out of a planner reply, defensively.

    The tolerant reading, kept for callers that hold a reply and no provider:
    anything unparsable means no search, never an error. `plan` uses the
    contract directly so it can tell "no search needed" from "unreadable".
    """
    try:
        return structured.decode(raw, QueryPlan).queries[:limit]
    except structured.OffContract:
        return []


# Fallback when the planner cannot answer. The planner is one model call, and
# model calls fail: a spent free-tier quota, a retired backend model, a 429.
# Losing web access every time the *cheap* model is down is the worst outcome
# — the expensive one is still there and would happily ground on results. So a
# failed planner drops to keywords: no LLM, no network, just "does this look
# like it needs today's information".
_FRESH_RE = re.compile(
    r"news|noticia|headline|titular|\btoday\b|\bhoy\b|\bnow\b|\bahora\b|"
    r"latest|[uú]ltim|reciente|recent|this (week|month|year)|"
    r"est[ae] (semana|mes|a[ñn]o)|\bprice\b|precio|cotiza|quote|earnings|"
    r"resultados|guidance|analyst|analista|rating|price target|objetivo|"
    r"upgrade|downgrade|rumor|forecast|previsi|outlook|perspectiv|dividend|"
    r"split|merger|fusi[oó]n|acquisi|adquisi|\bipo\b|filing|10-[kq]|8-k|"
    r"\bsec\b|\bfed\b|tipos de inter|\brates\b|inflation|inflaci|market|"
    r"mercado|why .{0,30}(up|down|drop|fell|rose|surge)|"
    r"por qu[eé] .{0,30}(sub|baj|cay|dispar)|\b20\d\d\b",
    re.IGNORECASE,
)
_FOCUS_RE = re.compile(r"ticker in focus is ([A-Z0-9][A-Z0-9.\-]{0,14})", re.IGNORECASE)
_CAPS_RE = re.compile(r"\b[A-Z]{2,5}(?:[.\-][A-Z]{1,4})?\b")
_TODAY_RE = re.compile(r"Today is (\d{4})-\d{2}-\d{2}")


def heuristic_queries(question: str, context: str = "") -> list[str]:
    """One search query built without a model, or [].

    Fires only when the message looks time-sensitive (or names a ticker in
    caps): everything else — greetings, "what is a P/E", questions about the
    user's own book — is answered from the prompt as before."""
    try:  # the same "that caps word is not a ticker" screen the quotes use
        from stocks.chat.market import NOT_TICKERS
    except Exception:  # pragma: no cover - defensive
        NOT_TICKERS = set()
    q = " ".join(question.split())
    names_ticker = any(t not in NOT_TICKERS for t in _CAPS_RE.findall(q))
    if not (_FRESH_RE.search(q) or names_ticker):
        return []
    focus = _FOCUS_RE.search(context)
    year = _TODAY_RE.search(context)
    parts = [q.strip("¿?¡!. ")[:160]]
    ticker = focus.group(1).upper().rstrip(".") if focus else ""
    if ticker and ticker not in q.upper():
        parts.insert(0, ticker)
    if year and year.group(1) not in q:
        parts.append(year.group(1))
    return [" ".join(parts)]


def plan(
    provider: Provider, api_key: str, question: str, context: str = ""
) -> list[str]:
    """Search queries for a message, via the provider's cheapest model.

    [] when the planner decides no search is needed — an explicit empty plan is
    obeyed, never second-guessed. When the planner *call* fails, or is still
    off-contract after the repair turn, the keyword heuristic decides instead:
    a dead classifier model must not silently take the web away."""
    user = (context + "\n\n" if context else "") + f"User message: {question}"
    try:
        chosen = structured.ask(provider, api_key, _PLANNER_SYSTEM, user,
                                QueryPlan)
        return chosen.queries[:MAX_QUERIES]
    except Exception:  # off-contract, network, quota — all cost relevance only
        return heuristic_queries(question, context)


# ------------------------------------------------------------- search


def search(queries: list[str], read_limit: int = READ_PAGES) -> list[Result]:
    """DuckDuckGo hits for the queries, deduped by URL and capped.

    Per-query failures are skipped (DDG throttles cloud IPs now and then, like
    Yahoo does); a dead ddgs install or a total failure returns []. With
    `read_limit`, that many top hits are also opened and their article text
    kept, so the model quotes the page rather than the search snippet."""
    if not queries or not available():
        return []
    out: list[Result] = []
    seen: set[str] = set()
    try:
        from ddgs import DDGS

        with DDGS(timeout=8) as ddg:
            for q in queries:
                try:
                    hits = ddg.text(q, max_results=_PER_QUERY)
                except Exception:
                    continue
                for h in hits or []:
                    url = (h.get("href") or h.get("url") or "").strip()
                    title = " ".join((h.get("title") or "").split())
                    body = " ".join((h.get("body") or "").split())
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    out.append(Result(title or url, url, body[:_SNIPPET_CHARS]))
    except Exception as exc:
        obs.warn("chat.web.search_failed", error_type=type(exc).__name__,
                 error=str(exc)[:300])
        # keep whatever was collected before the failure
    out = out[:MAX_RESULTS]
    return read_pages(out, read_limit) if read_limit > 0 else out


# --------------------------------------------------------- reading pages


def _normalize(text: str) -> str:
    """Whitespace collapsed per line, blank lines dropped.

    Paragraph breaks survive as single newlines — they cost one character each
    and tell the model where one claim ends and the next begins."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _extract_trafilatura(raw: bytes) -> str:
    """The article per trafilatura, or '' when it finds none / is not installed.

    This is the boilerplate remover: nav, cookie walls, related-article rails,
    newsletter forms and comment threads never reach the prompt. That matters
    because the whole page budget is _PAGE_CHARS — furniture crowds out the
    numbers the answer needs. `favor_precision` is the right side to err on
    here (and the only mode that keeps paragraphs separated rather than glued):
    a paragraph wrongly dropped costs less than a sidebar wrongly kept.

    Imported lazily and failure-swallowing like every other optional dep in
    this codebase — no trafilatura just means the lxml fallback does the job.
    """
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        text = trafilatura.extract(raw, include_comments=False,
                                   include_tables=False, favor_precision=True)
    except Exception:
        return ""
    return _normalize(text or "")


def _extract_lxml(raw: bytes) -> str:
    """Fallback extraction: paragraphs first, whole document if there are none.

    Paragraph text is what an article is; the whole document's text_content()
    is mostly nav and cookie banners, so it is only the fallback for pages
    that mark nothing up as <p>."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(raw)
    except Exception:
        return ""
    for bad in doc.xpath(
        "//script|//style|//nav|//header|//footer|//aside|//noscript|//form"
    ):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)
    paras = [" ".join(p.text_content().split()) for p in doc.xpath("//p")]
    text = " ".join(p for p in paras if len(p) > 40)
    if len(text) < _MIN_ARTICLE_CHARS:
        text = " ".join(doc.text_content().split())
    return text


def _extract(raw: bytes) -> str:
    """Readable text out of an HTML page, trimmed to the prompt budget.

    Two layers: trafilatura, then the hand-rolled lxml pass for what it
    returns nothing useful on (markup it finds no article in, a missing
    install). A short trafilatura result is treated as a miss, not as the
    answer — but it is still kept if the fallback does no better."""
    text = _extract_trafilatura(raw)
    if len(text) < _MIN_ARTICLE_CHARS:
        text = _extract_lxml(raw) or text
    return text[:_PAGE_CHARS]



def _public_host(host: str) -> bool:
    """Whether every address `host` resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def fetchable(url: str) -> bool:
    """Whether `url` may be opened at all.

    The assistant picks the URLs it reads, and part of what it picks from is
    text it read on some other page — so a hostile page can propose one.
    Unchecked, "read this link" reaches the loopback interface, the private
    network and the cloud metadata endpoint. Hence http(s) only (urllib will
    happily open file://), and a host that resolves to public addresses only,
    re-checked on every redirect (_GuardedRedirect) because a public host is
    free to send us inward.

    Not airtight: the name is resolved here and again by the socket, so a
    rebinding attacker keeps a narrow window. Closing it means connecting to
    the address this checked and carrying the hostname through TLS by hand —
    a lot of machinery for a reader of news pages.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return _public_host(parts.hostname)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Drops a redirect whose target fails `fetchable`. Returning None stops
    urllib following it; the 3xx then surfaces as an HTTPError, which every
    caller here already reads as "no text"."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not fetchable(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@lru_cache(maxsize=1)
def _opener() -> urllib.request.OpenerDirector:
    """The shared opener. build_opener swaps our handler in for the default
    redirect handler, since it subclasses it."""
    return urllib.request.build_opener(_GuardedRedirect)


def read_page(url: str, timeout: float = _PAGE_TIMEOUT) -> str:
    """The article text at `url`, or '' — a blocked host, a paywall, a 403, a
    PDF, a timeout and a parse failure are all just "no text", never an
    exception."""
    if not fetchable(url):
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        with _opener().open(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                return ""
            raw = resp.read(_MAX_PAGE_BYTES)
        return _extract(raw)
    except Exception:
        return ""


def read_pages(results: list[Result], limit: int = READ_PAGES) -> list[Result]:
    """`results` with the first `limit` hits' article text filled in.

    Fetched concurrently under one wall-clock budget: opening three pages one
    after another would add ~20s to a chat turn in the worst case, and a
    single slow host must not decide how long the answer takes."""
    if not results:
        return results
    head, tail = results[:limit], results[limit:]
    pool = ThreadPoolExecutor(max_workers=max(1, len(head)))
    try:
        futures = [pool.submit(read_page, r.url) for r in head]
        out = []
        for r, f in zip(head, futures, strict=True):
            try:
                text = f.result(timeout=_PAGE_TIMEOUT + 2)
            except Exception:
                text = ""
            out.append(Result(r.title, r.url, r.snippet, text) if text else r)
        return out + tail
    finally:
        # No wait: shutdown would block on exactly the hung fetch the timeout
        # just escaped. The threads die with the process.
        pool.shutdown(wait=False, cancel_futures=True)


def urls_in(message: str, limit: int = READ_PAGES) -> list[str]:
    """Links the user pasted, deduped and capped.

    A pasted URL is an explicit instruction to read that page, so it bypasses
    the planner entirely — there is nothing to decide."""
    out: list[str] = []
    for url in _URL_RE.findall(message):
        url = url.rstrip(".,;:)")
        if url not in out:
            out.append(url)
    return out[:limit]


def read_urls(urls: list[str]) -> list[Result]:
    """Results for pasted links, read like search hits (unreadable ones are
    still listed, so the model can say the page could not be opened)."""
    if not urls:
        return []
    return read_pages([Result(url, url, "") for url in urls])


def collect(queries: list[str], message: str = "") -> list[Result]:
    """Everything this turn should read: pasted links first, then search hits.

    Explicit beats inferred — a link the user typed is always opened, and the
    search hits share what is left of the page-reading budget, so a message
    with two links plus a search still opens READ_PAGES pages in total."""
    hits = read_urls(urls_in(message))
    seen = {h.url for h in hits}
    for r in search(queries, read_limit=max(0, READ_PAGES - len(hits))):
        if r.url not in seen:
            seen.add(r.url)
            hits.append(r)
    return hits[:MAX_RESULTS]


# ------------------------------------------------------------- prompt


def augment(message: str, results: list[Result]) -> str:
    """The user message with the search hits appended (unchanged when none).

    Only the outgoing copy of the turn is augmented — the stored history keeps
    the user's original text for display and for later context windows."""
    if not results:
        return message
    lines = [
        f"{i}. {r.title} — {r.url}" + (f"\n   {r.body}" if r.body else "")
        for i, r in enumerate(results, 1)
    ]
    return (
        message
        + "\n\n---\nWeb search results fetched for this message (ground your "
        "answer in them where relevant and cite the URLs you use):\n"
        + "\n".join(lines)
    )


def sources(results: list[Result]) -> list[dict]:
    """Compact {title, url} dicts for the history turn (JSON-persisted)."""
    return [{"title": r.title, "url": r.url} for r in results]
