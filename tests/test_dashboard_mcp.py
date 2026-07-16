"""Unit tests for the bundled `dashboard` MCP server (issue #95).

The tools are thin HTTP wrappers, so we monkeypatch the module's `_http` with a
recorder and assert each tool issues the right method/path/query/body — no live
dashboard needed. Loaded by path because the server ships under skills/, not the
package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "skills/dashboard-mcp/scripts/dashboard_mcp.py"


def _load():
    spec = importlib.util.spec_from_file_location("dashboard_mcp", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def rec(mod, monkeypatch):
    """Record _http calls; return per-path canned responses."""
    calls = []
    responses = {}

    def fake_http(method, path, query=None, body=None, timeout=None):
        calls.append({"method": method, "path": path, "query": query, "body": body})
        return responses.get((method, path), responses.get(path, {"ok": True}))

    monkeypatch.setattr(mod, "_http", fake_http)
    return calls, responses


def test_tool_registry_shape(mod):
    names = [t["name"] for t in mod.TOOL_DEFS]
    # core orchestration + new #95 tools
    for expected in ("list_sessions", "create_session", "send_message", "wait_for_reply",
                     "send_and_wait", "para_overview", "notify", "answer_question",
                     "capture_pane", "delete_session"):
        assert expected in names, f"missing tool {expected}"
    assert mod.SERVER_NAME == "dashboard"
    # every tool has a handler + an inputSchema
    assert set(mod.TOOL_MAP) == set(names)
    for t in mod.TOOL_DEFS:
        assert t["inputSchema"]["type"] == "object"


def test_list_sessions_passes_cwd(mod, rec):
    calls, _ = rec
    mod.t_list_sessions({"cwd": "/home/testuser/Areas/Home"})
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["path"] == "/orchestrator/sessions"
    assert calls[-1]["query"]["cwd"] == "/home/testuser/Areas/Home"


def test_create_session_body(mod, rec):
    calls, _ = rec
    mod.t_create_session({"cwd": "/home/testuser", "model": "opus", "title": "x"})
    c = calls[-1]
    assert c["method"] == "POST" and c["path"] == "/orchestrator/sessions"
    assert c["body"] == {"cwd": "/home/testuser", "model": "opus", "title": "x"}


def test_send_message_body(mod, rec):
    calls, _ = rec
    mod.t_send_message({"session_id": "S", "text": "hi", "interactive_mode": True})
    c = calls[-1]
    assert c["path"] == "/orchestrator/sessions/S/messages"
    assert c["body"]["text"] == "hi" and c["body"]["interactive_mode"] is True


def test_wait_clamps_timeout(mod, rec):
    calls, _ = rec
    mod.t_wait_for_reply({"session_id": "S", "since_turn": 7, "timeout": 999})
    assert calls[-1]["path"] == "/orchestrator/sessions/S/wait"
    assert calls[-1]["query"]["since_turn"] == 7
    assert calls[-1]["query"]["timeout"] == 60  # clamped to WAIT max


def test_notify_no_token(mod, rec):
    calls, _ = rec
    mod.t_notify({"text": "done", "topic": "cron"})
    c = calls[-1]
    assert c["method"] == "POST" and c["path"] == "/notify"
    assert c["body"] == {"text": "done", "topic": "cron"}


def test_para_overview_reshapes(mod, rec):
    calls, responses = rec
    responses[("GET", "/data")] = {
        "areas": [{"lib_id": "areas/Home", "label": "Home", "cwd": "/home/testuser/Areas/Home"}],
        "projects": [{"lib_id": "projects/x", "label": "x", "path": "/home/testuser/Projects/x"}],
        "resources": [], "system": {"k": 1}, "host": "test-host",
    }
    out = mod.t_para_overview({})
    assert out["areas"][0]["cwd"] == "/home/testuser/Areas/Home"
    assert out["projects"][0]["cwd"] == "/home/testuser/Projects/x"  # falls back to path
    assert out["host"] == "test-host"


def test_send_and_wait_returns_text(mod, rec):
    _, responses = rec
    responses[("POST", "/orchestrator/sessions/S/messages")] = {"ok": True, "expected_turn_idx": 4}
    responses[("GET", "/orchestrator/sessions/S/wait")] = {
        "status": "done", "latest_turn_idx": 5, "cost_usd": 0.01,
        "new_messages": [{"role": "assistant", "turn_idx": 5,
                          "blocks": [{"kind": "text", "text": "the answer"}]}],
    }
    out = mod.t_send_and_wait({"session_id": "S", "text": "go"})
    assert out["ok"] is True and out["status"] == "done"
    assert out["text"] == "the answer"


def test_send_and_wait_surfaces_busy(mod, rec):
    _, responses = rec
    responses[("POST", "/orchestrator/sessions/S/messages")] = {
        "ok": False, "status": "busy", "error": "turn already in flight; cancel first"}
    out = mod.t_send_and_wait({"session_id": "S", "text": "go"})
    assert out["ok"] is False and out["status"] == "busy"


def test_assistant_text_only_text_blocks(mod):
    msgs = [
        {"role": "assistant", "blocks": [
            {"kind": "thinking", "text": "hmm"},
            {"kind": "text", "text": "hello"},
            {"kind": "tool_use", "name": "Bash"},
            {"kind": "text", "text": "world"},
        ]},
        {"role": "user", "blocks": [{"kind": "text", "text": "ignore me"}]},
    ]
    assert mod._assistant_text(msgs) == "hello\n\nworld"


def test_pending_question_detection(mod):
    idle = " ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt\n❯ \n"
    assert mod._detect_pending(idle)["likely_waiting"] is False
    menu = ("❯ 1. APPLE\n     Choose APPLE\n  2. CHERRY\n     Choose CHERRY\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n")
    assert mod._detect_pending(menu)["likely_waiting"] is True
