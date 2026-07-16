"""close-session ``kill`` flag: detach (park) vs release (end the REPL)."""
from __future__ import annotations

import asyncio

from orbit import orchestrator as orch


def _capture(monkeypatch):
    calls = {}

    async def _fake_release(session_id, *, forget_persistent, detach_tmux=False):
        calls["session_id"] = session_id
        calls["forget_persistent"] = forget_persistent
        calls["detach_tmux"] = detach_tmux

    monkeypatch.setattr(orch, "_release_session_slots", _fake_release)
    return calls


def test_close_default_detaches(monkeypatch):
    calls = _capture(monkeypatch)
    out = asyncio.run(orch._close_session_handler("sid-1"))
    assert out == {"ok": True}
    assert calls["detach_tmux"] is True          # park: keep the REPL running
    assert calls["forget_persistent"] is False   # close preserves keep-alive tracking


def test_close_kill_releases(monkeypatch):
    calls = _capture(monkeypatch)
    out = asyncio.run(orch._close_session_handler("sid-2", kill=True))
    assert out == {"ok": True}
    assert calls["detach_tmux"] is False          # kill: actually tear down the REPL
    assert calls["forget_persistent"] is False    # transcript + keep-alive still kept


def test_close_kill_downgraded_to_park_for_keepalive(monkeypatch):
    """A kill request for a keep-alive session is downgraded to a park (detach) —
    server-side backstop for the client-side guards."""
    calls = _capture(monkeypatch)

    class _FakePool:
        def _is_persistent(self, sid):
            return sid == "keep"

    monkeypatch.setattr(orch, "_tmux_pool", _FakePool())
    asyncio.run(orch._close_session_handler("keep", kill=True))
    assert calls["detach_tmux"] is True            # persistent → parked, NOT killed
    asyncio.run(orch._close_session_handler("other", kill=True))
    assert calls["detach_tmux"] is False           # non-persistent → real kill
