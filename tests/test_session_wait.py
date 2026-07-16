"""The /wait long-poll primitive — driven against a real in-process hub.

Tests the pure ``await_turn`` coroutine (one event loop, no TestClient thread
hop) so the subscribe-first / monotonic-deadline / frame-filtering / unsubscribe
semantics are exercised for real.
"""
from __future__ import annotations

import asyncio

import pytest

from orbit import orchestrator_wait as wait_mod
from orbit.orchestrator_events import SessionEventHub, _format_sse


def _run(coro):
    return asyncio.run(coro)


def test_resolves_when_already_ahead():
    hub = SessionEventHub()
    msgs = [{"role": "assistant", "turn_idx": 5, "blocks": []}]
    res = _run(wait_mod.await_turn("s", 2, 2.0, hub=hub,
                                   read_session=lambda sid: {"ok": True, "messages": msgs}))
    assert res["status"] == "done"
    assert len(res["new_messages"]) == 1
    assert res["latest_turn_idx"] == 5
    assert "s" not in hub._subscribers  # unsubscribed in finally


def test_resolves_on_turn_done():
    hub = SessionEventHub()
    state = {"messages": []}

    def rs(sid):
        return {"ok": True, "messages": list(state["messages"])}

    async def scenario():
        async def pub():
            await asyncio.sleep(0.05)
            state["messages"] = [{"role": "assistant", "turn_idx": 1, "blocks": []}]
            hub.publish("s", "turn_done", {"session_id": "s", "turn_idx": 1, "cost_usd": 0.5})
        res, _ = await asyncio.gather(
            wait_mod.await_turn("s", 0, 2.0, hub=hub, read_session=rs), pub())
        return res

    res = _run(scenario())
    assert res["status"] == "done"
    assert len(res["new_messages"]) == 1
    assert res["cost_usd"] == 0.5


def test_times_out_without_event():
    hub = SessionEventHub()
    res = _run(wait_mod.await_turn("s", 0, 0.2, hub=hub,
                                   read_session=lambda sid: {"ok": True, "messages": []}))
    assert res["status"] == "timeout"
    assert res["new_messages"] == []
    assert "s" not in hub._subscribers


def test_resolves_on_turn_error():
    hub = SessionEventHub()

    async def scenario():
        async def pub():
            await asyncio.sleep(0.05)
            hub.publish("s", "turn_error", {"session_id": "s", "turn_idx": -1, "message": "boom"})
        res, _ = await asyncio.gather(
            wait_mod.await_turn("s", 0, 2.0, hub=hub,
                                read_session=lambda sid: {"ok": True, "messages": []}), pub())
        return res

    res = _run(scenario())
    assert res["status"] == "error"
    assert res["error"] == "boom"


def test_ignores_non_terminal_frames():
    hub = SessionEventHub()

    async def scenario():
        async def pub():
            await asyncio.sleep(0.03)
            hub.publish("s", "turn_started", {"session_id": "s", "turn_idx": 0})
            hub.publish("s", "artifact_create", {"artifact": {}})
            # no terminal frame → wait must keep waiting and time out
        res, _ = await asyncio.gather(
            wait_mod.await_turn("s", 0, 0.25, hub=hub,
                                read_session=lambda sid: {"ok": True, "messages": []}), pub())
        return res

    res = _run(scenario())
    assert res["status"] == "timeout"


def test_concurrency_cap_raises():
    saved = wait_mod._wait_inflight
    wait_mod._wait_inflight = wait_mod.WAIT_MAX_CONCURRENT
    try:
        hub = SessionEventHub()
        with pytest.raises(wait_mod.WaitSaturated):
            _run(wait_mod.await_turn_guarded("s", 0, 0.1, hub=hub,
                                             read_session=lambda sid: {"ok": True, "messages": []}))
    finally:
        wait_mod._wait_inflight = saved


def test_parse_terminal_frame():
    assert wait_mod._parse_terminal_frame(_format_sse("turn_done", {"turn_idx": 2}, seq=1)) == \
        ("turn_done", {"turn_idx": 2})
    assert wait_mod._parse_terminal_frame(_format_sse("turn_started", {}, seq=2)) is None
    assert wait_mod._parse_terminal_frame(_format_sse("artifact_create", {}, seq=3)) is None
    assert wait_mod._parse_terminal_frame(b": ping\n\n") is None


def test_route_rejects_bad_session_id(client):
    r = client.get("/api/orchestrator/sessions/not-a-uuid/wait?timeout=0.1")
    assert r.status_code == 400
