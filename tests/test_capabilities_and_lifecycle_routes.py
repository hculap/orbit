"""Route tests: /capabilities, /turns/running, /start, /stop, POST busy/cursor."""
from __future__ import annotations

import asyncio

import pytest

from orbit import orchestrator as orch
from orbit import orchestrator_runner as runner
from orbit import orchestrator_settings as settings_mod

SID = "00000000-0000-0000-0000-000000000000"


# ── capabilities ───────────────────────────────────────────────────
def test_capabilities_shape(client):
    j = client.get("/api/orchestrator/capabilities").json()
    assert j["ok"] is True
    f = j["features"]
    assert f["turn_lifecycle_events"] is True
    assert f["session_wait"] is True
    assert f["session_start_stop"] is True
    assert f["lexical_search"] is True
    assert f["messages_pagination"] is True
    assert f["global_events"] is False
    assert f["agent_context"] is False
    assert f["embedding_ready"] is False
    assert "turn_done" in j["sse_events"]
    assert j["limits"]["wait_timeout_max_s"] == 60


def test_capabilities_semantic_reflects_flag(client, monkeypatch):
    real = settings_mod.get_flag
    monkeypatch.setattr(
        settings_mod, "get_flag",
        lambda name: True if name == "semantic_search_enabled" else real(name),
    )
    j = client.get("/api/orchestrator/capabilities").json()
    assert j["features"]["semantic_search"] is True


# ── turns/running ──────────────────────────────────────────────────
class _FakeRunner:
    def __init__(self, done: bool):
        self._done = asyncio.Event()
        if done:
            self._done.set()
        self._started_at_ms = 1234000


def test_turns_running_excludes_done(client):
    runner._active_runs.clear()
    runner._active_runs["live"] = _FakeRunner(False)
    runner._active_runs["dead"] = _FakeRunner(True)
    try:
        j = client.get("/api/orchestrator/turns/running").json()
        ids = [r["session_id"] for r in j["running"]]
        assert "live" in ids
        assert "dead" not in ids
        live = next(r for r in j["running"] if r["session_id"] == "live")
        assert live["started_at"] == 1234.0
        assert live["runner"] == "programmatic"
    finally:
        runner._active_runs.clear()


# ── start / stop ───────────────────────────────────────────────────
def test_start_rejects_bad_session_id(client):
    assert client.post("/api/orchestrator/sessions/not-a-uuid/start").status_code == 400


def test_stop_rejects_bad_session_id(client):
    assert client.post("/api/orchestrator/sessions/not-a-uuid/stop").status_code == 400


def test_start_warms_slot(client, monkeypatch):
    async def fake_warm(sid, *, wait_ready=True):
        return True
    monkeypatch.setattr(orch, "_warm_session_slot", fake_warm)
    j = client.post(f"/api/orchestrator/sessions/{SID}/start").json()
    assert j["ok"] is True and j["started"] is True and j["spawned"] is True


def test_stop_tears_down_runtime(client, monkeypatch):
    calls: dict = {}

    async def fake_release(sid, *, forget_persistent):
        calls["sid"] = sid
        calls["fp"] = forget_persistent
    monkeypatch.setattr(orch, "_release_session_slots", fake_release)
    j = client.post(f"/api/orchestrator/sessions/{SID}/stop").json()
    assert j["ok"] is True and j["stopped"] is True
    assert calls == {"sid": SID, "fp": False}


def test_post_message_returns_expected_turn_idx(client, monkeypatch):
    """The load-bearing cursor: POST returns the transcript idx the next turn
    will exceed, so an orchestrator can pass it to GET /wait?since_turn=<this>."""
    import pathlib

    runner._active_runs.clear()
    monkeypatch.setattr(runner, "transcript_turn_idx", lambda sid: 7)

    class _FakeTmux:
        def __init__(self, *a, **k):
            self.session_id = a[0] if a else "x"

        async def start_turn(self, text):
            return None

    monkeypatch.setattr(orch.runner_tmux_mod, "TmuxClaudeRunner", _FakeTmux)
    monkeypatch.setattr(orch.runner, "ClaudeRunner", _FakeTmux)
    monkeypatch.setattr(orch.jsonl_mod, "jsonl_path", lambda sid: pathlib.Path("/nonexistent"))
    monkeypatch.setattr(orch.uploads_module, "pop_pending", lambda sid: [])
    try:
        r = client.post(f"/api/orchestrator/sessions/{SID}/messages", json={"text": "hi"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["expected_turn_idx"] == 7
        assert "turn_started_ts" in body
    finally:
        runner._active_runs.clear()


# ── POST /messages busy envelope (HTTP 200, machine-readable) ──────
def test_post_message_busy_envelope(client, monkeypatch):
    runner._active_runs.clear()
    runner._active_runs[SID] = _FakeRunner(False)  # in-flight
    try:
        r = client.post(f"/api/orchestrator/sessions/{SID}/messages", json={"text": "hi"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["status"] == "busy"
        assert body["session_id"] == SID
    finally:
        runner._active_runs.clear()
