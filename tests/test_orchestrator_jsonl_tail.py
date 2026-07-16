"""Tests for orchestrator_jsonl_tail — incremental JSONL reader.

The PoC `_tail_until_turn_end` proved the algorithm works against a real
claude flush. This module is a productionised, async-friendly version of
the same logic, used by `TmuxClaudeRunner` to bridge JSONL → SSE events.

Tests use a real temp file with controlled writes so we exercise the
poll loop empirically (no mocks of time/io).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest


def _write_line(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSONL line atomically enough for the tail to observe it."""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()


def _make_user_line(text: str = "hi") -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _make_assistant_line(text: str, *, end_turn: bool = False) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn" if end_turn else None,
        },
    }


def _make_stop_hook() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary"}


def _run(coro):
    return asyncio.run(coro)


# ── path / slug ────────────────────────────────────────────────────


def test_slug_replaces_slash_underscore_dot(tmp_path):
    """PoC bonus finding: claude project-dir slug replaces ALL of `/`, `_`, `.`
    with `-`, not just `/`. Plan's original assumption was wrong; jsonl_path
    helper must match this.

    Uses ``tmp_path`` (no symlinks) so the assertion is stable across
    macOS (where ``/tmp`` → ``/private/tmp``) and Linux.
    """
    from orbit import orchestrator_jsonl_tail as mod
    cwd = tmp_path / "foo_bar.baz"
    cwd.mkdir()
    expected = "".join("-" if ch in "/_." else ch for ch in str(cwd))
    assert mod.slug_for_cwd(cwd) == expected
    # Sanity-check the rule itself on a known string (assumes /home is not a
    # symlink — true on Linux; on macOS skip via realpath round-trip).
    plain = "/home/testuser"
    if os.path.realpath(plain) == plain:
        assert mod.slug_for_cwd(plain) == "-home-testuser"


def test_jsonl_path_assembles_under_claude_projects(tmp_path, monkeypatch):
    from orbit import orchestrator_jsonl_tail as mod
    monkeypatch.setattr(mod, "_CLAUDE_HOME", tmp_path / "_claude")
    cwd = tmp_path / "wd"
    cwd.mkdir()
    p = mod.jsonl_path_for(cwd, "abc-123")
    expected_slug = "".join("-" if ch in "/_." else ch for ch in str(cwd))
    assert p == tmp_path / "_claude" / "projects" / expected_slug / "abc-123.jsonl"


# ── happy path: stop_hook_summary detection ───────────────────────


def test_tail_returns_on_stop_hook_after_user(tmp_path):
    """Primary end-of-turn signal: `system / stop_hook_summary` AFTER a `user` line."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()

    def writer():
        time.sleep(0.2)
        _write_line(jsonl, _make_user_line("hi"))
        time.sleep(0.2)
        _write_line(jsonl, _make_assistant_line("hello"))
        time.sleep(0.2)
        _write_line(jsonl, _make_stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        lines, offset = _run(
            mod.tail_until_turn_end(jsonl, since_byte=0, timeout=5.0, poll_interval=0.05)
        )
    finally:
        th.join()

    assert offset > 0
    assert offset == jsonl.stat().st_size
    types = [(l.get("type"), l.get("subtype")) for l in lines]
    assert ("user", None) in types
    assert ("assistant", None) in types
    assert ("system", "stop_hook_summary") in types


def test_tail_respects_since_byte_offset(tmp_path):
    """Multi-turn: second call from `end_offset` of first must only see new lines."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()
    # Turn 1
    _write_line(jsonl, _make_user_line("turn1"))
    _write_line(jsonl, _make_assistant_line("a1"))
    _write_line(jsonl, _make_stop_hook())
    offset_after_t1 = jsonl.stat().st_size

    # Turn 2 written in background
    def writer():
        time.sleep(0.1)
        _write_line(jsonl, _make_user_line("turn2"))
        _write_line(jsonl, _make_assistant_line("a2"))
        _write_line(jsonl, _make_stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        lines, offset = _run(
            mod.tail_until_turn_end(
                jsonl, since_byte=offset_after_t1, timeout=5.0, poll_interval=0.05
            )
        )
    finally:
        th.join()

    assert offset > offset_after_t1
    user_texts = [
        l.get("message", {}).get("content", [{}])[0].get("text")
        for l in lines if l.get("type") == "user"
    ]
    assert user_texts == ["turn2"]  # turn1 NOT replayed


# ── fallback: end_turn + quiet mtime ──────────────────────────────


def test_tail_returns_on_end_turn_with_quiet_mtime(tmp_path):
    """Fallback signal: stop_reason='end_turn' on last assistant + 3s of no
    mtime change → consider turn done."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()
    _write_line(jsonl, _make_user_line("hi"))
    _write_line(jsonl, _make_assistant_line("done!", end_turn=True))
    # No stop_hook — rely on quiet-mtime path.

    start = time.monotonic()
    lines, _ = _run(
        mod.tail_until_turn_end(
            jsonl,
            since_byte=0,
            timeout=10.0,
            poll_interval=0.05,
            quiet_seconds=0.5,  # shorten the fallback window for the test
        )
    )
    elapsed = time.monotonic() - start
    assert elapsed >= 0.5
    types = [(l.get("type"), l.get("subtype")) for l in lines]
    assert ("assistant", None) in types


# ── timeout & error paths ─────────────────────────────────────────


def test_tail_times_out_when_no_turn_completes(tmp_path):
    """A user line written but no assistant/stop → must time out, not hang."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()
    _write_line(jsonl, _make_user_line("hello?"))

    with pytest.raises(TimeoutError):
        _run(
            mod.tail_until_turn_end(
                jsonl, since_byte=0, timeout=0.8, poll_interval=0.05, quiet_seconds=10.0
            )
        )


def test_tail_waits_for_file_to_appear(tmp_path):
    """JSONL doesn't exist immediately — claude only creates it once it spins up.
    The tail must wait (not raise) up to the timeout."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    # File doesn't exist yet.

    def writer():
        time.sleep(0.3)
        jsonl.touch()
        time.sleep(0.1)
        _write_line(jsonl, _make_user_line("hi"))
        _write_line(jsonl, _make_assistant_line("hey"))
        _write_line(jsonl, _make_stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        lines, _ = _run(
            mod.tail_until_turn_end(jsonl, since_byte=0, timeout=5.0, poll_interval=0.05)
        )
    finally:
        th.join()
    assert len(lines) >= 3


def test_tail_handles_partial_line_writes(tmp_path):
    """JSONL writer may flush half a line at a time across the poll boundary —
    the buffer must NOT yield until a full `\\n` has arrived."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()

    def writer():
        time.sleep(0.1)
        # Write half a JSON line first.
        partial = json.dumps(_make_user_line("hi"))
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(partial[: len(partial) // 2])
            fh.flush()
        time.sleep(0.2)
        # Now the rest + newline + the full turn.
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(partial[len(partial) // 2 :] + "\n")
            fh.flush()
        _write_line(jsonl, _make_assistant_line("hey"))
        _write_line(jsonl, _make_stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        lines, _ = _run(
            mod.tail_until_turn_end(jsonl, since_byte=0, timeout=5.0, poll_interval=0.05)
        )
    finally:
        th.join()

    user_lines = [l for l in lines if l.get("type") == "user"]
    assert len(user_lines) == 1
    assert user_lines[0]["message"]["content"][0]["text"] == "hi"


def test_tail_skips_malformed_json(tmp_path):
    """A corrupt line shouldn't blow up the reader — skip and continue."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    _write_line(jsonl, _make_user_line("hi"))
    _write_line(jsonl, _make_assistant_line("hey"))
    _write_line(jsonl, _make_stop_hook())

    lines, _ = _run(
        mod.tail_until_turn_end(jsonl, since_byte=0, timeout=5.0, poll_interval=0.05)
    )
    types = [l.get("type") for l in lines]
    assert "user" in types and "assistant" in types
    # The garbage line is silently dropped (no entry in `lines`).
    assert all(isinstance(l, dict) for l in lines)


# ── stop_hook BEFORE user line: ignore ────────────────────────────


def test_tail_yields_event_loop_each_data_rich_poll(tmp_path, monkeypatch):
    """Code review #3 (PR #39): the `await asyncio.sleep(poll_interval)` must
    yield the event loop EVEN when each poll consumes new bytes. Without this,
    a continuously-flushed JSONL turns the tail into a tight non-cooperative
    read loop that starves other coroutines (e.g. SSE writes to the connected
    client).

    Strategy: replace the synchronous file read with a stub that returns one
    JSONL line per call, never an empty chunk until the stop_hook is seen.
    This forces the data-rich branch on EVERY iteration. With the fix in
    place, `asyncio.sleep` is awaited once per iteration; without it, the
    counter stays at 0 because the loop never enters the else branch.
    """
    from orbit import orchestrator_jsonl_tail as mod

    sleep_calls = 0
    original_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        await original_sleep(delay)

    monkeypatch.setattr(mod.asyncio, "sleep", counting_sleep)

    # Produce a fake JSONL byte-stream that hands one line per `fh.read()` call.
    lines = [
        _make_user_line("real turn"),
        _make_user_line("more input"),
        _make_user_line("still more"),
        _make_assistant_line("done"),
        _make_stop_hook(),
    ]
    chunks = [(json.dumps(line) + "\n").encode("utf-8") for line in lines]

    class _SeqFile:
        """Single-shot stub: each `read()` pops one chunk from the SHARED
        list so successive polls consume different bytes."""

        def __init__(self, shared):
            self._shared = shared

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def seek(self, _offset):
            return None

        def read(self):
            if not self._shared:
                return b""
            return self._shared.pop(0)

    fake_path = tmp_path / "fake.jsonl"
    fake_path.touch()

    original_open = Path.open

    def stub_open(self, *args, **kwargs):
        if self == fake_path and (args and args[0] == "rb"):
            return _SeqFile(chunks)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", stub_open)

    _run(
        mod.tail_until_turn_end(
            fake_path, since_byte=0, timeout=5.0, poll_interval=0.001
        )
    )

    assert sleep_calls >= 3, (
        f"tail busy-looped through data-rich polls; "
        f"asyncio.sleep was awaited only {sleep_calls} times "
        f"(expected one yield per poll iteration)"
    )


def test_tail_ignores_stop_hook_before_any_user_line(tmp_path):
    """Some sessions have a leftover stop_hook from a previous turn at the
    start of the tailed range. Without a `user` line FIRST, it's a stale
    artifact — don't return prematurely."""
    from orbit import orchestrator_jsonl_tail as mod
    jsonl = tmp_path / "session.jsonl"
    jsonl.touch()
    # Leftover stop_hook BEFORE we send anything.
    _write_line(jsonl, _make_stop_hook())

    def writer():
        time.sleep(0.2)
        _write_line(jsonl, _make_user_line("real turn"))
        _write_line(jsonl, _make_assistant_line("reply"))
        _write_line(jsonl, _make_stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        lines, _ = _run(
            mod.tail_until_turn_end(jsonl, since_byte=0, timeout=5.0, poll_interval=0.05)
        )
    finally:
        th.join()

    user_lines = [l for l in lines if l.get("type") == "user"]
    assert len(user_lines) == 1  # we waited until OUR user line appeared
