"""Tests for orchestrator_oneshot.run_oneshot — subscription-billed one-shot.

Fakes the tmux pool + TmuxClaudeRunner so we exercise the spawn→tail→extract→
release→cleanup lifecycle without a real claude. The runner's interface here
is just: ``_buffered_events`` (SSE frames), ``start_turn``, ``cancel``.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _run(coro):
    return asyncio.run(coro)


def _sse(event: str, payload: dict) -> bytes:
    return (
        b"id: 1\nevent: " + event.encode() + b"\ndata: "
        + json.dumps(payload).encode() + b"\n\n"
    )


class _FakePool:
    def __init__(self):
        self.released: list[str] = []

    async def release(self, session_id):
        self.released.append(session_id)


def _make_runner_cls(events, *, hang=False, captured=None):
    class _FakeRunner:
        def __init__(self, session_id, *, pool, cwd=None, append_system_prompt_paths=None,
                     agent_skills_dir=None, model=None, has_run_before=False, extra_prompt_path=None,
                     env_extra=None):
            self.session_id = session_id
            self._buffered_events = []
            if captured is not None:
                captured["model"] = model
                captured["cwd"] = cwd
                captured["env_extra"] = env_extra

        async def start_turn(self, text):
            if captured is not None:
                captured["prompt"] = text
            if hang:
                await asyncio.sleep(30)  # cancellable; finally's turn.cancel() ends it
            self._buffered_events = list(events)

        async def cancel(self):
            if captured is not None:
                captured["cancelled"] = True

    return _FakeRunner


def _patch(monkeypatch, runner_cls, pool):
    from orbit import orchestrator as orch
    from orbit import orchestrator_oneshot as o
    from orbit import orchestrator_runner_tmux as rt
    monkeypatch.setattr(orch, "_get_tmux_pool", lambda: pool)
    monkeypatch.setattr(rt, "TmuxClaudeRunner", runner_cls)
    monkeypatch.setattr(o, "delete_bootstrap_jsonl", lambda sid, cwd: None)


def test_run_oneshot_happy_path(monkeypatch, tmp_path):
    from orbit import orchestrator_oneshot as o
    captured = {}
    events = [_sse("structured_blocks", {"blocks": [{"kind": "text", "content": "hello world"}]})]
    pool = _FakePool()
    _patch(monkeypatch, _make_runner_cls(events, captured=captured), pool)

    res = _run(o.run_oneshot("Say hi", cwd=tmp_path, model="haiku", label="t"))
    assert res["ok"] is True
    assert res["text"] == "hello world"
    assert res["error"] is None
    assert pool.released and len(pool.released) == 1     # single-use slot retired
    assert captured["model"] == "haiku"
    assert captured["prompt"].endswith("Say hi")          # headless preamble prepended
    assert captured["prompt"] != "Say hi"


def test_run_oneshot_raw_skips_code_fences(monkeypatch, tmp_path):
    from orbit import orchestrator_oneshot as o
    blocks = {"blocks": [{"kind": "code", "lang": "md", "content": "NAME: x\n---\nbody"}]}
    # raw=True → verbatim content (parser-safe); raw=False → ```-fenced.
    _patch(monkeypatch, _make_runner_cls([_sse("structured_blocks", blocks)]), _FakePool())
    raw = _run(o.run_oneshot("p", cwd=tmp_path, raw=True))
    fenced = _run(o.run_oneshot("p", cwd=tmp_path, raw=False))
    assert raw["text"] == "NAME: x\n---\nbody"
    assert fenced["text"].startswith("```md") and "NAME: x" in fenced["text"]


def test_run_oneshot_timeout_cancels_and_releases(monkeypatch, tmp_path):
    from orbit import orchestrator_oneshot as o
    monkeypatch.setattr(o, "_TEARDOWN_TIMEOUT_S", 0.02)  # keep the bounded waits fast
    captured = {}
    pool = _FakePool()
    _patch(monkeypatch, _make_runner_cls([], hang=True, captured=captured), pool)
    # Outer guard: a regression where the timeout isn't bounded would hang here.
    res = _run(asyncio.wait_for(o.run_oneshot("p", cwd=tmp_path, timeout_s=0.02), timeout=5))
    assert res["ok"] is False
    assert "timed out" in res["error"]
    assert captured.get("cancelled") is True       # runner.cancel() invoked
    assert len(pool.released) == 1                  # slot released exactly once


def test_run_oneshot_bounds_a_wedged_teardown(monkeypatch, tmp_path):
    """Regression: a stalled pool.release() (tmux /exit+kill under load) must
    NOT make run_oneshot exceed its budget — observed a title one-shot hang
    >130s post-timeout. The teardown is bounded by _TEARDOWN_TIMEOUT_S."""
    from orbit import orchestrator_oneshot as o
    monkeypatch.setattr(o, "_TEARDOWN_TIMEOUT_S", 0.05)

    class _HangPool:
        async def release(self, session_id):
            await asyncio.sleep(30)   # wedged teardown

    events = [_sse("structured_blocks", {"blocks": [{"kind": "text", "content": "hi"}]})]
    _patch(monkeypatch, _make_runner_cls(events), _HangPool())
    # Outer guard: if the teardown bound failed, this wait_for would raise.
    res = _run(asyncio.wait_for(o.run_oneshot("p", cwd=tmp_path), timeout=3))
    assert res["text"] == "hi"


def test_run_oneshot_empty_output_reports_runner_error(monkeypatch, tmp_path):
    from orbit import orchestrator_oneshot as o
    err = [_sse("error", {"message": "tmux slot acquisition failed"})]
    _patch(monkeypatch, _make_runner_cls(err), _FakePool())
    res = _run(o.run_oneshot("p", cwd=tmp_path))
    assert res["ok"] is False
    assert "tmux slot acquisition failed" in res["error"]


def test_delete_bootstrap_jsonl_uses_canonical_slug(monkeypatch, tmp_path):
    """Regression (review HIGH): the throwaway JSONL must be deleted even when
    cwd contains '_' or '.' — claude slugs those to '-' too, so a '/'-only
    replace would miss the file and pollute the session list."""
    from pathlib import Path
    from orbit import orchestrator_oneshot as o
    from orbit import orchestrator_jsonl_tail as tail
    monkeypatch.setattr(tail, "_CLAUDE_HOME", tmp_path)
    sid = "dead-beef"
    cwd = Path("/home/x/Projects/foo_bar.baz")  # underscores + dot
    jsonl = tail.jsonl_path_for(cwd, sid)        # canonical slug path
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text("{}")
    assert jsonl.exists()
    o.delete_bootstrap_jsonl(sid, cwd)
    assert not jsonl.exists()


def test_extract_runner_text_pure(monkeypatch):
    from orbit import orchestrator_oneshot as o

    class _R:
        _buffered_events = [_sse("assistant_message", {"blocks": [
            {"kind": "text", "content": "alpha"},
            {"kind": "code", "lang": "py", "content": "x=1"},
        ]})]
    assert o.extract_runner_text(_R(), raw=True) == "alpha\n\nx=1"
    assert "```py\nx=1\n```" in o.extract_runner_text(_R(), raw=False)


def test_extract_runner_text_raw_concatenates_across_events(monkeypatch):
    """Regression (review #3): raw mode must NOT drop the strict-format answer
    when an agentic turn emits a closing remark in a LATER assistant_message.
    The header/body live in event A; the remark in event B. raw=True must keep
    both (parser scans for markers); raw=False returns the final answer only."""
    from orbit import orchestrator_oneshot as o

    class _R:
        _buffered_events = [
            _sse("assistant_message", {"blocks": [{"kind": "markdown", "text": "ICON: 🌱\n\nThe body."}]}),
            _sse("assistant_message", {"blocks": [{"kind": "markdown", "text": "Done, created it."}]}),
        ]
    raw = o.extract_runner_text(_R(), raw=True)
    assert "ICON: 🌱" in raw and "The body." in raw          # answer preserved
    assert o.extract_runner_text(_R(), raw=False) == "Done, created it."  # final-answer prose


def test_extract_runner_text_raw_drops_choice_ask(monkeypatch):
    """Regression (review #4): a clarifying choice/ask must never reach a strict
    raw parser as the 'answer'; the prose path still surfaces it."""
    from orbit import orchestrator_oneshot as o

    class _R:
        _buffered_events = [_sse("assistant_message", {"blocks": [
            {"kind": "markdown", "text": "NAME: x\n---\nbody"},
            {"kind": "ask", "prompt": "Which runtime?"},
        ]})]
    raw = o.extract_runner_text(_R(), raw=True)
    assert "NAME: x" in raw and "Which runtime?" not in raw
    assert "Which runtime?" in o.extract_runner_text(_R(), raw=False)


def test_run_oneshot_require_text_false_tolerates_empty(monkeypatch, tmp_path):
    """Regression (review #2): a cron tool-only turn (no prose, no error event)
    is a SUCCESS — require_text=False must not stamp it FAILED."""
    from orbit import orchestrator_oneshot as o
    events = [_sse("assistant_message", {"blocks": [{"kind": "tool_use", "name": "Bash", "input": {}}]})]
    _patch(monkeypatch, _make_runner_cls(events), _FakePool())
    res = _run(o.run_oneshot("p", cwd=tmp_path, require_text=False))
    assert res["ok"] is True and res["text"] == "" and res["error"] is None


def test_run_oneshot_require_text_true_empty_is_failure(monkeypatch, tmp_path):
    """Default require_text=True (titles/identity/skill) still treats an empty
    reply as a failure."""
    from orbit import orchestrator_oneshot as o
    events = [_sse("assistant_message", {"blocks": [{"kind": "tool_use", "name": "Bash", "input": {}}]})]
    _patch(monkeypatch, _make_runner_cls(events), _FakePool())
    res = _run(o.run_oneshot("p", cwd=tmp_path))
    assert res["ok"] is False and "no output" in res["error"]


def test_run_oneshot_injects_scope_env(monkeypatch, tmp_path):
    """Regression (review #1): <cwd>/.env secrets reach the interactive spawn via
    env_extra, scrubbed of billing-forcing keys."""
    from orbit import orchestrator_oneshot as o
    (tmp_path / ".env").write_text("FOO=bar\nANTHROPIC_API_KEY=leak\n")
    captured = {}
    events = [_sse("structured_blocks", {"blocks": [{"kind": "text", "content": "ok"}]})]
    _patch(monkeypatch, _make_runner_cls(events, captured=captured), _FakePool())
    res = _run(o.run_oneshot("p", cwd=tmp_path))
    assert res["ok"] is True
    assert captured["env_extra"].get("FOO") == "bar"        # scope secret forwarded
    assert "ANTHROPIC_API_KEY" not in captured["env_extra"]  # billing key scrubbed


def test_titles_routes_interactive_by_default(monkeypatch):
    """_run_haiku with titles_runner_mode=interactive calls run_oneshot, not -p."""
    from orbit import orchestrator_titles as t
    from orbit import orchestrator_oneshot as o
    from orbit import orchestrator_settings as s
    monkeypatch.setattr(s, "get_flag", lambda k: "interactive" if k == "titles_runner_mode" else None)
    calls = {}

    async def _fake_oneshot(prompt, **kw):
        calls["prompt"] = prompt
        calls["model"] = kw.get("model")
        return {"ok": True, "text": "My Title", "error": None}

    monkeypatch.setattr(o, "run_oneshot", _fake_oneshot)
    out = _run(t._run_haiku("make a title"))
    assert out == "My Title"
    assert calls["model"] == "haiku"
