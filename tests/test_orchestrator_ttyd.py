"""Tests for orchestrator_ttyd — per-session ttyd pool.

Every shell invocation flows through :class:`TtydSpawner` which is
injectable, so we exercise the pool lifecycle (acquire / release /
evict / recover-orphans / port-allocation) without spawning real
ttyd processes.

The argv builder and port allocator are tested directly.
"""
from __future__ import annotations

import asyncio
import socket

import pytest


def _run(coro):
    return asyncio.run(coro)


# ── build_ttyd_argv ────────────────────────────────────────────────


def test_build_ttyd_argv_localhost_only():
    """ttyd MUST bind 127.0.0.1 — auth is the FastAPI proxy. Exposing the
    raw port on the Tailscale interface would skip auth entirely."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="abc", port=7700)
    assert "--interface" in argv
    assert argv[argv.index("--interface") + 1] == "127.0.0.1"


def test_build_ttyd_argv_writable_flag_present():
    """The whole point of the feature is letting the user type into the
    terminal — without --writable ttyd ignores all input."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="abc", port=7700)
    assert "--writable" in argv


def test_build_ttyd_argv_base_path_carries_session_id():
    """--base-path must be /api/orchestrator/sessions/<sid>/term so the
    FastAPI proxy can forward without prefix stripping."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="abc-123", port=7700)
    base_path_idx = argv.index("--base-path")
    assert argv[base_path_idx + 1] == "/api/orchestrator/sessions/abc-123/term"


def test_build_ttyd_argv_tmux_attach_to_pool_session():
    """The trailing command chain attaches to our pool's tmux session.
    Verified positionally on `attach -t hd-<sid>` (the last meaningful
    operation) regardless of how many ``;``-separated set-option
    preambles run before it."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="xyz", port=7700)
    assert "attach" in argv
    attach_idx = argv.index("attach")
    assert argv[attach_idx:attach_idx + 3] == ["attach", "-t", "hd-xyz"]
    # Single tmux binary invocation followed by chained commands.
    assert argv[argv.index("-L") - 1].endswith("tmux")


def test_build_ttyd_argv_sets_window_size_latest_before_attach():
    """Per-session ``window-size latest`` set BEFORE attach so the iframe's
    xterm.js viewport drives the pane size. Without this an out-of-band
    ``.tmux.conf`` with ``window-size largest`` would keep the pane at
    its spawn-time 200x50 and clip claude's input prompt below the
    visible area (UAT 2026-05-27 root cause of \"input cut off\")."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="xyz", port=7700)
    # Find the set-option ... window-size latest sequence.
    so_idx = argv.index("set-option")
    assert argv[so_idx:so_idx + 6] == [
        "set-option", "-t", "hd-xyz", "window-size", "latest", ";",
    ]
    # And it MUST come before the attach.
    assert so_idx < argv.index("attach")


def test_build_ttyd_argv_includes_theme_client_option():
    """xterm.js theme is the dashboard-matching dark palette, passed via
    --client-option theme=<json>. Without this the embedded terminal
    renders in ttyd's default white-on-black which clashes hard with
    the dashboard chrome."""
    from orbit import orchestrator_ttyd as mod
    import json as _json
    argv = mod.build_ttyd_argv(session_id="abc", port=7700)
    # Find every "--client-option" pair and locate the theme one.
    pairs = [
        argv[i + 1] for i, tok in enumerate(argv) if tok == "--client-option"
    ]
    theme_pairs = [p for p in pairs if p.startswith("theme=")]
    assert len(theme_pairs) == 1, f"expected one theme= client-option, got {theme_pairs!r}"
    theme = _json.loads(theme_pairs[0][len("theme="):])
    assert theme["background"] == "#0e0f12"  # --bg
    assert theme["foreground"] == "#ebe9e4"  # --fg


def test_build_ttyd_argv_includes_font_client_option():
    """fontFamily option must reach xterm.js so the terminal uses the
    same monospace stack as the rest of the dashboard. Tested by
    grepping the --client-option pairs rather than asserting positional
    order, because order is irrelevant to ttyd's CLI parser."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="abc", port=7700)
    pairs = [
        argv[i + 1] for i, tok in enumerate(argv) if tok == "--client-option"
    ]
    font_pairs = [p for p in pairs if p.startswith("fontFamily=")]
    assert font_pairs, f"no fontFamily client-option found among {pairs!r}"
    assert "JetBrains Mono" in font_pairs[0]


def test_build_ttyd_argv_check_origin_false():
    """The browser opens the WS via the dashboard origin (NOT ttyd's
    localhost URL), so ttyd's default origin check would reject every
    upgrade. Safe to disable because ttyd is 127.0.0.1-bound."""
    from orbit import orchestrator_ttyd as mod
    argv = mod.build_ttyd_argv(session_id="abc", port=7700)
    assert "--check-origin=false" in argv


def test_build_ttyd_argv_rejects_empty_session_id():
    from orbit import orchestrator_ttyd as mod
    with pytest.raises(ValueError):
        mod.build_ttyd_argv(session_id="", port=7700)


def test_build_ttyd_argv_rejects_out_of_range_port():
    from orbit import orchestrator_ttyd as mod
    with pytest.raises(ValueError):
        mod.build_ttyd_argv(session_id="abc", port=80)  # privileged
    with pytest.raises(ValueError):
        mod.build_ttyd_argv(session_id="abc", port=70000)  # over 65535


# ── port allocator ────────────────────────────────────────────────


def test_is_port_free_returns_false_for_bound_port():
    """Direct probe of the helper: a socket-held port reads as 'in use'."""
    from orbit import orchestrator_ttyd as mod
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        held_port = s.getsockname()[1]
        assert mod._is_port_free(held_port) is False
    # After release the same port may flicker due to TIME_WAIT on some
    # OSes; we don't assert the post-close state here.


# ── TtydSpawner fake ───────────────────────────────────────────────


class FakeProcessHandle:
    """Minimal stand-in for TtydProcessHandle.

    Records whether ``terminate_and_wait`` was called + supports the
    is_alive() / stderr_tail_text() surface the pool's watchdog uses.
    Setting ``alive=False`` simulates a crashed ttyd so liveness-check
    tests can drive the pool through dead-slot recovery.
    """
    def __init__(self, pid: int, *, alive: bool = True) -> None:
        self.pid = pid
        self.process = None  # signals "fake" to the real handle code
        self.terminated = False
        self.alive = alive
        self.stderr_tail = []

    def is_alive(self) -> bool:
        return self.alive

    def stderr_tail_text(self) -> str:
        return "".join(self.stderr_tail) if self.stderr_tail else ""

    async def terminate_and_wait(self, *, grace_s: float = 0.0) -> None:
        self.terminated = True
        self.alive = False


class FakeTtydSpawner:
    """Records every ``spawn`` argv + which pids were ``kill_pid``'d.

    Maintains an autoincrementing pid so concurrent spawn tests can
    distinguish "first spawn" from "second spawn" cheaply.
    """
    def __init__(self) -> None:
        self.spawn_argvs: list[list[str]] = []
        self.killed_pids: list[int] = []
        self.handles: list[FakeProcessHandle] = []
        self.next_pid = 1000
        # Names returned by `list_orphan_pids` — useful for crash-recovery test.
        self.orphan_pids: list[int] = []

    async def spawn(self, *, argv):
        self.spawn_argvs.append(list(argv))
        handle = FakeProcessHandle(self.next_pid)
        self.next_pid += 1
        self.handles.append(handle)
        return handle

    async def wait_until_listening(
        self, port: int, *, interface: str, base_path: str = "/",
    ) -> bool:
        # Fakes don't bind real sockets, so we short-circuit the
        # readiness probe to True. Tests asserting a failed readiness
        # wait should set ``ready=False`` on the spawner instance.
        return getattr(self, "ready", True)

    async def list_orphan_pids(self, base_path_substring: str):
        # Honour the substring filter so the test asserting "matches our
        # prefix" still works against a non-matching list.
        if base_path_substring == "/api/orchestrator/sessions/":
            return list(self.orphan_pids)
        return []

    async def kill_pid(self, pid: int):
        self.killed_pids.append(pid)


def _make_pool(spawner: FakeTtydSpawner, *, idle_ttl_s: float = 600.0,
               port_range: tuple[int, int] = (7700, 7710)):
    """Build a pool wired to the fake spawner. Default port range is
    intentionally small (11 ports) so exhaustion tests are fast."""
    from orbit import orchestrator_ttyd as mod
    return mod.TtydPool(
        idle_ttl_s=idle_ttl_s,
        port_range=port_range,
        spawner=spawner,
    )


# ── acquire / reuse ────────────────────────────────────────────────


def test_acquire_spawns_once_and_returns_port():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        port = await pool.acquire(session_id="alpha")
        return port

    port = _run(driver())
    assert port in range(7700, 7711)
    assert len(spawner.spawn_argvs) == 1
    # The argv must encode the session_id in --base-path.
    assert any("/sessions/alpha/term" in tok for tok in spawner.spawn_argvs[0])


def test_acquire_reuses_existing_slot_for_same_session_id():
    """Second acquire of an already-warm session_id must NOT respawn."""
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        p1 = await pool.acquire(session_id="alpha")
        p2 = await pool.acquire(session_id="alpha")
        return p1, p2

    p1, p2 = _run(driver())
    assert p1 == p2
    assert len(spawner.spawn_argvs) == 1, "second acquire respawned ttyd"


def test_acquire_concurrent_same_session_does_not_double_spawn():
    """Same defence-in-depth as TmuxPool: two acquires racing the same
    session_id must spawn ttyd exactly once."""
    from orbit import orchestrator_ttyd as mod

    class _Slow(FakeTtydSpawner):
        async def spawn(self, *, argv):
            await asyncio.sleep(0.1)  # hold the spawn so the second caller registers as a waiter
            return await super().spawn(argv=argv)

    spawner = _Slow()
    pool = mod.TtydPool(idle_ttl_s=600.0, port_range=(7700, 7710), spawner=spawner)

    async def driver():
        t1 = asyncio.create_task(pool.acquire(session_id="dup"))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(pool.acquire(session_id="dup"))
        return await asyncio.gather(t1, t2)

    p1, p2 = _run(driver())
    assert p1 == p2
    assert len(spawner.spawn_argvs) == 1, (
        f"expected exactly 1 spawn for racing acquires; got {len(spawner.spawn_argvs)}"
    )


def test_acquire_different_session_ids_use_distinct_ports():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        return await pool.acquire(session_id="a"), await pool.acquire(session_id="b")

    pa, pb = _run(driver())
    assert pa != pb


# ── release / eviction ─────────────────────────────────────────────


def test_release_terminates_slot_and_removes_it():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.acquire(session_id="alpha")
        handle = spawner.handles[0]
        await pool.release(session_id="alpha")
        return handle.terminated

    assert _run(driver()) is True
    # And a subsequent acquire for the same id must spawn fresh.

    spawner2 = FakeTtydSpawner()
    pool2 = _make_pool(spawner2)

    async def driver2():
        await pool2.acquire(session_id="alpha")
        await pool2.release(session_id="alpha")
        await pool2.acquire(session_id="alpha")

    _run(driver2())
    assert len(spawner2.spawn_argvs) == 2


def test_release_unknown_session_is_noop():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.release(session_id="never-existed")

    _run(driver())
    assert spawner.killed_pids == []


def test_evict_idle_kills_stale_slots_only():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner, idle_ttl_s=0.05)

    async def driver():
        await pool.acquire(session_id="old")
        await asyncio.sleep(0.1)  # cross the idle threshold
        await pool.acquire(session_id="fresh")  # touched right before sweep
        await pool.evict_idle()

    _run(driver())
    # "old" must have been terminated; "fresh" must still be live.
    assert spawner.handles[0].terminated is True
    assert spawner.handles[1].terminated is False
    assert pool.is_warm("old") is False
    assert pool.is_warm("fresh") is True


def test_touch_extends_lifetime():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner, idle_ttl_s=0.1)

    async def driver():
        await pool.acquire(session_id="alpha")
        # Touch a few times within the TTL window — slot should survive.
        for _ in range(5):
            await asyncio.sleep(0.03)
            pool.touch("alpha")
        await pool.evict_idle()
        return pool.is_warm("alpha")

    assert _run(driver()) is True
    assert spawner.handles[0].terminated is False


# ── orphan recovery ────────────────────────────────────────────────


def test_recover_orphans_kills_pids_returned_by_pgrep():
    spawner = FakeTtydSpawner()
    spawner.orphan_pids = [9991, 9992, 9993]
    pool = _make_pool(spawner)

    async def driver():
        await pool.recover_orphans()

    _run(driver())
    assert sorted(spawner.killed_pids) == [9991, 9992, 9993]


def test_recover_orphans_empty_is_noop():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.recover_orphans()

    _run(driver())
    assert spawner.killed_pids == []


# ── liveness + watchdog ────────────────────────────────────────────


def test_acquire_respawns_when_existing_slot_ttyd_is_dead():
    """If the previously spawned ttyd has exited (crash, signal, OOM),
    the next acquire MUST detect the dead slot and respawn — otherwise
    the pool hands out a dead port forever. UAT 2026-05-27 root cause
    of the 502 reconnect loop after the theme rollout."""
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        port_a = await pool.acquire(session_id="alpha")
        # Simulate ttyd crash (process exited, slot still in pool).
        spawner.handles[0].alive = False
        spawner.handles[0].stderr_tail = ["lws_socket_bind: source ads 127.0.0.1\n"]
        port_b = await pool.acquire(session_id="alpha")
        return port_a, port_b

    port_a, port_b = _run(driver())
    # Two spawns total — the second one because the first slot was dead.
    assert len(spawner.spawn_argvs) == 2, (
        f"expected respawn after dead slot; got {len(spawner.spawn_argvs)} spawns"
    )
    # Ports may match (port reused) or differ; what matters is the
    # second handle is alive and the pool reports it.
    assert spawner.handles[1].alive is True
    assert pool.is_warm("alpha")


def test_sweep_dead_slots_evicts_dead_keeps_alive():
    """Watchdog sweep removes dead slots from the pool without touching
    live ones. The reaped session_ids are returned for log clarity."""
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.acquire(session_id="alive")
        await pool.acquire(session_id="dead")
        # Kill the second ttyd from underneath the pool.
        spawner.handles[1].alive = False
        evicted = await pool.sweep_dead_slots()
        return evicted

    evicted = _run(driver())
    assert evicted == ["dead"]
    assert pool.is_warm("alive") is True
    assert pool.is_warm("dead") is False


def test_watchdog_loop_runs_periodic_sweeps():
    """start() must launch BOTH the eviction loop and the watchdog —
    a pool with only the evictor wouldn't catch ttyd crashes within
    the idle_ttl_s window."""
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.start()
        evictor = pool._evictor_task
        watchdog = pool._watchdog_task
        await pool.shutdown()
        return evictor, watchdog

    evictor, watchdog = _run(driver())
    assert evictor is not None
    assert watchdog is not None
    assert evictor is not watchdog


# ── readiness ──────────────────────────────────────────────────────


def test_acquire_raises_when_ttyd_fails_to_bind():
    """If ttyd never binds within READINESS_TIMEOUT_S, ``acquire`` must
    raise and reap the spawned subprocess so we don't leak a dead handle
    into the pool."""
    spawner = FakeTtydSpawner()
    spawner.ready = False  # fake says "never bound"
    pool = _make_pool(spawner)

    async def driver():
        with pytest.raises(RuntimeError, match="did not bind"):
            await pool.acquire(session_id="dead")

    _run(driver())
    # The fake subprocess handle must have been terminated.
    assert spawner.handles[0].terminated is True
    # The slot must NOT be in the pool (failed acquire).
    assert pool.is_warm("dead") is False


# ── port exhaustion ────────────────────────────────────────────────


def test_acquire_raises_when_port_range_exhausted():
    """Tiny range (1 port wide) + 2 distinct session_ids = exhaustion."""
    spawner = FakeTtydSpawner()

    from orbit import orchestrator_ttyd as mod
    pool = mod.TtydPool(
        idle_ttl_s=600.0, port_range=(7799, 7799), spawner=spawner,
    )

    async def driver():
        await pool.acquire(session_id="a")
        with pytest.raises(RuntimeError, match="exhausted"):
            await pool.acquire(session_id="b")

    _run(driver())


# ── shutdown ───────────────────────────────────────────────────────


def test_shutdown_terminates_all_slots():
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.acquire(session_id="a")
        await pool.acquire(session_id="b")
        await pool.acquire(session_id="c")
        await pool.shutdown()

    _run(driver())
    assert all(h.terminated for h in spawner.handles)


def test_start_is_idempotent():
    """Double-start must not spawn a second evictor task."""
    spawner = FakeTtydSpawner()
    pool = _make_pool(spawner)

    async def driver():
        await pool.start()
        task1 = pool._evictor_task
        await pool.start()
        task2 = pool._evictor_task
        await pool.shutdown()
        return task1 is task2

    assert _run(driver()) is True
