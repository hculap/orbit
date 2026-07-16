"""Tests for TmuxClaudeRunner — interactive-mode drop-in for ClaudeRunner.

These tests inject fakes for the pool + JSONL tail so the runner is
exercised end-to-end without touching real tmux/claude.

Hypothesis under test (H1): TmuxClaudeRunner must expose the SAME public
interface as ClaudeRunner — orchestrator.py's dispatch site doesn't
introspect classes, it just calls `subscribe`/`start_turn`/`cancel`/
`status_snapshot` and reads `_done` / `_buffered_events`. Any drift
breaks the dispatch.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest


def _run(coro):
    return asyncio.run(coro)


# ── fakes ──────────────────────────────────────────────────────────


class FakePool:
    """In-memory stand-in for TmuxPool with assertable call log."""

    def __init__(self) -> None:
        self.acquired: list[tuple[str, Path]] = []
        # Last set of append paths + add_dirs passed to acquire — verifies
        # the runner forwards them (regression guard for code-review #1).
        self.last_append_paths: list[Path] | None = None
        self.last_add_dirs: list[Path] | None = None
        self.prompts: list[tuple[str, str]] = []
        self.released: list[str] = []

    async def acquire(
        self,
        *,
        session_id: str,
        cwd: Path,
        append_system_prompt_paths: list[Path] | None = None,
        add_dirs: list[Path] | None = None,
        resume: bool = False,
        model: str | None = None,
        env_extra: dict[str, str] | None = None,
    ):
        from orbit import orchestrator_tmux as tmux_mod
        self.acquired.append((session_id, cwd))
        self.last_append_paths = list(append_system_prompt_paths or [])
        self.last_add_dirs = list(add_dirs or [])
        self.last_resume = resume
        self.last_model = model
        self.last_env_extra = env_extra
        return tmux_mod.TmuxSlot(
            session_id=session_id,
            session_name=f"hd-{session_id}",
            cwd=cwd,
        )

    async def pipe_prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))

    async def release(self, session_id: str) -> None:
        self.released.append(session_id)

    def has_warm_slot(self, session_id: str) -> bool:
        # Default: never warm — tests for spawning-on-cold-start subclass
        # this to flip after first acquire.
        return False


def _user_line(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_text_line(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
        },
    }


def _assistant_tool_line(tool_name: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "tu-1",
                "name": tool_name,
                "input": {"path": "/etc/hosts"},
            }],
            "stop_reason": None,
        },
    }


def _tool_result_line() -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu-1",
                "content": [{"type": "text", "text": "127.0.0.1 localhost"}],
                "is_error": False,
                "duration_ms": 12,
            }],
        },
    }


def _stop_hook() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary"}


def _valid_envelope_text() -> str:
    """Sample assistant reply text. The envelope pipeline was removed — Claude
    writes plain markdown now — so this is just an arbitrary text body the
    runner forwards verbatim on an ``assistant_message`` markdown block."""
    return "hello"


# ── tail injection ─────────────────────────────────────────────────


def _install_tail_fake(monkeypatch, lines: list[dict[str, Any]]):
    """Patch the runner's tail to return ``lines`` immediately."""
    from orbit import orchestrator_runner_tmux as mod

    async def fake_tail(path, *, since_byte=0, **kwargs):
        return lines, since_byte + 1

    monkeypatch.setattr(mod, "tail_until_turn_end", fake_tail)


# ── interface compatibility (H1) ──────────────────────────────────


def test_runner_exposes_claude_runner_interface(tmp_path):
    """H1: TmuxClaudeRunner must expose every attribute orchestrator.py
    + the SSE stream handler read off ClaudeRunner. Drift here breaks
    the dispatch + reconnect machinery.

    Instance-level check because most of these are set in __init__.
    """
    from orbit import orchestrator_runner as legacy
    from orbit import orchestrator_runner_tmux as new

    legacy_instance = legacy.ClaudeRunner(session_id="x", has_run_before=False)
    new_instance = new.TmuxClaudeRunner(
        session_id="x", pool=FakePool(), cwd=tmp_path, append_system_prompt_paths=[]
    )

    required = [
        "subscribe",
        "start_turn",
        "cancel",
        "status_snapshot",
        "_done",
        "subscribers",
        "_buffered_events",
        "_seq",
    ]
    for name in required:
        assert hasattr(legacy_instance, name), (
            f"baseline contract slipped — ClaudeRunner missing {name!r}"
        )
        assert hasattr(new_instance, name), (
            f"TmuxClaudeRunner missing {name!r} — H1 falsified"
        )


# ── happy path ─────────────────────────────────────────────────────


def test_start_turn_forwards_append_paths_to_pool(tmp_path, monkeypatch):
    """Code review #1 (PR #39): the runner must forward its `_append_paths`
    + `_agent_skills_dir` through to `pool.acquire`. Without this the slot
    gets spawned with no system prompt stack."""
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    prompt_a = tmp_path / "general.md"
    prompt_b = tmp_path / "identity.md"
    prompt_a.write_text("g")
    prompt_b.write_text("i")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    runner = mod.TmuxClaudeRunner(
        session_id="sess-prompt",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[prompt_a, prompt_b],
        agent_skills_dir=skills_dir,
    )
    _run(runner.start_turn("hi"))
    assert pool.last_append_paths == [prompt_a, prompt_b]
    assert pool.last_add_dirs == [skills_dir]


def test_start_turn_uses_resume_when_jsonl_exists(tmp_path, monkeypatch):
    """UAT 2026-05-15: re-opening a session whose JSONL already exists
    (slot was reaped after restart or cooldown) hung forever with
    "claude REPL never became ready". Root cause: spawn always used
    `--session-id <sid>`, which makes claude exit with "Session ID is
    already in use" when the JSONL file exists.

    Fix: detect existing JSONL via `tail_mod.jsonl_path_for(cwd, sid).is_file()`
    BEFORE pool.acquire and forward `resume=True` so build_spawn_cmd
    emits `--resume <sid>` instead.
    """
    from orbit import orchestrator_runner_tmux as mod
    from orbit import orchestrator_jsonl_tail as tail_mod

    captured: dict = {}

    class _CapturePool(FakePool):
        async def acquire(self, **kwargs):
            captured.update(kwargs)
            return await super().acquire(**kwargs)

    pool = _CapturePool()
    runner = mod.TmuxClaudeRunner(
        session_id="sess-resume",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )
    # Pre-seed an existing JSONL at the slug'd path.
    monkeypatch.setattr(tail_mod, "_CLAUDE_HOME", tmp_path / ".claude")
    jsonl_path = tail_mod.jsonl_path_for(runner._cwd, runner.session_id)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text("{}\n")
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )

    _run(runner.start_turn("hi"))
    assert captured.get("resume") is True, (
        f"expected resume=True due to existing JSONL; got {captured.get('resume')!r}"
    )


def test_start_turn_no_resume_for_fresh_session(tmp_path, monkeypatch):
    """Inverse of the above: no JSONL → no `--resume` (claude would
    fail with 'Session not found')."""
    from orbit import orchestrator_runner_tmux as mod

    captured: dict = {}

    class _CapturePool(FakePool):
        async def acquire(self, **kwargs):
            captured.update(kwargs)
            return await super().acquire(**kwargs)

    pool = _CapturePool()
    runner = mod.TmuxClaudeRunner(
        session_id="sess-fresh",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )

    _run(runner.start_turn("hi"))
    assert captured.get("resume") is False


def test_start_turn_anchors_tail_at_current_jsonl_size(tmp_path, monkeypatch):
    """UAT 2026-05-15 bug: each turn instantiated a fresh TmuxClaudeRunner
    with `_jsonl_offset = 0` so the tail re-read from the start of the
    JSONL and returned the FIRST turn's user+assistant+stop_hook trio —
    UI showed the first response repeated for every subsequent prompt.

    Fix: anchor `_jsonl_offset` at the current file size BEFORE piping the
    prompt. The tail then only consumes lines written AFTER the prompt.

    This test pre-populates the JSONL file with a fake prior turn so we
    can assert the runner skipped past it.
    """
    from orbit import orchestrator_runner_tmux as mod
    from orbit import orchestrator_jsonl_tail as tail_mod

    pool = FakePool()
    # Pre-create the JSONL at the path the runner will compute.
    runner = mod.TmuxClaudeRunner(
        session_id="sess-anchor",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )
    monkeypatch.setattr(tail_mod, "_CLAUDE_HOME", tmp_path / ".claude")
    jsonl_path = tail_mod.jsonl_path_for(runner._cwd, runner.session_id)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    prior_text = json.dumps(_user_line("ancient prompt")) + "\n"
    prior_text += json.dumps(_assistant_text_line("ancient response")) + "\n"
    prior_text += json.dumps(_stop_hook()) + "\n"
    jsonl_path.write_text(prior_text)
    prior_size = jsonl_path.stat().st_size
    assert prior_size > 0

    # Stub the tail to return whatever its caller passed via since_byte so
    # we can introspect the anchor.
    captured: dict[str, int] = {}

    async def fake_tail(path, *, since_byte=0, **kwargs):
        captured["since_byte"] = since_byte
        return [_user_line("fresh"), _assistant_text_line(_valid_envelope_text()), _stop_hook()], since_byte + 1

    monkeypatch.setattr(mod, "tail_until_turn_end", fake_tail)
    _run(runner.start_turn("fresh prompt"))

    assert captured["since_byte"] == prior_size, (
        f"tail must skip past prior turn ({prior_size} bytes); "
        f"got since_byte={captured.get('since_byte')}"
    )


def test_start_turn_acquires_pool_and_pipes_prompt(tmp_path, monkeypatch):
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-1",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )
    _run(runner.start_turn("hi from user"))
    assert pool.acquired == [("sess-1", tmp_path)]
    assert pool.prompts == [("sess-1", "hi from user")]
    assert runner._done.is_set()


def test_start_turn_broadcasts_assistant_markdown(tmp_path, monkeypatch):
    """Envelope removed: assistant text is forwarded verbatim as a plain
    markdown block on ``assistant_message`` (no JSON parsing, no
    ``structured_blocks``)."""
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line("hello"), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-2", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _run(runner.start_turn("hi"))
    events = [_parse_event(e) for e in runner._buffered_events]
    kinds = [e[0] for e in events]
    assert "structured_blocks" not in kinds
    assert "assistant_message" in kinds
    msgs = [e[1] for e in events if e[0] == "assistant_message"]
    text_block = next(
        b for m in msgs for b in m.get("blocks", [])
        if b.get("kind") == "markdown"
    )
    assert text_block["text"] == "hello"


def test_start_turn_broadcasts_tool_use_and_tool_result(tmp_path, monkeypatch):
    """Multi-step turn: tool_use should fire `assistant_message`, the matching
    tool_result wrapped in a user line should fire `tool_result`."""
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [
            _user_line("read /etc/hosts"),
            _assistant_tool_line("Read"),
            _tool_result_line(),
            _assistant_text_line(_valid_envelope_text()),
            _stop_hook(),
        ],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-3", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _run(runner.start_turn("read hosts"))
    events = [_parse_event(e) for e in runner._buffered_events]
    kinds = [e[0] for e in events]
    assert "assistant_message" in kinds  # tool_use block surfaces here
    assert "tool_result" in kinds


def test_start_turn_emits_done_event(tmp_path, monkeypatch):
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-4", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _run(runner.start_turn("hi"))
    events = [_parse_event(e) for e in runner._buffered_events]
    kinds = [e[0] for e in events]
    assert kinds[-1] == "done"


# ── subscribe / replay ─────────────────────────────────────────────


def test_subscribe_after_completion_gets_buffered_events(tmp_path, monkeypatch):
    """A client reconnecting after `_done` is set still gets the buffered tail
    + a `None` sentinel — required for the SSE reconnect contract."""
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-5", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _run(runner.start_turn("hi"))

    queue = runner.subscribe()
    received: list[bytes | None] = []
    while True:
        try:
            received.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    assert received[-1] is None
    payload = b"".join(b for b in received if isinstance(b, bytes))
    assert b"assistant_message" in payload


def test_prose_reply_emits_assistant_markdown(tmp_path, monkeypatch):
    """Any assistant text — even something that looks like JSON — is forwarded
    as a plain markdown block (the envelope parser + layer-3 repair are gone)."""
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [
            _user_line("hi"),
            _assistant_text_line("this is just prose, not an envelope"),
            _stop_hook(),
        ],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-6", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _run(runner.start_turn("hi"))

    events = [_parse_event(e) for e in runner._buffered_events]
    kinds = [e[0] for e in events]
    assert "structured_blocks" not in kinds
    assert "assistant_message" in kinds
    text_block = next(
        b for e in events if e[0] == "assistant_message"
        for b in e[1].get("blocks", []) if b.get("kind") == "markdown"
    )
    assert text_block["text"] == "this is just prose, not an envelope"


# ── cancel mid-acquire (Phase 2A.1) ────────────────────────────────


def test_cancel_during_acquire_returns_fast_with_cancelled_event(tmp_path, monkeypatch):
    """Phase 2A.1: when `cancel()` fires during the pool's cold-start
    `wait_until_ready` poll, the runner must wake up immediately (not after
    the 60 s readiness timeout). Empirically observed in Phase 1 smoke:
    cancel at t=15s, then in_flight stays True for ~45s more before
    timing out with the misleading "failed to acquire" error.

    Strategy: wrap `pool.acquire` in a cancellable task on the runner so
    `cancel()` can break it; pool's wait_until_ready then raises
    CancelledError which start_turn handles as a clean cancel.
    """
    from orbit import orchestrator_runner_tmux as mod

    class _SlowAcquirePool(FakePool):
        async def acquire(self, **kwargs):
            # Block for a long time to simulate wait_until_ready spinning.
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Real pool fires teardown as a fire-and-forget task on cancel
                # (a synchronous `await teardown(...)` would itself be re-
                # cancelled at its first internal yield). We record the
                # release here to confirm the cancellation path was taken.
                self.released.append(kwargs.get("session_id", ""))
                raise
            from orbit import orchestrator_tmux as tmux_mod
            return tmux_mod.TmuxSlot(
                session_id=kwargs["session_id"],
                session_name=f"hd-{kwargs['session_id']}",
                cwd=kwargs["cwd"],
            )

    pool = _SlowAcquirePool()
    runner = mod.TmuxClaudeRunner(
        session_id="sess-cancel-acq",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )

    async def driver():
        task = asyncio.create_task(runner.start_turn("hi"))
        await asyncio.sleep(0.05)
        t0 = asyncio.get_running_loop().time()
        await runner.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("start_turn did not exit within 2s of cancel")
        elapsed = asyncio.get_running_loop().time() - t0
        return elapsed

    elapsed = _run(driver())
    assert elapsed < 1.0, f"cancel propagation took {elapsed:.2f}s, expected <1.0s"
    assert runner._done.is_set()
    # The pool's acquire saw the cancellation and recorded a "release"
    # (mimicking a teardown of the half-spawned slot in our fake).
    assert "sess-cancel-acq" in pool.released
    # The broadcast events should reflect cancellation, not "failed to acquire".
    events = [_parse_event(e) for e in runner._buffered_events]
    error_events = [e for e in events if e[0] == "error"]
    assert error_events, "expected at least one error event"
    messages = " ".join(e[1].get("message", "") for e in error_events)
    assert "cancelled" in messages.lower()
    assert "failed to acquire" not in messages.lower()


def test_cancel_before_acquire_task_created_aborts_turn(tmp_path, monkeypatch):
    """Phase 2A code review (R3): there's a narrow window between
    `start_turn` entry and the line `self._acquire_task = create_task(...)`
    where `cancel()` can run. In that window, `_acquire_task is None` so
    cancel()'s `_acquire_task.cancel()` skips, only `_finalize` runs (sets
    `_done`).

    Without a guard, start_turn then PROCEEDS to:
      - create _acquire_task fresh
      - await it (NOT cancelled, runs to completion)
      - broadcast init/thinking
      - pipe_prompt (sends user's text to claude even though they cancelled!)

    Reproduce: patch `has_warm_slot` to invoke `cancel()` synchronously,
    so the race is deterministic. Assert pipe_prompt is NOT called and
    no init/thinking events are emitted.
    """
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-cancel-race",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )

    async def driver():
        # Reproduce the exact production timing:
        # 1) dispatcher creates runner + schedules start_turn task
        # 2) user POSTs cancel BEFORE the scheduled task wakes up
        # 3) cancel() runs — _acquire_task is still None, so the
        #    `_acquire_task.cancel()` branch is skipped; only _done is set
        # 4) start_turn task wakes, creates its own _acquire_task, awaits
        #    successfully — without a guard, it proceeds to pipe_prompt
        turn_task = asyncio.create_task(runner.start_turn("user prompt"))
        # cancel runs immediately, before turn_task gets a chance to execute.
        await runner.cancel()
        try:
            await asyncio.wait_for(turn_task, timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("start_turn never returned after early cancel")

    _run(driver())

    # Pool's pipe_prompt MUST NOT have been called — the user cancelled
    # BEFORE start_turn even ran.
    assert pool.prompts == [], (
        f"pipe_prompt fired after cancel-before-acquire-task race; "
        f"got {pool.prompts!r}"
    )


def test_cancel_during_acquire_does_not_block_other_sessions(tmp_path, monkeypatch):
    """Side effect of the cancel-mid-acquire refactor (R1's lock-scope fix):
    after we drop the lock around `wait_until_ready`, a slow cold start
    of session A no longer blocks session B's acquire. Test with the real
    `TmuxPool` against an inline minimal TmuxRunner that delays
    `wait_until_ready` per session name."""
    from orbit import orchestrator_tmux as mod

    class _DelayingRunner:
        def __init__(self):
            self.live: set[str] = set()
            self.ready_delays: dict[str, float] = {}

        async def spawn(self, name: str, cmd: list[str]) -> None:
            self.live.add(name)

        async def kill_session(self, name: str) -> None:
            self.live.discard(name)

        async def has_session(self, name: str) -> bool:
            return name in self.live

        async def list_sessions(self) -> list[str]:
            return list(self.live)

        async def send_prompt(self, name: str, text: str) -> None:
            pass

        async def send_exit(self, name: str) -> None:
            self.live.discard(name)

        async def capture_pane(self, name: str) -> str:
            return ""

        async def send_enter(self, name: str) -> None:
            pass

        async def wait_until_ready(self, name: str) -> bool:
            await asyncio.sleep(self.ready_delays.get(name, 0))
            return True

    runner = _DelayingRunner()
    pool = mod.TmuxPool(pool_size=4, idle_ttl_s=600.0, runner=runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    runner.ready_delays["hd-slow"] = 0.5  # half-second cold start
    runner.ready_delays["hd-fast"] = 0.0

    async def driver():
        slow_task = asyncio.create_task(
            pool.acquire(session_id="slow", cwd=tmp_path)
        )
        # Give slow a head-start so it's mid-wait_until_ready when fast arrives.
        await asyncio.sleep(0.05)
        t0 = asyncio.get_running_loop().time()
        await pool.acquire(session_id="fast", cwd=tmp_path)
        fast_elapsed = asyncio.get_running_loop().time() - t0
        await slow_task
        return fast_elapsed

    fast_elapsed = _run(driver())
    # Pre-fix: fast would wait behind slow's 0.5s wait_until_ready (under the
    # global lock). Post-fix: fast returns in well under 200ms.
    assert fast_elapsed < 0.3, (
        f"fast acquire blocked behind slow's wait_until_ready ({fast_elapsed:.3f}s); "
        f"pool lock is held across I/O"
    )


# ── cancel (tail phase, regression guard from Phase 1) ─────────────


def test_cancel_sets_done_and_releases_slot(tmp_path, monkeypatch):
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    # Tail that never returns until cancelled.

    async def never_ending_tail(path, *, since_byte=0, **kwargs):
        await asyncio.sleep(60)  # would hang in tests if not cancelled
        return [], since_byte

    monkeypatch.setattr(mod, "tail_until_turn_end", never_ending_tail)

    runner = mod.TmuxClaudeRunner(
        session_id="sess-8", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )

    async def driver():
        task = asyncio.create_task(runner.start_turn("hi"))
        await asyncio.sleep(0.05)
        await runner.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass

    _run(driver())
    assert runner._done.is_set()


# ── status_snapshot ────────────────────────────────────────────────


def test_status_snapshot_shape(tmp_path):
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    runner = mod.TmuxClaudeRunner(
        session_id="sess-9", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    snap = runner.status_snapshot()
    assert set(snap.keys()) >= {"in_flight", "started_at_ms", "last_seq"}
    assert snap["in_flight"] is True  # not yet done


# ── spawning event ─────────────────────────────────────────────────


def test_spawning_event_emitted_before_init_on_cold_start(tmp_path, monkeypatch):
    """Phase 2A.2: when the pool has no slot for this session yet, the cold
    start can take 10-20 s. Frontend needs an explicit `spawning` event
    *before* `init` so it can show a "Spawning interactive session..."
    placeholder with a timer instead of a silent stall.
    """
    from orbit import orchestrator_runner_tmux as mod
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-spawn",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )
    _run(runner.start_turn("hi"))
    events = [_parse_event(e) for e in runner._buffered_events]
    kinds = [e[0] for e in events]
    assert "spawning" in kinds, f"expected spawning event before init; got {kinds!r}"
    assert kinds.index("spawning") < kinds.index("init")


def test_spawning_event_skipped_on_warm_slot_reuse(tmp_path, monkeypatch):
    """Subsequent turns on an already-warm slot must NOT emit `spawning` —
    there's no cold start to indicate. The runner peeks at the pool via
    `has_warm_slot(session_id)` before deciding to emit, so the answer is
    available BEFORE the (potentially long) acquire blocks.
    """
    from orbit import orchestrator_runner_tmux as mod

    class _WarmAfterFirst(FakePool):
        def has_warm_slot(self, session_id: str) -> bool:
            return any(sid == session_id for sid, _ in self.acquired)

    pool = _WarmAfterFirst()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner1 = mod.TmuxClaudeRunner(
        session_id="sess-warm", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _run(runner1.start_turn("first"))
    events1 = [_parse_event(e)[0] for e in runner1._buffered_events]
    assert "spawning" in events1

    runner2 = mod.TmuxClaudeRunner(
        session_id="sess-warm", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    _install_tail_fake(
        monkeypatch,
        [_user_line("second"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    _run(runner2.start_turn("second"))
    events2 = [_parse_event(e)[0] for e in runner2._buffered_events]
    assert "spawning" not in events2, f"warm reuse must skip spawning; got {events2!r}"


# ── reap / _active_runs cleanup ────────────────────────────────────


def test_finalize_schedules_reap_and_clears_buffer(tmp_path, monkeypatch):
    """Code review #2 (PR #39): the legacy ClaudeRunner defers `_reap` via
    `call_later(REAP_GRACE_S, ...)` to pop itself from `_active_runs` AND
    clear `_buffered_events` (~1 MB cap). TmuxClaudeRunner must mirror this
    or every interactive turn leaks indefinitely.

    Patch REAP_GRACE_S to 0 so the call_later fires on the very next loop
    tick — we sleep briefly to let it run.
    """
    from orbit import orchestrator_runner as legacy
    from orbit import orchestrator_runner_tmux as mod

    monkeypatch.setattr(mod, "REAP_GRACE_S", 0.0)

    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    runner = mod.TmuxClaudeRunner(
        session_id="sess-reap",
        pool=pool,
        cwd=tmp_path,
        append_system_prompt_paths=[],
    )

    async def driver():
        legacy._active_runs["sess-reap"] = runner  # type: ignore[assignment]
        await runner.start_turn("hi")
        # Buffered events are filled, runner still in registry — same as
        # immediately after `_finalize` on the legacy runner.
        assert len(runner._buffered_events) > 0
        assert legacy._active_runs.get("sess-reap") is runner
        # Yield long enough for the call_later(0, _reap) to fire.
        await asyncio.sleep(0.05)
        return runner

    _run(driver())
    assert "sess-reap" not in legacy._active_runs
    assert len(runner._buffered_events) == 0


def test_reap_only_pops_matching_runner(tmp_path, monkeypatch):
    """If another runner has REPLACED this one in `_active_runs` (e.g. a new
    turn started after the old reap was scheduled), reap must NOT pop the
    replacement. Mirror of legacy `_reap`'s `is self` guard."""
    from orbit import orchestrator_runner as legacy
    from orbit import orchestrator_runner_tmux as mod

    monkeypatch.setattr(mod, "REAP_GRACE_S", 0.0)
    pool = FakePool()
    _install_tail_fake(
        monkeypatch,
        [_user_line("hi"), _assistant_text_line(_valid_envelope_text()), _stop_hook()],
    )
    old_runner = mod.TmuxClaudeRunner(
        session_id="sess-replace", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )
    new_runner = mod.TmuxClaudeRunner(
        session_id="sess-replace", pool=pool, cwd=tmp_path, append_system_prompt_paths=[]
    )

    async def driver():
        legacy._active_runs["sess-replace"] = old_runner  # type: ignore[assignment]
        await old_runner.start_turn("hi")
        # Replace before the deferred _reap runs.
        legacy._active_runs["sess-replace"] = new_runner  # type: ignore[assignment]
        await asyncio.sleep(0.05)

    _run(driver())
    assert legacy._active_runs.get("sess-replace") is new_runner
    # cleanup so other tests don't see a stale entry
    legacy._active_runs.pop("sess-replace", None)


# ── helpers ────────────────────────────────────────────────────────


def _parse_event(raw: bytes) -> tuple[str, dict[str, Any]]:
    """Parse an SSE event blob into (event_name, data_dict).

    Reverse of `_format_sse`. Useful for buffered-event introspection
    without coupling to the wire format.
    """
    lines = raw.split(b"\n")
    name = ""
    data = ""
    for line in lines:
        if line.startswith(b"event: "):
            name = line[len(b"event: "):].decode("utf-8")
        elif line.startswith(b"data: "):
            data = line[len(b"data: "):].decode("utf-8")
    return name, (json.loads(data) if data else {})
