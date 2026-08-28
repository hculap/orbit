"""PreToolUse guard that keeps claude.ai artifacts from impersonating panel ones.

The native ``Artifact`` tool publishes to claude.ai — invisible in the
dashboard. The guard blocks it until a short opt-in window is open, so the
agent has to ask the user which kind of artifact they meant. Reads stay open.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "skills/artifacts/scripts/native_artifact_guard.py"


@pytest.fixture
def guard(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("native_artifact_guard_under_test", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OPTIN_PATH", tmp_path / "native-artifact-optin.json")
    return mod


def _run_hook(guard, monkeypatch, payload, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(payload)))
    rc = guard.cmd_hook()
    return rc, capsys.readouterr()


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_other_tools_pass_through(guard, monkeypatch, capsys):
    rc, _ = _run_hook(guard, monkeypatch, {"tool_name": "Bash", "tool_input": {}}, capsys)
    assert rc == 0


def test_publish_is_blocked_without_optin(guard, monkeypatch, capsys):
    rc, out = _run_hook(guard, monkeypatch, {"tool_name": "Artifact", "tool_input": {}}, capsys)
    assert rc == 2
    # The refusal has to teach the agent BOTH the panel CLI and the ask-first rule.
    assert "artifacts_cli.py" in out.err
    assert "AskUserQuestion" in out.err
    decision = json.loads(out.out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_read_only_actions_are_never_blocked(guard, monkeypatch, capsys):
    for action in ("list", "comments", "list_assets", "read_asset"):
        rc, _ = _run_hook(
            guard, monkeypatch,
            {"tool_name": "Artifact", "tool_input": {"action": action}}, capsys,
        )
        assert rc == 0, action


def test_allow_opens_a_window_then_publish_passes(guard, monkeypatch, capsys):
    assert guard.cmd_allow(30, "user asked for a shareable link") == 0
    capsys.readouterr()
    rc, _ = _run_hook(guard, monkeypatch, {"tool_name": "Artifact", "tool_input": {}}, capsys)
    assert rc == 0


def test_expired_window_blocks_again(guard, monkeypatch, capsys):
    guard.OPTIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    guard.OPTIN_PATH.write_text(json.dumps({"expires_at": time.time() - 1}), encoding="utf-8")
    rc, _ = _run_hook(guard, monkeypatch, {"tool_name": "Artifact", "tool_input": {}}, capsys)
    assert rc == 2


def test_revoke_closes_the_window(guard, monkeypatch, capsys):
    guard.cmd_allow(30, None)
    guard.cmd_revoke()
    capsys.readouterr()
    rc, _ = _run_hook(guard, monkeypatch, {"tool_name": "Artifact", "tool_input": {}}, capsys)
    assert rc == 2


def test_malformed_payload_does_not_block_the_harness(guard, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin("not json at all"))
    assert guard.cmd_hook() == 0


def test_allow_clamps_absurd_windows(guard, capsys):
    guard.cmd_allow(99_999, None)
    capsys.readouterr()
    rec = json.loads(guard.OPTIN_PATH.read_text(encoding="utf-8"))
    assert rec["minutes"] == guard.MAX_MINUTES
