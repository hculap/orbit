"""GET /messages ?limit / ?after_turn pagination — via TestClient."""
from __future__ import annotations

import pytest

from orbit import orchestrator_jsonl as jsonl_mod

SID = "00000000-0000-0000-0000-000000000000"


def _msgs(n: int):
    return [{"role": "user" if i % 2 == 0 else "assistant", "turn_idx": i, "blocks": []}
            for i in range(n)]


@pytest.fixture(autouse=True)
def patched(monkeypatch):
    monkeypatch.setattr(jsonl_mod, "read_session",
                        lambda sid: {"ok": True, "messages": _msgs(6)})


def _get(client, qs=""):
    return client.get(f"/api/orchestrator/sessions/{SID}/messages{qs}").json()


def test_no_params_returns_full(client):
    j = _get(client)
    assert len(j["messages"]) == 6
    assert j["total"] == 6
    assert j["truncated"] is False


def test_limit_returns_tail(client):
    j = _get(client, "?limit=2")
    assert [m["turn_idx"] for m in j["messages"]] == [4, 5]
    assert j["truncated"] is True
    assert j["total"] == 6


def test_after_turn_filters(client):
    j = _get(client, "?after_turn=3")
    assert [m["turn_idx"] for m in j["messages"]] == [4, 5]


def test_combined_after_and_limit(client):
    j = _get(client, "?after_turn=1&limit=2")
    assert [m["turn_idx"] for m in j["messages"]] == [4, 5]


def test_limit_zero_is_no_cap(client):
    j = _get(client, "?limit=0")
    assert len(j["messages"]) == 6
    assert j["truncated"] is False


def test_negative_limit_400(client):
    r = client.get(f"/api/orchestrator/sessions/{SID}/messages?limit=-1")
    assert r.status_code == 400


def test_after_turn_past_end_is_empty(client):
    j = _get(client, "?after_turn=999")
    assert j["messages"] == []
    assert j["total"] == 6
