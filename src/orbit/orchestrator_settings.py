"""Server-side flags for the Orchestrator (currently just auto-titles).

Per-device localStorage settings already exist on the frontend, but flags
that gate SERVER behaviour (like Haiku auto-title generation, which runs
in the background after every turn) need to live next to the server. This
module is a tiny key/value store at ``~/.orchestrator/settings.json``,
mirroring the simple atomic-write style of ``orchestrator_meta.py``.

Adding a new flag = (a) bump ``_DEFAULTS``, (b) accept it in
``set_settings``, (c) add a frontend toggle. No schema migration needed —
unknown keys round-trip through.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path.home() / ".orchestrator" / "settings.json"

# Default values for every supported flag. Persisting None means "use default";
# the GET endpoint always returns concrete values so the frontend never sees
# undefined.
_DEFAULTS: dict[str, Any] = {
    "auto_titles": True,
    # tmux-driven interactive runner is now the DEFAULT for user chat —
    # long-lived claude REPLs in the pool, subscription billing, and the
    # terminal view all assume it. The legacy `claude -p` path is still
    # one click away (per-session "Interactive: OFF" in the chat kebab,
    # or flip this flag globally). No UI copy needed — it's just the
    # default.
    "runner_mode": "interactive",  # "programmatic" | "interactive" — user-chat dispatch
    # Per-task billing routing for the headless one-shots. `interactive`
    # routes through the tmux pool (Max SUBSCRIPTION billing — the default
    # post-2026-06-15); `programmatic` is the legacy `claude -p` path (rolls
    # under the programmatic credit pool / API) kept only as a manual rollback.
    # NO auto-fallback to `-p`: a broken interactive path surfaces as a FAILED
    # run (operator sees it) and flips this flag to `programmatic` to roll back.
    "cron_runner_mode": "interactive",
    "titles_runner_mode": "interactive",
    "identity_runner_mode": "interactive",
    "skill_runner_mode": "interactive",
    "pool_size": 4,
    # 4-hour default: covers typical browse-back-and-forth UAT patterns where
    # the user may step away for an hour or two before returning. Idle claude
    # uses ~1.3 GB; 4 slots × 4 h is acceptable on a 12 GB box and avoids
    # surprise cold-restarts on returning to a session. Lower it via the
    # Settings UI if you're tight on RAM.
    "pool_idle_ttl_s": 14400,
    # Pre-warm the tmux pool on app/server startup with the most-recently-used
    # sessions (up to pool_size, newest first) so the first terminal/chat open
    # after a restart is instant instead of a 10-20 s cold spawn. OFF by
    # default — it spawns up to pool_size claude processes on boot (each
    # ~1.3 GB), which isn't free; opt in via Settings. Warming runs in the
    # background so it never delays boot.
    "pool_prewarm_on_start": False,
    # Inline interactive terminal (ttyd-backed) — Phase B of the migration
    # from the static <pre> pane preview to a real xterm.js terminal.
    # Defaults to OFF so a dashboard restart never starts spawning ttyd
    # subprocesses until the user explicitly opts in (mirrors the
    # `runner_mode: programmatic` precedent for the tmux migration).
    # Toggling the flag in Settings is the runtime kill-switch — no
    # restart needed.
    "ttyd_enabled": False,
    # Terminal-open attaches ttyd to the live tmux pane the instant the
    # session is spawned, WITHOUT blocking on claude's readiness poll. The
    # user then watches claude boot — and drives the "Resume from summary /
    # full session" picker — directly in the terminal, instead of staring at
    # a spinner for up to 60 s (a resume picker never satisfies the readiness
    # marker, so the old path always timed out on large/old sessions). Flip
    # OFF to restore the legacy blocking spawn (acquire waits out
    # wait_until_ready before /term/ensure returns). Runtime kill-switch — no
    # restart needed. Programmatic chat paste is unaffected (it still waits).
    "terminal_instant_attach": True,
    # 15 min — ttyd is cheap so we keep it warm for instant re-opens.
    "ttyd_idle_ttl_s": 900,
    # Localhost-only port window. 100 ports is wildly more than a
    # single-user dashboard needs; the pool surfaces a clear error if
    # ever exhausted.
    "ttyd_port_min": 7700,
    "ttyd_port_max": 7799,
    # Opt-in editor for the mobile terminal soft-keyboard shortcuts (remap the
    # key each button sends + show/hide). OFF by default → the toolbar uses its
    # hard-coded defaults and the Settings editor stays hidden (this is the
    # documented level-1 rollback). When ON, the toolbar merges the sparse
    # overrides from ~/.orchestrator/terminal_shortcuts.json over the defaults.
    # Runtime kill-switch — no restart needed. See orchestrator_terminal_shortcuts.py.
    "terminal_shortcuts_enabled": False,
    # Agent-to-agent messaging (A2A). OFF by default → POST
    # /api/orchestrator/a2a/send returns 403 ("a2a disabled") so no agent can
    # drop messages into another's inbox maildir or auto-spawn a cold target to
    # deliver. When ON, agents arm a Monitor-backed inbox watcher (see
    # general.md) and exchange messages via the `a2a` CLI skill
    # (~/.orchestrator/skills-registry/a2a/). Same-host only, best-effort (no
    # loop/cost guardrails). Runtime kill-switch — no restart needed. See
    # orchestrator_a2a.py + skills/a2a/.
    "a2a_enabled": False,
    # Desktop ⌥+⇥ session-switcher overlay (macOS Cmd+Tab feel: hold ⌥/Option,
    # tap ⇥ Tab to cycle the warm tmux pool — all active sessions — release ⌥ to
    # jump). ON by default → DesktopHub's <SessionSwitcher> attaches its key
    # listeners; the ⌥⇥ trigger is forwarded across the ttyd iframe too. The
    # ⌘←/→ agent-switch + ⌘↑/↓ session-cycle shortcuts are independent of this.
    # Frontend reads it via GET /api/orchestrator/settings (no boot-data change).
    # Rollback L1: set False (or toggle off in Settings → Terminal) → no
    # listeners attach + ⌥⇥ is a no-op. L2: remove the <SessionSwitcher> mount +
    # the ⌥⇥ branch in _forwardAppShortcuts (orchestrator-terminal-preview.jsx).
    # L3: delete session-switcher.jsx, session-switcher-order.js (+ its
    # node:test), the two index.html script tags, this flag, and the toggle.
    "session_switcher_enabled": True,
    # Opt-in read-aloud (TTS) for the terminal surface. OFF by default →
    # the speaker button in the terminal toolbar is hidden AND the passive
    # JSONL watcher (orchestrator_read_aloud.py) never arms, so a restart
    # never starts watching sessions until the user opts in. When ON, the
    # device-scoped voiceOutput mode (manual|on-voice|always) still decides
    # whether TTS actually plays. Server scope so all of a user's devices
    # agree the feature exists. Runtime kill-switch — no restart needed.
    # Rollback L1: set False (or toggle off in Settings → Terminal). L2:
    # the /read-aloud SSE route 404s + the speaker button is hidden. L3:
    # delete orchestrator_read_aloud.py + orchestrator-read-aloud.jsx, the
    # route, this flag, and the Settings toggle.
    "read_aloud_tmux_enabled": False,
    # Gates the DEFERRED neural-embedding tier of session search
    # (orchestrator_search.EmbeddingBackend / a remote /v1/embeddings client).
    # OFF by default → search runs on the always-on BM25 ⊕ sklearn-TF-IDF
    # hybrid, which needs no flag. Flipping this on only matters once a
    # RemoteBackend is wired (currently NullBackend, so it's a no-op today);
    # advertised as `semantic_search` in GET /api/orchestrator/capabilities.
    "semantic_search_enabled": False,
    # Detached orchestrator sessions — survive a dashboard restart. The
    # dashboard runs as a systemd *system* service with the default
    # KillMode=control-group, so a tmux server forked from it inherits the
    # service cgroup and `systemctl restart` SIGKILLs the whole tree (every
    # claude REPL dies). ON (default) makes three things happen together:
    #   1. SubprocessTmuxRunner.spawn wraps the tmux server launch in
    #      `systemd-run --user --scope` so the server lands in the user
    #      manager's cgroup (/user.slice/…), a sibling of the service —
    #      untouched by the service lifecycle. (Falls back to an in-cgroup
    #      spawn if systemd-run / the per-user bus is unavailable, so a
    #      session always *starts*, even if it then can't survive a restart.)
    #   2. The lifespan shutdown skips `tmux kill-session` on every slot
    #      (app.py passes kill_sessions=not flag) so a graceful stop leaves
    #      the REPLs running instead of /exit-ing them.
    #   3. acquire() re-attaches to an already-live `hd-<id>` session instead
    #      of `new-session` (which errors "duplicate session") so the new
    #      process adopts the survivors.
    # Rollback L1: set False (or toggle in Settings → Terminal) → sessions
    # spawn in the service cgroup, are torn down on shutdown, and never
    # re-attached — i.e. the pre-feature behavior. Needs a restart to take
    # full effect (the spawn path is read per-spawn, but already-detached
    # servers stay detached until they exit). Requires linger enabled for the
    # service user (`loginctl enable-linger`) + XDG_RUNTIME_DIR reachable;
    # see orchestrator_tmux._user_scope_prefix.
    "tmux_detached_sessions": True,
    # Self-healing reaper for stranded `-L hd-orch` tmux servers (and their
    # claude REPLs) left off-socket by a restart that rebound the socket —
    # otherwise they leak ~0.5 GB each forever (see TmuxPool.reap_orphan_servers).
    # Default ON: it only reaps servers that DON'T own the socket (already
    # unreachable), fail-closed when the socket is unowned. Flip OFF to disable.
    "tmux_reap_orphan_servers": True,
}

# Validation bounds for non-boolean flags. Out-of-range values are dropped
# silently (matches the existing contract for unknown values on known keys).
_RUNNER_MODES: tuple[str, ...] = ("programmatic", "interactive")
_POOL_SIZE_RANGE: tuple[int, int] = (1, 32)
_POOL_IDLE_TTL_RANGE: tuple[int, int] = (1, 86_400)  # 1s..24h
_TTYD_IDLE_TTL_RANGE: tuple[int, int] = (30, 86_400)  # 30s..24h
_TTYD_PORT_RANGE: tuple[int, int] = (1024, 65535)

_data: dict[str, Any] | None = None
_lock = asyncio.Lock()


def _warn(msg: str) -> None:
    print(f"[orchestrator_settings] {msg}", file=sys.stderr)


def _ensure_dir() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_from_disk() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt or unreadable {SETTINGS_PATH.name}: {exc}; treating as empty")
        return {}
    if not isinstance(payload, dict):
        _warn(f"{SETTINGS_PATH.name} is not an object; treating as empty")
        return {}
    return payload


def _ensure_loaded() -> dict[str, Any]:
    global _data
    if _data is None:
        _ensure_dir()
        _data = _load_from_disk()
    return _data


def _atomic_write(payload: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(SETTINGS_PATH.parent),
        prefix=".settings.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, SETTINGS_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def get_settings() -> dict[str, Any]:
    """Return all flags merged with defaults — concrete values guaranteed.

    Includes a ``_bounds`` dict describing the validation ranges for each
    non-boolean flag so the frontend can read them on mount instead of
    hardcoding the same numbers and risking drift. Underscore prefix marks
    the field as metadata (read-only, never accepted by ``set_settings``).
    """
    data = _ensure_loaded()
    return {
        **_DEFAULTS,
        **data,
        "_bounds": {
            "runner_mode": list(_RUNNER_MODES),
            "cron_runner_mode": list(_RUNNER_MODES),
            "titles_runner_mode": list(_RUNNER_MODES),
            "identity_runner_mode": list(_RUNNER_MODES),
            "skill_runner_mode": list(_RUNNER_MODES),
            "pool_size": list(_POOL_SIZE_RANGE),
            "pool_idle_ttl_s": list(_POOL_IDLE_TTL_RANGE),
            "ttyd_idle_ttl_s": list(_TTYD_IDLE_TTL_RANGE),
            "ttyd_port_min": list(_TTYD_PORT_RANGE),
            "ttyd_port_max": list(_TTYD_PORT_RANGE),
        },
    }


def get_flag(name: str) -> Any:
    """Single-flag lookup; falls back to default when unset."""
    settings = get_settings()
    return settings.get(name, _DEFAULTS.get(name))


def resolve_runner_mode(flag_name: str, *, default: str = "interactive") -> str:
    """Read a ``*_runner_mode`` flag → a VALIDATED mode, defaulting to subscription.

    The single source of truth for the 5 one-shot dispatch sites (cron, titles,
    identity, skill, compaction). Returns ``default`` ("interactive" =
    subscription) on any settings error or an unrecognized value, so every site
    fails the SAME way — closed onto subscription, never open onto the ``-p``
    credit pool. Replaces the hand-rolled ``get_flag(...) or "interactive"``
    blocks that had already drifted (cron used to default to ``programmatic``).
    """
    try:
        mode = get_flag(flag_name)
    except Exception:  # noqa: BLE001 — settings unavailable → subscription default
        return default
    return mode if isinstance(mode, str) and mode in _RUNNER_MODES else default


async def set_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the on-disk settings and return the new state.

    Coerces booleans for known boolean flags so a stray string from the
    frontend doesn't poison the file. Unknown keys are dropped silently.
    """
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in _DEFAULTS:
            # Silently drop unknown keys, including read-only metadata like
            # ``_bounds`` if the frontend ever round-trips it.
            continue
        default = _DEFAULTS[key]
        if isinstance(default, bool):
            cleaned[key] = bool(value)
            continue
        if key in (
            "runner_mode", "cron_runner_mode",
            "titles_runner_mode", "identity_runner_mode", "skill_runner_mode",
        ):
            if isinstance(value, str) and value in _RUNNER_MODES:
                cleaned[key] = value
            continue
        if key in ("pool_size", "pool_idle_ttl_s"):
            # Reject bool explicitly — `True`/`False` are `int` in Python and
            # would otherwise sneak past `isinstance(value, int)`.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            lo, hi = (
                _POOL_SIZE_RANGE if key == "pool_size" else _POOL_IDLE_TTL_RANGE
            )
            if lo <= value <= hi:
                cleaned[key] = value
            continue
        if key in ("ttyd_idle_ttl_s", "ttyd_port_min", "ttyd_port_max"):
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            lo, hi = (
                _TTYD_IDLE_TTL_RANGE if key == "ttyd_idle_ttl_s"
                else _TTYD_PORT_RANGE
            )
            if lo <= value <= hi:
                cleaned[key] = value
            continue
        cleaned[key] = value
    if not cleaned:
        return get_settings()
    async with _lock:
        data = _ensure_loaded()
        new_data = {**data, **cleaned}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
    return get_settings()
