"""Turn-lifecycle events emitted on the persistent hub from _finalize.

Drives each runner's _finalize directly (no live claude) with a recording fake
hub and a monkeypatched transcript, asserting exactly one terminal event per
turn, the transcript turn_idx (not the per-run counter), and done/error mapping.
"""
from __future__ import annotations

import pytest

from orbit import orchestrator_events as events_mod
from orbit import orchestrator_runner as runner
from orbit import orchestrator_runner_tmux as runner_tmux
from orbit.orchestrator_events import SessionEventHub, _format_sse


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, sid, event, data, *, buffer=True):
        self.events.append((sid, event, data))


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(events_mod, "get_hub", lambda: rec)
    return rec


def _patch_transcript(monkeypatch, turn_idx: int):
    msgs = [{"role": "assistant", "turn_idx": turn_idx, "blocks": []}] if turn_idx >= 0 else []
    monkeypatch.setattr(runner.jsonl_mod, "read_session",
                        lambda sid: {"ok": True, "messages": msgs})


def _terminal(rec):
    return [e for e in rec.events if e[1] in ("turn_done", "turn_error")]


def test_claude_runner_turn_done(monkeypatch, recorder):
    _patch_transcript(monkeypatch, 3)
    r = runner.ClaudeRunner("sid-done", False)
    r._final_event = ("done", {"total_cost_usd": 0.02, "num_turns": 2,
                               "usage": {"input_tokens": 10, "output_tokens": 5}})
    r._finalize()
    done = [e for e in recorder.events if e[1] == "turn_done"]
    assert len(done) == 1
    data = done[0][2]
    assert data["turn_idx"] == 3
    assert data["cost_usd"] == 0.02
    assert data["total_tokens"] == 15
    assert data["num_turns"] == 2


def test_claude_runner_turn_error(monkeypatch, recorder):
    _patch_transcript(monkeypatch, 1)
    r = runner.ClaudeRunner("sid-err", False)
    r._final_event = ("error", {"message": "claude exited with code 1",
                                "stderr_tail": ["boom"]})
    r._finalize()
    errs = [e for e in recorder.events if e[1] == "turn_error"]
    assert len(errs) == 1
    assert errs[0][2]["message"] == "claude exited with code 1"
    assert errs[0][2]["stderr_tail"] == ["boom"]


def test_claude_runner_cancelled_maps_to_error(monkeypatch, recorder):
    _patch_transcript(monkeypatch, 0)
    r = runner.ClaudeRunner("sid-cxl", False)
    r._cancelled = True
    r._final_event = ("error", {"message": "cancelled"})
    r._finalize()
    errs = [e for e in recorder.events if e[1] == "turn_error"]
    assert len(errs) == 1
    assert errs[0][2]["message"] == "cancelled"


def test_finalize_emits_exactly_once(monkeypatch, recorder):
    _patch_transcript(monkeypatch, 2)
    r = runner.ClaudeRunner("sid-once", False)
    r._final_event = ("done", {})
    r._finalize()
    r._finalize()  # idempotent — _done guard
    assert len(_terminal(recorder)) == 1


def test_tmux_runner_turn_done(monkeypatch, recorder):
    _patch_transcript(monkeypatch, 7)
    r = runner_tmux.TmuxClaudeRunner("sid-tmux", pool=object())
    r._final_event = ("done", {"reason": "turn complete"})
    r._finalize()
    done = [e for e in recorder.events if e[1] == "turn_done"]
    assert len(done) == 1
    assert done[0][2]["turn_idx"] == 7


def test_tmux_runner_subprocess_death_is_error(monkeypatch, recorder):
    # No assistant message written (subprocess died) → turn_idx -1, turn_error.
    _patch_transcript(monkeypatch, -1)
    r = runner_tmux.TmuxClaudeRunner("sid-tmux-e", pool=object())
    r._final_event = ("error", {"message": "turn timed out waiting for JSONL flush"})
    r._finalize()
    errs = [e for e in recorder.events if e[1] == "turn_error"]
    assert len(errs) == 1
    assert "timed out" in errs[0][2]["message"]
    assert errs[0][2]["turn_idx"] == -1


def test_hub_replays_terminal_only_on_resume():
    hub = SessionEventHub()
    hub.publish("s", "turn_done", {"turn_idx": 1})
    # Resume (Last-Event-ID supplied) replays the buffered terminal frame.
    q_resume = hub.subscribe("s", last_event_id=0)
    assert not q_resume.empty()
    assert b"turn_done" in q_resume.get_nowait()
    # Fresh connect (no Last-Event-ID) replays nothing — it's a notification bus.
    q_fresh = hub.subscribe("s", None)
    assert q_fresh.empty()


def test_format_sse_roundtrip():
    frame = _format_sse("turn_done", {"turn_idx": 9}, seq=4)
    assert b"event: turn_done" in frame
    assert b"id: 4" in frame
