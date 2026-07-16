"""Keep-alive (persistent) session slots — exempt from idle eviction.

Tests the TmuxPool eviction core directly (populating ``_slots`` + driving
``_mark_cooldowns_locked`` / ``evict_idle`` / ``set_persistent`` / ``snapshot``)
so we don't pull in the full ``acquire`` spawn side effects.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class _MiniRunner:
    """Minimal TmuxRunner double for pool eviction tests."""

    def __init__(self) -> None:
        self.killed: list[str] = []

    async def spawn(self, name, cmd):  # pragma: no cover — not exercised here
        pass

    async def wait_until_ready(self, name):  # pragma: no cover
        return True

    async def send_exit(self, name):
        pass

    async def has_session(self, name):
        return False  # already gone after /exit

    async def kill_session(self, name):
        self.killed.append(name)


def _slot(mod, sid):
    return mod.TmuxSlot(session_id=sid, session_name=f"hd-{sid}", cwd=Path("/tmp"))


def _populate(pool, mod, sids):
    """Insert slots in LRU→MRU order (dict insertion order = LRU first)."""
    for sid in sids:
        pool._slots[sid] = _slot(mod, sid)


def test_mark_cooldowns_exempts_persistent():
    from orbit import orchestrator_tmux as mod
    pool = mod.TmuxPool(pool_size=2, runner=_MiniRunner(), persistent_ids={"keep"})
    _populate(pool, mod, ["keep", "a", "b", "c"])  # keep is LRU but persistent
    pool._mark_cooldowns_locked()
    # keep is persistent → never cools AND doesn't occupy a hot-ring slot, so
    # the 2 hot non-persistent slots are b + c; only a (LRU non-persistent)
    # falls out and cools.
    assert pool._slots["keep"].evict_at is None
    assert pool._slots["a"].evict_at is not None
    assert pool._slots["b"].evict_at is None
    assert pool._slots["c"].evict_at is None


def test_evict_idle_spares_persistent():
    from orbit import orchestrator_tmux as mod
    pool = mod.TmuxPool(pool_size=2, runner=_MiniRunner(), persistent_ids={"keep"})
    _populate(pool, mod, ["keep", "a"])
    # Even with a stale/expired deadline, a persistent slot is spared.
    pool._slots["keep"].evict_at = 0.0
    pool._slots["a"].evict_at = 0.0  # non-persistent, expired → reaped
    _run(pool.evict_idle())
    assert "keep" in pool._slots
    assert "a" not in pool._slots


def test_set_persistent_clears_cooldown():
    from orbit import orchestrator_tmux as mod
    pool = mod.TmuxPool(pool_size=1, runner=_MiniRunner())
    _populate(pool, mod, ["a", "b"])  # pool_size 1 → a (LRU) cools
    pool._mark_cooldowns_locked()
    assert pool._slots["a"].evict_at is not None
    live = _run(pool.set_persistent("a", True))
    assert live is True
    assert pool._slots["a"].evict_at is None  # toggling ON cleared the deadline
    assert pool._is_persistent("a")


def test_set_persistent_off_rearms_cooldown():
    from orbit import orchestrator_tmux as mod
    pool = mod.TmuxPool(pool_size=1, runner=_MiniRunner(), persistent_ids={"a"})
    _populate(pool, mod, ["a", "b"])
    pool._mark_cooldowns_locked()
    assert pool._slots["a"].evict_at is None  # persistent → hot
    _run(pool.set_persistent("a", False))
    assert not pool._is_persistent("a")
    assert pool._slots["a"].evict_at is not None  # now over-capacity → cooling


def test_forget_persistent_clears_id():
    """Deleting a session forgets its keep-alive id (no stale-set leak)."""
    from orbit import orchestrator_tmux as mod
    pool = mod.TmuxPool(pool_size=2, runner=_MiniRunner(), persistent_ids={"gone"})
    assert pool._is_persistent("gone")
    _run(pool.forget_persistent("gone"))
    assert not pool._is_persistent("gone")


def test_snapshot_reports_persistent():
    from orbit import orchestrator_tmux as mod
    pool = mod.TmuxPool(pool_size=4, runner=_MiniRunner(), persistent_ids={"keep"})
    _populate(pool, mod, ["keep", "a"])
    by_id = {s["session_id"]: s for s in pool.snapshot()["slots"]}
    assert by_id["keep"]["persistent"] is True
    assert by_id["a"]["persistent"] is False
