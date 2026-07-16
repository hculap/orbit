"""Per-session ``ttyd`` subprocess pool for the inline interactive terminal.

Phase B of the migration from the static ``<pre>`` pane preview to a real
xterm.js terminal embedded in the orchestrator panel. Each dashboard chat
session that opens the terminal modal gets its own ``ttyd`` subprocess
bound to a localhost-only port, running

    ttyd ... tmux -L hd-orch attach -t hd-<session_id>

attached to the existing :class:`orchestrator_tmux.TmuxPool` slot. The
FastAPI proxy in :mod:`orchestrator_terminal` reverse-proxies HTTP + the
xterm WebSocket through to that port, keeping everything on a single
origin (the dashboard's nginx/Tailscale gate handles auth — ttyd binds
127.0.0.1 only).

Key invariants:

* **Subscription routing (H11)** — ``ttyd`` just spawns ``tmux attach``;
  the tmux server's pre-existing env (set by :mod:`orchestrator_tmux`)
  carries ``ANTHROPIC_API_KEY=`` so claude keeps routing under the
  interactive subscription. ttyd never sees ANTHROPIC_API_KEY.
* **Localhost only** — every spawn passes ``--interface 127.0.0.1`` so
  the port is never directly reachable from Tailscale; the FastAPI
  proxy is the only ingress.
* **Idle TTL** — ttyd is cheap (~5 MB RSS, single goroutine equivalent
  in C); we keep it warm for ``idle_ttl_s`` after the last client
  disconnect, then SIGTERM. The underlying tmux session is NOT touched
  — re-opening the modal spawns a fresh ttyd that re-attaches.
* **Crash recovery** — at startup, ``recover_orphans()`` greps for any
  ttyd carrying our ``--base-path`` signature and reaps them. Dashboard
  hard-crash + restart no longer leaves zombie ttyds bound to our
  port range.
* **Concurrency** — ``acquire`` uses the same lock + ``_acquiring``
  event reservation pattern as :class:`orchestrator_tmux.TmuxPool` so
  two concurrent calls for the same session_id spawn at most one ttyd.

All shell operations flow through :class:`TtydSpawner`, an injectable
abstraction so unit tests can swap in an in-memory fake.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import shutil
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

# ── constants ──────────────────────────────────────────────────────

# Default port window for per-session ttyds. 100 ports is wildly more
# than a single-user dashboard could ever need (the pool gives up with
# a clear error if exhausted).
DEFAULT_PORT_MIN: int = 7700
DEFAULT_PORT_MAX: int = 7799

# How long ttyd stays warm after the last client disconnect. ttyd's
# RSS is tiny so we err on the side of "instant reopen for the user".
DEFAULT_IDLE_TTL_S: float = 900.0

# Wait at most this long after ``spawn`` for ttyd's listening socket to
# accept connections. ttyd typically binds within ~50 ms; this generous
# cap covers a cold host or stalled binary, while still surfacing a
# clean failure if the binary is broken.
READINESS_TIMEOUT_S: float = 5.0
READINESS_POLL_INTERVAL_S: float = 0.05

# Signature passed via ``--base-path`` lets us pgrep our own ttyd
# processes during orphan recovery without matching anything else.
BASE_PATH_PREFIX: str = "/api/orchestrator/sessions/"
BASE_PATH_SUFFIX: str = "/term"

# xterm.js theme + client options. Forwarded to ttyd via repeated
# ``--client-option key=value`` flags. The theme intentionally tracks
# the dashboard's ``tokens.css`` palette so the embedded terminal feels
# like a native panel rather than a foreign iframe (background matches
# ``--bg``, foreground matches ``--fg``, cursor matches ``--accent``).
# ANSI 0-15 use Tokyo Night-style hues that complement the purple
# accent without clashing with claude's TUI defaults.
_TTYD_THEME: dict[str, str] = {
    "foreground":          "#ebe9e4",   # --fg
    "background":          "#0e0f12",   # --bg
    "cursor":              "#a78bfa",   # --accent (oklch(0.72 0.18 295) ≈ lavender)
    "cursorAccent":        "#0e0f12",
    "selectionBackground": "rgba(167,139,250,0.30)",
    "black":               "#15161e",
    "red":                 "#f7768e",
    "green":               "#9ece6a",
    "yellow":              "#e0af68",
    "blue":                "#7aa2f7",
    "magenta":             "#bb9af7",
    "cyan":                "#7dcfff",
    "white":               "#a9b1d6",
    "brightBlack":         "#414868",
    "brightRed":           "#ff7a93",
    "brightGreen":         "#b9f27c",
    "brightYellow":        "#ff9e64",
    "brightBlue":          "#7da6ff",
    "brightMagenta":       "#bb9af7",
    "brightCyan":          "#0db9d7",
    "brightWhite":         "#c0caf5",
}

# Non-theme xterm.js Terminal options. Stringified by ttyd's CLI parser
# before being passed to ``new Terminal(options)`` on the client side.
# Keep these conservative — fontFamily must match what the OS actually
# has installed (JetBrains Mono is present on the Hetzner box; the
# fallback chain matches the dashboard's own monospace stack).
_TTYD_CLIENT_OPTIONS: dict[str, str | int | bool] = {
    "fontFamily": "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace",
    "fontSize": 13,
    "lineHeight": 1.2,
    "cursorBlink": True,
    "cursorStyle": "bar",
    # Modest scrollback so the user can wheel back a screen or two,
    # but not so much that an initial connect lands the viewport
    # mid-buffer (UAT 2026-05-27: with scrollback=5000 xterm.js
    # stranded the user at line 759/1262, hiding claude's input).
    # We pair this with an explicit scrollToBottom() call from the
    # iframe wrapper on load to anchor the initial viewport.
    "scrollback": 200,
    "macOptionIsMeta": True,  # so option+key emits Esc-prefixed seqs on Mac
    # tmux runs with mouse-mode ON, so a plain drag goes to tmux instead of
    # selecting text. This lets Mac users hold ⌥Option + drag to force a LOCAL
    # xterm selection (to copy a URL/token). Non-Mac already has Shift+drag.
    # (Mouse-only; doesn't affect option+key, which stays Meta via the flag above.)
    "macOptionClickForcesSelection": True,
}

# tmux socket name owned by :mod:`orchestrator_tmux`. Duplicated as a
# string literal here so we don't reach into that module from spawn
# argv construction (importing it would create a circular-import risk
# if tmux ever needs to call back into ttyd code).
TMUX_SOCKET: str = "hd-orch"
TMUX_SESSION_PREFIX: str = "hd-"

# Grace window before falling back from SIGTERM to SIGKILL.
TEARDOWN_GRACE_S: float = 3.0

# Strong refs for fire-and-forget teardown tasks (matches the same set
# in :mod:`orchestrator_tmux` — without this the GC can collect the
# task before the kill flush completes).
_teardown_tasks: set[asyncio.Task[None]] = set()


def _warn(msg: str) -> None:
    """Single hop for all stderr noise so tests can capture / silence."""
    print(f"[orchestrator_ttyd] {msg}", file=sys.stderr)


def _ttyd_bin() -> str:
    """Locate the ttyd binary. Honours $TTYD_BIN for tests + non-standard
    installs. Falls back to ``ttyd`` (PATH) when nothing is resolvable so
    the error surface lands at spawn time with the OS-level "not found"
    message rather than at module import."""
    override = os.environ.get("TTYD_BIN")
    if override:
        return override
    return shutil.which("ttyd") or "ttyd"


def _tmux_bin() -> str:
    """Match :func:`orchestrator_tmux._tmux_bin`'s resolution order so the
    ttyd-spawned tmux client uses the same binary as the pool."""
    return shutil.which("tmux") or "/opt/homebrew/bin/tmux"


# ── argv builder ───────────────────────────────────────────────────


def build_ttyd_argv(
    *,
    session_id: str,
    port: int,
    interface: str = "127.0.0.1",
) -> list[str]:
    """Compose the ttyd invocation that attaches to one tmux session.

    ``--base-path`` carries the session id so reverse-proxy URL paths
    on the FastAPI side line up 1:1 with what ttyd serves internally
    (no path stripping in the proxy).

    ``--writable`` is REQUIRED for the feature to fulfill its purpose
    (typing in the modal to dismiss claude TUI pickers). Without it
    ttyd ignores all client → server bytes.

    ``--check-origin=false`` because the WebSocket Origin header that
    the browser sends is the dashboard's, NOT ttyd's localhost URL;
    ttyd would otherwise reject the upgrade. Safe because ttyd is
    bound to 127.0.0.1 only — the auth boundary is the FastAPI proxy.
    """
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if not (1024 <= port <= 65535):
        raise ValueError(f"port {port} out of range")

    base_path = f"{BASE_PATH_PREFIX}{session_id}{BASE_PATH_SUFFIX}"
    tmux_session = f"{TMUX_SESSION_PREFIX}{session_id}"
    # tmux command chain: set the per-session resize policy BEFORE the
    # attach so the first client's PTY size wins. Without this, a user
    # `.tmux.conf` that sets `window-size manual` or `largest` would
    # keep the pane at its spawn-time 200x50 even when the browser
    # iframe is much smaller, leaving claude's input prompt clipped
    # below the visible area (UAT 2026-05-27).
    #
    # `;` as its own argv element is tmux's command separator (see man
    # tmux, "Commands"). aggressive-resize lets tmux follow the most-
    # recently-active client when multiple are attached — relevant when
    # the user has a parallel ssh tmux client open for debugging.
    tmux_attach_chain = [
        _tmux_bin(), "-L", TMUX_SOCKET,
        "set-option", "-t", tmux_session, "window-size", "latest",
        ";",
        "set-window-option", "-t", f"{tmux_session}:0", "aggressive-resize", "on",
        ";",
        "attach", "-t", tmux_session,
    ]
    argv: list[str] = [
        _ttyd_bin(),
        "--port", str(port),
        "--interface", interface,
        "--base-path", base_path,
        "--writable",
        # Headroom for reconnect overlap: when xterm.js drops + retries
        # WS (network blip, idle timeout, tab refresh), the new attempt
        # can land BEFORE ttyd has reaped the old slot. With --max-clients 1
        # the second WS handshake gets rejected with a malformed close
        # that surfaces as ``did not receive a valid HTTP response`` on
        # the proxy side, sending xterm.js into a permanent reconnect
        # loop. 5 is wildly enough for one user — our proxy still holds
        # at most one upstream WS per browser tab.
        "--max-clients", "5",
        "--terminal-type", "xterm-256color",
        "--check-origin=false",
        "--ping-interval", "30",
    ]
    # Theme + Terminal options forwarded to xterm.js via repeated
    # ``--client-option key=value`` flags. JSON values stay as compact
    # one-liners (no spaces in keys) so ttyd's "split on first =" parser
    # doesn't mishandle them. Booleans are lowercased per xterm.js spec.
    argv.extend(("--client-option", "theme=" + json.dumps(_TTYD_THEME, separators=(",", ":"))))
    for key, value in _TTYD_CLIENT_OPTIONS.items():
        if isinstance(value, bool):
            value_str = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value_str = str(value)
        else:
            value_str = str(value)
        argv.extend(("--client-option", f"{key}={value_str}"))
    argv.extend(tmux_attach_chain)
    return argv


# ── spawner abstraction ────────────────────────────────────────────


class TtydSpawner(Protocol):
    """Indirection over real subprocess exec for testability.

    Production implementation (:class:`SubprocessTtydSpawner`) runs
    ``asyncio.create_subprocess_exec``. Tests use a fake that records
    spawn argv + maintains an in-memory "alive" set.
    """

    async def spawn(self, *, argv: list[str]) -> "TtydProcessHandle": ...
    async def wait_until_listening(
        self, port: int, *, interface: str, base_path: str = "/",
    ) -> bool: ...
    async def list_orphan_pids(self, base_path_substring: str) -> list[int]: ...
    async def kill_pid(self, pid: int) -> None: ...


@dataclass
class TtydProcessHandle:
    """Minimal subset of the asyncio.subprocess.Process surface we depend on.

    Holds the pid + a coroutine to wait on exit + a sync terminator.
    Tests can stub this without instantiating a real subprocess.

    ``stderr_tail`` is a deque populated by the spawner's drain task
    with the last few KB of ttyd's stderr — surfaced via :func:`is_alive`
    + watchdog log when ttyd dies unexpectedly. Empty by default in the
    fake handle used in unit tests.
    """
    pid: int
    process: asyncio.subprocess.Process | None  # None in fakes
    stderr_tail: collections.deque[bytes] = field(default_factory=lambda: collections.deque(maxlen=64))  # up to ~4 KB

    def is_alive(self) -> bool:
        """True iff the spawn process hasn't exited.

        Conservative: a missing ``process`` (fakes / restored orphans
        we don't own) is treated as "alive" so we don't accidentally
        evict slots whose state we can't introspect.
        """
        proc = self.process
        if proc is None:
            return True
        return proc.returncode is None

    def stderr_tail_text(self) -> str:
        """Best-effort UTF-8 decode of the captured stderr ring buffer."""
        if not self.stderr_tail:
            return ""
        return b"".join(self.stderr_tail).decode("utf-8", errors="replace")

    async def terminate_and_wait(self, *, grace_s: float = TEARDOWN_GRACE_S) -> None:
        """SIGTERM the process; if still alive after ``grace_s``, SIGKILL.

        Idempotent on already-exited processes (terminate() raises
        ProcessLookupError which we swallow). Always returns once the
        process is reaped or proven dead.
        """
        proc = self.process
        if proc is None:
            return
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_s)
            return
        except asyncio.TimeoutError:
            pass
        # Escalate to SIGKILL — ttyd ignored or was busy in SIGTERM handler.
        try:
            proc.kill()
        except ProcessLookupError:
            return
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=grace_s)


class SubprocessTtydSpawner:
    """Production spawner — talks to a real ttyd binary via ``asyncio``.

    stdin/stdout are pinned to ``DEVNULL`` (same pattern as
    :class:`SubprocessTmuxRunner` — long-lived daemons inheriting pipe
    FDs cause ``proc.communicate()`` to block forever).

    stderr is captured to a per-handle ring buffer via a background
    drain task so :class:`TtydPool`'s watchdog can surface the tail
    when ttyd dies unexpectedly. UAT 2026-05-27: silent ttyd deaths
    cost ~30 min of debugging before we realized the binary was
    actually exiting — the readable stderr would have surfaced
    "lws_socket_bind" / signal traces instantly.
    """

    async def spawn(self, *, argv: list[str]) -> TtydProcessHandle:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"ttyd binary not found: {argv[0]!r}. "
                f"Install via `brew install ttyd` (Mac) or `apt install ttyd` (Debian)."
            ) from exc
        handle = TtydProcessHandle(pid=proc.pid, process=proc)
        # Fire-and-forget stderr drain so the pipe never fills (would
        # block ttyd on write). On EOF the task exits cleanly.
        asyncio.create_task(_drain_stderr_into_handle(proc, handle))
        return handle

    async def wait_until_listening(
        self, port: int, *, interface: str, base_path: str = "/",
    ) -> bool:
        """Delegate to the module-level probe — exposed via the protocol
        so fakes can short-circuit it without a real socket."""
        return await _wait_until_listening(
            port, interface=interface, base_path=base_path,
        )

    async def list_orphan_pids(self, base_path_substring: str) -> list[int]:
        """Return PIDs of any ttyd processes carrying our ``--base-path``.

        Uses ``pgrep -af`` (full command line match). Missing pgrep on
        the host (e.g. minimal CI image) returns an empty list rather
        than raising — orphan recovery is best-effort.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-af", base_path_substring,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return []
        stdout_b, _ = await proc.communicate()
        if proc.returncode not in (0, 1):  # 1 = no match, both fine
            return []
        pids: list[int] = []
        for line in stdout_b.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pids.append(int(parts[0]))
            except ValueError:
                continue
        return pids

    async def kill_pid(self, pid: int) -> None:
        """SIGTERM a PID we don't own a handle for (orphan reaper path).

        Best-effort: missing PID = already dead = fine.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)


# ── port allocator ─────────────────────────────────────────────────


async def _drain_stderr_into_handle(
    proc: asyncio.subprocess.Process,
    handle: TtydProcessHandle,
) -> None:
    """Pump ttyd's stderr into the handle's ring buffer until EOF.

    Reads in 64-byte chunks (each chunk is appended as one deque
    element with maxlen=64 → ~4 KB total) so we keep the tail of any
    error trace ttyd prints just before exiting. Never raises — a
    broken pipe / closed transport just ends the loop.
    """
    if proc.stderr is None:
        return
    try:
        while True:
            chunk = await proc.stderr.read(64)
            if not chunk:
                return
            handle.stderr_tail.append(chunk)
    except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
        return
    except Exception as exc:  # noqa: BLE001 — never crash the drain
        _warn(f"stderr drain for pid={proc.pid} raised: {exc}")


def _is_port_free(port: int, interface: str = "127.0.0.1") -> bool:
    """Probe ``interface:port`` by attempting to bind a transient socket.

    Returns True iff the bind succeeded — meaning nothing else is
    listening. The probe socket is closed immediately so there's no
    TIME_WAIT race; the next caller (ttyd itself) will bind cleanly.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((interface, port))
        except OSError:
            return False
    return True


async def _wait_until_listening(
    port: int,
    *,
    interface: str = "127.0.0.1",
    base_path: str = "/",
    timeout_s: float = READINESS_TIMEOUT_S,
    poll_interval_s: float = READINESS_POLL_INTERVAL_S,
) -> bool:
    """Poll ``interface:port`` until ttyd answers a full HTTP request.

    Sends a tiny HTTP/1.0 GET and waits for ``HTTP/1.`` in the response
    line, then closes cleanly. Materially safer than a bare TCP
    connect+close: ttyd 1.7.4 + libwebsockets treats a TCP probe that
    disconnects mid-handshake as a half-broken client, and (under some
    Hetzner conditions) self-exits ~50 ms later. UAT 2026-05-27: the
    earlier connect+close probe consistently caused ttyd to die between
    `acquire` and the subsequent proxy connect — pool returned a port
    pointing at a dead process. Replacing the probe with a request +
    status-line read leaves ttyd in a normal "served one client" state.

    Returns True once any 1xx/2xx/3xx/4xx HTTP response is received,
    False on timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    path = base_path if base_path.endswith("/") else base_path + "/"
    request = (
        f"GET {path} HTTP/1.0\r\n"
        f"Host: 127.0.0.1\r\n"
        f"User-Agent: ttyd-pool-readiness/1\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(interface, port)
        except OSError:
            await asyncio.sleep(poll_interval_s)
            continue
        try:
            writer.write(request)
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            ok = status_line.startswith(b"HTTP/1.")
        except (asyncio.TimeoutError, OSError, ConnectionResetError):
            ok = False
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if ok:
            return True
        await asyncio.sleep(poll_interval_s)
    return False


# ── slot + pool ────────────────────────────────────────────────────


@dataclass
class TtydSlot:
    """One running ttyd subprocess pinned to one dashboard session."""
    session_id: str
    port: int
    handle: TtydProcessHandle
    last_touched_at: float = field(default_factory=time.monotonic)


class TtydPool:
    """LRU-by-touch pool of long-lived ttyd subprocesses.

    Unlike :class:`orchestrator_tmux.TmuxPool` we don't enforce a hard
    capacity (ttyd is cheap; the port range is the natural cap). Slots
    age out purely by ``idle_ttl_s`` since their last touch.
    """

    def __init__(
        self,
        *,
        idle_ttl_s: float = DEFAULT_IDLE_TTL_S,
        port_range: tuple[int, int] = (DEFAULT_PORT_MIN, DEFAULT_PORT_MAX),
        interface: str = "127.0.0.1",
        spawner: TtydSpawner | None = None,
    ) -> None:
        self.idle_ttl_s = float(idle_ttl_s)
        lo, hi = port_range
        if lo > hi or lo < 1024 or hi > 65535:
            raise ValueError(f"invalid port range {port_range!r}")
        self.port_range = (int(lo), int(hi))
        self.interface = interface
        self._spawner: TtydSpawner = spawner or SubprocessTtydSpawner()

        self._slots: dict[str, TtydSlot] = {}
        self._lock = asyncio.Lock()
        # Reservation events so concurrent acquires of the same session_id
        # don't double-spawn — same shape as TmuxPool._acquiring.
        self._acquiring: dict[str, asyncio.Event] = {}
        self._evictor_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the background idle-evictor + liveness watchdog. Idempotent."""
        if self._evictor_task is None or self._evictor_task.done():
            self._evictor_task = asyncio.create_task(self._evict_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def shutdown(self) -> None:
        """Stop background tasks and SIGTERM every live ttyd slot."""
        for task_attr in ("_evictor_task", "_watchdog_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, task_attr, None)
        async with self._lock:
            slots = list(self._slots.values())
            self._slots.clear()
        for slot in slots:
            await self._teardown_slot(slot)

    # ── acquire / release ──────────────────────────────────────────

    def is_warm(self, session_id: str) -> bool:
        """Read-only check — has this session already got a live ttyd?"""
        return session_id in self._slots

    async def acquire(self, *, session_id: str) -> int:
        """Return the ttyd port for ``session_id``, spawning if needed.

        Concurrency: two concurrent acquires for the same session_id
        will at most spawn one ttyd. The second caller waits on the
        in-flight reservation event, then sees the committed slot.

        Spawn failures (binary missing, port allocation exhausted, OS
        denied bind) raise — callers in the proxy turn that into HTTP
        503 so the frontend can surface a useful error.
        """
        async with self._lock:
            existing = self._slots.get(session_id)
            if existing is not None:
                # Liveness check: if the ttyd we previously spawned has
                # exited (crash, signal, OOM, etc.) the slot is stale —
                # drop it and fall through to a fresh spawn. Without this
                # guard the pool happily hands out a dead port forever
                # because the slot dict only sees acquire/release events.
                if existing.handle.is_alive():
                    existing.last_touched_at = time.monotonic()
                    return existing.port
                rc = (
                    existing.handle.process.returncode
                    if existing.handle.process is not None else None
                )
                tail = existing.handle.stderr_tail_text().strip()
                _warn(
                    f"acquire: slot {session_id!r} ttyd dead "
                    f"(pid={existing.handle.pid}, rc={rc}); respawning. "
                    f"stderr tail: {tail!r}"
                )
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
                    return committed.port
            # First spawner failed before committing — retry as a fresh
            # owner. Same recursive-fallback shape as TmuxPool.acquire.
            return await self.acquire(session_id=session_id)

        try:
            port = self._allocate_port_locked_free()
            argv = build_ttyd_argv(
                session_id=session_id, port=port, interface=self.interface,
            )
            handle = await self._spawner.spawn(argv=argv)
            # Block until ttyd has actually started accepting connections.
            # Sends a real HTTP request rather than a bare TCP probe — see
            # _wait_until_listening docstring for the ttyd 1.7.4 race
            # that motivated that change. Targets the session's own
            # --base-path so ttyd's routing logic exercises a normal
            # served request.
            base_path = f"{BASE_PATH_PREFIX}{session_id}{BASE_PATH_SUFFIX}/"
            ready = await self._spawner.wait_until_listening(
                port, interface=self.interface, base_path=base_path,
            )
            if not ready:
                tail = handle.stderr_tail_text().strip()
                try:
                    await handle.terminate_and_wait()
                except Exception as exc:  # noqa: BLE001
                    _warn(f"teardown after readiness timeout failed: {exc}")
                raise RuntimeError(
                    f"ttyd did not bind 127.0.0.1:{port} within "
                    f"{READINESS_TIMEOUT_S}s for session {session_id!r}. "
                    f"stderr tail: {tail!r}"
                )
            # Belt-and-suspenders: verify the process is STILL alive
            # after the readiness probe. ttyd 1.7.4 has a rare path where
            # libwebsockets crashes between accept and reply, so the
            # probe succeeded but the process is already gone.
            if not handle.is_alive():
                rc = handle.process.returncode if handle.process else None
                tail = handle.stderr_tail_text().strip()
                raise RuntimeError(
                    f"ttyd died during readiness probe on port {port} "
                    f"(rc={rc}) for session {session_id!r}. stderr tail: {tail!r}"
                )
            slot = TtydSlot(session_id=session_id, port=port, handle=handle)
            async with self._lock:
                self._slots[session_id] = slot
            return port
        finally:
            async with self._lock:
                evt = self._acquiring.pop(session_id, None)
            if evt is not None:
                evt.set()

    async def release(self, session_id: str) -> None:
        """SIGTERM the slot for ``session_id``. Idempotent."""
        async with self._lock:
            slot = self._slots.pop(session_id, None)
        if slot is not None:
            await self._teardown_slot(slot)

    def touch(self, session_id: str) -> None:
        """Mark a slot as recently used (proxy calls this on every request).

        Lock-free because dict get is atomic in CPython and we tolerate
        a stale read here — worst case the evictor kills a slot at the
        same millisecond the user reconnects, and the next request
        spawns a fresh ttyd. That's fine.
        """
        slot = self._slots.get(session_id)
        if slot is not None:
            slot.last_touched_at = time.monotonic()

    # ── port allocation ────────────────────────────────────────────

    def _allocate_port_locked_free(self) -> int:
        """Find the lowest unused port in our range and return it.

        We don't hold the pool lock here — port probing involves blocking
        socket binds and we already serialize spawns of the same
        session_id via ``_acquiring``. Different session_ids racing for
        ports is fine: the OS bind is the source of truth, and a brief
        TOCTOU window between probe and ttyd start would surface as a
        clean ttyd error on the losing side (it would retry via
        ``acquire`` again).
        """
        in_use = {slot.port for slot in self._slots.values()}
        lo, hi = self.port_range
        for port in range(lo, hi + 1):
            if port in in_use:
                continue
            if _is_port_free(port, self.interface):
                return port
        raise RuntimeError(
            f"ttyd port range {lo}-{hi} exhausted "
            f"({len(in_use)} slots in pool, {hi - lo + 1} ports total)"
        )

    # ── eviction ───────────────────────────────────────────────────

    async def evict_idle(self) -> None:
        """One sweep: SIGTERM every slot idle past ``idle_ttl_s``."""
        now = time.monotonic()
        async with self._lock:
            expired = [
                slot for slot in list(self._slots.values())
                if now - slot.last_touched_at >= self.idle_ttl_s
            ]
            for slot in expired:
                self._slots.pop(slot.session_id, None)
        for slot in expired:
            await self._teardown_slot(slot)

    async def _evict_loop(self, interval_s: float = 30.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.evict_idle()
                except Exception as exc:  # noqa: BLE001 — sweep MUST NOT crash
                    _warn(f"evict_idle raised: {exc}")
        except asyncio.CancelledError:
            return

    # ── liveness watchdog ──────────────────────────────────────────

    async def sweep_dead_slots(self) -> list[str]:
        """One pass: evict slots whose ttyd process is no longer alive.

        Returns the list of session_ids that got reaped (mostly useful
        for tests + log clarity). Reused by both the periodic watchdog
        and the proxy's startup hook so an iframe re-load on a stale
        slot triggers an immediate respawn rather than waiting up to
        ``_watchdog_loop`` interval.
        """
        evicted: list[str] = []
        async with self._lock:
            for sid, slot in list(self._slots.items()):
                if not slot.handle.is_alive():
                    rc = (
                        slot.handle.process.returncode
                        if slot.handle.process is not None else None
                    )
                    tail = slot.handle.stderr_tail_text().strip()
                    _warn(
                        f"watchdog: evicting dead slot {sid!r} "
                        f"(pid={slot.handle.pid}, rc={rc}). "
                        f"stderr tail: {tail!r}"
                    )
                    self._slots.pop(sid, None)
                    evicted.append(sid)
        return evicted

    async def _watchdog_loop(self, interval_s: float = 30.0) -> None:
        """Periodic liveness sweep so the iframe auto-recovers from a
        ttyd crash without waiting for the next user-driven acquire."""
        try:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.sweep_dead_slots()
                except Exception as exc:  # noqa: BLE001 — never crash the loop
                    _warn(f"watchdog sweep raised: {exc}")
        except asyncio.CancelledError:
            return

    # ── crash recovery ─────────────────────────────────────────────

    async def recover_orphans(self) -> None:
        """SIGTERM any ttyd carrying our ``--base-path`` signature.

        Defensive against dashboard hard-crash + restart: ttyd
        subprocesses are children of uvicorn, so a clean exit reaps
        them via signal propagation. A SIGKILL of uvicorn (OOM, panic)
        leaves the ttyds dangling. This sweep picks them up.

        Best-effort: missing ``pgrep``, permission errors on the kill,
        etc. all degrade gracefully — the worst case is one stale ttyd
        holding a port until manually reaped, which is harmless
        (loopback only, no auth surface).
        """
        try:
            pids = await self._spawner.list_orphan_pids(BASE_PATH_PREFIX)
        except Exception as exc:  # noqa: BLE001 — diagnostic, best-effort
            _warn(f"recover_orphans pgrep failed: {exc}")
            return
        for pid in pids:
            try:
                await self._spawner.kill_pid(pid)
            except Exception as exc:  # noqa: BLE001
                _warn(f"recover_orphans kill {pid} failed: {exc}")

    # ── teardown helper ────────────────────────────────────────────

    async def _teardown_slot(self, slot: TtydSlot) -> None:
        """Reap one slot's subprocess. Soft-fails."""
        try:
            await slot.handle.terminate_and_wait()
        except Exception as exc:  # noqa: BLE001
            _warn(f"teardown ttyd pid={slot.handle.pid} failed: {exc}")
