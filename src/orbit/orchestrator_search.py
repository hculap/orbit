"""Server-side search over session transcripts — "find the session where we
talked about X".

Two ranking signals fused with Reciprocal Rank Fusion (RRF):

* **BM25** (hand-rolled, pure-stdlib) — the always-on lexical floor. Ranks by
  term frequency × inverse document frequency over the control-tag-stripped,
  80 KB-capped ``corpus`` that ``orchestrator_jsonl`` already builds per session
  (so the expensive JSONL parse is reused from that module's mtime cache).
* **TF-IDF cosine** (scikit-learn, already installed on the box) — a real
  vector tier that catches morphological / phrasing variants BM25 misses
  (word 1–2 grams, L2-normalised → dot product = cosine). Imported lazily; if
  sklearn is ever absent the module degrades to **lexical-only** with no error.

When both signals are available the response ``mode`` is ``"hybrid"``, else
``"lexical"``. True neural embeddings (a remote ``/v1/embeddings`` backend) are
a deliberately-deferred upgrade behind the :class:`EmbeddingBackend` seam +
``semantic_search_enabled`` flag — see ``orchestrator_settings``.

Robustness (each guards a concrete failure mode for an unattended client):

* **Per-session incremental tokenization** — a rebuild reuses cached tokens for
  unchanged sessions (keyed on each session's ``updated_at``), re-tokenizing
  only what changed, so a chatty agent doesn't force a full-corpus re-parse.
* **Double-checked-locked rebuild off the event loop** (``asyncio.to_thread``)
  so concurrent searches never stampede a rebuild or block the uvicorn worker.
* **Query + corpus bounds** — query capped at 1 KB, ``limit`` clamped 1..100,
  a session-count ceiling so the small-corpus invariant can't silently rot.
* **Process-wide concurrency cap** → :class:`SearchSaturated` (HTTP 429).
* Any ``list_sessions`` failure degrades to ``results: []`` — never a 500.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from collections import defaultdict
from typing import Any, Callable, Protocol

from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod

# ── tunables ───────────────────────────────────────────────────────
_BM25_K1: float = 1.5
_BM25_B: float = 0.75
_RRF_K: int = 60
_QUERY_MAX_CHARS: int = 1024
_LIMIT_MAX: int = 100
_LIMIT_DEFAULT: int = 20
# Above this many sessions we index only the most-recent N so the in-memory
# BM25/TF-IDF build can't silently degrade into a multi-second scan years out.
_MAX_INDEXED: int = 500
_SNIPPET_RADIUS: int = 80
_SEARCH_MAX_CONCURRENT: int = 16

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)  # unicode \w covers Polish diacritics


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


# ── optional vector tier (scikit-learn, lazy) ──────────────────────
_sklearn: Any = None
_sklearn_checked: bool = False


def _get_sklearn() -> Any:
    """Lazy, cached import of the TF-IDF vectorizer; None if sklearn absent."""
    global _sklearn, _sklearn_checked
    if not _sklearn_checked:
        _sklearn_checked = True
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: F401
            _sklearn = TfidfVectorizer
        except Exception:  # noqa: BLE001 — degrade to lexical-only
            _sklearn = None
    return _sklearn


# ── deferred neural-embedding seam ─────────────────────────────────
class EmbeddingBackend(Protocol):
    """Pluggable true-embedding backend (deferred). ``embed`` returns one
    vector per text, or ``None`` when unavailable so callers fall back."""

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...

    @property
    def ready(self) -> bool: ...


class NullBackend:
    """Default backend: no neural embeddings. The hybrid tier runs on the
    always-available sklearn TF-IDF vectors; this seam is where a remote
    ``/v1/embeddings`` client drops in once configured + evaluated."""

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None

    @property
    def ready(self) -> bool:
        return False


_backend: EmbeddingBackend = NullBackend()


def embedding_ready() -> bool:
    return _backend.ready


# ── the index ──────────────────────────────────────────────────────
class SearchIndex:
    """An immutable inverted index + optional TF-IDF matrix over a session set.

    Rebuilt (not mutated) when the session signature changes; ``previous`` is
    passed so unchanged sessions reuse their cached token lists.
    """

    def __init__(self, sessions: list[dict[str, Any]], previous: "SearchIndex | None" = None):
        prev_tokens = previous._token_cache if previous else {}
        # Drop corpus-less (orphan) sessions; cap to the most-recent N.
        usable = [s for s in sessions if isinstance(s, dict) and s.get("id") and (s.get("corpus") or "")]
        usable.sort(key=lambda s: -float(s.get("updated_at") or 0.0))
        if len(usable) > _MAX_INDEXED:
            usable = usable[:_MAX_INDEXED]

        self.entries: list[dict[str, Any]] = []
        self._token_cache: dict[tuple[str, float], list[str]] = {}
        self.fresh_tokenized: set[str] = set()  # sids re-tokenized this build (test hook)
        docs_tokens: list[list[str]] = []
        for s in usable:
            sid = s["id"]
            updated_at = float(s.get("updated_at") or 0.0)
            key = (sid, updated_at)
            tokens = prev_tokens.get(key)
            if tokens is None:
                tokens = _tokenize(s.get("corpus") or "")
                self.fresh_tokenized.add(sid)
            self._token_cache[key] = tokens
            self.entries.append({
                "session_id": sid,
                # Native Claude Code title (issue #85) → first-message preview.
                # Raw JSONL summaries carry no sidecar overlay, so a manual
                # rename isn't visible here, but ai_title backfills every
                # session that ever ran and beats the raw prompt preview.
                "title": s.get("ai_title") or s.get("first_user_preview") or None,
                "updated_at": updated_at,
                "corpus": s.get("corpus") or "",
            })
            docs_tokens.append(tokens)

        self._docs_tokens = docs_tokens
        self.signature = signature(sessions)
        self._build_bm25(docs_tokens)
        self._fit_tfidf()

    # ── BM25 ──
    def _build_bm25(self, docs_tokens: list[list[str]]) -> None:
        self._n = len(docs_tokens)
        self._doc_len = [len(t) for t in docs_tokens]
        self._avgdl = (sum(self._doc_len) / self._n) if self._n else 0.0
        self._tf: list[dict[str, int]] = []
        df: dict[str, int] = defaultdict(int)
        for tokens in docs_tokens:
            counts: dict[str, int] = defaultdict(int)
            for t in tokens:
                counts[t] += 1
            self._tf.append(counts)
            for term in counts:
                df[term] += 1
        self._idf: dict[str, float] = {}
        for term, freq in df.items():
            self._idf[term] = math.log(1 + (self._n - freq + 0.5) / (freq + 0.5))

    def _bm25_scores(self, query_terms: list[str]) -> list[float]:
        scores = [0.0] * self._n
        if not self._avgdl:
            return scores
        for i in range(self._n):
            counts = self._tf[i]
            dl = self._doc_len[i]
            denom_norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / self._avgdl)
            s = 0.0
            for term in query_terms:
                f = counts.get(term, 0)
                if not f:
                    continue
                s += self._idf.get(term, 0.0) * (f * (_BM25_K1 + 1)) / (f + denom_norm)
            scores[i] = s
        return scores

    # ── TF-IDF cosine (optional) ──
    def _fit_tfidf(self) -> None:
        self._vectorizer = None
        self._doc_matrix = None
        Vectorizer = _get_sklearn()
        if Vectorizer is None or self._n < 2:
            return
        corpora = [e["corpus"] for e in self.entries]
        try:
            vec = Vectorizer(
                lowercase=True, ngram_range=(1, 2), min_df=1,
                sublinear_tf=True, norm="l2", token_pattern=r"(?u)\b\w\w+\b",
            )
            self._doc_matrix = vec.fit_transform(corpora)
            self._vectorizer = vec
        except Exception:  # noqa: BLE001 — vector tier optional; lexical still works
            self._vectorizer = None
            self._doc_matrix = None

    def _cosine_scores(self, query: str) -> list[float] | None:
        if self._vectorizer is None or self._doc_matrix is None:
            return None
        try:
            qv = self._vectorizer.transform([query])
            sims = (self._doc_matrix @ qv.T).toarray().ravel()
            return [float(x) for x in sims]
        except Exception:  # noqa: BLE001
            return None

    @property
    def mode(self) -> str:
        return "hybrid" if self._vectorizer is not None else "lexical"

    # ── query ──
    def query(self, q: str, limit: int, *, cwd_map: dict[str, str | None] | None = None,
              cwd_filter: str | None = None) -> list[dict[str, Any]]:
        query_terms = _tokenize(q)
        bm25 = self._bm25_scores(query_terms)
        cosine = self._cosine_scores(q)

        # Candidate docs: any positive lexical OR vector signal.
        candidates: set[int] = {i for i, s in enumerate(bm25) if s > 0.0}
        if cosine is not None:
            candidates |= {i for i, s in enumerate(cosine) if s > 0.0}
        if not candidates:
            return []

        bm25_rank = [i for i in sorted(candidates, key=lambda i: bm25[i], reverse=True) if bm25[i] > 0]
        rankings = [bm25_rank]
        if cosine is not None:
            cos_rank = [i for i in sorted(candidates, key=lambda i: cosine[i], reverse=True) if cosine[i] > 0]
            rankings.append(cos_rank)

        fused: dict[int, float] = defaultdict(float)
        for ranking in rankings:
            for rank, idx in enumerate(ranking):
                fused[idx] += 1.0 / (_RRF_K + rank + 1)

        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        results: list[dict[str, Any]] = []
        term_set = set(query_terms)
        for idx, score in ordered:
            entry = self.entries[idx]
            sid = entry["session_id"]
            if cwd_filter is not None and cwd_map is not None and cwd_map.get(sid) != cwd_filter:
                continue
            corpus = entry["corpus"]
            matched = sorted({t for t in term_set if t in (self._tf[idx])})
            results.append({
                "session_id": sid,
                "score": round(float(score), 6),
                "title": entry["title"],
                "updated_at": entry["updated_at"],
                "snippet": _snippet(corpus, query_terms),
                "matched_terms": matched,
            })
            if len(results) >= limit:
                break
        return results


def _snippet(corpus: str, query_terms: list[str]) -> str:
    """A window around the first query-term hit, else the head of the corpus."""
    if not corpus:
        return ""
    low = corpus.lower()
    pos = -1
    for term in query_terms:
        p = low.find(term)
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos == -1:
        return corpus[:_SNIPPET_RADIUS * 2].strip().replace("\n", " ")
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(corpus), pos + _SNIPPET_RADIUS)
    snip = corpus[start:end].strip().replace("\n", " ")
    return (("…" if start > 0 else "") + snip + ("…" if end < len(corpus) else ""))


def signature(sessions: list[dict[str, Any]]) -> tuple[tuple[str, float], ...]:
    """Content-change fingerprint: (session_id, updated_at) for every session.

    Changes whenever a transcript is written, deleted, or added — drives the
    incremental rebuild. ``updated_at`` mirrors the same ``st_mtime`` semantics
    ``list_sessions`` already exposes per session."""
    out = [(s["id"], float(s.get("updated_at") or 0.0))
           for s in sessions if isinstance(s, dict) and s.get("id")]
    out.sort()
    return tuple(out)


# ── async cache + concurrency cap ──────────────────────────────────
_index: SearchIndex | None = None
_build_lock = asyncio.Lock()
_search_inflight: int = 0


class SearchSaturated(Exception):
    """Raised when the process-wide search concurrency cap is hit (→ 429)."""


async def _get_index(sessions: list[dict[str, Any]]) -> SearchIndex:
    """Return a current index, rebuilding off the event loop under a
    double-checked lock so concurrent searches never stampede a rebuild."""
    global _index
    sig = signature(sessions)
    if _index is not None and _index.signature == sig:
        return _index
    async with _build_lock:
        if _index is not None and _index.signature == sig:  # re-check after lock
            return _index
        prev = _index
        _index = await asyncio.to_thread(SearchIndex, sessions, prev)
        return _index


async def search(
    q: str,
    limit: int = _LIMIT_DEFAULT,
    cwd_filter: str | None = None,
    *,
    list_sessions: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Search sessions by transcript content. Returns ``{ok, mode, results}``.

    ``cwd_filter`` (already validated by the route) restricts to sessions whose
    sidecar cwd matches exactly. ``list_sessions`` is injectable for tests.
    """
    global _search_inflight
    q = (q or "").strip()[:_QUERY_MAX_CHARS]
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _LIMIT_DEFAULT
    limit = max(1, min(limit, _LIMIT_MAX))
    if not q:
        return {"ok": True, "mode": "lexical", "results": []}
    if _search_inflight >= _SEARCH_MAX_CONCURRENT:
        raise SearchSaturated()
    _search_inflight += 1
    try:
        lister = list_sessions or jsonl_mod.list_sessions
        try:
            sessions = await asyncio.to_thread(lister)
        except Exception:  # noqa: BLE001 — degrade, never 500
            return {"ok": True, "mode": "lexical", "results": []}
        index = await _get_index(sessions)
        cwd_map: dict[str, str | None] | None = None
        if cwd_filter is not None:
            try:
                overlay = meta_mod.all_meta()
                cwd_map = {sid: (m.get("cwd") if isinstance(m, dict) else None)
                           for sid, m in overlay.items()}
            except Exception:  # noqa: BLE001
                cwd_map = {}
        results = await asyncio.to_thread(
            index.query, q, limit, cwd_map=cwd_map, cwd_filter=cwd_filter
        )
        return {"ok": True, "mode": index.mode, "results": results}
    finally:
        _search_inflight -= 1


def _reset_for_tests() -> None:
    """Drop the cached index (test isolation)."""
    global _index
    _index = None
