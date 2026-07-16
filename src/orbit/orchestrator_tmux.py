"""Long-lived tmux session pool for the interactive Claude runner.

Phase 1 of the swap from ``claude -p`` per turn to a persistent interactive
``claude`` driven through tmux. Each dashboard chat session maps to a single
tmux session ``hd-<session-uuid>`` running on a dedicated socket ``hd-orch``
(isolated from the user's existing tmux server).

Key invariants enforced by this module:

* **Subscription routing (H11)** — every spawn passes ``-e ANTHROPIC_API_KEY=``
  (empty value) so the child claude routes under the interactive subscription,
  NOT the post-2026-06-15 programmatic credit pool / pay-as-you-go API.
* **PTY rendering (H10)** — every spawn passes ``-e TERM=xterm-256color``
  because without it claude renders into a void PTY and the pane stays blank
  even though the process is alive.
* **Isolation (H13)** — every tmux call uses ``-L hd-orch`` to never collide
  with the user's long-running ``orchestrator`` tmux server.
* **Trust pre-seed (H3)** — ``~/.claude.json projects[<cwd>].hasTrustDialogAccepted``
  is set to ``True`` before the first spawn in a new cwd, so the workspace-
  trust menu doesn't block the first prompt.
* **LRU + idle TTL (H5)** — pool caps at ``pool_size`` slots; the 5th acquire
  evicts the LRU slot. A background loop kills slots idle for ``idle_ttl_s``.
* **Crash recovery (H7)** — at startup, untracked sessions on the socket are
  swept so RAM doesn't leak across restarts. With detached sessions (default)
  the sweep ADOPTS ``hd-*`` REPLs that intentionally outlived the prior process
  into the pool (so the idle/cooldown evictor still bounds them and an explicit
  close can reap them); without it, every untracked session dies.
* **Detached sessions (H14)** — the tmux server is launched inside a
  user-manager scope (``systemd-run --user --scope``) so it lives outside this
  systemd service's cgroup and survives ``systemctl restart``; the lifespan
  shutdown leaves the REPLs running and ``acquire`` re-attaches. Gated by the
  ``tmux_detached_sessions`` flag. See :func:`_user_scope_prefix`.

All shell operations flow through :class:`TmuxRunner`, an injectable
abstraction so unit tests can replace it with an in-memory fake.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ── constants ──────────────────────────────────────────────────────

TMUX_SOCKET: str = "hd-orch"
SESSION_PREFIX: str = "hd-"
TEARDOWN_GRACE_S: float = 3.0
# A tmux server younger than this is spared by the orphan-server reaper: it may
# be a legitimate just-spawned server that has not yet bound the `-L hd-orch`
# socket, so it would transiently look like a non-owner.
TMUX_SERVER_MIN_AGE_S: float = 30.0
SPAWN_PANE_W: int = 200
SPAWN_PANE_H: int = 50
# How long to wait for claude's REPL to render the input prompt marker after
# spawn before giving up. PoC measured ~9s on Mac with full skill/MCP load;
# we cap at 60s so a misconfigured spawn doesn't hang the runner forever.
READINESS_TIMEOUT_S: float = 60.0
READINESS_POLL_INTERVAL_S: float = 0.5
# Substrings the wait loop scans for in `capture-pane` output. claude renders
# the input-prompt arrow `❯` EARLY during boot (before the mode-status badge
# is drawn at the bottom of the pane), and a paste sent in that narrow
# window is silently dropped — the input box ends up empty by the time
# claude is actually reading stdin. Observed on Hetzner first-spawn: user
# typed a prompt, runner reported in_flight=true forever, JSONL was never
# created. The "⏵⏵ <mode> on (shift+tab to cycle)" status row is the LAST
# thing claude paints before genuinely accepting input, so we anchor
# readiness on it.
#
# UAT 2026-05-27 (claude 2.1.152): the user toggled the permission mode
# via shift+tab and claude now displays "▶▶ bypass permissions on" instead
# of the original "⏵⏵ auto mode on". The original hard-coded "auto mode on"
# marker no longer matched and the readiness check timed out at 60s on
# every spawn after the toggle.
#
# Format of the status row is "<arrows> <mode-name> on (shift+tab to
# cycle)" — the parenthetical hint is identical across all four
# documented permission modes (auto / bypass / accept-edits / plan), so
# we anchor on that single mode-agnostic substring rather than enumerating
# every mode name (which would need a code change every time claude ships
# a new mode). `❯` remains as a defence-in-depth fallback for a
# hypothetical future claude that drops the hint string.
READINESS_MARKERS: tuple[str, ...] = (
    "(shift+tab to cycle)",
)
READINESS_MARKER_FALLBACK: str = "❯"

# Patched by tests; production resolves at module-load time.
# The path of the claude config we need to pre-seed (`hasTrustDialogAccepted`,
# `hasCompletedOnboarding`, etc.). Claude resolves its config file as
# ``$CLAUDE_CONFIG_DIR/.claude.json`` when the env var is set (the hetzner
# systemd service does this), and ``~/.claude.json`` otherwise. Without
# respecting that, our trust pre-seed lands in the wrong file and claude
# pops a trust dialog or onboarding picker on first spawn — observed during
# Hetzner UAT, root cause of the "spawning interactive session... 119s"
# hang.
_CLAUDE_HOME: Path = Path.home() / ".claude"


def _resolve_claude_config_path() -> Path:
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir) / ".claude.json"
    return Path.home() / ".claude.json"


_CLAUDE_CONFIG: Path = _resolve_claude_config_path()

# Strong references for fire-and-forget teardown tasks spawned from
# `acquire`'s cancellation path. Without this set, asyncio's weak refs would
# let the teardown task be garbage-collected before it actually kills the
# half-spawned tmux session. Tasks self-remove via add_done_callback.
_teardown_tasks: set[asyncio.Task[None]] = set()


def _sync_kill_session(session_name: str) -> None:
    """Blocking last-resort `tmux kill-session` for paths where asyncio
    isn't available (event loop closed, interpreter shutdown).

    Used by `TmuxPool.acquire`'s cancellation cleanup when
    `asyncio.create_task` raises `RuntimeError`. Best-effort: any error is
    swallowed because we're already on a failure path.
    """
    import subprocess
    try:
        subprocess.run(
            [_tmux_bin(), "-L", TMUX_SOCKET, "kill-session", "-t", session_name],
            check=False,
            timeout=3.0,
            capture_output=True,
        )
    except Exception:  # noqa: BLE001 — last-resort path, never raise
        pass

# Resolve binaries lazily so a missing tmux/claude doesn't crash module
# import (callers will hit the real error when they try to spawn).


def _claude_bin() -> str:
    """Resolve the claude executable for `tmux new-session ... claude` invocations.

    Checks well-known install locations explicitly before falling back to
    ``shutil.which("claude")`` — because the dashboard runs under systemd
    with a minimal PATH that does NOT include user-local bin dirs (notably
    ``~/.local/bin``). Interactive shells set up by ``.bashrc`` / ``.zshrc``
    find user-installed claude fine, but the dashboard's spawn does not.

    UAT 2026-05-27: Hetzner had claude installed at
    ``/home/user/.local/bin/claude`` (versioned symlink to
    ``~/.local/share/claude/versions/<ver>``). The dashboard's
    ``shutil.which("claude")`` returned None and we fell through to the
    literal ``"claude"`` string, which tmux then failed to exec —
    surfacing as a 60s readiness timeout with no obvious cause.
    """
    candidates: list[str] = [
        "/usr/bin/claude",
        str(Path.home() / ".local" / "bin" / "claude"),
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return shutil.which("claude") or "claude"


def _tmux_bin() -> str:
    return shutil.which("tmux") or "/opt/homebrew/bin/tmux"


# User-local bin dirs the login zsh prepends (matches ~/.zshrc line 45):
# ``~/.local/bin:~/.cargo/bin:~/.npm-global/bin``. Kept as a constant so the
# spawn PATH and any future callers agree on the set.
_USER_LOCAL_BIN_DIRS = (".local/bin", ".cargo/bin", ".npm-global/bin")


def _spawn_path() -> str:
    """PATH to force into spawned claude panes via the inner ``env`` prefix.

    The dashboard runs under systemd with a minimal PATH that omits the
    user-local bin dirs an interactive login zsh adds (notably
    ``~/.local/bin``). ``_claude_bin()`` resolves claude by absolute path so
    it still *execs*, but claude inspects ``$PATH`` on startup and, finding
    ``~/.local/bin`` absent, prints a setup warning into every orchestrator
    terminal (UAT 2026-05-29: the "Native installation exists but
    ~/.local/bin is not in your PATH" banner rendered in the ttyd pane).

    An outer ``tmux new-session -e PATH=`` does NOT survive: tmux runs the
    pane command via zsh, and Debian's /etc/zsh/zshenv resets PATH. So the
    value is applied in the inner ``env PATH=… claude`` prefix instead, which
    wins after zsh's reset (verified empirically on Hetzner).

    Prepend the login-zsh user-local dirs to the inherited PATH, deduped and
    order-preserving, so spawned panes match an interactive shell. Returns a
    single ``os.pathsep``-joined string.
    """
    home = Path.home()
    prepend = [str(home / rel) for rel in _USER_LOCAL_BIN_DIRS]
    inherited = os.environ.get("PATH", "").split(os.pathsep)
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in [*prepend, *inherited]:
        if chunk and chunk not in seen:
            seen.add(chunk)
            ordered.append(chunk)
    return os.pathsep.join(ordered)


# ── detached-session cgroup escape ─────────────────────────────────


def _detached_socket_dir() -> Path:
    """Directory for the dedicated ``-L hd-orch`` tmux socket in detached mode.

    tmux puts the socket at ``$TMUX_TMPDIR/tmux-<uid>/hd-orch`` (default
    ``$TMUX_TMPDIR=/tmp``). The service unit sets ``PrivateTmp=true``, so /tmp
    is a per-START ephemeral mount destroyed on restart — a socket there would
    vanish from the NEXT process's view, stranding the (still-running,
    cgroup-escaped) survivor server as an invisible orphan and defeating
    re-attach. ``~/.orchestrator`` is on the shared host filesystem
    (``ReadWritePaths=/home/user`` keeps it writable under the sandbox),
    persists across restarts, and resolves to the SAME inode for the dashboard
    and the user-scope server.
    """
    return Path.home() / ".orchestrator" / "tmux"


def _claude_tmpdir() -> str:
    """Host-fs scratch dir handed to spawned claude REPLs as ``$TMPDIR``.

    claude defaults its scratch dir to ``/tmp`` and does ``mkdir
    /tmp/claude-<uid>`` ONCE at startup. But the detached tmux server is
    cgroup-escaped (``systemd-run --user --scope``, so it survives
    ``systemctl restart``) and therefore FREEZES the mount namespace it was
    born in. The unit sets ``PrivateTmp=true``, so each service generation
    gets a fresh private ``/tmp`` and the previous one is unlinked on restart —
    the survivor server's ``/tmp`` then shows ``//deleted`` and is empty. A new
    pane forked into that stale server fails ``mkdir /tmp/claude-<uid>`` with
    ENOENT and the REPL dies in ~20 s ("[exited]"). Pinning ``$TMPDIR`` to a
    path on the REAL host filesystem (``~/.orchestrator``, the same place the
    tmux socket already lives for the exact same reason — see
    :func:`_detached_socket_dir`) makes the mkdir land on shared storage that
    exists in EVERY namespace, regardless of PrivateTmp churn. Idempotent;
    created in the dashboard's live namespace and visible to the survivor via
    the shared ``/home/user`` mount.
    """
    d = Path.home() / ".orchestrator" / "tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "/tmp"  # degrade to default; better a try than a hard fail
    return str(d)


def ensure_tmux_socket_dir() -> None:
    """Pin ``TMUX_TMPDIR`` process-wide so spawn, every ``_tmux`` client, AND
    the ttyd attach all resolve the SAME ``-L hd-orch`` socket at a
    restart-stable, non-/tmp path.

    Must run BEFORE the first tmux call in the process (the app lifespan calls
    it ahead of ``recover_orphans``). Idempotent; honors a pre-set
    ``TMUX_TMPDIR`` (e.g. from the unit). tmux silently FALLS BACK to /tmp if
    ``TMUX_TMPDIR`` does not exist, so the directory is created here first.

    Pinned UNCONDITIONALLY — even when detached sessions are off — so the
    socket location is STABLE across a flag flip. Otherwise a rollback (flip
    OFF after a detached run) would boot with the default /tmp socket and
    ``recover_orphans`` could never see, let alone sweep, the ~/.orchestrator
    survivors left by the ON run — leaking them. The relocation is otherwise
    behavior-neutral (the pool owns this socket exclusively).
    """
    target = os.environ.get("TMUX_TMPDIR") or str(_detached_socket_dir())
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:  # noqa: BLE001 — degrade to default /tmp socket
        print(f"[orchestrator_tmux] TMUX_TMPDIR {target!r} unusable: {exc}")
        return
    os.environ["TMUX_TMPDIR"] = target


def _user_runtime_dir() -> str | None:
    """Return the per-user systemd runtime dir IFF the user bus is reachable.

    ``systemd-run --user`` (and tmux's own native systemd-scope integration)
    talk to the per-user manager over ``$XDG_RUNTIME_DIR/bus``. The dashboard
    runs as a *system* service, whose environment does NOT carry
    ``XDG_RUNTIME_DIR`` — so we reconstruct the conventional
    ``/run/user/<uid>`` and only return it when the bus socket actually
    exists (linger on + a live ``user@<uid>.service``). ``None`` signals the
    caller to fall back to an in-cgroup spawn.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime_dir) and os.path.exists(os.path.join(runtime_dir, "bus")):
        return runtime_dir
    return None


# One-shot guard so the in-cgroup fallback warning prints at most once.
_fallback_warned: bool = False


def _user_scope_prefix() -> tuple[list[str], dict[str, str] | None]:
    """argv prefix + env overlay that launches a tmux command in a *user scope*.

    Returns ``(prefix, env_overlay)``:

    * ``prefix`` — ``systemd-run --user --scope …`` tokens to prepend to the
      tmux argv, or ``[]`` when detached sessions are disabled / unavailable.
      With ``[]`` the caller execs tmux directly (legacy, in-cgroup) so a
      session always starts even when the escape can't be performed.
    * ``env_overlay`` — ``{"XDG_RUNTIME_DIR": …}`` to merge into the child env
      so ``systemd-run`` (and the resulting tmux server) can reach the user
      bus, or ``None`` when no wrapping happens.

    Why a scope and not just ``XDG_RUNTIME_DIR``: tmux's native integration
    only moves each *pane* into its own scope — the tmux *server* itself stays
    where it was forked (the service cgroup), and killing the server HUPs every
    pane. Wrapping the server launch in ``systemd-run --user --scope`` puts the
    SERVER in ``/user.slice/…`` too, so the whole tree outlives the service.
    ``--`` terminates systemd-run's option parsing before tmux's own ``-L`` /
    ``-d`` flags (GNU getopt would otherwise try to consume them).
    """
    from . import orchestrator_settings as _settings

    if not _settings.get_flag("tmux_detached_sessions"):
        return [], None
    runtime_dir = _user_runtime_dir()
    systemd_run = shutil.which("systemd-run") if runtime_dir else None
    if runtime_dir is None or systemd_run is None:
        # Flag is ON but we can't escape — sessions will spawn in-cgroup and
        # die on restart. Surface it ONCE (not per-spawn) so a broken
        # linger / missing-systemd-run setup is diagnosable instead of a
        # silent regression to the old kill-on-restart behavior.
        global _fallback_warned
        if not _fallback_warned:
            _fallback_warned = True
            reason = (
                "per-user bus unreachable (XDG_RUNTIME_DIR/linger)"
                if runtime_dir is None
                else "systemd-run not on PATH"
            )
            print(
                "[orchestrator_tmux] tmux_detached_sessions=ON but the "
                f"user-scope escape is unavailable ({reason}); sessions spawn "
                "in-cgroup and will NOT survive a dashboard restart"
            )
        return [], None
    prefix = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        # GC the transient scope once empty (the short-lived client scopes for
        # sessions that attach to an already-running server collect instantly;
        # the one holding the server persists while the server lives).
        "--collect",
        # Stable, accurate identity in `systemctl --user list-units` instead of
        # the default scope name embedding one session's full claude argv. Not a
        # unit name (which would collide on respawn), so it needs no uniqueness.
        "--description=orbit orchestrator tmux server (hd-orch)",
        "--",
    ]
    return prefix, {"XDG_RUNTIME_DIR": runtime_dir}


# ── spawn command builder ──────────────────────────────────────────


def build_spawn_cmd(
    *,
    session_name: str,
    cwd: Path,
    session_id: str,
    append_system_prompt_paths: list[Path],
    resume: bool = False,
    add_dirs: list[Path] | None = None,
    model: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> list[str]:
    """Compose ``tmux new-session ... claude --session-id ...`` argv.

    The inner ``claude`` invocation is built as a single string and passed
    as the last positional arg to ``tmux new-session`` so it runs as the
    pane's command.

    Defensive: rejects ``--bare`` because it forces API billing — never
    accidentally emit it from this codepath.
    """
    # `env TERM=xterm-256color` prepended because tmux silently overrides our
    # outer `-e TERM=xterm-256color` with the server-wide `default-terminal`
    # setting from the user's ~/.tmux.conf (commonly `screen-256color`).
    # Empirically, claude 2.1.116 on Hetzner saw `screen-256color` even
    # though we set the outer var → it treated the terminal as fresh and
    # popped the theme picker on first spawn, hanging every interactive
    # turn. The shell `VAR=value cmd` form forces the var into claude's
    # exec env regardless of tmux's TERM massaging.
    # PATH is set in the inner `env` (not the outer `-e PATH=`) for the SAME
    # reason as TERM above, but a different override path: tmux runs the pane
    # command via `default-shell` (zsh on Hetzner), and Debian's
    # /etc/zsh/zshenv resets PATH to the /etc/profile default — wiping any
    # outer `-e PATH=` AND dropping ~/.local/bin, which makes claude print
    # "Native installation exists but ~/.local/bin is not in your PATH" into
    # every orchestrator pane (UAT 2026-05-29). `env PATH=value cmd` forces
    # the var into claude's exec env AFTER zsh's reset, so it wins. See
    # _spawn_path() for the value.
    inner_parts: list[str] = [
        "env",
        "TERM=xterm-256color",
        f"PATH={_spawn_path()}",
        # TMPDIR pin (same `env VAR=value cmd` technique as TERM/PATH above, so
        # it wins over tmux/zsh env massaging). Forces claude's startup
        # `mkdir $TMPDIR/claude-<uid>` onto the host fs instead of the
        # PrivateTmp `/tmp`, which goes stale (`//deleted`) inside a
        # cgroup-escaped detached server after a `systemctl restart` and
        # otherwise kills every NEW session with ENOENT. See _claude_tmpdir().
        f"TMPDIR={_claude_tmpdir()}",
        _claude_bin(),
        "--resume" if resume else "--session-id",
        session_id,
        "--permission-mode",
        "auto",
        "--add-dir",
        str(_CLAUDE_HOME),
    ]
    for extra in add_dirs or []:
        extra_path = Path(extra)
        if extra_path.is_dir():
            inner_parts.extend(["--add-dir", str(extra_path)])
    for raw in append_system_prompt_paths or []:
        if raw is None:
            continue
        p = Path(raw)
        if p.is_file():
            inner_parts.extend(["--append-system-prompt-file", str(p)])
    # Optional ``--model`` override (alias `opus`/`sonnet`/`haiku` or full id).
    # Without this, claude-cli falls back to whatever the user's default is —
    # which made all cron fires route through opus regardless of the job's
    # `action.model` field (UAT 2026-05-16).
    if isinstance(model, str) and model.strip():
        inner_parts.extend(["--model", model.strip()])

    # Billing guard: the interactive (subscription) path must NEVER carry a
    # flag that forces programmatic / API billing. A hard raise (not assert —
    # asserts are stripped under `python -O`) for this billing-critical check.
    for _flag in ("--bare", "-p", "--print"):
        if _flag in inner_parts:
            raise ValueError(
                f"refusing to spawn interactive claude with {_flag!r} "
                f"(forces API / programmatic billing); inner argv={inner_parts!r}"
            )

    outer: list[str] = [
        _tmux_bin(),
        "-L",
        TMUX_SOCKET,
        "new-session",
        "-d",
        "-s",
        session_name,
        "-x",
        str(SPAWN_PANE_W),
        "-y",
        str(SPAWN_PANE_H),
        "-c",
        str(cwd),
        # Subscription routing — see module docstring.
        "-e",
        "ANTHROPIC_API_KEY=",
        # PTY rendering — see module docstring.
        "-e",
        "TERM=xterm-256color",
    ]
    # Per-session env for the `artifact` CLI skill (HD_SESSION_ID, HD_LIB_ID,
    # HD_NOTIFY_URL, HD_ARTIFACT_TOKEN_FILE). Appended as additional `-e K=V`
    # pairs CONTIGUOUS with the block above so `SubprocessTmuxRunner.spawn`'s
    # "last -e" outer/inner boundary scan still lands after them, leaving the
    # inner claude argv intact. Keys/values must not be literally "-e".
    for key, value in (env_extra or {}).items():
        # Defense-in-depth: env_extra is emitted AFTER the `-e ANTHROPIC_API_KEY=`
        # scrub above, so a scope `.env` carrying a billing-forcing key would
        # otherwise OVERRIDE the scrub and force API/credit-pool billing on the
        # subscription path. Callers pre-scrub (orchestrator_env), but never
        # trust that here — this is the billing-critical boundary.
        if key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            continue
        outer.extend(["-e", f"{key}={value}"])
    # Inner claude argv stays as individual tokens at the tail so tests +
    # callers can introspect each flag without re-parsing a joined string.
    # ``SubprocessTmuxRunner.spawn`` shell-quotes + joins right before exec
    # because tmux concatenates trailing tokens with spaces under /bin/sh -c.
    return outer + inner_parts


# ── trust pre-seed ─────────────────────────────────────────────────


def ensure_cwd_trusted(cwd: Path) -> None:
    """Pre-seed claude config so first-spawn doesn't pop trust/onboarding pickers.

    Writes (idempotent, atomic) to whichever .claude.json claude itself will
    read — ``$CLAUDE_CONFIG_DIR/.claude.json`` when the env var is set
    (hetzner systemd does this), ``~/.claude.json`` otherwise. Without the
    env-aware path, the seed landed in the WRONG file and dashboard spawns
    on Hetzner hung forever at the theme picker (UAT 2026-05-15).

    Seeds three flags:

    * Per-project ``hasTrustDialogAccepted=True`` — kills the workspace
      trust dialog claude pops the first time it's invoked in a new cwd.
    * Per-project ``hasCompletedProjectOnboarding=True`` — kills the
      "tips for getting started" walkthrough.
    * Global ``hasCompletedOnboarding=True`` — kills the theme + login
      pickers that claude re-runs whenever ``lastOnboardingVersion`` lags
      behind the running binary.

    Preserves all sibling keys so we don't clobber the user's existing
    history / preferences. Safe to call on every spawn.
    """
    real = os.path.realpath(str(cwd))
    cfg = _CLAUDE_CONFIG
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
        cfg.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, dict):
        data = {}
    # Track whether anything actually changes — on a re-spawn the three flags
    # are already set, so we can skip the tempfile + fsync entirely (the common
    # case). This shortens how long the caller holds the pool lock.
    changed = False
    # Global onboarding flag — claude re-prompts the theme picker whenever
    # this is absent or False, regardless of `lastOnboardingVersion`.
    if data.get("hasCompletedOnboarding") is not True:
        data["hasCompletedOnboarding"] = True
        changed = True
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
        changed = True
    entry = projects.get(real)
    if not isinstance(entry, dict):
        entry = {}
        projects[real] = entry
        changed = True
    if entry.get("hasTrustDialogAccepted") is not True:
        entry["hasTrustDialogAccepted"] = True
        changed = True
    # Skip the per-project "tips for getting started" panel on first spawn.
    if entry.get("hasCompletedProjectOnboarding") is not True:
        entry["hasCompletedProjectOnboarding"] = True
        changed = True
    if not changed:
        return
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(cfg.parent),
        prefix=".claude.json.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, cfg)
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


# ── TmuxRunner (injectable shell layer) ────────────────────────────


# ── orphan tmux-server reaping (/proc helpers, Linux-only) ──────────
#
# A dashboard restart that rebinds the `-L hd-orch` socket can strand the
# PREVIOUS tmux server in its user scope — still running, but invisible to
# every `tmux -L hd-orch` client (which only resolves the new socket owner).
# Its claude REPLs (~0.5 GB each) then leak forever. These helpers find such
# off-socket servers at the OS level (the only vantage that sees them) so the
# pool's reaper can reclaim them. All degrade to a no-op off Linux (no /proc).


def _read_proc(pid: int, name: str) -> str | None:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _proc_comm(pid: int) -> str:
    raw = _read_proc(pid, "comm")
    return raw.strip() if raw else ""


def _proc_cmdline(pid: int) -> str:
    raw = _read_proc(pid, "cmdline")
    return raw.replace("\x00", " ").strip() if raw else ""


def _proc_age_s(pid: int) -> float | None:
    """Seconds since ``pid`` started (``/proc/<pid>/stat`` vs ``/proc/uptime``).

    None if unreadable (process gone / non-Linux). The comm field can contain
    spaces and parens, so parse after the final ``)``.
    """
    stat = _read_proc(pid, "stat")
    if not stat:
        return None
    try:
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
        rhs = stat[stat.rindex(")") + 1:].split()
        starttime_ticks = float(rhs[19])  # /proc stat field 22 → index 19 post-')'
        return uptime - (starttime_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def _find_hd_orch_servers() -> list[int]:
    """PIDs of daemonized ``tmux -L hd-orch`` servers owned by our uid.

    Identified STRUCTURALLY — ``comm == 'tmux: server'`` AND the ``-L hd-orch``
    socket in the cmdline — so an off-socket orphan (unreachable via any tmux
    client) is still found. Returns [] off Linux.
    """
    if not os.path.isdir("/proc"):
        return []
    uid = os.getuid()
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        if _proc_comm(pid) != "tmux: server":
            continue
        toks = _proc_cmdline(pid).split()
        if "-L" in toks and TMUX_SOCKET in toks:
            out.append(pid)
    return out


def _descendant_claude_pids(root: int) -> list[int]:
    """All descendant PIDs of ``root`` whose cmdline is a ``claude`` process."""
    if not os.path.isdir("/proc"):
        return []
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = _read_proc(pid, "stat")
        if not stat:
            continue
        try:
            ppid = int(stat[stat.rindex(")") + 1:].split()[1])  # field 4 → index 1
        except (ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    out: list[int] = []
    queue = list(children.get(root, []))
    while queue:
        pid = queue.pop()
        if "claude" in _proc_cmdline(pid):
            out.append(pid)
        queue.extend(children.get(pid, []))
    return out


class TmuxRunner(Protocol):
    """Shell wrapper boundary so unit tests can use an in-memory fake.

    Production implementation (:class:`SubprocessTmuxRunner`) shells out to
    real ``tmux``; tests inject a fake that just records calls and updates an
    in-memory ``live`` set.
    """

    async def spawn(self, name: str, cmd: list[str]) -> None: ...
    async def kill_session(self, name: str) -> None: ...
    async def has_session(self, name: str) -> bool: ...
    async def list_sessions(self) -> list[str]: ...
    async def send_prompt(self, name: str, text: str) -> None: ...
    async def paste_text(self, name: str, text: str) -> None:
        """Paste text into the pane WITHOUT submitting (no Enter)."""
        ...
    async def send_exit(self, name: str) -> None: ...
    async def send_enter(self, name: str) -> None:
        """Press Enter in the pane WITHOUT pasting any text.

        Used by :meth:`wait_until_ready` to dismiss onboarding pickers
        (theme, login method, etc.) that claude pops on a fresh-version
        first-spawn even when ``hasCompletedOnboarding=True`` in
        ~/.claude.json.
        """
        ...
    async def capture_pane(self, name: str) -> str: ...
    async def wait_until_ready(self, name: str) -> bool:
        """Block until claude's REPL is accepting input.

        Returns ``True`` once the readiness marker is observed, ``False`` if
        the wait times out. Implementations are expected to poll
        ``capture_pane`` for any substring in :data:`READINESS_MARKERS`.
        """
        ...

    async def pane_in_mode(self, name: str) -> bool:
        """True when the pane is in copy-mode (the user scrolled into history)."""
        ...

    async def cancel_copy_mode(self, name: str) -> None:
        """Exit copy-mode → snap to the live bottom. No-op when not in a mode."""
        ...

    async def set_mouse(self, name: str, on: bool) -> None:
        """Toggle tmux mouse-mode (off → native xterm selection for copying)."""
        ...


class SubprocessTmuxRunner:
    """Production TmuxRunner — talks to real ``tmux`` on the dedicated socket."""

    async def _tmux(self, *args: str, check: bool = True) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            _tmux_bin(),
            "-L",
            TMUX_SOCKET,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise RuntimeError(f"tmux {args!r} failed (rc={proc.returncode}): {stderr}")
        return proc.returncode or 0, stdout, stderr

    async def spawn(self, name: str, cmd: list[str]) -> None:
        """Exec ``cmd`` (outer tmux argv + inner claude argv as separate tokens).

        tmux concatenates the trailing inner tokens via /bin/sh -c, so any
        path with whitespace would be split. We pre-quote each inner token
        (the segment after the final ``-e`` flag pair) with ``shlex.quote``
        before joining it into a single trailing argv element.

        Critical: stdout/stderr go to DEVNULL, NOT PIPE. ``tmux new-session
        -d`` is a client invocation that talks to (or spawns) a long-lived
        daemon server. If we use PIPE, the daemon inherits our stdout/stderr
        FDs at server-start time and never closes them — ``proc.communicate()``
        would then block forever waiting for EOF on pipes the tmux daemon
        keeps alive for its entire lifetime. Observed during Hetzner UAT:
        first dashboard spawn after dashboard restart hung indefinitely.
        DEVNULL gives the daemon a closed-immediately FD and lets the client
        process exit cleanly.
        """
        import shlex

        boundary = 0
        for i in range(len(cmd) - 1, -1, -1):
            if cmd[i] == "-e":
                boundary = i + 2
                break
        outer = cmd[:boundary]
        inner = cmd[boundary:]
        if inner:
            quoted_inner = " ".join(shlex.quote(tok) for tok in inner)
            base_argv = [*outer, quoted_inner]
        else:
            base_argv = outer

        async def _exec(argv: list[str], env: dict[str, str] | None) -> int:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            return await proc.wait()

        # Detached sessions (default ON): launch the tmux SERVER inside a
        # transient user-manager scope so it lives in /user.slice/… — a
        # sibling of this systemd service, untouched by `systemctl restart`.
        # Empty prefix ⇒ legacy in-cgroup spawn (escape unavailable / flag
        # off). The XDG_RUNTIME_DIR overlay lets systemd-run reach the user
        # bus; we layer it onto the inherited env rather than replacing it.
        prefix, env_overlay = _user_scope_prefix()
        if prefix:
            rc = await _exec(
                [*prefix, *base_argv], {**os.environ, **(env_overlay or {})}
            )
            if rc != 0:
                # The user-scope wrapper failed at RUNTIME despite the pre-flight
                # checks passing (transient D-Bus / user-manager hiccup, scope
                # error). Degrade to a direct in-cgroup spawn so the turn gets a
                # working — if non-restart-surviving — session instead of a hard
                # failure. `new-session` is idempotent-safe here: a non-zero rc
                # means the session wasn't created, so the retry can't dup it.
                print(
                    f"[orchestrator_tmux] scope-wrapped spawn for {name!r} failed "
                    f"(rc={rc}); retrying in-cgroup (won't survive restart)"
                )
                rc = await _exec(base_argv, None)
        else:
            rc = await _exec(base_argv, None)
        if rc != 0:
            raise RuntimeError(
                f"tmux spawn for {name!r} failed (rc={rc})"
            )
        # Disable the SECONDARY prefix (prefix2) for this session only. A common
        # ~/.tmux.conf sets `prefix2 C-a` (for nested-tmux), which on the orchestrator
        # pane swallows Ctrl+A before it reaches claude's input — so readline's
        # "jump to start of line" never fires. We unset prefix2 just here; the
        # PRIMARY prefix (C-Space, used by the split/kill/zoom chord buttons) and
        # the user's other tmux sessions are untouched. Best-effort.
        await self._tmux("set-option", "-t", name, "prefix2", "None", check=False)

    async def kill_session(self, name: str) -> None:
        await self._tmux("kill-session", "-t", name, check=False)

    async def has_session(self, name: str) -> bool:
        rc, _, _ = await self._tmux("has-session", "-t", name, check=False)
        return rc == 0

    async def server_pid(self) -> int | None:
        """PID of the tmux server currently OWNING the ``-L hd-orch`` socket.

        ``None`` if the socket isn't answering (no server, or a transient
        restart window). This is the authoritative "live server" signal the
        orphan-server reaper trusts: any OTHER ``hd-orch`` server is off-socket
        and therefore unreachable. ``display-message`` never auto-starts a
        server (unlike ``new-session``), so a None result is safe to fail
        closed on.
        """
        rc, out, _ = await self._tmux("display-message", "-p", "#{pid}", check=False)
        if rc != 0:
            return None
        try:
            return int(out.strip())
        except ValueError:
            return None

    async def list_sessions(self) -> list[str]:
        """Live session names on our socket.

        ``tmux list-sessions`` exits non-zero in TWO very different cases that
        must NOT be conflated: (a) the benign "no server running" / missing
        socket — there genuinely are zero sessions, so return ``[]``; (b) a real
        probe error (socket contention, fork/EAGAIN under load, a momentarily
        unreachable per-user bus on the systemd-run scope) — here ``[]`` would be
        a LIE that callers reconciling against "live" sessions read as "every
        session is dead". So a non-benign failure RAISES, letting reconcilers
        (``tmux_pool_snapshot_live``) fall back to an unfiltered snapshot instead
        of blanking the whole agent strip for a poll. ``recover_orphans`` already
        wraps this in try/except (→ ``[]``), so the raise is safe there too.
        """
        rc, stdout, stderr = await self._tmux(
            "list-sessions", "-F", "#{session_name}", check=False
        )
        if rc == 0:
            return [line.strip() for line in stdout.splitlines() if line.strip()]
        # Benign "there is no server" signatures → genuinely empty, not an error.
        low = stderr.lower()
        benign = ("no server running", "no such file or directory", "error connecting")
        if any(sig in low for sig in benign):
            return []
        raise RuntimeError(f"tmux list-sessions failed (rc={rc}): {stderr.strip()[:200]}")

    async def paste_text(self, name: str, text: str) -> None:
        """Paste ``text`` into the pane WITHOUT submitting (no Enter).

        Uses ``load-buffer + paste-buffer -p -d`` so multi-line content +
        special characters (e.g. ``[choice:id=value]``, bracketed-paste
        markers) are preserved verbatim instead of being interpreted as
        keystrokes. The text lands at claude's input prompt and waits for the
        user to edit / submit — the server-side equivalent of typing into the
        live terminal (used by the gallery's "Skomentuj": drop an artifact
        path in without sending). ttyd reflects the pane, so this works
        regardless of the browser xterm's mount/readiness timing.
        """
        buf_name = f"in-{name}"
        # load-buffer takes stdin via `-` so we can stream arbitrary bytes.
        loader = await asyncio.create_subprocess_exec(
            _tmux_bin(),
            "-L",
            TMUX_SOCKET,
            "load-buffer",
            "-b",
            buf_name,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await loader.communicate(text.encode("utf-8"))
        if loader.returncode != 0:
            raise RuntimeError(
                f"tmux load-buffer failed (rc={loader.returncode}): "
                f"{stderr_b.decode('utf-8', errors='replace')}"
            )
        await self._tmux("paste-buffer", "-b", buf_name, "-p", "-d", "-t", name)

    async def send_prompt(self, name: str, text: str) -> None:
        """Send ``text`` to the claude REPL in tmux session ``name`` + submit.

        Pastes via :meth:`paste_text`, then a short gap so the paste buffer
        fully renders into the pane before the ``Enter`` keystroke submits it.
        """
        await self.paste_text(name, text)
        # Small gap before Enter so paste buffer fully renders into the pane.
        await asyncio.sleep(0.1)
        await self._tmux("send-keys", "-t", name, "Enter")

    async def send_exit(self, name: str) -> None:
        """Type ``/exit`` + Enter to let claude shut itself down gracefully."""
        await self.send_prompt(name, "/exit")
        # Best-effort wait for the session to die.
        deadline = asyncio.get_running_loop().time() + TEARDOWN_GRACE_S
        while asyncio.get_running_loop().time() < deadline:
            if not await self.has_session(name):
                return
            await asyncio.sleep(0.1)
        await self.kill_session(name)

    async def capture_pane(self, name: str) -> str:
        """Snapshot the FULL pane (scrollback + visible) for readiness polling.

        Default ``capture-pane -p`` only returns the currently-visible screen.
        Empirically (UAT 2026-05-15): claude's TUI renders banners + the
        ``⏵⏵ auto mode on`` status line at the top of the pane, then often
        scrolls it off-screen via subsequent redraws. ``capture-pane -p``
        then returns 49 blank lines — readiness check never matches the
        marker even though claude is truly idle. ``-S -`` starts from the
        oldest line in scrollback, ``-E -`` ends at the most recent — the
        full transcript visible to our text search.
        """
        rc, stdout, _ = await self._tmux(
            "capture-pane", "-p", "-S", "-", "-E", "-", "-t", f"{name}:0.0",
            check=False,
        )
        return stdout if rc == 0 else ""

    async def send_enter(self, name: str) -> None:
        await self._tmux("send-keys", "-t", name, "Enter", check=False)

    async def pane_in_mode(self, name: str) -> bool:
        """``#{pane_in_mode}`` is 1 while tmux copy-mode is active (scrolled up)."""
        rc, stdout, _ = await self._tmux(
            "display-message", "-p", "-t", name, "#{pane_in_mode}", check=False
        )
        return rc == 0 and stdout.strip() == "1"

    async def cancel_copy_mode(self, name: str) -> None:
        """``-X cancel`` exits copy-mode → live bottom. When the pane isn't in a
        mode tmux no-ops with rc!=0 and types NOTHING into the pane, so
        ``check=False`` makes this safe to call unconditionally."""
        await self._tmux("send-keys", "-t", name, "-X", "cancel", check=False)

    async def set_mouse(self, name: str, on: bool) -> None:
        """Turn tmux mouse-mode on/off for the session. With mouse OFF a browser
        drag does a native xterm text selection (drag-to-copy a URL/token)
        instead of being captured by tmux's copy-mode."""
        await self._tmux("set-option", "-t", name, "mouse", "on" if on else "off", check=False)

    async def wait_until_ready(self, name: str) -> bool:
        """Poll capture-pane until claude's REPL is past onboarding + accepting input.

        Two things can keep claude from being ready:

        1. **PTY init race** — claude prints its banners and the "❯" input
           arrow appears EARLY in boot, before the input layer is wired up.
           Sending a prompt at this point lands in a buffer claude isn't
           reading; the paste gets clobbered by claude's own post-boot
           prompt reset and the input box ends up empty. The
           ``⏵⏵ auto mode on`` status row is the LAST thing claude paints
           and a reliable "truly idle" signal.

        2. **Onboarding pickers** — on a fresh-version first-spawn (and
           sometimes when claude detects a "new" terminal characteristic
           even with ``hasCompletedOnboarding=True``), claude pops a chain
           of TUI pickers: theme picker, login-method picker, etc. The
           user already completed onboarding system-wide, so we
           auto-confirm each picker's default by sending Enter once it
           shows up, rate-limited so we don't out-type claude's redraw.

        Returns ``True`` once the readiness marker is observed.
        Falls back to :data:`READINESS_MARKER_FALLBACK` (``❯``) if the
        primary marker doesn't appear within ``READINESS_TIMEOUT_S``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READINESS_TIMEOUT_S
        # Onboarding pickers we know how to dismiss with Enter alone. Each
        # entry matches text claude renders in the picker. The OAuth
        # "Paste code here" picker is intentionally absent — we can't fill
        # an OAuth code; if it appears the user's credentials are broken
        # and the spawn will time out so the user sees a clear failure
        # rather than us mashing Enter into a paste prompt forever.
        picker_markers = (
            "Choose the text style",
            "Select login method",
            "Press Enter to continue",
            "Yes, I trust this folder",  # defence-in-depth, normally pre-seeded
        )
        last_enter_at = 0.0
        enters_sent = 0
        while loop.time() < deadline:
            pane = await self.capture_pane(name)
            if any(m in pane for m in READINESS_MARKERS):
                return True
            now = loop.time()
            on_picker = any(m in pane for m in picker_markers)
            # Rate-limit Enters to one per 1.5s and cap total to 6 so a
            # genuinely-stuck pane can't loop forever.
            if on_picker and (now - last_enter_at) > 1.5 and enters_sent < 6:
                await self.send_enter(name)
                last_enter_at = now
                enters_sent += 1
            await asyncio.sleep(READINESS_POLL_INTERVAL_S)
        # Primary marker timed out — try the fallback once before giving up.
        pane = await self.capture_pane(name)
        return READINESS_MARKER_FALLBACK in pane


# ── slot + pool ────────────────────────────────────────────────────


@dataclass
class TmuxSlot:
    session_id: str
    session_name: str
    cwd: Path
    last_touched_at: float = field(default_factory=time.monotonic)
    # Wall-clock-independent creation stamp (monotonic) for uptime display;
    # unlike last_touched_at it is never bumped, so now - spawned_at is the
    # slot's true age.
    spawned_at: float = field(default_factory=time.monotonic)
    # When set, this slot has been bumped out of the top-N hot ring by newer
    # acquires and is scheduled for eviction at ``monotonic >= evict_at``.
    # Re-acquiring this session_id promotes it back to hot (sets to None).
    evict_at: float | None = None


class TmuxPool:
    """LRU-bounded pool of long-lived tmux+claude slots."""

    def __init__(
        self,
        *,
        pool_size: int = 4,
        idle_ttl_s: float = 600.0,
        runner: TmuxRunner | None = None,
        persistent_ids: set[str] | None = None,
    ) -> None:
        self.pool_size = max(1, int(pool_size))
        self.idle_ttl_s = float(idle_ttl_s)
        self._runner: TmuxRunner = runner or SubprocessTmuxRunner()
        # Session IDs the user marked "keep-alive": exempt from idle eviction
        # AND from the LRU hot-ring cutoff (they don't push others into
        # cooldown). Tracked at the SESSION level — not on TmuxSlot — so the
        # flag survives a slot being killed + respawned. Seeded from the meta
        # sidecar at pool construction; toggled live via ``set_persistent``.
        self._persistent_ids: set[str] = set(persistent_ids or ())
        # Order matters: dict preserves insertion order, so the FIRST entry
        # is the LRU and the LAST is the MRU. `acquire` re-inserts on touch.
        self._slots: dict[str, TmuxSlot] = {}
        self._lock = asyncio.Lock()
        self._evictor_task: asyncio.Task[None] | None = None
        self._reap_task: asyncio.Task[None] | None = None
        # Session IDs currently in their Phase 2 readiness wait (post-spawn,
        # pre-commit). A second concurrent `acquire(session_id=X)` would not
        # see X in `_slots` yet AND must NOT spawn a duplicate `hd-X` — tmux
        # rejects duplicate session names and the latent slot would leak.
        # Each entry maps to an Event the second caller awaits; the first
        # caller fires it after commit (or eviction on failure). Production
        # callers go through `_post_message_handler`'s in-flight check so
        # this is defence-in-depth for future internal callers.
        self._acquiring: dict[str, asyncio.Event] = {}

    # ── lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the background idle-evictor (called from app startup)."""
        if self._evictor_task is not None and not self._evictor_task.done():
            return
        self._evictor_task = asyncio.create_task(self._evict_loop())
        if self._reap_task is None or self._reap_task.done():
            self._reap_task = asyncio.create_task(self._reap_loop())

    async def shutdown(self, *, kill_sessions: bool = True) -> None:
        """Cancel the evictor and clear the slot registry.

        ``kill_sessions=True`` (default) also ``/exit``s + ``kill-session``s
        every slot — the legacy behavior, and what an actual teardown wants.
        ``kill_sessions=False`` (detached-sessions mode, passed from the app
        lifespan) leaves the underlying tmux/claude REPLs RUNNING so they
        outlive a dashboard restart; only the in-memory pool state is dropped,
        and the next process re-attaches via :meth:`acquire`.
        """
        if self._evictor_task is not None:
            self._evictor_task.cancel()
            try:
                await self._evictor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._evictor_task = None
        if self._reap_task is not None:
            self._reap_task.cancel()
            try:
                await self._reap_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reap_task = None
        async with self._lock:
            slots = list(self._slots.values())
            self._slots.clear()
        if not kill_sessions:
            return
        for slot in slots:
            await self._teardown(slot.session_name)

    # ── acquire / release ──────────────────────────────────────────

    def has_warm_slot(self, session_id: str) -> bool:
        """Quick check for whether a session_id is already pooled.

        Callers (notably :class:`TmuxClaudeRunner`) use this BEFORE
        ``acquire`` to decide whether to emit a ``spawning`` SSE event —
        warm slots return in milliseconds, while a cold start can take
        10-20 s and needs UI feedback. Read-only, no lock needed because
        ``dict.__contains__`` is atomic in CPython.
        """
        return session_id in self._slots

    async def has_live_slot(self, session_id: str) -> bool:
        """Like :meth:`has_warm_slot` but verifies the tmux session is ALIVE.

        ``has_warm_slot`` is pure in-memory (``session_id in _slots``) and goes
        stale when a slot's tmux dies out-of-band (user scope torn down, claude
        ``/exit``, OOM, manual kill) while the dashboard keeps running — the
        slot lingers "warm" forever and ensure/paste no-op onto a dead window
        (``no such window: hd-<id>``). This probes the authoritative
        ``tmux has-session`` signal, which is attach-agnostic, so a LIVE
        detached survivor (#119, alive-but-unattached) still counts as live.
        Read-only: it does NOT drop a dead slot — :meth:`acquire` is the
        authoritative self-heal that pops + re-spawns. Callers use this to
        decide whether a real (cold) spawn is needed.
        """
        slot = self._slots.get(session_id)
        if slot is None:
            return False
        return await self._runner.has_session(slot.session_name)

    async def live_session_ids(self) -> set[str]:
        """Session-ids whose ``hd-<id>`` tmux session is ACTUALLY alive right now.

        Probes the authoritative ``tmux list-sessions`` on our socket (not the
        in-memory ``_slots`` dict, which over-reports a hot/persistent slot whose
        REPL died out-of-band — the idle evictor never reaps those). Callers
        reconcile a snapshot against this so the UI (agent tabs, session-list
        dots) only ever shows agents backed by a live tmux session. Read-only:
        does NOT mutate ``_slots`` — ``acquire`` remains the authoritative
        self-heal. One ``tmux list-sessions`` fork; lock-free.

        Caveat: this confirms the *tmux session* is alive, not that the *claude
        REPL* inside the pane is healthy — a slot acquired with
        ``wait_ready=False`` (terminal-open fast path) is reported live the
        instant tmux exists, even if claude dies seconds later. That window is
        tiny and self-heals on the next ``acquire``.

        Raises (does NOT swallow) on a genuine probe error — ``list_sessions``
        only returns ``[]`` for a benign "no server" — so reconcilers can tell a
        confirmed-empty pool (fold every stale slot) from a transient tmux
        hiccup (leave the snapshot unfiltered).
        """
        names = await self._runner.list_sessions()
        out: set[str] = set()
        for name in names:
            if isinstance(name, str) and name.startswith(SESSION_PREFIX):
                sid = name[len(SESSION_PREFIX):]
                if sid:
                    out.add(sid)
        return out

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
        wait_ready: bool = True,
    ) -> TmuxSlot:
        """Return a slot for ``session_id``, spawning if needed.

        If the pool is at capacity AND ``session_id`` is new, the LRU slot
        is evicted to make room.

        ``append_system_prompt_paths`` + ``add_dirs`` are forwarded to
        :func:`build_spawn_cmd` ONLY on a fresh spawn — once a slot is warm,
        the long-lived claude process already has its flags baked in and we
        can't change them without restarting (which would lose conversation
        context). Re-acquires of an existing slot silently ignore newly
        passed paths. A future phase can invalidate slots when the prompt
        stack changes; for now the slot's TTL eventually picks up edits.
        """
        # Phase 1: lock-only critical section — reserve the slot via the
        # `_acquiring` set so a concurrent acquire of the same session_id
        # waits on our spawn instead of spawning a duplicate; evict LRU if
        # needed; spawn. We release the lock BEFORE wait_until_ready so:
        #   (a) cancellation through the long readiness poll propagates
        #       naturally via asyncio.CancelledError (Phase 2A.1);
        #   (b) a slow cold start of session A doesn't serialize session B's
        #       acquire behind a 60 s lock hold (Phase 1 review item #1).
        async with self._lock:
            existing = self._slots.get(session_id)
            if existing is not None:
                # Self-heal: a tracked slot is only truly "warm" if its tmux
                # session is still alive. `has_session` is attach-agnostic
                # (`tmux has-session`, rc==0), so a LIVE detached survivor
                # (#119, alive but unattached after a restart) still answers
                # True here and is preserved untouched. Only a CONFIRMED-ABSENT
                # session (user scope torn down, claude /exit, OOM, manual kill)
                # falls through: we drop the corpse in-memory (detach semantics
                # — no /exit, no kill) and spawn a fresh `hd-<id>` below instead
                # of handing back a dead window. Without this, ensure/paste
                # no-op'd on the stale slot → "no such window: hd-<id>".
                if await self._runner.has_session(existing.session_name):
                    # Touch + promote out of cooldown if it was scheduled for
                    # eviction (user came back before the grace window expired).
                    existing.last_touched_at = time.monotonic()
                    existing.evict_at = None
                    # Refresh a placeholder cwd: recover_orphans adopts survivors
                    # with cwd=~ (it can't know their dir), so the first caller that
                    # supplies the real cwd fixes snapshot()/init metadata + the
                    # agent label (which falls back to the cwd basename). Only
                    # upgrade FROM the home placeholder so a genuine home-cwd slot
                    # isn't churned.
                    if existing.cwd == Path.home() and cwd != Path.home():
                        existing.cwd = cwd
                    self._slots.pop(session_id)
                    self._slots[session_id] = existing
                    self._mark_cooldowns_locked()
                    return existing
                # Stale slot — tmux is gone. Drop it (in-memory only) and fall
                # through to the reservation + fresh-spawn path below.
                self._slots.pop(session_id, None)
            in_flight = self._acquiring.get(session_id)
            if in_flight is None:
                in_flight = asyncio.Event()
                self._acquiring[session_id] = in_flight
                we_own_spawn = True
            else:
                we_own_spawn = False

        if not we_own_spawn:
            await in_flight.wait()
            async with self._lock:
                committed = self._slots.get(session_id)
                if committed is not None:
                    committed.last_touched_at = time.monotonic()
                    committed.evict_at = None
                    self._slots.pop(session_id)
                    self._slots[session_id] = committed
                    self._mark_cooldowns_locked()
                    return committed
            # Forward every spawn-controlling kwarg — `resume` (added in
            # 6389a58) decides between `--resume <id>` and `--session-id <id>`
            # (claude aborts with "Session ID already in use" if the wrong
            # one is sent for an existing JSONL), and `model` (added in
            # 9683643) emits `--model <alias>`. Both were missed when this
            # fallback path was written in 069b83f. Caught by code review.
            return await self.acquire(
                session_id=session_id,
                cwd=cwd,
                append_system_prompt_paths=append_system_prompt_paths,
                add_dirs=add_dirs,
                resume=resume,
                model=model,
                env_extra=env_extra,
                wait_ready=wait_ready,
            )

        try:
            async with self._lock:
                # NB: we do NOT evict the LRU here anymore — we just spawn the
                # new slot and `_mark_cooldowns_locked` after commit schedules
                # any over-capacity slot for cooldown eviction (idle_ttl_s
                # grace window). User can therefore have arbitrarily many
                # slots alive at once; only `pool_size` are "hot" (no TTL)
                # and the rest are cooling down on a timer.
                session_name = f"{SESSION_PREFIX}{session_id}"
                # Re-attach path (detached-sessions mode): after a dashboard
                # restart the `hd-<id>` session may still be alive (it outlived
                # the old process in its user scope). Adopt it instead of
                # `new-session`, which tmux rejects with "duplicate session".
                # `has_session` never starts a server, so a miss costs nothing
                # and falls through to a fresh spawn below. We do NOT know the
                # survivor's pane state (it could be mid-turn or on a picker if
                # the restart caught it busy), so the readiness handling below
                # is honored just like a fresh spawn — except a survivor is
                # NEVER torn down on a not-ready/cancel (it predates us).
                from . import orchestrator_settings as _settings
                is_reattach = bool(
                    _settings.get_flag("tmux_detached_sessions")
                    and await self._runner.has_session(session_name)
                )
                if not is_reattach:
                    # read_text + json parse + (conditional) fsync atomic write —
                    # off the loop. Kept inside the lock (it read-modify-writes
                    # the shared .claude.json, so concurrent cwds must
                    # serialize); the lock is held across the await, the event
                    # loop is not blocked.
                    await asyncio.to_thread(ensure_cwd_trusted, cwd)
                    cmd = build_spawn_cmd(
                        session_name=session_name,
                        cwd=cwd,
                        session_id=session_id,
                        append_system_prompt_paths=append_system_prompt_paths or [],
                        add_dirs=add_dirs,
                        resume=resume,
                        model=model,
                        env_extra=env_extra,
                    )
                    await self._runner.spawn(session_name, cmd)

            # Phase 2: cancellation-safe readiness wait WITHOUT the pool lock.
            # On CancelledError (or any other failure), tear down the half-
            # spawned tmux session so it doesn't sit orphaned. The slot was
            # NOT inserted into `self._slots` yet — that happens only after
            # readiness confirms.
            #
            # ``wait_ready=False`` skips this poll entirely and commits the
            # slot the instant the tmux session exists. The terminal-open
            # path (/term/ensure) uses it: ttyd attaches to the live pane, so
            # the USER sees claude boot — including the "Resume from summary /
            # full session" picker — and drives it directly. Blocking the
            # iframe on a 60 s readiness poll (which a resume picker never
            # satisfies, since claude is parked on the picker, not the input
            # prompt) was the cause of the "open old session → wait 60 s"
            # stall. Programmatic callers (chat paste) keep wait_ready=True
            # because they push bytes claude must be ready to receive.
            if not wait_ready:
                slot = TmuxSlot(session_id=session_id, session_name=session_name, cwd=cwd)
                async with self._lock:
                    self._slots[session_id] = slot
                    self._mark_cooldowns_locked()
                return slot
            try:
                ready = await self._runner.wait_until_ready(session_name)
            except BaseException:
                # Tear down ONLY a session we spawned this call — NEVER a
                # re-attached survivor (it predates us; killing it would lose
                # the user's running session). CRITICAL: a synchronous
                # `await self._teardown(...)` here would be re-cancelled at its
                # first internal await (CancelledError keeps firing on every
                # yield while the surrounding task is being cancelled), leaving
                # the spawned tmux session orphaned. Fire-and-forget so the
                # teardown completes on the event loop AFTER we re-raise — the
                # strong ref set keeps it alive past GC.
                if not is_reattach:
                    try:
                        td = asyncio.create_task(self._teardown(session_name))
                        _teardown_tasks.add(td)
                        td.add_done_callback(_teardown_tasks.discard)
                    except RuntimeError:
                        # No running loop (extreme edge case — test teardown or
                        # interpreter shutdown). Fall back to a synchronous
                        # blocking kill so we don't leak the tmux session.
                        _sync_kill_session(session_name)
                raise
            if not ready and not is_reattach:
                await self._teardown(session_name)
                raise RuntimeError(
                    f"claude REPL never became ready in tmux session {session_name!r}"
                )
            # A re-attached survivor that didn't paint the readiness marker in
            # time (e.g. mid-generation when the restart hit) is committed
            # anyway — better a possibly-busy REPL the user can re-prompt than
            # killing their session. We still waited out wait_until_ready above,
            # so the common "briefly busy" case resolves before commit.

            # Phase 3: commit the slot under the lock, then re-mark cooldowns:
            # the new slot pushed one older slot out of the top-`pool_size`
            # hot ring → that one now has `evict_at = now + idle_ttl_s`.
            slot = TmuxSlot(session_id=session_id, session_name=session_name, cwd=cwd)
            async with self._lock:
                self._slots[session_id] = slot
                self._mark_cooldowns_locked()
            return slot
        finally:
            # Unblock any caller waiting on the reservation event (success or
            # failure — they re-check `_slots` and retry on miss).
            async with self._lock:
                evt = self._acquiring.pop(session_id, None)
            if evt is not None:
                evt.set()

    async def release(self, session_id: str) -> None:
        """Tear down the slot for ``session_id``. Idempotent."""
        async with self._lock:
            slot = self._slots.pop(session_id, None)
        if slot is not None:
            await self._teardown(slot.session_name)

    async def detach(self, session_id: str) -> None:
        """Drop the in-memory slot for ``session_id`` WITHOUT ``/exit``ing or
        killing the underlying tmux session / claude REPL.

        The ``hd-<id>`` session keeps running detached (same survival mechanism
        as ``shutdown(kill_sessions=False)``) and is re-adopted by
        :meth:`acquire` on reopen. Frees the pool slot only — what "zamknij
        sesję" wants: reclaim a scarce slot without ending the conversation.
        Idempotent.
        """
        async with self._lock:
            self._slots.pop(session_id, None)

    # ── prompt I/O ─────────────────────────────────────────────────

    async def pipe_prompt(self, session_id: str, text: str) -> None:
        """Send ``text`` to the slot's claude REPL. Touches LRU + un-cools."""
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                raise KeyError(f"no acquired slot for session {session_id!r}")
            slot.last_touched_at = time.monotonic()
            slot.evict_at = None  # active use cancels any pending cooldown
            self._slots.pop(session_id)
            self._slots[session_id] = slot
            self._mark_cooldowns_locked()
        await self._runner.send_prompt(slot.session_name, text)

    async def paste_into(self, session_id: str, text: str) -> None:
        """Paste ``text`` into the slot's pane WITHOUT submitting. Touches LRU.

        Powers the gallery "Skomentuj" → terminal flow: the artifact path
        lands at claude's input prompt and waits for the user to add context /
        hit Enter. Raises ``KeyError`` if the session has no acquired slot.
        """
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                raise KeyError(f"no acquired slot for session {session_id!r}")
            slot.last_touched_at = time.monotonic()
            slot.evict_at = None
            self._slots.pop(session_id)
            self._slots[session_id] = slot
            self._mark_cooldowns_locked()
        await self._runner.paste_text(slot.session_name, text)

    async def pane_in_mode(self, session_id: str) -> bool:
        """Whether the session's pane is in tmux copy-mode (scrolled into history).

        Read-only and deliberately does NOT touch the LRU — it's a passive UI
        poll (the terminal scroll-to-bottom FAB), not user activity that should
        keep an idle slot warm. Returns ``False`` when there's no warm slot.
        """
        async with self._lock:
            slot = self._slots.get(session_id)
        if slot is None:
            return False
        return await self._runner.pane_in_mode(slot.session_name)

    async def cancel_copy_mode(self, session_id: str) -> None:
        """Exit copy-mode in the session's pane (snap to the live bottom).

        Raises ``KeyError`` if the session has no acquired slot.
        """
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                raise KeyError(f"no acquired slot for session {session_id!r}")
        await self._runner.cancel_copy_mode(slot.session_name)

    async def set_mouse(self, session_id: str, on: bool) -> None:
        """Toggle tmux mouse-mode for the session's pane (off → drag-to-select).

        Raises ``KeyError`` if the session has no acquired slot.
        """
        async with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                raise KeyError(f"no acquired slot for session {session_id!r}")
        await self._runner.set_mouse(slot.session_name, on)

    # ── eviction ───────────────────────────────────────────────────

    def _is_persistent(self, session_id: str) -> bool:
        """Whether ``session_id`` is keep-alive (exempt from idle eviction)."""
        return session_id in self._persistent_ids

    async def set_persistent(self, session_id: str, persistent: bool) -> bool:
        """Mark a session keep-alive (or clear it) and re-stamp cooldowns.

        Tracked at the session level so it survives slot respawn. Toggling ON
        immediately clears a cooling slot's deadline; toggling OFF re-arms it
        if the slot is over-capacity. Returns whether a LIVE slot currently
        exists for the session (the meta sidecar is the durable record, so a
        not-yet-spawned session can still be marked).
        """
        async with self._lock:
            if persistent:
                self._persistent_ids.add(session_id)
            else:
                self._persistent_ids.discard(session_id)
            self._mark_cooldowns_locked()
            return session_id in self._slots

    async def forget_persistent(self, session_id: str) -> None:
        """Drop keep-alive tracking for a session — call when it is DELETED so a
        dead session's id doesn't linger in ``_persistent_ids``.

        NOT called on a plain slot teardown/``release`` (which also fires
        per-turn): keep-alive is a session-level intent that must survive a
        slot being killed + respawned.
        """
        async with self._lock:
            self._persistent_ids.discard(session_id)

    def _mark_cooldowns_locked(self) -> None:
        """Stamp ``evict_at`` on slots that no longer fit in the top-N hot ring.

        Must be called WITH ``self._lock`` held. The newest ``pool_size``
        slots (by insertion-order MRU position) keep ``evict_at = None`` —
        they live forever. Any slot pushed out of that ring by newer
        acquires gets ``evict_at = now + idle_ttl_s`` if it doesn't already
        have one, starting its cooldown. Slots that re-enter the hot ring
        (e.g. user resumes a cooling session) had ``evict_at`` already
        cleared by ``acquire``; this method is idempotent and won't re-
        stamp them.

        Semantics requested during UAT (2026-05-15):
        > "100 sessions: 96 in cooldown, 4 stay forever"
        > "5th acquire pushes the LRU into a 10-min cooldown"
        """
        now = time.monotonic()
        items = list(self._slots.items())
        # Keep-alive slots never cool down AND don't occupy a coolable hot-ring
        # position — only non-persistent slots compete for the top-N. So the
        # cutoff is computed over non-persistent slots only; a persistent slot
        # is "free" and never pushes a normal slot into cooldown.
        non_persistent = [sid for sid, _ in items if not self._is_persistent(sid)]
        cutoff_idx = max(0, len(non_persistent) - self.pool_size)
        coolable = set(non_persistent[:cutoff_idx])  # LRU over-capacity, non-persistent
        for sid, slot in items:
            if self._is_persistent(sid):
                slot.evict_at = None
            elif sid in coolable:
                if slot.evict_at is None:
                    slot.evict_at = now + self.idle_ttl_s
            else:
                slot.evict_at = None

    async def evict_idle(self) -> None:
        """One sweep: kill every slot whose cooldown window has expired.

        Slots inside the top-``pool_size`` hot ring have ``evict_at = None``
        and are exempt. Slots that were pushed out of the ring have an
        absolute deadline stamped on them; once monotonic-now passes that
        deadline, the slot is torn down.
        """
        now = time.monotonic()
        async with self._lock:
            expired = [
                slot for slot in list(self._slots.values())
                if slot.evict_at is not None and now >= slot.evict_at
                and not self._is_persistent(slot.session_id)  # defensive: never reap keep-alive
            ]
            for slot in expired:
                self._slots.pop(slot.session_id, None)
        for slot in expired:
            await self._teardown(slot.session_name)

    def snapshot(self) -> dict:
        """Diagnostic view of the pool: active slots with age / idle / cooldown.

        Synchronous + lock-free by design (it's polled by the System view
        every few seconds and must never block acquire). Iterating a copy of
        ``_slots.items()`` is atomic enough for a best-effort readout — a slot
        racing in/out mid-snapshot just shows up (or not) on the next poll.

        Slots are sorted youngest-first (smallest uptime). ``evict_in_s`` is
        the seconds until a cooling (over-capacity) slot is torn down; ``None``
        for the ``pool_size`` hot slots that live forever.
        """
        now = time.monotonic()
        slots = []
        for sid, slot in list(self._slots.items()):
            evict_in = None
            if slot.evict_at is not None:
                evict_in = max(0.0, slot.evict_at - now)
            slots.append({
                "session_id": sid,
                "cwd": str(slot.cwd),
                "uptime_s": max(0.0, now - slot.spawned_at),
                "idle_s": max(0.0, now - slot.last_touched_at),
                "cooling": slot.evict_at is not None,
                "evict_in_s": evict_in,
                "persistent": self._is_persistent(sid),
            })
        slots.sort(key=lambda s: s["uptime_s"])
        return {
            "active": len(slots),
            "pool_size": self.pool_size,
            "idle_ttl_s": self.idle_ttl_s,
            "slots": slots,
        }

    async def _evict_loop(self, interval_s: float = 30.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.evict_idle()
                except Exception as exc:  # noqa: BLE001 — sweep must never crash the loop
                    print(f"[orchestrator_tmux] evict_idle raised: {exc}")
        except asyncio.CancelledError:
            return

    async def reap_orphan_servers(self) -> None:
        """Reclaim stranded ``tmux -L hd-orch`` servers and their claude REPLs.

        A restart that rebinds the socket can leave the PREVIOUS tmux server
        running off-socket in its user scope — invisible to ``tmux ls`` and to
        every socket-scoped path, leaking its claude REPLs (~0.5 GB each)
        forever (validated on the box 2026-06-16: 6 orphans / ~4.6 GB). The
        stable ``TMUX_TMPDIR`` pin is the prevention; this is the self-healing
        safety net for any residual / future stranding.

        SAFETY (why this never kills a live session):
        * The LIVE server is the socket owner (``server_pid()``). We exclude it
          and only its subtree, so #119 detached survivors on the live socket
          are structurally untouched.
        * A non-owner ``hd-orch`` server is BY DEFINITION unreachable by the
          dashboard (all access routes through the socket → the owner), so its
          sessions are already inaccessible; only their RAM is reclaimable and
          the JSONL transcript remains the source of truth. Reaping loses
          nothing the user could still reach.
        * Fail-closed: if the socket has no owner (the transient restart window
          that CAUSES the leak), we reap NOTHING — never amplify the bug into a
          kill of a momentarily-unanswering live server.
        * A min-age guard spares a just-spawned server that has not yet bound
          the socket. PID-reuse is re-checked immediately before each signal.
        * Runs only via ``_reap_loop`` after a settle delay (never inside the
          startup adoption window). /proc scans run off-loop via ``to_thread``.
        """
        from . import orchestrator_settings as _settings
        if not _settings.get_flag("tmux_reap_orphan_servers"):
            return
        get_pid = getattr(self._runner, "server_pid", None)
        if get_pid is None:
            return  # fake/unsupported runner (tests)
        live = await get_pid()
        if live is None:
            return  # fail-closed: socket unowned → reap nothing
        servers = await asyncio.to_thread(_find_hd_orch_servers)
        victims: list[tuple[int, list[int]]] = []
        for srv in servers:
            if srv == live:
                continue
            age = _proc_age_s(srv)
            if age is not None and age < TMUX_SERVER_MIN_AGE_S:
                continue  # may be a legit just-born server not yet on-socket
            kids = await asyncio.to_thread(_descendant_claude_pids, srv)
            victims.append((srv, kids))
        if not victims:
            return
        # Re-confirm the live owner is unchanged before signalling anything
        # (guards a restart racing us between enumeration and the kill).
        if (await get_pid()) != live:
            return

        def _still_victim(pid: int, srv: int) -> bool:
            # Re-validate identity immediately before EACH signal (both the
            # SIGTERM and the SIGKILL pass — symmetric): during the grace window
            # a signalled victim can exit and have its PID recycled by the OS
            # onto an unrelated (possibly live) same-uid process. Never signal
            # the live owner; a server pid must still be a tmux server, a child
            # pid must still be a claude.
            if pid == live:
                return False
            if pid == srv:
                return _proc_comm(pid) == "tmux: server"
            return "claude" in _proc_cmdline(pid)

        def _signal(sig: int) -> None:
            for srv, kids in victims:
                for pid in (*kids, srv):
                    if not _still_victim(pid, srv):
                        continue
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass

        _signal(signal.SIGTERM)
        await asyncio.sleep(TEARDOWN_GRACE_S)
        _signal(signal.SIGKILL)
        print(
            f"[orchestrator_tmux] reaper: live={live} "
            f"reaped orphan servers={[s for s, _ in victims]}"
        )

    async def _reap_loop(
        self, interval_s: float = 900.0, initial_delay_s: float = 120.0
    ) -> None:
        """Periodic orphan-server reaper. The initial delay keeps the first
        pass well clear of the startup recover_orphans/adoption window, so the
        socket owner is settled before any reap (see reap_orphan_servers)."""
        try:
            await asyncio.sleep(initial_delay_s)
            while True:
                try:
                    await self.reap_orphan_servers()
                except Exception as exc:  # noqa: BLE001 — never crash the loop
                    print(f"[orchestrator_tmux] reap_orphan_servers raised: {exc}")
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return

    # ── crash recovery ─────────────────────────────────────────────

    async def recover_orphans(self) -> None:
        """Kill every untracked session on our dedicated socket.

        ``-L hd-orch`` is wholly owned by this pool — nothing else in the
        codebase touches it (verified by grep). Anything we didn't put there
        is an orphan: either a leftover ``hd-*`` slot from a previous run
        (dashboard crashed without graceful shutdown) or an accidental
        session created by ``~/.tmux.conf``'s ``new-session -A -s main``
        when our spawn first lights up the tmux server.

        Tracked slots are spared. In production this guard is dead-defensive
        because the only caller is the FastAPI lifespan startup hook, which
        runs strictly before any HTTP handler can invoke ``acquire``. The
        guard exists so a future runtime caller (manual /restart endpoint,
        admin tooling) can't accidentally evict live slots.

        Detached-sessions mode (default) flips the posture for ``hd-*``
        sessions: they're REPLs that DELIBERATELY outlived the previous
        dashboard process (their tmux server lives in a user-manager scope,
        not this service's cgroup). Rather than blindly sparing them — which
        would leak RAM unboundedly, since a survivor the user never re-opens
        would be in neither ``tracked`` nor ``_slots`` and so never reaped —
        we ADOPT each one into ``_slots`` so the normal idle/cooldown evictor
        bounds it exactly like any other slot (keep-alive survivors stay via
        ``_is_persistent``; the rest age out after ``idle_ttl_s``), and an
        explicit close/delete can reach it through ``release``. Non-``hd-``
        junk (notably the ``main`` session ``~/.tmux.conf``'s
        ``new-session -A -s main`` auto-creates) is still swept.
        """
        try:
            sessions = await self._runner.list_sessions()
        except Exception:  # noqa: BLE001 — socket may not exist yet on first boot
            sessions = []
        from . import orchestrator_settings as _settings
        detached = _settings.get_flag("tmux_detached_sessions")
        adopted = False
        async with self._lock:
            tracked = {slot.session_name for slot in self._slots.values()}
            for name in sessions:
                if name in tracked:
                    continue
                if detached and name.startswith(SESSION_PREFIX):
                    session_id = name[len(SESSION_PREFIX):]
                    if session_id:
                        # cwd is display-only for a live slot (snapshot); the
                        # meta sidecar supplies the UI title/lib_id, so home is
                        # fine — acquire() upgrades it on the first re-acquire
                        # with the real cwd. last_touched defaults to now → the
                        # survivor gets a full idle_ttl grace before the evictor
                        # can reap it (user can resume meanwhile).
                        if session_id not in self._slots:
                            self._slots[session_id] = TmuxSlot(
                                session_id=session_id,
                                session_name=name,
                                cwd=Path.home(),
                            )
                            adopted = True
                        continue
                    # Degenerate ``hd-`` with an empty id is not adoptable —
                    # fall through and sweep it like junk (the legacy path
                    # always killed it).
                await self._runner.kill_session(name)
            if adopted:
                # Schedule any over-capacity adopted survivors for cooldown
                # eviction (keep-alive ones are exempt inside this call).
                self._mark_cooldowns_locked()

    # ── teardown helper ────────────────────────────────────────────

    async def _teardown(self, session_name: str) -> None:
        """Best-effort graceful ``/exit`` then ``kill-session`` fallback."""
        try:
            await self._runner.send_exit(session_name)
        except Exception as exc:  # noqa: BLE001 — fall through to kill
            print(f"[orchestrator_tmux] /exit failed on {session_name}: {exc}")
        try:
            if await self._runner.has_session(session_name):
                await self._runner.kill_session(session_name)
        except Exception as exc:  # noqa: BLE001
            print(f"[orchestrator_tmux] kill-session failed on {session_name}: {exc}")
