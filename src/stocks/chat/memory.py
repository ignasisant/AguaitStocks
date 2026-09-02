"""Long-term memory for the chat: past conversations, searchable by meaning.

The thread the model sees is the last MAX_CONTEXT_MSGS messages of the current
conversation (chat/engine.py). Everything before that — the other threads, last
month's reasoning about a position, the conclusion the user asked for in March
— is on disk and invisible. Asking "what did I decide about ASML?" gets an
answer built from nothing.

This is the index that makes it visible: every turn worth keeping is embedded
and stored next to the account's other files, and `recall` finds the ones that
mean the same thing as a question, not the ones sharing its words. It is
surfaced as one more tool the gather step may call (chat/toolbox.py), so the
model reaches for it when a question sounds like it has history, and ignores it
otherwise.

Two small dependencies do the work, both chosen so this survives a Cloud Run
cold start with no network and no GPU:

- **model2vec** (`potion-base-32M`, 512 dims) — static embeddings: a lookup
  table, not a transformer forward pass. CPU, milliseconds per turn, no torch.
  Baked into the image like the tiktoken table; it costs ~250MB there, which
  the smaller potion-base-8M would not — but 8M ranked an unrelated answer
  above the right one on the first question tried, and a memory that returns
  the wrong memory is worse than no memory.
- **sqlite-vec** — vector search as a SQLite extension. No server, no daemon,
  one file per account that syncs to the bucket with everything else.

Search is **hybrid**: the vectors above, fused with SQLite's own FTS5 keyword
index. Neither half is enough on its own. The static model is trained on
English and misranks Spanish — "¿qué decidí sobre ASML?" put an unrelated
Nvidia answer first in testing — while a keyword index cannot find "the
conclusion about concentration" from "how much of one thing is too much". But
tickers, company names and numbers are exactly what a user asks about later,
and those the keyword half nails in any language. Ranks are combined with
reciprocal rank fusion, which needs no score calibration between two searches
whose numbers mean different things.

Both dependencies are optional. Without them `available()` is False, the tool
is never offered, and the chat behaves exactly as it did before.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from stocks import obs

FILE = "chat_memory.db"
MODEL = "minishlab/potion-base-32M"
DIM = 512

# Turns shorter than this are not memories — "ok", "gracias", "y eso?" carry no
# meaning to find later, and every one of them dilutes a search.
MIN_CHARS = 60
# One turn's share of the index. A long answer's opening paragraphs are what
# makes it findable; the rest is detail the model can re-derive.
MAX_CHARS = 1500

RECALL_LIMIT = 4  # memories returned per search

# Reciprocal rank fusion's damping constant. The literature's 60 was tuned on
# TREC runs of hundreds of documents; against the handful of candidates each
# half returns here it flattens rank 1 and rank 4 to within 5% of each other,
# which is the same as having no ranking. 10 keeps rank meaningful at this
# scale.
_RRF_K = 10

# A term matching more than this share of the index is not a search term, it is
# grammar ("qué", "sobre", "the", "position"). Derived from the corpus rather
# than a stopword list, so it works in whatever language the user writes —
# below _DF_MIN_NOTES there is not enough corpus to tell, and every term stays.
_DF_MAX_SHARE = 0.5
_DF_MIN_NOTES = 20

_WORD_RE = re.compile(r"[^\W\d_]{2,}|\d[\w.\-]*", re.UNICODE)


def path_for(root: Path) -> Path:
    """The account's memory file, beside its watchlist and chat history."""
    return root / FILE


@dataclass(frozen=True)
class Memory:
    """One recalled turn. `rank` is its place in the fused ranking (1 = best);
    there is no single score to report, because the two searches behind it do
    not share one."""

    thread: str
    role: str
    when: str  # ISO date
    text: str
    rank: int

    def line(self) -> str:
        who = "You" if self.role == "user" else "The assistant"
        return f"[{self.when}] {who}: {self.text}"


# ------------------------------------------------------------- dependencies


@lru_cache(maxsize=1)
def _model():
    """The static embedding model, or None when it cannot be loaded.

    Cached for the process: loading reads (and on a cold machine downloads)
    ~30MB. A miss is not an error anywhere — it just means no memory."""
    try:
        from model2vec import StaticModel

        return StaticModel.from_pretrained(MODEL)
    except Exception as exc:
        obs.warn("chat.memory.model_unavailable", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return None


def available() -> bool:
    """Whether this deploy can remember anything at all."""
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    return _model() is not None


def embed(texts: list[str]) -> list[bytes] | None:
    """`texts` as sqlite-vec float32 blobs, or None when embedding is off."""
    model = _model()
    if model is None or not texts:
        return None
    try:
        import numpy as np
        import sqlite_vec

        vectors = np.asarray(model.encode(texts), dtype="float32")
        # Normalized, so the cosine distance the index is built on is a pure
        # angle: model2vec's raw vectors are not unit length, and their
        # magnitudes track text length, which would rank long turns as "close"
        # to everything.
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.clip(norms, 1e-9, None)
        return [sqlite_vec.serialize_float32(v.tolist()) for v in vectors]
    except Exception as exc:
        obs.warn("chat.memory.embed_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return None


# ------------------------------------------------------------------- store


def _connect(path: Path) -> sqlite3.Connection:
    """An open, migrated memory database with the vector extension loaded."""
    import sqlite_vec

    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS notes ("
        " id INTEGER PRIMARY KEY, thread TEXT NOT NULL, role TEXT NOT NULL,"
        " when_iso TEXT NOT NULL, text TEXT NOT NULL,"
        # thread + content hash: re-indexing a thread after every turn must
        # not store the same turn twice.
        " fingerprint TEXT NOT NULL UNIQUE)"
    )
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes_vec USING vec0("
        f" note_id INTEGER PRIMARY KEY, embedding float[{DIM}]"
        " distance_metric=cosine)"
    )
    # remove_diacritics 2 so "posicion" finds "posición" — half of what a
    # Spanish-speaking user types has the accents dropped.
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
        " text, content='notes', content_rowid='id',"
        ' tokenize="unicode61 remove_diacritics 2")'
    )
    return db


def _fingerprint(thread: str, text: str) -> str:
    return hashlib.sha256(f"{thread}\x00{text}".encode()).hexdigest()[:32]


def _keepable(turn: dict) -> str:
    """The indexable text of a turn, or "" when it is not worth remembering."""
    if turn.get("role") not in ("user", "assistant"):
        return ""
    text = " ".join(str(turn.get("content") or "").split())
    return text[:MAX_CHARS] if len(text) >= MIN_CHARS else ""


def remember(path: Path, history: list[dict], thread: str) -> int:
    """Index whatever of `history` is not indexed yet. Returns turns added.

    Idempotent: callers run it after every answer, and re-indexing a whole
    thread must cost one embed of the new turns, not a duplicate of the old
    ones. Never raises — an index that fails to update is a worse search, not
    a failed conversation.
    """
    if not available() or not history:
        return 0
    try:
        db = _connect(path)
    except Exception as exc:
        obs.warn("chat.memory.open_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return 0
    try:
        with db:
            known = {r[0] for r in db.execute(
                "SELECT fingerprint FROM notes WHERE thread = ?", (thread,))}
            fresh = []
            for turn in history:
                text = _keepable(turn)
                if not text:
                    continue
                fp = _fingerprint(thread, text)
                if fp in known:
                    continue
                known.add(fp)
                fresh.append((fp, turn.get("role"), text))
            if not fresh:
                return 0
            vectors = embed([t[2] for t in fresh])
            if vectors is None:
                return 0
            when = datetime.now(UTC).date().isoformat()
            for (fp, role, text), vector in zip(fresh, vectors, strict=True):
                cur = db.execute(
                    "INSERT INTO notes (thread, role, when_iso, text, fingerprint)"
                    " VALUES (?, ?, ?, ?, ?)", (thread, role, when, text, fp))
                db.execute("INSERT INTO notes_vec (note_id, embedding)"
                           " VALUES (?, ?)", (cur.lastrowid, vector))
                db.execute("INSERT INTO notes_fts (rowid, text) VALUES (?, ?)",
                           (cur.lastrowid, text))
            return len(fresh)
    except Exception as exc:
        obs.warn("chat.memory.write_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return 0
    finally:
        db.close()


def _content_terms(db: sqlite3.Connection | None, terms: set[str]) -> set[str]:
    """`terms` without the ones this corpus treats as grammar.

    A word in half the account's memories tells the search nothing — it only
    drags in every note that happens to contain it, which is how "¿qué decidí
    sobre ASML?" comes back with a conversation about Nvidia. Dropped only
    once there is enough corpus for document frequency to mean anything, and
    never all of them: a question made entirely of common words still has to
    search for something."""
    if db is None or len(terms) < 2:
        return terms
    try:
        total = db.execute("SELECT count(*) FROM notes").fetchone()[0]
        if total < _DF_MIN_NOTES:
            return terms
        keep = set()
        for term in terms:
            hits = db.execute(
                "SELECT count(*) FROM notes_fts WHERE notes_fts MATCH ?",
                (f'"{term}"',)).fetchone()[0]
            if hits <= total * _DF_MAX_SHARE:
                keep.add(term)
        return keep or terms
    except Exception:
        return terms


def fts_query(text: str, db: sqlite3.Connection | None = None) -> str:
    """`text` as an FTS5 MATCH expression, or "" when there is nothing to match.

    Built from extracted words rather than passed through: a raw question is a
    syntax error to FTS5 the moment it contains a quote, a hyphen or the word
    "OR". Each term is quoted and OR-ed, so the match is "any of these words"
    and BM25 decides which memory had the most telling ones."""
    terms = {w.lower() for w in _WORD_RE.findall(text or "")}
    terms = _content_terms(db, terms)
    return " OR ".join(f'"{t}"' for t in sorted(terms)) if terms else ""


def _fuse(*rankings: list[int]) -> list[int]:
    """Note ids ordered by reciprocal rank fusion across several rankings.

    RRF because the two halves' scores are not comparable — a cosine distance
    and a BM25 score have neither the same scale nor the same direction — but
    their *ranks* are. A note both searches like beats one that either loves.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, note_id in enumerate(ranking):
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])


def _semantic(db: sqlite3.Connection, query: str, depth: int) -> list[int]:
    vectors = embed([query])
    if not vectors:
        return []
    try:
        return [r[0] for r in db.execute(
            "SELECT note_id FROM notes_vec WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance", (vectors[0], depth))]
    except Exception as exc:
        obs.warn("chat.memory.search_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return []


def _lexical(db: sqlite3.Connection, query: str, depth: int) -> list[int]:
    match = fts_query(query, db)
    if not match:
        return []
    try:
        return [r[0] for r in db.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?"
            " ORDER BY rank LIMIT ?", (match, depth))]
    except Exception as exc:
        obs.warn("chat.memory.fts_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return []


def recall(path: Path, query: str, limit: int = RECALL_LIMIT,
           exclude_thread: str = "") -> list[Memory]:
    """The stored turns most like `query`, best first — meaning and words both.

    `exclude_thread` drops the conversation the user is already in: those
    messages are in the prompt already, and spending the search on them is how
    a memory tool returns nothing useful. Both halves therefore over-fetch,
    since the filtering happens after the ranking.
    """
    if not available() or not query.strip() or not path.exists():
        return []
    try:
        db = _connect(path)
    except Exception:
        return []
    try:
        depth = max(limit * 4, 8)
        order = _fuse(_semantic(db, query, depth), _lexical(db, query, depth))
        if not order:
            return []
        rows = db.execute(
            "SELECT id, thread, role, when_iso, text FROM notes"
            f" WHERE id IN ({','.join('?' * len(order))})", order,
        ).fetchall()
    except Exception as exc:
        obs.warn("chat.memory.search_failed", error_type=type(exc).__name__,
                 error=str(exc)[:200])
        return []
    finally:
        db.close()
    by_id = {r[0]: r for r in rows}
    out = []
    for rank, note_id in enumerate(order):
        row = by_id.get(note_id)
        if row is None or row[1] == exclude_thread:
            continue
        out.append(Memory(thread=row[1], role=row[2], when=row[3], text=row[4],
                          rank=rank + 1))
        if len(out) == limit:
            break
    return out


def forget(path: Path, thread: str) -> int:
    """Drop one thread's memories — what a deleted conversation must mean.

    Returns rows removed. Never raises: a delete that half-worked is not worth
    failing the delete the user actually asked for."""
    if not path.exists():
        return 0
    try:
        db = _connect(path)
    except Exception:
        return 0
    try:
        with db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM notes WHERE thread = ?", (thread,))]
            for note_id in ids:
                db.execute("DELETE FROM notes_vec WHERE note_id = ?", (note_id,))
                db.execute("INSERT INTO notes_fts (notes_fts, rowid, text)"
                           " VALUES ('delete', ?, (SELECT text FROM notes"
                           " WHERE id = ?))", (note_id, note_id))
            db.execute("DELETE FROM notes WHERE thread = ?", (thread,))
            return len(ids)
    except Exception:
        return 0
    finally:
        db.close()
