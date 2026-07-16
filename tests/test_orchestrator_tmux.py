"""Tests for orchestrator_tmux — pool manager + tmux primitives.

The pool itself is mock-friendly: every shell invocation flows through
``TmuxRunner`` which is overridable, so we exercise lifecycle logic
(acquire/release/evict/recover) without spawning real tmux processes.

Trust pre-seed and command-construction helpers are tested directly
against the disk / argv builder.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest


def _run(coro):
    return asyncio.run(coro)


# ── build_spawn_cmd ────────────────────────────────────────────────


def test_build_spawn_cmd_contains_dedicated_socket():
    """H13: pool MUST use `-L hd-orch` to never collide with the user's
    long-running `orchestrator` tmux server. Assertion is in the builder."""
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc",
        append_system_prompt_paths=[],
    )
    assert "-L" in cmd
    socket_idx = cmd.index("-L")
    assert cmd[socket_idx + 1] == mod.TMUX_SOCKET


def test_build_spawn_cmd_pins_tmpdir_off_slash_tmp():
    """Regression: a detached (cgroup-escaped) tmux server freezes its birth
    mount namespace; PrivateTmp=true makes that namespace's /tmp go stale
    (`//deleted`) after a restart, so claude's startup `mkdir /tmp/claude-<uid>`
    fails ENOENT and every NEW session dies. The inner spawn env MUST pin
    TMPDIR onto the host fs (~/.orchestrator) so the mkdir survives namespace
    churn — same reasoning that moved the tmux socket off /tmp (#119)."""
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc",
        append_system_prompt_paths=[],
    )
    tmpdir_tokens = [t for t in cmd if t.startswith("TMPDIR=")]
    assert len(tmpdir_tokens) == 1, f"expected exactly one TMPDIR= token, got {tmpdir_tokens!r}"
    val = tmpdir_tokens[0].split("=", 1)[1]
    assert val.endswith("/.orchestrator/tmp"), f"TMPDIR must be off /tmp on host fs, got {val!r}"
    assert val != "/tmp" and not val.startswith("/tmp/")
    # ordering: the TMPDIR pin must precede the claude binary in the `env` block
    assert cmd.index(tmpdir_tokens[0]) < cmd.index(mod._claude_bin())


def test_build_spawn_cmd_scrubs_anthropic_api_key():
    """H11 (CRITICAL): spawn env MUST include `-e ANTHROPIC_API_KEY=` (empty
    value) so the child claude routes under the user's interactive
    subscription, NOT the post-2026-06-15 programmatic credit pool / API
    billing. Without this, the whole migration is moot."""
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc",
        append_system_prompt_paths=[],
    )
    # Walk every `-e ENV=VALUE` pair and confirm exactly one matches.
    env_pairs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-e"]
    api_key_pairs = [p for p in env_pairs if p.startswith("ANTHROPIC_API_KEY=")]
    assert api_key_pairs == ["ANTHROPIC_API_KEY="], (
        f"expected exactly one scrubbed ANTHROPIC_API_KEY entry, got {api_key_pairs!r}"
    )


def test_build_spawn_cmd_sets_term():
    """H10 (PoC bonus): TERM=xterm-256color is REQUIRED. Without it claude
    renders into a void PTY and the pane stays blank even though the
    process is alive."""
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc",
        append_system_prompt_paths=[],
    )
    env_pairs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-e"]
    assert any(p.startswith("TERM=xterm") for p in env_pairs)


def test_build_spawn_cmd_prepends_env_term_to_inner(tmp_path):
    """UAT bug (Hetzner): tmux silently overrides our outer ``-e TERM=...``
    with the server-wide ``default-terminal`` from the user's ~/.tmux.conf
    (commonly ``screen-256color``). claude then sees screen-256color, treats
    the terminal as fresh, and pops the theme picker — hanging the first
    spawn forever. Prepending ``env TERM=xterm-256color`` to the inner cmd
    forces the variable into claude's exec env after tmux is done with it.
    """
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=tmp_path,
        session_id="abc",
        append_system_prompt_paths=[],
    )
    # Find the boundary between outer and inner (last `-e ENV=` pair).
    boundary = 0
    for i in range(len(cmd) - 1, -1, -1):
        if cmd[i] == "-e":
            boundary = i + 2
            break
    inner = cmd[boundary:]
    # Inner must start with `env TERM=xterm-256color <claude_bin>`.
    assert inner[0] == "env", f"inner cmd must start with `env`; got {inner[:3]!r}"
    assert inner[1].startswith("TERM=xterm"), (
        f"inner env must set TERM=xterm-...; got {inner[1]!r}"
    )


def test_build_spawn_cmd_injects_user_local_bin_path(tmp_path):
    """UAT 2026-05-29 (Hetzner): the dashboard runs under systemd with a
    minimal PATH that omits ``~/.local/bin``. claude execs fine (resolved by
    absolute path) but warns "Native installation exists but ~/.local/bin is
    not in your PATH" into every orchestrator terminal.

    An outer ``-e PATH=`` is wiped by Debian's /etc/zsh/zshenv (tmux runs the
    pane via zsh), so the fix applies PATH in the inner ``env`` prefix
    (``env TERM=… PATH=… claude``) where it wins after zsh's reset. This test
    asserts the inner env carries ``PATH=…`` containing ~/.local/bin, right
    after TERM and before the claude binary."""
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=tmp_path,
        session_id="abc",
        append_system_prompt_paths=[],
    )
    # Locate the inner cmd: everything after the last outer `-e ENV=` pair.
    boundary = next(i + 2 for i in range(len(cmd) - 1, -1, -1) if cmd[i] == "-e")
    inner = cmd[boundary:]
    assert inner[0] == "env", f"inner cmd must start with `env`; got {inner[:3]!r}"
    path_tokens = [t for t in inner if t.startswith("PATH=")]
    assert len(path_tokens) == 1, f"expected one inner PATH= token, got {path_tokens!r}"
    local_bin = str(Path.home() / ".local" / "bin")
    assert local_bin in path_tokens[0].split("=", 1)[1].split(os.pathsep), (
        f"~/.local/bin must be on the spawned PATH; got {path_tokens[0]!r}"
    )
    # PATH token must precede the claude binary so `env` applies it to claude.
    assert inner.index(path_tokens[0]) < inner.index(mod._claude_bin()), (
        "inner PATH= must come before the claude binary"
    )


def test_build_spawn_cmd_includes_model_flag_when_set(tmp_path):
    """UAT 2026-05-16: cron jobs with action.model=sonnet|haiku ran on the
    user's default model (opus) because TmuxClaudeRunner stored `self.model`
    but build_spawn_cmd never received it. Verified empirically — sonnet job
    self-identified as ``claude-opus-4-7[1m]`` in the response.

    Fix: build_spawn_cmd accepts optional `model` and emits
    ``--model <alias>`` so claude-cli routes to the requested model.
    """
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=tmp_path,
        session_id="abc",
        append_system_prompt_paths=[],
        model="haiku",
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_build_spawn_cmd_omits_model_flag_when_none(tmp_path):
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=tmp_path,
        session_id="abc",
        append_system_prompt_paths=[],
    )
    assert "--model" not in cmd


def test_build_spawn_cmd_never_includes_bare_flag():
    """H_NEW: `claude --bare` forces API billing — defensive assertion."""
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc",
        append_system_prompt_paths=[],
    )
    assert "--bare" not in cmd
    # Billing guard: the interactive (subscription) path must never carry a
    # programmatic-billing flag either.
    assert "-p" not in cmd and "--print" not in cmd


def test_build_spawn_cmd_includes_session_id_flag_on_first_spawn():
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc-123",
        append_system_prompt_paths=[],
        resume=False,
    )
    assert "--session-id" in cmd
    assert "--resume" not in cmd
    assert cmd[cmd.index("--session-id") + 1] == "abc-123"


def test_build_spawn_cmd_uses_resume_flag_when_requested():
    from orbit import orchestrator_tmux as mod
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=Path("/tmp"),
        session_id="abc-123",
        append_system_prompt_paths=[],
        resume=True,
    )
    assert "--resume" in cmd
    assert "--session-id" not in cmd


def test_build_spawn_cmd_appends_system_prompts(tmp_path):
    from orbit import orchestrator_tmux as mod
    prompt_a = tmp_path / "a.md"
    prompt_b = tmp_path / "b.md"
    prompt_a.write_text("A")
    prompt_b.write_text("B")
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=tmp_path,
        session_id="abc",
        append_system_prompt_paths=[prompt_a, prompt_b],
    )
    # Each prompt path is preceded by --append-system-prompt-file.
    indices = [i for i, tok in enumerate(cmd) if tok == "--append-system-prompt-file"]
    assert len(indices) == 2
    assert cmd[indices[0] + 1] == str(prompt_a)
    assert cmd[indices[1] + 1] == str(prompt_b)


def test_build_spawn_cmd_skips_missing_prompts(tmp_path):
    from orbit import orchestrator_tmux as mod
    real = tmp_path / "real.md"
    real.write_text("hi")
    missing = tmp_path / "ghost.md"
    cmd = mod.build_spawn_cmd(
        session_name="hd-abc",
        cwd=tmp_path,
        session_id="abc",
        append_system_prompt_paths=[real, missing],
    )
    indices = [i for i, tok in enumerate(cmd) if tok == "--append-system-prompt-file"]
    assert len(indices) == 1
    assert cmd[indices[0] + 1] == str(real)


# ── trust pre-seed ─────────────────────────────────────────────────


def test_trust_cwd_seeds_fresh_config(tmp_path, monkeypatch):
    """H3: writing ~/.claude.json projects[<cwd>].hasTrustDialogAccepted=true
    must succeed even when the config doesn't exist yet."""
    from orbit import orchestrator_tmux as mod
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", fake_home / ".claude.json")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    mod.ensure_cwd_trusted(workdir)
    data = json.loads((fake_home / ".claude.json").read_text())
    key = os.path.realpath(str(workdir))
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


def test_trust_cwd_preserves_existing_entries(tmp_path, monkeypatch):
    from orbit import orchestrator_tmux as mod
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cfg = fake_home / ".claude.json"
    cfg.write_text(json.dumps({
        "projects": {"/somewhere": {"history": ["x"]}},
        "other_key": 42,
    }))
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", cfg)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    mod.ensure_cwd_trusted(workdir)
    data = json.loads(cfg.read_text())
    assert data["other_key"] == 42
    assert data["projects"]["/somewhere"]["history"] == ["x"]
    key = os.path.realpath(str(workdir))
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


def test_trust_cwd_idempotent(tmp_path, monkeypatch):
    from orbit import orchestrator_tmux as mod
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cfg = fake_home / ".claude.json"
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", cfg)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    mod.ensure_cwd_trusted(workdir)
    mtime1 = cfg.stat().st_mtime_ns
    mod.ensure_cwd_trusted(workdir)
    data = json.loads(cfg.read_text())
    key = os.path.realpath(str(workdir))
    assert data["projects"][key]["hasTrustDialogAccepted"] is True
    # Even if written twice, the trust bit remains true (idempotent).
    assert isinstance(mtime1, int)  # sanity


# ── TmuxRunner fake ────────────────────────────────────────────────


class FakeTmuxRunner:
    """Records every tmux/load-buffer call instead of executing it.

    Maintains an in-memory model of which `hd-*` sessions are "live" so
    `has_session` answers truthfully across spawn/kill cycles.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.live: set[str] = set()
        # Names returned by `list_sessions` — useful for crash-recovery test.
        self.preexisting: list[str] = []

    async def spawn(self, name: str, cmd: list[str]) -> None:
        self.calls.append(("spawn", (name, *cmd)))
        self.live.add(name)

    async def kill_session(self, name: str) -> None:
        self.calls.append(("kill_session", (name,)))
        self.live.discard(name)

    async def has_session(self, name: str) -> bool:
        return name in self.live

    async def list_sessions(self) -> list[str]:
        return list(self.live | set(self.preexisting))

    async def send_prompt(self, name: str, text: str) -> None:
        self.calls.append(("send_prompt", (name, text)))

    async def paste_text(self, name: str, text: str) -> None:
        self.calls.append(("paste_text", (name, text)))

    async def send_exit(self, name: str) -> None:
        """Simulates `/exit` slash — claude tears itself down, then the
        tmux session dies."""
        self.calls.append(("send_exit", (name,)))
        self.live.discard(name)

    async def capture_pane(self, name: str) -> str:
        return ""  # not used in tests

    async def send_enter(self, name: str) -> None:
        self.calls.append(("send_enter", (name,)))

    async def wait_until_ready(self, name: str) -> bool:
        """Always returns True so tests don't have to simulate readiness polling."""
        return True


# ── TmuxPool: acquire / release / LRU ──────────────────────────────


def _make_pool(runner: FakeTmuxRunner, *, pool_size: int = 4, idle_ttl_s: float = 600):
    from orbit import orchestrator_tmux as mod
    return mod.TmuxPool(
        pool_size=pool_size,
        idle_ttl_s=idle_ttl_s,
        runner=runner,
    )


def _force_detached(monkeypatch, value: bool) -> None:
    """Pin the ``tmux_detached_sessions`` flag for a test, independent of any
    on-box settings.json. Other flags fall through to their real value."""
    from orbit import orchestrator_settings as settings_mod
    real = settings_mod.get_flag
    monkeypatch.setattr(
        settings_mod,
        "get_flag",
        lambda name: value if name == "tmux_detached_sessions" else real(name),
    )


def test_acquire_concurrent_same_session_does_not_double_spawn(tmp_path, monkeypatch):
    """Follow-up from PR #40 review (R2+R4 concurrency audit): with the
    Phase 2A lock-scope refactor, two concurrent `acquire(session_id=X)`
    calls could both spawn `hd-X` because A releases the lock during
    `wait_until_ready` and B then sees no slot for X in `_slots` (A hasn't
    committed yet).

    Mitigated in production by the dispatcher's in-flight check, but a
    future internal caller (preflight warmup, /restart endpoint) could
    bypass that gate. An `_acquiring: set[str]` reservation guard makes
    the pool itself safe regardless of caller discipline.
    """
    from orbit import orchestrator_tmux as mod

    class _SlowReady(FakeTmuxRunner):
        def __init__(self):
            super().__init__()
            self.spawn_count = 0

        async def spawn(self, name, cmd):
            self.spawn_count += 1
            await super().spawn(name, cmd)

        async def wait_until_ready(self, name):
            await asyncio.sleep(0.3)
            return True

    runner = _SlowReady()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    async def driver():
        t1 = asyncio.create_task(pool.acquire(session_id="dup", cwd=tmp_path))
        await asyncio.sleep(0.05)  # let t1 enter wait_until_ready
        t2 = asyncio.create_task(pool.acquire(session_id="dup", cwd=tmp_path))
        slot1 = await t1
        slot2 = await t2
        return slot1, slot2

    slot1, slot2 = _run(driver())
    assert runner.spawn_count == 1, (
        f"double-spawn for same session_id; got {runner.spawn_count} spawns"
    )
    assert slot1 is slot2, "second acquire must return the same slot, not a new one"


def test_acquire_race_lost_waiter_retry_forwards_resume_and_model(tmp_path, monkeypatch):
    """Code-review regression (2026-05-18): when the first spawner for a
    session_id fails before committing the slot, the `_acquiring` event
    fires and any waiter falls through to `return await self.acquire(...)`.
    That recursive call must forward every spawn-controlling kwarg —
    `resume` (added 6389a58) and `model` (added 9683643). The original
    fallback in 069b83f predated both and dropped them silently, so a
    waiter retrying a `resume=True` session would re-spawn with
    `--session-id` (claude aborts: "Session ID already in use") and a
    waiter expecting `--model haiku` would get no model flag at all.
    """
    from orbit import orchestrator_tmux as mod

    class _FailFirstThenSucceed(FakeTmuxRunner):
        """First spawner sleeps long enough for the waiter to register,
        then raises so the slot is never committed; the waiter wakes,
        sees no slot, and retries via the recursive `acquire()` fallback.
        We capture the retry's spawn argv to assert kwargs are forwarded.
        """
        def __init__(self):
            super().__init__()
            self.spawn_calls = 0

        async def spawn(self, name, cmd):
            self.spawn_calls += 1
            if self.spawn_calls == 1:
                # Hold the spawn open so t2 has time to register as a
                # waiter, THEN fail without committing the slot.
                await asyncio.sleep(0.1)
                raise RuntimeError("simulated cold-start failure on first spawner")
            await super().spawn(name, cmd)

        async def wait_until_ready(self, name):
            await asyncio.sleep(0.02)
            return True

    runner = _FailFirstThenSucceed()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    async def driver():
        # Owner kicks off first; will sleep 100ms in spawn before raising.
        t1 = asyncio.create_task(pool.acquire(
            session_id="raced", cwd=tmp_path, resume=True, model="haiku",
        ))
        # Wait long enough for t1 to enter spawn (and hold the
        # `_acquiring` reservation) but not long enough for it to fail.
        await asyncio.sleep(0.03)
        # t2 now sees `_acquiring['raced']` already populated and goes
        # the WAITER path → `await in_flight.wait()` → recursive retry.
        t2 = asyncio.create_task(pool.acquire(
            session_id="raced", cwd=tmp_path, resume=True, model="haiku",
        ))
        results = await asyncio.gather(t1, t2, return_exceptions=True)
        return results

    results = _run(driver())
    # One of the two failed (the original spawner); the other succeeded as
    # a fresh owner via the retry path.
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1, f"expected one slot to come back, got {results}"
    assert len(failures) == 1, f"expected one spawn failure, got {results}"

    # The successful spawn's argv must contain BOTH --resume <id> and
    # --model haiku — dropped kwargs would manifest as `--session-id raced`
    # and no `--model` flag.
    spawn_calls = [c for c in runner.calls if c[0] == "spawn"]
    assert len(spawn_calls) == 1, f"expected exactly 1 successful spawn, got {len(spawn_calls)}"
    argv = spawn_calls[0][1]  # (session_name, *cmd)
    argv_str = " ".join(argv)
    assert "--resume raced" in argv_str, (
        f"retry dropped resume= ; expected --resume in argv, got: {argv_str}"
    )
    assert "--session-id raced" not in argv_str, (
        f"retry must NOT use --session-id when resume=True; got: {argv_str}"
    )
    assert "--model haiku" in argv_str, (
        f"retry dropped model= ; expected --model haiku in argv, got: {argv_str}"
    )


def test_subprocess_wait_until_ready_dismisses_theme_picker(monkeypatch):
    """UAT bug on Hetzner: claude 2.1.116 popped the theme picker on first
    spawn even with ``hasCompletedOnboarding=True`` in ~/.claude.json. The
    ``❯`` was present in the picker (next to the highlighted option), so
    the legacy readiness probe returned True and the runner piped the
    user's prompt straight into a menu that didn't accept text — hanging
    every interactive turn.

    Fix: detect the picker's title text in the pane and auto-Enter through
    it. This test simulates the picker → ready transition by patching
    `capture_pane` to return a picker first, then the ready marker.
    """
    from orbit import orchestrator_tmux as mod

    class _PickerRunner(mod.SubprocessTmuxRunner):
        def __init__(self):
            self.captures = 0
            self.enters_sent = 0

        async def capture_pane(self, name: str) -> str:
            self.captures += 1
            # First 2 polls show the picker, then claude advances to ready.
            if self.captures <= 2:
                return "Welcome\n\nChoose the text style that looks best with your terminal\n  1. Auto\n❯ 2. Dark mode ✔\n"
            return "claude REPL\n❯ \n  ⏵⏵ auto mode on (shift+tab to cycle)\n"

        async def send_enter(self, name: str) -> None:
            self.enters_sent += 1

    runner = _PickerRunner()
    # Speed up the polling so the test runs fast.
    monkeypatch.setattr(mod, "READINESS_POLL_INTERVAL_S", 0.01)
    ok = _run(runner.wait_until_ready("hd-pickertest"))
    assert ok is True
    assert runner.enters_sent >= 1, (
        f"wait_until_ready did not dismiss the theme picker; enters_sent={runner.enters_sent}"
    )


@pytest.mark.parametrize("status_row", [
    # claude 2.1.152's four documented permission modes — toggled via
    # shift+tab in the TUI. Each one was untested before the marker
    # refactor; covering all four guards against a future regression
    # narrowing the substring back to one mode name.
    pytest.param(
        "  ⏵⏵ auto mode on (shift+tab to cycle)\n",
        id="auto-mode",
    ),
    pytest.param(
        "  ▶▶ bypass permissions on (shift+tab to cycle)  ·  ← for agents\n",
        id="bypass-mode",
    ),
    pytest.param(
        "  ⏵⏵ accept edits on (shift+tab to cycle)\n",
        id="accept-edits-mode",
    ),
    pytest.param(
        "  ⏵⏵ plan mode on (shift+tab to cycle)\n",
        id="plan-mode",
    ),
])
def test_wait_until_ready_accepts_all_permission_modes(monkeypatch, status_row):
    """UAT bug on Hetzner (2026-05-27, claude 2.1.152): user toggled the
    permission mode via shift+tab and claude rendered
    ``▶▶ bypass permissions on`` instead of the original
    ``⏵⏵ auto mode on``. The original hard-coded marker no longer
    matched and every spawn timed out at 60s.

    Fix: anchor readiness on the mode-agnostic ``(shift+tab to cycle)``
    hint that's present in EVERY permission mode's status row, so a
    future claude shipping a new mode doesn't require a code change.
    This parametrized test covers all four documented modes.
    """
    from orbit import orchestrator_tmux as mod

    class _ModeRunner(mod.SubprocessTmuxRunner):
        async def capture_pane(self, name: str) -> str:
            return "claude REPL\n❯ \n" + status_row

        async def send_enter(self, name: str) -> None:  # pragma: no cover — picker path not exercised
            pass

    monkeypatch.setattr(mod, "READINESS_POLL_INTERVAL_S", 0.01)
    assert _run(_ModeRunner().wait_until_ready("hd-modetest")) is True


def test_acquire_waits_for_readiness_before_returning(tmp_path, monkeypatch):
    """Empirically discovered during manual smoke (Phase 1): a freshly
    spawned claude needs ~9-15 s before its PTY is accepting input. Without
    the wait, the runner's first pipe_prompt pastes into a buffer claude
    isn't reading yet — Enter falls on the floor and the turn never starts.

    Acquire MUST block on `wait_until_ready` so the slot is hot before
    callers send a prompt.
    """
    from orbit import orchestrator_tmux as mod

    class _LatentRunner(FakeTmuxRunner):
        def __init__(self):
            super().__init__()
            self.ready_polls = 0

        async def wait_until_ready(self, name: str) -> bool:
            self.ready_polls += 1
            return True

    runner = _LatentRunner()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    assert runner.ready_polls == 1  # called on first acquire


def test_paste_into_pastes_without_enter(tmp_path, monkeypatch):
    """Gallery "Skomentuj" → terminal drops the artifact path at claude's
    input WITHOUT submitting. paste_into must call the runner's paste_text
    (no Enter), never send_prompt (which appends Enter)."""
    from orbit import orchestrator_tmux as mod
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    runner.calls.clear()
    _run(pool.paste_into("abc", "/home/testuser/.orchestrator/artifacts/global/x.png"))

    kinds = [c[0] for c in runner.calls]
    assert "paste_text" in kinds, f"expected a paste_text call, got {kinds!r}"
    assert "send_prompt" not in kinds  # no submit
    assert not any(c[0] == "send_enter" for c in runner.calls)
    paste = next(c for c in runner.calls if c[0] == "paste_text")
    assert paste[1] == ("hd-abc", "/home/testuser/.orchestrator/artifacts/global/x.png")


def test_paste_into_unknown_session_raises(tmp_path, monkeypatch):
    from orbit import orchestrator_tmux as mod
    pool = _make_pool(FakeTmuxRunner())
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    with pytest.raises(KeyError):
        _run(pool.paste_into("missing", "x"))


def test_send_prompt_still_submits_with_enter(tmp_path):
    """Regression guard: send_prompt (chat path) must STILL append Enter even
    though it now delegates the paste to paste_text."""
    from orbit import orchestrator_tmux as mod

    class _RecordingTmux(mod.SubprocessTmuxRunner):
        def __init__(self):
            self.tmux_calls = []
            self.loaded = []

        async def _tmux(self, *args, check=True):
            self.tmux_calls.append(args)
            return 0, "", ""

        async def paste_text(self, name, text):
            # Record but skip the real load-buffer subprocess.
            self.loaded.append((name, text))
            await self._tmux("paste-buffer", "-b", f"in-{name}", "-p", "-d", "-t", name)

    r = _RecordingTmux()
    _run(r.send_prompt("hd-x", "hello"))
    assert r.loaded == [("hd-x", "hello")]
    assert any(a[0] == "send-keys" and "Enter" in a for a in r.tmux_calls), (
        f"send_prompt must send Enter; calls={r.tmux_calls!r}"
    )


def test_acquire_wait_ready_false_commits_without_readiness_poll(tmp_path, monkeypatch):
    """The terminal-open path (/term/ensure with terminal_instant_attach on)
    passes wait_ready=False so ttyd can attach to the live pane the instant
    the tmux session exists — the user then drives claude's boot / resume
    picker directly. A resume picker never satisfies the readiness marker, so
    blocking on wait_until_ready stalled the iframe up to 60 s; this path must
    NOT poll, yet must still commit a warm slot.
    """
    from orbit import orchestrator_tmux as mod

    class _CountingRunner(FakeTmuxRunner):
        def __init__(self):
            super().__init__()
            self.ready_polls = 0

        async def wait_until_ready(self, name: str) -> bool:
            self.ready_polls += 1
            return True

    runner = _CountingRunner()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    slot = _run(pool.acquire(session_id="abc", cwd=tmp_path, wait_ready=False))
    # Spawned + committed warm, but readiness was never polled.
    assert runner.ready_polls == 0
    assert pool.has_warm_slot("abc")
    assert slot.session_name == "hd-abc"
    # The tmux session is live (so ttyd's `attach` will succeed).
    assert "hd-abc" in runner.live


def test_acquire_tears_down_slot_on_cancellation(tmp_path, monkeypatch):
    """Phase 2A.1: when `acquire` is cancelled mid-`wait_until_ready`, the
    spawned tmux session must be cleaned up.

    Regression risk: a naive `try/except BaseException: await teardown(); raise`
    would be re-cancelled at teardown's first internal `await`, leaving an
    orphan tmux session. The fix uses a fire-and-forget task held by a
    module-level strong-ref set so teardown completes on the event loop
    after `acquire` re-raises.
    """
    from orbit import orchestrator_tmux as mod

    class _SlowReady(FakeTmuxRunner):
        async def wait_until_ready(self, name: str) -> bool:
            await asyncio.sleep(60)
            return True

    runner = _SlowReady()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    async def driver():
        task = asyncio.create_task(pool.acquire(session_id="cancelme", cwd=tmp_path))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Let the fire-and-forget teardown task finish on the event loop.
        for _ in range(20):
            if "hd-cancelme" not in runner.live:
                break
            await asyncio.sleep(0.05)

    _run(driver())
    assert "hd-cancelme" not in runner.live, (
        "half-spawned tmux session orphaned on cancellation — "
        "fire-and-forget teardown didn't complete"
    )


def test_acquire_tears_down_slot_when_readiness_times_out(tmp_path, monkeypatch):
    """If claude never becomes ready (config error, OOM, segfault on boot),
    acquire must surface the failure AND clean up the orphan tmux session
    so it doesn't squat in the pool consuming a slot forever."""
    from orbit import orchestrator_tmux as mod

    class _NeverReady(FakeTmuxRunner):
        async def wait_until_ready(self, name: str) -> bool:
            return False

    runner = _NeverReady()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    with pytest.raises(RuntimeError):
        _run(pool.acquire(session_id="abc", cwd=tmp_path))
    # Orphan was killed; slot is not retained.
    assert "hd-abc" not in runner.live
    assert "abc" not in pool._slots  # type: ignore[attr-defined]


def test_acquire_spawns_new_session(tmp_path, monkeypatch):
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    slot = _run(pool.acquire(session_id="abc", cwd=tmp_path))
    assert slot.session_name == "hd-abc"
    assert any(c[0] == "spawn" for c in runner.calls)
    assert "hd-abc" in runner.live


def test_acquire_forwards_append_system_prompt_paths_to_spawn(tmp_path, monkeypatch):
    """Code review #1 (PR #39): the 4-layer prompt stack (general / orchestrator
    / identity / custom) must reach `build_spawn_cmd`, not get silently dropped
    by a hardcoded `append_system_prompt_paths=[]`.

    Same class of regression as PR #33's `_run_llm_isolated` review feedback.
    """
    from orbit import orchestrator_tmux as mod
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    prompt_a = tmp_path / "general.md"
    prompt_b = tmp_path / "identity.md"
    prompt_a.write_text("general layer")
    prompt_b.write_text("identity layer")

    _run(pool.acquire(
        session_id="abc",
        cwd=tmp_path,
        append_system_prompt_paths=[prompt_a, prompt_b],
    ))
    # The spawn call's argv must include both paths after
    # `--append-system-prompt-file` flags. FakeTmuxRunner records spawn args
    # as ("spawn", (name, *cmd)).
    spawn_calls = [c for c in runner.calls if c[0] == "spawn"]
    assert len(spawn_calls) == 1
    spawn_argv = list(spawn_calls[0][1][1:])  # drop the leading session name
    flag_indices = [i for i, tok in enumerate(spawn_argv) if tok == "--append-system-prompt-file"]
    assert len(flag_indices) == 2
    forwarded_paths = [spawn_argv[i + 1] for i in flag_indices]
    assert forwarded_paths == [str(prompt_a), str(prompt_b)]


def test_acquire_ignores_append_paths_when_slot_already_warm(tmp_path, monkeypatch):
    """Re-acquiring an existing slot must NOT re-spawn — the long-lived claude
    process already has its system prompt flags baked in. Passing different
    paths on the second call is silently ignored (with a docstring caveat
    pointing at future invalidation work)."""
    from orbit import orchestrator_tmux as mod
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    p = tmp_path / "x.md"
    p.write_text("x")
    _run(pool.acquire(session_id="abc", cwd=tmp_path, append_system_prompt_paths=[p]))
    _run(pool.acquire(session_id="abc", cwd=tmp_path, append_system_prompt_paths=[p, p]))
    spawn_count = sum(1 for c in runner.calls if c[0] == "spawn")
    assert spawn_count == 1


def test_acquire_existing_session_does_not_respawn(tmp_path, monkeypatch):
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    spawn_calls_first = sum(1 for c in runner.calls if c[0] == "spawn")
    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    spawn_calls_second = sum(1 for c in runner.calls if c[0] == "spawn")
    assert spawn_calls_first == spawn_calls_second == 1


def test_overflow_does_not_immediately_evict(tmp_path, monkeypatch):
    """Capacity-aware cooldown (UAT 2026-05-15): the 5th acquire with
    POOL_SIZE=4 does NOT immediately kill the LRU — it puts the LRU into
    a cooldown window (idle_ttl_s grace) but keeps it alive until then.

    User's exact wording: "100 sessions, 96 cool time, 4 zostaną".
    """
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=4, idle_ttl_s=600.0)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    for sid in ["a", "b", "c", "d"]:
        _run(pool.acquire(session_id=sid, cwd=tmp_path))
    assert len(runner.live) == 4

    _run(pool.acquire(session_id="e", cwd=tmp_path))
    # All 5 are alive; 'a' (LRU) is scheduled for cooldown eviction.
    assert runner.live == {"hd-a", "hd-b", "hd-c", "hd-d", "hd-e"}
    assert pool._slots["a"].evict_at is not None
    for sid in ["b", "c", "d", "e"]:
        assert pool._slots[sid].evict_at is None, f"{sid} must stay hot"


def test_cooldown_expires_after_grace(tmp_path, monkeypatch):
    """Slot in cooldown is killed once now >= evict_at."""
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=2, idle_ttl_s=0.05)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    for sid in ["a", "b", "c"]:
        _run(pool.acquire(session_id=sid, cwd=tmp_path))
    # 'a' is in cooldown with ~0.05s grace.
    assert pool._slots["a"].evict_at is not None
    # Force the deadline into the past.
    pool._slots["a"].evict_at = 0.0
    _run(pool.evict_idle())
    assert "hd-a" not in runner.live
    # The top-N (b, c) remain.
    assert runner.live == {"hd-b", "hd-c"}


def test_reacquire_cooling_slot_promotes_to_hot(tmp_path, monkeypatch):
    """User returns to a session before the cooldown expires → revive."""
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=2, idle_ttl_s=600.0)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    for sid in ["a", "b", "c"]:
        _run(pool.acquire(session_id=sid, cwd=tmp_path))
    assert pool._slots["a"].evict_at is not None
    # User comes back to 'a' before it expires.
    _run(pool.acquire(session_id="a", cwd=tmp_path))
    assert pool._slots["a"].evict_at is None  # promoted back to hot
    # Now 'b' (the new LRU) should be cooling.
    assert pool._slots["b"].evict_at is not None
    assert pool._slots["c"].evict_at is None


def test_hot_slots_never_evicted_regardless_of_age(tmp_path, monkeypatch):
    """A slot inside the top-N hot ring stays alive forever, even if its
    last_touched_at is ancient — only over-capacity slots get a TTL."""
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=4, idle_ttl_s=0.01)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    # Force the timestamp WAY into the past — should still survive because
    # the slot is the only one and well within pool_size.
    pool._slots["abc"].last_touched_at -= 100.0  # type: ignore[attr-defined]
    _run(pool.evict_idle())
    assert "hd-abc" in runner.live


def test_100_sessions_keeps_4_hot_rest_cooling(tmp_path, monkeypatch):
    """Acceptance test for UAT spec: 100 sessions, 96 cool, 4 stay."""
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=4, idle_ttl_s=600.0)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    for i in range(100):
        _run(pool.acquire(session_id=f"s{i}", cwd=tmp_path))

    assert len(runner.live) == 100  # everyone's alive; cooldown not yet expired
    hot = [sid for sid, slot in pool._slots.items() if slot.evict_at is None]
    cooling = [sid for sid, slot in pool._slots.items() if slot.evict_at is not None]
    assert len(hot) == 4
    assert len(cooling) == 96
    # Hot ones are the 4 most recently acquired.
    assert hot == ["s96", "s97", "s98", "s99"]


def test_acquire_touches_lru_order(tmp_path, monkeypatch):
    """Re-acquiring an older sid promotes it back to hot, pushing the new
    LRU into cooldown."""
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=3, idle_ttl_s=600.0)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    for sid in ["a", "b", "c"]:
        _run(pool.acquire(session_id=sid, cwd=tmp_path))
    # touch 'a' — now 'b' is LRU but still hot (still ≤ pool_size)
    _run(pool.acquire(session_id="a", cwd=tmp_path))
    _run(pool.acquire(session_id="d", cwd=tmp_path))
    # All 4 alive (capacity-aware); 'b' is the new oldest → cooling.
    assert runner.live == {"hd-a", "hd-b", "hd-c", "hd-d"}
    assert pool._slots["b"].evict_at is not None
    for sid in ["a", "c", "d"]:
        assert pool._slots[sid].evict_at is None


# ── idle TTL eviction ──────────────────────────────────────────────


def test_evict_idle_keeps_fresh_slots(tmp_path, monkeypatch):
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=4, idle_ttl_s=600.0)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    _run(pool.evict_idle())
    assert "hd-abc" in runner.live  # untouched, hot


# ── pipe_prompt ────────────────────────────────────────────────────


def test_pipe_prompt_after_acquire(tmp_path, monkeypatch):
    """H2: pipe_prompt is delegated to the runner so unit tests can confirm
    the call shape without invoking real load-buffer/paste-buffer."""
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="abc", cwd=tmp_path))
    _run(pool.pipe_prompt("abc", "[choice:opt=42]"))
    send_calls = [c for c in runner.calls if c[0] == "send_prompt"]
    assert send_calls == [("send_prompt", ("hd-abc", "[choice:opt=42]"))]


def test_pipe_prompt_unknown_session_raises(tmp_path, monkeypatch):
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    with pytest.raises(KeyError):
        _run(pool.pipe_prompt("never-acquired", "hello"))


# ── shutdown / teardown ────────────────────────────────────────────


def test_shutdown_kills_all_slots(tmp_path, monkeypatch):
    runner = FakeTmuxRunner()
    pool = _make_pool(runner, pool_size=4)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    for sid in ["a", "b", "c"]:
        _run(pool.acquire(session_id=sid, cwd=tmp_path))
    _run(pool.shutdown())
    assert runner.live == set()


# ── crash recovery (orphans) ───────────────────────────────────────


def test_recover_orphans_kills_every_session_on_socket(tmp_path, monkeypatch):
    """Phase 2A.4: ``-L hd-orch`` is a private socket owned entirely by the
    pool. Any session on it that we don't track is an orphan — including
    accidental sessions like ``main`` from a user .tmux.conf that runs
    ``new-session -A -s main`` whenever a tmux server spins up.

    The previous policy kept the ``hd-*`` prefix filter as defence-in-depth,
    but a non-tmux-pool process creating sessions on our socket is implausible
    (4 grep hits in the entire codebase for ``hd-orch``, all internal) and
    keeping them around silently leaks RAM across restarts.
    """
    _force_detached(monkeypatch, False)  # legacy sweep: kill ALL untracked
    runner = FakeTmuxRunner()
    runner.preexisting = ["hd-ghost1", "hd-ghost2", "main", "user-debug"]
    pool = _make_pool(runner)
    _run(pool.recover_orphans())
    # Every preexisting session is killed regardless of name.
    assert runner.live == set()
    killed = sorted(c[1][0] for c in runner.calls if c[0] == "kill_session")
    assert killed == sorted(runner.preexisting)


def test_recover_orphans_spares_tracked_slots(tmp_path, monkeypatch):
    """recover_orphans must NEVER kill a slot the pool itself is using —
    that's how we survive a (hypothetical) double-boot race where Phase 1
    spawn ran but lifespan startup hasn't completed."""
    _force_detached(monkeypatch, False)  # legacy sweep: untracked hd-* die too
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    from orbit import orchestrator_tmux as mod
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    _run(pool.acquire(session_id="alive", cwd=tmp_path))
    # Simulate an orphan + the live slot both reported by `list_sessions`.
    runner.preexisting = ["hd-orphan", "main"]
    _run(pool.recover_orphans())
    assert "hd-alive" in runner.live  # tracked, spared
    assert "hd-orphan" not in runner.live
    assert "main" not in runner.live


def test_recover_orphans_no_op_when_empty(tmp_path):
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    _run(pool.recover_orphans())
    assert runner.calls == []


# ── detached sessions (H14): cgroup escape + survive-restart ───────


def test_user_scope_prefix_empty_when_flag_off(monkeypatch):
    """Flag off → no wrapping, caller execs tmux directly (legacy in-cgroup)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, False)
    prefix, env = mod._user_scope_prefix()
    assert prefix == []
    assert env is None


def test_user_scope_prefix_empty_when_bus_unreachable(monkeypatch):
    """Flag on but the per-user bus isn't reachable → graceful fallback so a
    session still STARTS (it just won't survive a restart)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_user_runtime_dir", lambda: None)
    prefix, env = mod._user_scope_prefix()
    assert prefix == []
    assert env is None


def test_user_scope_prefix_empty_when_systemd_run_missing(monkeypatch):
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_user_runtime_dir", lambda: "/run/user/1000")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    prefix, env = mod._user_scope_prefix()
    assert prefix == []
    assert env is None


def test_user_scope_prefix_wraps_when_available(monkeypatch):
    """Flag on + bus + systemd-run → a `systemd-run --user --scope -- …`
    prefix terminated with `--`, plus the XDG_RUNTIME_DIR overlay."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_user_runtime_dir", lambda: "/run/user/1000")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/systemd-run")
    prefix, env = mod._user_scope_prefix()
    assert prefix[0] == "/usr/bin/systemd-run"
    assert "--user" in prefix and "--scope" in prefix
    assert any(p.startswith("--description=") for p in prefix), "stable scope identity"
    assert prefix[-1] == "--", "must terminate options before tmux's own flags"
    assert env == {"XDG_RUNTIME_DIR": "/run/user/1000"}


def test_user_runtime_dir_requires_bus_socket(monkeypatch, tmp_path):
    """`_user_runtime_dir` only returns a dir when `$dir/bus` exists."""
    from orbit import orchestrator_tmux as mod
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert mod._user_runtime_dir() is None       # no bus socket yet
    (tmp_path / "bus").write_text("")
    assert mod._user_runtime_dir() == str(tmp_path)


def test_spawn_wraps_argv_and_sets_env(monkeypatch):
    """SubprocessTmuxRunner.spawn prepends the scope prefix to the FINAL argv
    and passes the env overlay to create_subprocess_exec."""
    from orbit import orchestrator_tmux as mod

    captured: dict[str, Any] = {}

    class _FakeProc:
        async def wait(self):
            return 0

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(
        mod, "_user_scope_prefix",
        lambda: (["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--"],
                 {"XDG_RUNTIME_DIR": "/run/user/1000"}),
    )
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec)
    runner = mod.SubprocessTmuxRunner()
    # A minimal but realistic outer/inner cmd: last `-e K=V` then one inner tok.
    cmd = [mod._tmux_bin(), "-L", "hd-orch", "new-session", "-d", "-s", "hd-x",
           "-e", "TERM=xterm-256color", "claudeplaceholder"]
    # _tmux (prefix2 unset) runs after spawn; stub it so we don't touch tmux.
    async def _noop_tmux(*a, **k):
        return (0, "", "")
    monkeypatch.setattr(runner, "_tmux", _noop_tmux)

    monkeypatch.setenv("HD_SENTINEL_ENV", "inherited-value")
    _run(runner.spawn("hd-x", cmd))
    argv = captured["argv"]
    assert argv[:6] == ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--"]
    assert argv[6] == mod._tmux_bin()  # real tmux command follows the prefix's `--`
    assert captured["env"]["XDG_RUNTIME_DIR"] == "/run/user/1000"
    # The overlay LAYERS over os.environ (doesn't replace it) — claude's PATH,
    # tokens, etc. must survive. Pin that with a sentinel.
    assert captured["env"]["HD_SENTINEL_ENV"] == "inherited-value"


def test_spawn_no_wrap_passes_env_none(monkeypatch):
    """Empty prefix → argv unwrapped and env=None (inherit) — legacy path."""
    from orbit import orchestrator_tmux as mod
    captured: dict[str, Any] = {}

    class _FakeProc:
        async def wait(self):
            return 0

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(mod, "_user_scope_prefix", lambda: ([], None))
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec)
    runner = mod.SubprocessTmuxRunner()
    async def _noop_tmux(*a, **k):
        return (0, "", "")
    monkeypatch.setattr(runner, "_tmux", _noop_tmux)

    cmd = [mod._tmux_bin(), "-L", "hd-orch", "new-session", "-d", "-s", "hd-x",
           "-e", "TERM=xterm-256color", "claudeplaceholder"]
    _run(runner.spawn("hd-x", cmd))
    assert captured["argv"][0] == mod._tmux_bin()
    assert captured["env"] is None


def test_acquire_reattaches_to_surviving_session(tmp_path, monkeypatch):
    """Detached mode: a `hd-<id>` session that outlived the prior process is
    ADOPTED — no `new-session` (which tmux rejects as a duplicate)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = FakeTmuxRunner()
    runner.live.add("hd-survivor")  # pretend it survived a restart

    slot = _run(pool_then_acquire(mod, runner, "survivor", tmp_path))
    assert slot.session_name == "hd-survivor"
    assert not any(c[0] == "spawn" for c in runner.calls), "must re-attach, not spawn"


def test_acquire_spawns_when_not_surviving_even_in_detached_mode(tmp_path, monkeypatch):
    """Detached mode but no live session → normal fresh spawn."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = FakeTmuxRunner()
    slot = _run(pool_then_acquire(mod, runner, "fresh", tmp_path))
    assert slot.session_name == "hd-fresh"
    assert any(c[0] == "spawn" for c in runner.calls)


def test_acquire_no_reattach_when_flag_off(tmp_path, monkeypatch):
    """Flag off → even a live `hd-*` session is ignored by acquire (legacy):
    it goes down the spawn path. (FakeTmuxRunner.spawn just re-adds the name;
    REAL tmux would reject `new-session` on a live duplicate, so the documented
    rollback requires survivors to exit first — see orchestrator_settings.)"""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, False)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = FakeTmuxRunner()
    runner.live.add("hd-x")
    _run(pool_then_acquire(mod, runner, "x", tmp_path))
    assert any(c[0] == "spawn" for c in runner.calls)


def test_shutdown_keep_sessions_does_not_teardown(tmp_path, monkeypatch):
    """shutdown(kill_sessions=False) drops in-memory state but leaves the
    tmux/claude REPLs running (so they survive a restart)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    for sid in ["a", "b"]:
        _run(pool.acquire(session_id=sid, cwd=tmp_path))
    _run(pool.shutdown(kill_sessions=False))
    assert runner.live == {"hd-a", "hd-b"}, "REPLs must keep running"
    assert not any(c[0] in ("send_exit", "kill_session") for c in runner.calls)
    assert pool._slots == {}, "in-memory pool state is dropped"


def test_recover_orphans_detached_adopts_hd_kills_junk(tmp_path, monkeypatch):
    """Detached mode: hd-* survivors are ADOPTED into the pool (so the evictor
    bounds them + an explicit close can reap them); non-hd junk (e.g. the
    `main` session from ~/.tmux.conf) is still swept."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    runner = FakeTmuxRunner()
    runner.preexisting = ["hd-keep1", "hd-keep2", "main"]
    pool = _make_pool(runner)
    _run(pool.recover_orphans())
    killed = sorted(c[1][0] for c in runner.calls if c[0] == "kill_session")
    assert killed == ["main"]
    # Survivors are now tracked slots, not invisible orphans → bounded by the
    # evictor, reapable via release().
    assert set(pool._slots) == {"keep1", "keep2"}
    assert pool._slots["keep1"].session_name == "hd-keep1"


def test_detach_drops_slot_without_killing_repl(tmp_path, monkeypatch):
    """detach() frees the in-memory slot but leaves the tmux/claude REPL RUNNING
    (no send_exit, no kill_session) — what "zamknij sesję" wants so the
    conversation survives and is re-adopted on reopen."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = FakeTmuxRunner()
    pool = _make_pool(runner)
    _run(pool.acquire(session_id="keep", cwd=tmp_path))
    _run(pool.detach("keep"))
    assert "keep" not in pool._slots, "slot must be freed"
    assert "hd-keep" in runner.live, "REPL must keep running detached"
    assert not any(c[0] in ("send_exit", "kill_session") for c in runner.calls)
    # idempotent — a second detach is a no-op, not an error.
    _run(pool.detach("keep"))


def test_recover_orphans_adopted_survivor_is_reapable_via_release(tmp_path, monkeypatch):
    """Regression for the RAM-leak finding: a survivor adopted at boot can be
    torn down by an explicit close/delete (release), unlike a bare-spared one."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    runner = FakeTmuxRunner()
    runner.live.add("hd-zombie")
    runner.preexisting = ["hd-zombie"]
    pool = _make_pool(runner)
    _run(pool.recover_orphans())
    assert "zombie" in pool._slots
    _run(pool.release("zombie"))
    assert "hd-zombie" not in runner.live  # actually killed, not leaked
    assert "zombie" not in pool._slots


def test_recover_orphans_adopted_survivors_evicted_when_over_capacity(tmp_path, monkeypatch):
    """Adopted survivors past pool_size are scheduled for cooldown eviction —
    so RAM is bounded exactly like normal idle slots (not leaked forever)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    runner = FakeTmuxRunner()
    runner.preexisting = [f"hd-s{i}" for i in range(6)]
    pool = _make_pool(runner, pool_size=2, idle_ttl_s=600.0)
    _run(pool.recover_orphans())
    assert len(pool._slots) == 6
    cooling = [s for s in pool._slots.values() if s.evict_at is not None]
    assert len(cooling) == 4, "4 of 6 survivors must be scheduled for eviction"


def pool_then_acquire(mod, runner, sid, cwd):
    """Build a pool around `runner` and acquire `sid` — returns the acquire
    coroutine (the FakeTmuxRunner's wait_until_ready is instant)."""
    pool = mod.TmuxPool(pool_size=4, idle_ttl_s=600.0, runner=runner)
    return pool.acquire(session_id=sid, cwd=cwd)


# ── PrivateTmp socket relocation (review fix: TMUX_TMPDIR off /tmp) ─


def test_ensure_tmux_socket_dir_sets_and_creates_tmpdir(tmp_path, monkeypatch):
    """Detached on + no preset → TMUX_TMPDIR pinned to ~/.orchestrator/tmux
    (here redirected to tmp_path) and the dir is PRE-CREATED so tmux doesn't
    silently fall back to /tmp (the PrivateTmp namespace trap)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    sockdir = tmp_path / "sock"
    monkeypatch.setattr(mod, "_detached_socket_dir", lambda: sockdir)
    mod.ensure_tmux_socket_dir()
    assert os.environ["TMUX_TMPDIR"] == str(sockdir)
    assert sockdir.is_dir(), "must pre-create so tmux won't fall back to /tmp"


def test_ensure_tmux_socket_dir_honors_preset(tmp_path, monkeypatch):
    """A pre-set TMUX_TMPDIR (e.g. from the unit) is respected, not clobbered."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    preset = tmp_path / "preset"
    monkeypatch.setenv("TMUX_TMPDIR", str(preset))
    mod.ensure_tmux_socket_dir()
    assert os.environ["TMUX_TMPDIR"] == str(preset)
    assert preset.is_dir()


def test_ensure_tmux_socket_dir_pins_even_when_flag_off(tmp_path, monkeypatch):
    """Pinned UNCONDITIONALLY so the socket path is stable across a flag flip:
    a rollback (flip OFF after a detached run) must still boot on the same
    socket so recover_orphans can see + sweep the ON-run survivors."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, False)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    sockdir = tmp_path / "sock"
    monkeypatch.setattr(mod, "_detached_socket_dir", lambda: sockdir)
    mod.ensure_tmux_socket_dir()
    assert os.environ["TMUX_TMPDIR"] == str(sockdir)
    assert sockdir.is_dir()


def test_user_runtime_dir_reconstructs_from_uid_when_xdg_absent(monkeypatch):
    """The actual system-service scenario: no XDG_RUNTIME_DIR in env →
    reconstruct /run/user/<uid> and validate its bus socket."""
    from orbit import orchestrator_tmux as mod
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(mod.os, "getuid", lambda: 4242)
    real_isdir, real_exists = os.path.isdir, os.path.exists
    monkeypatch.setattr(
        mod.os.path, "isdir", lambda p: p == "/run/user/4242" or real_isdir(p)
    )
    monkeypatch.setattr(
        mod.os.path, "exists", lambda p: p == "/run/user/4242/bus" or real_exists(p)
    )
    assert mod._user_runtime_dir() == "/run/user/4242"


def test_spawn_fallback_in_cgroup_when_bus_unavailable(monkeypatch):
    """End-to-end graceful fallback through the REAL _user_scope_prefix: flag
    on but bus unreachable → spawn execs tmux directly, env=None (in-cgroup)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_user_runtime_dir", lambda: None)  # bus gone
    captured: dict[str, Any] = {}

    class _P:
        async def wait(self):
            return 0

    async def _exec(*argv, **kw):
        captured["argv"] = list(argv)
        captured["env"] = kw.get("env")
        return _P()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _exec)
    runner = mod.SubprocessTmuxRunner()
    async def _noop(*a, **k):
        return (0, "", "")
    monkeypatch.setattr(runner, "_tmux", _noop)
    cmd = [mod._tmux_bin(), "-L", "hd-orch", "new-session", "-d", "-s", "hd-x",
           "-e", "TERM=x", "inner"]
    _run(runner.spawn("hd-x", cmd))
    assert captured["argv"][0] == mod._tmux_bin()  # no systemd-run prefix
    assert captured["env"] is None


# ── app lifespan shutdown wiring (flag → kill_sessions kwarg) ───────


def test_lifespan_shutdown_keeps_sessions_when_flag_on(monkeypatch):
    from orbit import app as app_mod
    from orbit import orchestrator as orch_mod
    from orbit import orchestrator_settings as settings_mod
    rec: dict[str, Any] = {}

    class _FakePool:
        async def shutdown(self, *, kill_sessions=True):
            rec["kill_sessions"] = kill_sessions

    monkeypatch.setattr(orch_mod, "_get_tmux_pool", lambda: _FakePool())
    monkeypatch.setattr(
        settings_mod, "get_flag",
        lambda n: True if n == "tmux_detached_sessions" else None,
    )
    _run(app_mod._shutdown_tmux_pool())
    assert rec["kill_sessions"] is False  # REPLs kept running across restart


def test_lifespan_shutdown_kills_sessions_when_flag_off(monkeypatch):
    from orbit import app as app_mod
    from orbit import orchestrator as orch_mod
    from orbit import orchestrator_settings as settings_mod
    rec: dict[str, Any] = {}

    class _FakePool:
        async def shutdown(self, *, kill_sessions=True):
            rec["kill_sessions"] = kill_sessions

    monkeypatch.setattr(orch_mod, "_get_tmux_pool", lambda: _FakePool())
    monkeypatch.setattr(
        settings_mod, "get_flag",
        lambda n: False if n == "tmux_detached_sessions" else None,
    )
    _run(app_mod._shutdown_tmux_pool())
    assert rec["kill_sessions"] is True  # legacy teardown


def test_startup_pins_tmpdir_before_recover_orphans(monkeypatch):
    """CRITICAL ordering: ensure_tmux_socket_dir MUST run before the first
    tmux call (recover_orphans), or survivors resolve to the wrong socket."""
    from orbit import app as app_mod
    from orbit import orchestrator as orch_mod
    from orbit import orchestrator_tmux as tmux_mod
    from orbit import orchestrator_settings as settings_mod
    order: list[str] = []

    class _FakePool:
        async def recover_orphans(self):
            order.append("recover_orphans")
        async def start(self):
            order.append("start")

    monkeypatch.setattr(tmux_mod, "ensure_tmux_socket_dir", lambda: order.append("ensure_tmpdir"))
    monkeypatch.setattr(orch_mod, "_get_tmux_pool", lambda: _FakePool())
    monkeypatch.setattr(settings_mod, "get_flag", lambda n: False)  # skip prewarm
    _run(app_mod._startup_tmux_pool())
    assert order == ["ensure_tmpdir", "recover_orphans", "start"]


class _CountingReadyRunner(FakeTmuxRunner):
    def __init__(self, ready=True):
        super().__init__()
        self.ready_polls = 0
        self._ready = ready
    async def wait_until_ready(self, name):
        self.ready_polls += 1
        return self._ready


def test_reattach_wait_ready_false_skips_readiness_poll(tmp_path, monkeypatch):
    """Terminal-open re-attach (wait_ready=False): commit immediately, no poll
    — the user drives the live pane."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = _CountingReadyRunner()
    runner.live.add("hd-surv")
    pool = mod.TmuxPool(pool_size=4, idle_ttl_s=600.0, runner=runner)
    slot = _run(pool.acquire(session_id="surv", cwd=tmp_path, wait_ready=False))
    assert slot.session_name == "hd-surv"
    assert runner.ready_polls == 0
    assert pool.has_warm_slot("surv")


def test_reattach_wait_ready_true_runs_readiness_poll(tmp_path, monkeypatch):
    """Programmatic re-attach (wait_ready=True): confirm the survivor is at its
    prompt before committing, so a prompt isn't piped into a busy/picker pane."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = _CountingReadyRunner(ready=True)
    runner.live.add("hd-surv")
    pool = mod.TmuxPool(pool_size=4, idle_ttl_s=600.0, runner=runner)
    slot = _run(pool.acquire(session_id="surv", cwd=tmp_path, wait_ready=True))
    assert slot.session_name == "hd-surv"
    assert runner.ready_polls == 1
    assert pool.has_warm_slot("surv")


def test_reattach_not_ready_commits_without_killing_survivor(tmp_path, monkeypatch):
    """A survivor that never paints the readiness marker (busy at restart) is
    committed anyway and NEVER torn down — losing it would kill the user's
    running session."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = _CountingReadyRunner(ready=False)  # never ready
    runner.live.add("hd-surv")
    pool = mod.TmuxPool(pool_size=4, idle_ttl_s=600.0, runner=runner)
    slot = _run(pool.acquire(session_id="surv", cwd=tmp_path, wait_ready=True))
    assert slot.session_name == "hd-surv"
    assert "hd-surv" in runner.live, "survivor must NOT be torn down"
    assert not any(c[0] in ("send_exit", "kill_session") for c in runner.calls)
    assert pool.has_warm_slot("surv")


def test_reattach_concurrent_does_not_double_adopt_or_spawn(tmp_path, monkeypatch):
    """Two concurrent acquires of the same survivor → one slot, no spawn,
    has_session resolved once (no duplicate adoption)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")

    class _SlowHasSession(FakeTmuxRunner):
        def __init__(self):
            super().__init__()
            self.has_calls = 0
        async def has_session(self, name):
            self.has_calls += 1
            await asyncio.sleep(0.05)  # widen the race window
            return name in self.live

    runner = _SlowHasSession()
    runner.live.add("hd-x")
    pool = mod.TmuxPool(pool_size=4, idle_ttl_s=600.0, runner=runner)

    async def driver():
        a = asyncio.create_task(pool.acquire(session_id="x", cwd=tmp_path))
        b = asyncio.create_task(pool.acquire(session_id="x", cwd=tmp_path))
        return await asyncio.gather(a, b)

    s1, s2 = _run(driver())
    assert s1.session_name == s2.session_name == "hd-x"
    assert not any(c[0] == "spawn" for c in runner.calls), "must adopt, never spawn"
    assert list(pool._slots) == ["x"], "exactly one slot"
    assert runner.has_calls == 1, "second caller waited on the reservation, no 2nd adopt"


def test_ensure_tmux_socket_dir_degrades_on_oserror(tmp_path, monkeypatch):
    """If the socket dir can't be created, TMUX_TMPDIR stays unset and no
    exception escapes (falls back to the legacy /tmp socket)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.setattr(mod, "_detached_socket_dir", lambda: tmp_path / "x")
    def _boom(*a, **k):
        raise OSError("read-only fs")
    monkeypatch.setattr(mod.os, "makedirs", _boom)
    mod.ensure_tmux_socket_dir()  # must not raise
    assert "TMUX_TMPDIR" not in os.environ


def test_user_scope_prefix_fallback_warns_once(monkeypatch, capsys):
    """The in-cgroup fallback warning prints at most once across calls."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_user_runtime_dir", lambda: None)  # bus gone → fallback
    monkeypatch.setattr(mod, "_fallback_warned", False)
    mod._user_scope_prefix()
    mod._user_scope_prefix()
    out = capsys.readouterr().out
    assert out.count("user-scope escape is unavailable") == 1


def test_reacquire_refreshes_adopted_survivor_cwd(tmp_path, monkeypatch):
    """An adopted survivor starts at cwd=~ (recover_orphans can't know its dir);
    the first re-acquire with the real cwd upgrades it so snapshot()/the agent
    label stop showing home."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    monkeypatch.setattr(mod, "_CLAUDE_CONFIG", tmp_path / ".claude.json")
    runner = FakeTmuxRunner()
    runner.live.add("hd-s")
    runner.preexisting = ["hd-s"]
    pool = _make_pool(runner)
    _run(pool.recover_orphans())
    assert pool._slots["s"].cwd == Path.home()  # adopted placeholder
    real_cwd = tmp_path / "Projects" / "my-project"
    real_cwd.mkdir(parents=True)
    slot = _run(pool.acquire(session_id="s", cwd=real_cwd))
    assert slot.cwd == real_cwd  # refreshed
    assert pool.snapshot()["slots"][0]["cwd"] == str(real_cwd)


def test_recover_orphans_detached_sweeps_empty_hd_name(monkeypatch):
    """A degenerate `hd-` (empty id) is not adoptable → swept like junk, not
    stranded (the leak the legacy sweep would never have allowed)."""
    from orbit import orchestrator_tmux as mod
    _force_detached(monkeypatch, True)
    runner = FakeTmuxRunner()
    runner.preexisting = ["hd-", "hd-real", "main"]
    pool = _make_pool(runner)
    _run(pool.recover_orphans())
    killed = sorted(c[1][0] for c in runner.calls if c[0] == "kill_session")
    assert killed == ["hd-", "main"]      # empty-id hd- + junk both swept
    assert set(pool._slots) == {"real"}    # only the valid survivor adopted


def test_spawn_retries_in_cgroup_on_scope_failure(monkeypatch, capsys):
    """A RUNTIME failure of the scope-wrapped spawn (transient user-manager
    hiccup) degrades to a direct in-cgroup spawn instead of failing the turn."""
    from orbit import orchestrator_tmux as mod
    calls: list[dict[str, Any]] = []

    class _P:
        def __init__(self, rc):
            self._rc = rc
        async def wait(self):
            return self._rc

    async def _exec(*argv, **kw):
        calls.append({"argv": list(argv), "env": kw.get("env")})
        return _P(1) if len(calls) == 1 else _P(0)  # 1st (scoped) fails, retry ok

    monkeypatch.setattr(
        mod, "_user_scope_prefix",
        lambda: (["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--"],
                 {"XDG_RUNTIME_DIR": "/run/user/1000"}),
    )
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _exec)
    runner = mod.SubprocessTmuxRunner()
    async def _noop(*a, **k):
        return (0, "", "")
    monkeypatch.setattr(runner, "_tmux", _noop)
    cmd = [mod._tmux_bin(), "-L", "hd-orch", "new-session", "-d", "-s", "hd-x",
           "-e", "TERM=x", "inner"]
    _run(runner.spawn("hd-x", cmd))
    assert len(calls) == 2
    assert calls[0]["argv"][0] == "/usr/bin/systemd-run"  # scoped first
    assert calls[1]["argv"][0] == mod._tmux_bin()          # in-cgroup retry
    assert calls[1]["env"] is None
    assert "retrying in-cgroup" in capsys.readouterr().out


def test_spawn_raises_when_both_scoped_and_retry_fail(monkeypatch):
    from orbit import orchestrator_tmux as mod

    class _P:
        async def wait(self):
            return 1

    async def _exec(*argv, **kw):
        return _P()

    monkeypatch.setattr(
        mod, "_user_scope_prefix",
        lambda: (["/usr/bin/systemd-run", "--scope", "--"], {"XDG_RUNTIME_DIR": "/x"}),
    )
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _exec)
    runner = mod.SubprocessTmuxRunner()
    cmd = [mod._tmux_bin(), "-L", "hd-orch", "new-session", "-d", "-s", "hd-x",
           "-e", "TERM=x", "inner"]
    with pytest.raises(RuntimeError):
        _run(runner.spawn("hd-x", cmd))


def test_startup_fires_prewarm_when_enabled(monkeypatch):
    """_startup_tmux_pool schedules the background prewarm when
    pool_prewarm_on_start is ON."""
    from orbit import app as app_mod
    from orbit import orchestrator as orch_mod
    from orbit import orchestrator_tmux as tmux_mod
    from orbit import orchestrator_settings as settings_mod

    class _FakePool:
        async def recover_orphans(self): pass
        async def start(self): pass

    fired = {"prewarm": False}
    async def _fake_prewarm():
        fired["prewarm"] = True

    scheduled: list[Any] = []
    def _cap_task(coro, *a, **k):
        scheduled.append(coro)
        class _T:
            def add_done_callback(self, cb): pass
        return _T()

    monkeypatch.setattr(tmux_mod, "ensure_tmux_socket_dir", lambda: None)
    monkeypatch.setattr(orch_mod, "_get_tmux_pool", lambda: _FakePool())
    monkeypatch.setattr(orch_mod, "prewarm_recent_sessions", _fake_prewarm)
    monkeypatch.setattr(settings_mod, "get_flag",
                        lambda n: n == "pool_prewarm_on_start")  # only this flag ON
    monkeypatch.setattr(app_mod.asyncio, "create_task", _cap_task)
    _run(app_mod._startup_tmux_pool())
    assert len(scheduled) == 1, "prewarm must be scheduled"
    _run(scheduled[0])  # run the captured coro → confirm it IS prewarm
    assert fired["prewarm"] is True


def test_ttyd_spawn_inherits_env_for_tmux_tmpdir(monkeypatch):
    """ttyd attaches via `tmux -L hd-orch attach`; it MUST inherit the pinned
    TMUX_TMPDIR (no env override) or it resolves a different socket than the
    pool's server. Pin the cross-process contract: spawn passes no `env`."""
    from orbit import orchestrator_ttyd as ttyd
    captured: dict[str, Any] = {}

    class _P:
        pid = 4321
        async def wait(self):
            return 0

    async def _exec(*argv, **kw):
        captured["argv"] = list(argv)
        captured["env"] = kw.get("env", "ABSENT")
        return _P()

    def _noop_task(coro, *a, **k):
        if hasattr(coro, "close"):
            coro.close()  # avoid "coroutine never awaited" warning
        return None

    monkeypatch.setattr(ttyd.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(ttyd.asyncio, "create_task", _noop_task)
    spawner = ttyd.SubprocessTtydSpawner()
    _run(spawner.spawn(argv=["ttyd", "tmux", "-L", "hd-orch", "attach"]))
    # env not passed → child inherits os.environ (where ensure_tmux_socket_dir
    # pinned TMUX_TMPDIR). Either absent or explicitly None both inherit.
    assert captured["env"] in ("ABSENT", None)


# ── orphan tmux-server reaper ───────────────────────────────────────


class _ReaperRunner(FakeTmuxRunner):
    """FakeTmuxRunner that also reports a configurable live socket-owner pid."""

    def __init__(self, live_pid: int | None) -> None:
        super().__init__()
        self._live_pid = live_pid

    async def server_pid(self) -> int | None:
        return self._live_pid


def _reaper_pool(mod, live_pid):
    return mod.TmuxPool(pool_size=2, idle_ttl_s=999, runner=_ReaperRunner(live_pid))


def _patch_proc(monkeypatch, mod, *, servers, children, comm, cmdline, age=999.0):
    """Stub the /proc-scanning helpers + capture os.kill signals."""
    monkeypatch.setattr(mod, "_find_hd_orch_servers", lambda: list(servers))
    monkeypatch.setattr(mod, "_descendant_claude_pids", lambda srv: list(children.get(srv, [])))
    monkeypatch.setattr(mod, "_proc_age_s", lambda pid: age)
    monkeypatch.setattr(mod, "_proc_comm", lambda pid: comm.get(pid, ""))
    monkeypatch.setattr(mod, "_proc_cmdline", lambda pid: cmdline.get(pid, ""))
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_reaper_kills_orphan_server_and_children_not_live(monkeypatch):
    from orbit import orchestrator_tmux as mod
    LIVE, ORPHAN = 100, 200
    KIDS = [201, 202]
    sent = _patch_proc(
        monkeypatch, mod,
        servers=[LIVE, ORPHAN],
        children={ORPHAN: KIDS, LIVE: [101, 102]},
        comm={LIVE: "tmux: server", ORPHAN: "tmux: server"},
        cmdline={201: "claude --resume x", 202: "claude --resume y"},
    )
    _run(_reaper_pool(mod, LIVE).reap_orphan_servers())
    killed = {pid for pid, _ in sent}
    assert killed == {ORPHAN, 201, 202}            # orphan tree only
    assert LIVE not in killed and 101 not in killed  # live server + its kids spared
    # both SIGTERM and SIGKILL delivered to each victim
    assert {sig for _, sig in sent} == {mod.signal.SIGTERM, mod.signal.SIGKILL}


def test_reaper_fail_closed_when_socket_unowned(monkeypatch):
    from orbit import orchestrator_tmux as mod
    sent = _patch_proc(
        monkeypatch, mod,
        servers=[200], children={200: [201]},
        comm={200: "tmux: server"}, cmdline={201: "claude"},
    )
    _run(_reaper_pool(mod, None).reap_orphan_servers())  # server_pid() → None
    assert sent == []  # reap nothing without a confirmed live owner


def test_reaper_spares_just_born_server(monkeypatch):
    from orbit import orchestrator_tmux as mod
    LIVE, YOUNG = 100, 300
    sent = _patch_proc(
        monkeypatch, mod,
        servers=[LIVE, YOUNG], children={YOUNG: [301]},
        comm={LIVE: "tmux: server", YOUNG: "tmux: server"},
        cmdline={301: "claude"},
        age=5.0,  # below TMUX_SERVER_MIN_AGE_S
    )
    _run(_reaper_pool(mod, LIVE).reap_orphan_servers())
    assert sent == []  # too young → not yet socket-bound, spared


def test_reaper_skips_recycled_pid(monkeypatch):
    from orbit import orchestrator_tmux as mod
    LIVE, ORPHAN = 100, 200
    # ORPHAN's comm is no longer a tmux server (PID recycled) → must be skipped
    sent = _patch_proc(
        monkeypatch, mod,
        servers=[LIVE, ORPHAN], children={ORPHAN: [201]},
        comm={LIVE: "tmux: server", ORPHAN: "bash"},
        cmdline={201: "vim notes.txt"},  # child also recycled (not claude)
    )
    _run(_reaper_pool(mod, LIVE).reap_orphan_servers())
    assert sent == []  # neither the recycled server nor the recycled child signalled


def test_reaper_noop_when_flag_off(monkeypatch):
    from orbit import orchestrator_tmux as mod
    from orbit import orchestrator_settings as settings
    monkeypatch.setattr(settings, "get_flag", lambda name: False)
    sent = _patch_proc(
        monkeypatch, mod,
        servers=[100, 200], children={200: [201]},
        comm={100: "tmux: server", 200: "tmux: server"}, cmdline={201: "claude"},
    )
    _run(_reaper_pool(mod, 100).reap_orphan_servers())
    assert sent == []


# ── live_session_ids: authoritative tmux probe → set of session_ids ─────────


def test_live_session_ids_strips_prefix_and_drops_junk():
    runner = FakeTmuxRunner()
    # A mix of real hd-* sessions, the ~/.tmux.conf "main" junk, a degenerate
    # bare "hd-", and a non-string straggler — only the real ids survive.
    runner.preexisting = ["hd-aaa", "hd-bbb", "main", "hd-", "random-thing"]
    pool = _make_pool(runner)
    assert _run(pool.live_session_ids()) == {"aaa", "bbb"}


def test_live_session_ids_empty_when_no_sessions():
    pool = _make_pool(FakeTmuxRunner())
    assert _run(pool.live_session_ids()) == set()


# ── list_sessions: benign "no server" → [] vs real error → raise ────────────


def _stub_list_runner(rc, stdout, stderr):
    """A SubprocessTmuxRunner whose _tmux returns a canned (rc, out, err) so we
    can exercise list_sessions' error discrimination without a real tmux."""
    from orbit import orchestrator_tmux as mod

    class _Stub(mod.SubprocessTmuxRunner):
        def __init__(self):
            pass  # skip real init; list_sessions only needs _tmux

        async def _tmux(self, *args, check=True):
            return rc, stdout, stderr

    return _Stub()


def test_list_sessions_benign_no_server_returns_empty():
    r = _stub_list_runner(1, "", "no server running on /tmp/tmux-1000/hd-orch")
    assert _run(r.list_sessions()) == []


def test_list_sessions_missing_socket_returns_empty():
    r = _stub_list_runner(1, "", "error connecting to /tmp/.../hd-orch (No such file or directory)")
    assert _run(r.list_sessions()) == []


def test_list_sessions_real_error_raises_not_silently_empty():
    """A genuine probe failure must RAISE so the reconciler passes the snapshot
    through unfiltered instead of reading [] as 'every session is dead'."""
    r = _stub_list_runner(1, "", "lost server: connection reset by peer")
    with pytest.raises(RuntimeError):
        _run(r.list_sessions())


def test_list_sessions_ok_parses_names():
    r = _stub_list_runner(0, "hd-aaa\nmain\nhd-bbb\n", "")
    assert _run(r.list_sessions()) == ["hd-aaa", "main", "hd-bbb"]


def test_live_session_ids_propagates_probe_error():
    """The raise must reach the caller so tmux_pool_snapshot_live's except can
    fall back to an unfiltered snapshot."""
    runner = _stub_list_runner(1, "", "lost server: connection reset by peer")
    pool = _make_pool(runner)
    with pytest.raises(RuntimeError):
        _run(pool.live_session_ids())
