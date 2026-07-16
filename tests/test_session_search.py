"""Hybrid session search (BM25 ⊕ TF-IDF cosine) — pure-function + route tests."""
from __future__ import annotations

import asyncio

import pytest

from orbit import orchestrator_search as search_mod


def _run(coro):
    return asyncio.run(coro)


def _sessions():
    return [
        {"id": "a", "updated_at": 3.0,
         "corpus": "we discussed kubernetes deployment and helm charts at length"},
        {"id": "b", "updated_at": 2.0,
         "corpus": "shopping list milk eggs bread cheese tomatoes"},
        {"id": "c", "updated_at": 1.0,
         "corpus": "kubernetes kubernetes kubernetes pods and services networking"},
    ]


@pytest.fixture(autouse=True)
def reset_index():
    search_mod._reset_for_tests()
    yield
    search_mod._reset_for_tests()


def test_match_ranks_relevant_only():
    res = _run(search_mod.search("kubernetes", 10, None, list_sessions=_sessions))
    ids = [r["session_id"] for r in res["results"]]
    assert "a" in ids and "c" in ids
    assert "b" not in ids


def test_frequency_ordering():
    res = _run(search_mod.search("kubernetes", 10, None, list_sessions=_sessions))
    ids = [r["session_id"] for r in res["results"]]
    assert ids.index("c") < ids.index("a")  # 3× mention outranks 1×


def test_empty_query_returns_empty():
    res = _run(search_mod.search("   ", 10, None, list_sessions=_sessions))
    assert res["results"] == []


def test_no_match_returns_empty():
    res = _run(search_mod.search("zzzznonexistentterm", 10, None, list_sessions=_sessions))
    assert res["results"] == []


def test_snippet_contains_term():
    res = _run(search_mod.search("helm", 10, None, list_sessions=_sessions))
    assert res["results"]
    assert "helm" in res["results"][0]["snippet"].lower()


def test_matched_terms_reported():
    res = _run(search_mod.search("kubernetes pods", 10, None, list_sessions=_sessions))
    top = next(r for r in res["results"] if r["session_id"] == "c")
    assert "kubernetes" in top["matched_terms"]


def test_graceful_when_list_sessions_raises():
    def boom():
        raise RuntimeError("nope")
    res = _run(search_mod.search("kubernetes", 10, None, list_sessions=boom))
    assert res["ok"] is True
    assert res["results"] == []


def test_limit_is_clamped():
    res = _run(search_mod.search("and", 999, None, list_sessions=_sessions))
    assert len(res["results"]) <= search_mod._LIMIT_MAX


def test_mode_is_hybrid_when_sklearn_present():
    pytest.importorskip("sklearn")  # box-only/ad-hoc dep; not in uv.lock → skip in CI
    res = _run(search_mod.search("kubernetes", 10, None, list_sessions=_sessions))
    assert res["mode"] == "hybrid"  # sklearn installed on this box


def test_incremental_tokenization_reuses_unchanged():
    s1 = _sessions()
    idx1 = search_mod.SearchIndex(s1)
    assert idx1.fresh_tokenized == {"a", "b", "c"}
    s2 = [dict(x) for x in s1]
    s2[1]["updated_at"] = 99.0           # only 'b' changed
    s2[1]["corpus"] = "totally new content here now"
    idx2 = search_mod.SearchIndex(s2, previous=idx1)
    assert idx2.fresh_tokenized == {"b"}


def test_route_smoke(client, monkeypatch):
    monkeypatch.setattr(search_mod.jsonl_mod, "list_sessions", _sessions)
    search_mod._reset_for_tests()
    j = client.get("/api/orchestrator/sessions/search?q=kubernetes&limit=5").json()
    assert j["ok"] is True
    assert j["mode"] in ("hybrid", "lexical")
    ids = [r["session_id"] for r in j["results"]]
    assert "a" in ids or "c" in ids
