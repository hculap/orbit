"""Tests for ``_extract_runner_response`` in :mod:`cron_runner`.

Regression: 2026-05-16 UAT failure where Haiku replied with a valid envelope
whose only block was ``{"type":"code","lang":"yaml","content":"..."}``. The
extractor only recognised ``markdown`` / ``text`` block kinds, so the cron
recorded ``error="interactive runner produced no output"`` even though Claude
had actually responded as requested.
"""
from __future__ import annotations

import collections
import json

# The extraction/error helpers moved from cron_runner into the shared
# orchestrator_oneshot module (subscription-only migration); cron now calls
# run_oneshot. These regression cases still pin the exact block→text behavior.
from orbit import orchestrator_oneshot as oneshot


class _FakeRunner:
    def __init__(self, events: list[bytes]) -> None:
        self._buffered_events = collections.deque(events)


def _sse(event: str, payload: dict, seq: int = 1) -> bytes:
    body = json.dumps(payload, ensure_ascii=False)
    return (f"id: {seq}\nevent: {event}\ndata: {body}\n\n").encode("utf-8")


def _structured(blocks: list[dict], seq: int = 1) -> bytes:
    return _sse("structured_blocks", {"turn_idx": 0, "schema": "structured", "blocks": blocks}, seq=seq)


def test_extracts_code_block_content_as_fenced_markdown() -> None:
    runner = _FakeRunner([
        _structured([
            {"type": "code", "lang": "yaml", "content": "model: claude-haiku-4-5-20251001\nepoch: 1778832000"},
        ]),
    ])
    out = oneshot.extract_runner_text(runner)
    assert "claude-haiku-4-5-20251001" in out
    assert "epoch: 1778832000" in out
    assert "```yaml" in out


def test_extracts_markdown_block_content() -> None:
    runner = _FakeRunner([
        _structured([{"type": "markdown", "content": "Model: `claude-sonnet-4-6`."}]),
    ])
    assert oneshot.extract_runner_text(runner) == "Model: `claude-sonnet-4-6`."


def test_extracts_mixed_markdown_and_code_blocks_in_order() -> None:
    runner = _FakeRunner([
        _structured([
            {"type": "markdown", "content": "Result:"},
            {"type": "code", "lang": "json", "content": "{\"ok\":true}"},
        ]),
    ])
    out = oneshot.extract_runner_text(runner)
    assert out.index("Result:") < out.index("```json")
    assert '{"ok":true}' in out


def test_falls_back_to_assistant_message_text_blocks() -> None:
    runner = _FakeRunner([
        _sse("assistant_message", {
            "turn_idx": 0,
            "blocks": [{"kind": "text", "text": "raw repair fallback"}],
        }),
    ])
    assert oneshot.extract_runner_text(runner) == "raw repair fallback"


def test_skips_pure_tool_use_assistant_message() -> None:
    runner = _FakeRunner([
        _sse("assistant_message", {
            "turn_idx": 0,
            "blocks": [{"kind": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
        }),
    ])
    assert oneshot.extract_runner_text(runner) == ""


def test_prefers_latest_structured_blocks_event() -> None:
    runner = _FakeRunner([
        _structured([{"type": "markdown", "content": "first turn"}], seq=1),
        _structured([{"type": "markdown", "content": "second turn"}], seq=2),
    ])
    assert oneshot.extract_runner_text(runner) == "second turn"


def test_runner_error_event_message_returns_most_recent() -> None:
    """Order is reverse-chronological — return the latest ``error`` event's
    message so a late JSONL-timeout doesn't get masked by an earlier
    `failed to send prompt` from a retry that already moved on.
    """
    runner = _FakeRunner([
        _sse("error", {"message": "failed to send prompt: EAGAIN"}, seq=1),
        _sse("init", {"model": "haiku", "session_id": "x", "cwd": "/", "mode": "interactive"}, seq=2),
        _sse("error", {"message": "turn timed out waiting for JSONL flush"}, seq=3),
    ])
    assert oneshot.runner_error_message(runner) == "turn timed out waiting for JSONL flush"


def test_runner_error_event_message_returns_none_when_absent() -> None:
    runner = _FakeRunner([
        _sse("init", {"model": "sonnet", "session_id": "x", "cwd": "/", "mode": "interactive"}),
        _sse("thinking", {"turn_idx": 0}),
        _sse("done", {"reason": "turn complete"}),
    ])
    assert oneshot.runner_error_message(runner) is None


def test_returns_empty_when_no_assistant_events() -> None:
    runner = _FakeRunner([
        _sse("init", {"model": "sonnet", "session_id": "x", "cwd": "/", "mode": "interactive"}),
        _sse("thinking", {"turn_idx": 0}),
        _sse("done", {"reason": "turn complete"}),
    ])
    assert oneshot.extract_runner_text(runner) == ""
