"""Tests for orchestrator_read_aloud — passive watcher → 'speak' SSE events.

Mirrors test_orchestrator_jsonl_tail.py: real temp JSONL files, background-thread
writes, a real asyncio loop via asyncio.run(), and short sleeps. Assertions read
the SessionEventHub's per-session buffer to verify published 'speak' frames.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest


# ── JSONL line builders (match real claude shape: top-level uuid) ──────


def _write_line(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")
        fh.flush()


def _user(text: str = "hi") -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text: str, *, uuid: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"},
    }
    if uuid:
        line["uuid"] = uuid
    return line


def _stop_hook() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary"}


def _run(coro):
    return asyncio.run(coro)


def _speak_events(published, session_id: str) -> list[dict[str, Any]]:
    """Filter captured publish() calls to this session's 'speak' payloads.

    Reads from the fixture's captured-publish list (not hub._buffers) because the
    watcher publishes speak frames with buffer=False, so they never land in the
    replay deque.
    """
    return [data for (sid, event, data) in published
            if event == "speak" and sid == session_id and not data.get("end_flow")]


# ── fixture: fresh hub + manager, feature enabled, patched path resolver ──


@pytest.fixture
def ra(monkeypatch, tmp_path):
    from orbit import orchestrator_events as events_mod
    from orbit import orchestrator_read_aloud as mod

    # Fresh singletons so per-session state never leaks across tests.
    monkeypatch.setattr(events_mod, "_hub", None)
    monkeypatch.setattr(mod, "_manager", None)
    # Feature on, and JSONL paths land in tmp_path/<sid>.jsonl.
    monkeypatch.setattr(mod, "is_enabled", lambda: True)
    monkeypatch.setattr(mod, "_resolve_jsonl_path", lambda sid: tmp_path / f"{sid}.jsonl")
    hub = events_mod.get_hub()  # no .start() — no loop needed
    # Capture publish() calls (speak frames use buffer=False, so _buffers is empty).
    published: list[tuple] = []
    orig_publish = hub.publish

    def _capture(session_id, event, data, **kw):
        published.append((session_id, event, data))
        return orig_publish(session_id, event, data, **kw)

    monkeypatch.setattr(hub, "publish", _capture)
    return types.SimpleNamespace(mod=mod, hub=hub, tmp=tmp_path, published=published)


# ── (1) publishes the last assistant turn, anchored (no history replay) ──


def test_watcher_publishes_last_turn_and_does_not_replay(ra):
    sid = "sess1"
    jsonl = ra.tmp / f"{sid}.jsonl"
    jsonl.touch()
    # Turn 1 already on disk BEFORE arming → must NOT be spoken.
    _write_line(jsonl, _user("old q"))
    _write_line(jsonl, _assistant("OLD answer", uuid="u-old"))
    _write_line(jsonl, _stop_hook())

    def writer():
        time.sleep(0.12)
        _write_line(jsonl, _user("new q"))
        _write_line(jsonl, _assistant("NEW answer", uuid="u-new"))
        _write_line(jsonl, _stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        async def run():
            m = ra.mod.get_manager()
            m.arm(sid)  # anchors at EOF (after turn 1)
            for _ in range(100):  # poll up to ~2s instead of a fixed sleep (CI-robust)
                if _speak_events(ra.published, sid):
                    break
                await asyncio.sleep(0.02)
            m.disarm(sid)
        _run(run())
    finally:
        th.join()

    events = _speak_events(ra.published, sid)
    assert len(events) == 1, f"expected exactly the new turn, got {events}"
    assert events[0]["text"] == "NEW answer"
    assert events[0]["key"] == "u-new"
    assert all(e["key"] != "u-old" for e in events)  # history not replayed


# ── (1b) intermediate blocks read in order + end_flow on stop_hook ────


def test_watcher_reads_intermediate_blocks_in_order(ra):
    sid = "multi"
    jsonl = ra.tmp / f"{sid}.jsonl"
    jsonl.touch()

    def writer():
        time.sleep(0.1)
        _write_line(jsonl, _user("do a multi-step thing"))
        _write_line(jsonl, _assistant("Sprawdzam to.", uuid="u-b1"))            # intermediate block
        _write_line(jsonl, _assistant("Gotowe, wynik to dziesięć.", uuid="u-b2"))  # final block
        _write_line(jsonl, _stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        async def run():
            m = ra.mod.get_manager()
            m.arm(sid)
            for _ in range(120):  # poll until both blocks have arrived (~2.4s cap)
                if len(_speak_events(ra.published, sid)) >= 2:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)  # let the stop_hook end_flow land too
            m.disarm(sid)
        _run(run())
    finally:
        th.join()

    evs = _speak_events(ra.published, sid)
    assert [e["text"] for e in evs] == ["Sprawdzam to.", "Gotowe, wynik to dziesięć."]
    assert [e["key"] for e in evs] == ["u-b1", "u-b2"]  # block-granular, in order
    # an end_flow marker is published on the turn's stop_hook_summary
    ends = [d for (s, e, d) in ra.published if e == "speak" and s == sid and d.get("end_flow")]
    assert len(ends) >= 1


# ── (1c) warm-resume across an SSE flap (the car lost-turn fix) ───────


def _keys(published, sid):
    return [e["key"] for e in _speak_events(published, sid)]


def test_warm_resume_recovers_a_turn_written_during_a_flap(ra):
    """The car bug: a turn that completes while the EventSource is briefly
    disconnected must be re-read + published on reconnect (within grace),
    without re-publishing the already-heard prior turn."""
    sid = "flap"
    jsonl = ra.tmp / f"{sid}.jsonl"
    jsonl.touch()

    async def run():
        m = ra.mod.get_manager()
        # Connection 1: arm, turn A flushes and publishes.
        m.arm(sid)
        await asyncio.sleep(0.05)
        _write_line(jsonl, _user("q1"))
        _write_line(jsonl, _assistant("Odpowiedź A", uuid="A"))
        _write_line(jsonl, _stop_hook())
        for _ in range(120):
            if "A" in _keys(ra.published, sid):
                break
            await asyncio.sleep(0.02)
        # Flap: disarm (offset saved). Turn B flushes WHILE disconnected.
        m.disarm(sid)
        await asyncio.sleep(0.05)
        _write_line(jsonl, _user("q2"))
        _write_line(jsonl, _assistant("Odpowiedź B", uuid="B"))
        _write_line(jsonl, _stop_hook())
        await asyncio.sleep(0.05)
        # Reconnect within grace → resume from the saved offset → B published.
        m.arm(sid)
        for _ in range(120):
            if "B" in _keys(ra.published, sid):
                break
            await asyncio.sleep(0.02)
        m.disarm(sid)

    _run(run())
    keys = _keys(ra.published, sid)
    assert "A" in keys, f"turn A never published: {keys}"
    assert "B" in keys, f"turn B (during flap) LOST — resume failed: {keys}"
    assert keys.count("A") == 1, f"turn A re-spoken on resume: {keys}"


def test_cold_open_after_grace_anchors_at_eof_no_replay(ra, monkeypatch):
    """Past the grace window a re-arm is a cold open: anchor at EOF, never
    replay history (the original no-history-replay guarantee)."""
    monkeypatch.setattr(ra.mod, "RESUME_GRACE_S", 0.0)
    sid = "cold"
    jsonl = ra.tmp / f"{sid}.jsonl"
    jsonl.touch()

    async def run():
        m = ra.mod.get_manager()
        m.arm(sid)
        await asyncio.sleep(0.05)
        _write_line(jsonl, _user("q1"))
        _write_line(jsonl, _assistant("Odpowiedź A", uuid="A"))
        _write_line(jsonl, _stop_hook())
        for _ in range(120):
            if "A" in _keys(ra.published, sid):
                break
            await asyncio.sleep(0.02)
        m.disarm(sid)
        await asyncio.sleep(0.05)
        _write_line(jsonl, _user("q2"))
        _write_line(jsonl, _assistant("Odpowiedź B", uuid="B"))
        _write_line(jsonl, _stop_hook())
        await asyncio.sleep(0.05)
        m.arm(sid)  # grace=0 → cold open → anchors past B
        await asyncio.sleep(0.4)
        m.disarm(sid)

    _run(run())
    keys = _keys(ra.published, sid)
    assert "A" in keys
    assert "B" not in keys, f"cold open replayed history: {keys}"


def test_resume_rejected_when_offset_exceeds_size(ra):
    """Truncation / a /compact file swap (saved offset > current size, or a
    different path) must NOT resume — else stale turns get re-spoken eyes-free."""
    sid = "trunc"
    jsonl = ra.tmp / f"{sid}.jsonl"
    jsonl.touch()
    _write_line(jsonl, _user("old"))
    _write_line(jsonl, _assistant("stara odpowiedź", uuid="OLD"))
    _write_line(jsonl, _stop_hook())

    async def run():
        m = ra.mod.get_manager()
        # Pre-seed a recent disarm with an offset BEYOND the file → guard rejects.
        m._offsets[sid] = (10_000_000, str(jsonl))
        m._disarmed_at[sid] = time.monotonic()
        m.arm(sid)  # resume hint invalid (offset > size) → anchor EOF
        await asyncio.sleep(0.3)
        m.disarm(sid)

    _run(run())
    assert "OLD" not in _keys(ra.published, sid), "re-spoke stale history on bad resume"


# ── (2) ref-count lifecycle ───────────────────────────────────────────


def test_ref_count_keeps_watcher_until_last_disarm(ra):
    sid = "refs"
    (ra.tmp / f"{sid}.jsonl").touch()

    async def run():
        m = ra.mod.get_manager()
        m.arm(sid)
        m.arm(sid)
        assert m._refs[sid] == 2
        task = m._tasks[sid]
        assert not task.done()

        m.disarm(sid)  # 2 → 1, still alive
        assert m._refs[sid] == 1
        assert not task.done()

        m.disarm(sid)  # 1 → 0, cancelled
        assert sid not in m._refs
        assert sid not in m._tasks
        await asyncio.sleep(0.05)
        assert task.done()

    _run(run())


def test_disarm_unknown_session_is_noop(ra):
    async def run():
        ra.mod.get_manager().disarm("never-armed")  # must not raise

    _run(run())


# ── (3) flag gating ───────────────────────────────────────────────────


def test_arm_is_noop_when_flag_disabled(ra, monkeypatch):
    monkeypatch.setattr(ra.mod, "is_enabled", lambda: False)
    sid = "disabled"
    (ra.tmp / f"{sid}.jsonl").touch()

    async def run():
        m = ra.mod.get_manager()
        m.arm(sid)
        assert sid not in m._tasks
        assert sid not in m._refs

    _run(run())


# ── (4) multiple sessions are independent ─────────────────────────────


def test_sessions_are_independent(ra):
    sid_a, sid_b = "sa", "sb"
    (ra.tmp / f"{sid_a}.jsonl").touch()
    (ra.tmp / f"{sid_b}.jsonl").touch()

    async def run():
        m = ra.mod.get_manager()
        m.arm(sid_a)
        m.arm(sid_b)
        task_a, task_b = m._tasks[sid_a], m._tasks[sid_b]
        assert not task_a.done() and not task_b.done()

        m.disarm(sid_a)
        await asyncio.sleep(0.05)
        assert task_a.done()
        assert not task_b.done()  # B unaffected

        m.disarm(sid_b)
        await asyncio.sleep(0.05)
        assert task_b.done()

    _run(run())


# ── (5) a pure non-prose (tool-use) turn publishes nothing ────────────


def test_turn_without_prose_publishes_nothing(ra):
    sid = "noprose"
    jsonl = ra.tmp / f"{sid}.jsonl"
    jsonl.touch()

    def writer():
        time.sleep(0.12)
        _write_line(jsonl, _user("do a thing"))
        # assistant turn with only a tool_use block — no speakable text
        _write_line(jsonl, {
            "type": "assistant",
            "uuid": "u-tool",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash"}], "stop_reason": "end_turn"},
        })
        _write_line(jsonl, _stop_hook())

    th = threading.Thread(target=writer)
    th.start()
    try:
        async def run():
            ra.mod.get_manager().arm(sid)
            await asyncio.sleep(0.5)
            ra.mod.get_manager().disarm(sid)
        _run(run())
    finally:
        th.join()

    assert _speak_events(ra.published, sid) == []


# ── (6) unit: last-assistant extraction picks the final prose turn ────


def test_extract_last_assistant_picks_final_prose():
    from orbit import orchestrator_read_aloud as mod

    lines = [
        _user("hi"),
        _assistant("first reply", uuid="u1"),
        {"type": "assistant", "uuid": "u-mid", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t"}]}},
        _assistant("final reply", uuid="u2"),
    ]
    result = mod._extract_last_assistant(lines)
    assert result == ("final reply", "u2")  # NamedTuple compares equal to the tuple
    assert result.text == "final reply" and result.key == "u2"


def test_extract_last_assistant_none_when_no_prose():
    from orbit import orchestrator_read_aloud as mod

    assert mod._extract_last_assistant([_user("hi")]) is None


def test_key_falls_back_to_message_id_then_hash():
    from orbit import orchestrator_read_aloud as mod

    # no top-level uuid → message.id
    assert mod._key_for({"message": {"id": "msg_123"}}, "x") == "msg_123"
    # neither → content hash, stable for the same text
    k1 = mod._key_for({}, "hello")
    k2 = mod._key_for({}, "hello")
    assert k1 == k2 and k1.startswith("sha:")


# ── (7) the route's frame filter: speak + keepalive pass, artifacts drop ──


def test_is_forwardable_frame_discriminates():
    """The /read-aloud route forwards only 'speak' frames + keepalive comments
    from the SHARED per-session bus; artifact frames must be dropped. Built with
    the real _format_sse so a frame-format change is caught."""
    from orbit import orchestrator_read_aloud as mod
    from orbit import orchestrator_events as ev

    speak = ev._format_sse("speak", {"text": "hi", "key": "k"}, seq=1)
    artifact = ev._format_sse("artifact_created", {"artifact": {"id": "a"}}, seq=2)
    ping = b": ping\n\n"
    assert mod.is_forwardable_frame(speak) is True
    assert mod.is_forwardable_frame(ping) is True
    assert mod.is_forwardable_frame(artifact) is False
    # An artifact frame whose DATA literally contains "event: speak" must NOT
    # leak: json.dumps escapes the newline, so the header marker only ever
    # appears as a real header line.
    tricky = ev._format_sse("artifact_created", {"text": "event: speak\n"}, seq=3)
    assert mod.is_forwardable_frame(tricky) is False
