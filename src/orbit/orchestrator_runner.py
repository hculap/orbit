"""Orchestrator runner — `claude -p` subprocess wrapper + NDJSON→SSE bridge.

Owns the per-session in-flight `ClaudeRunner` and its private helpers. The
sibling `orchestrator` module imports from here for its route handlers; this
module owns the `_active_runs` registry so subprocess lifecycle state lives
next to the class that drives it.
"""
from __future__ import annotations
import asyncio
import collections
import json
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import orchestrator_artifacts as artifacts_mod
from .public_url import public_link
from . import orchestrator_env as env_mod
from . import orchestrator_events as events_mod
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from . import orchestrator_notifications as notifs_module
from . import orchestrator_prompts as prompts_mod
from . import orchestrator_uploads as uploads_mod
from . import secrets_manager as secrets_mgr_mod

# ── constants ──────────────────────────────────────────────────────

HOME: Path = Path(os.environ.get("HOME", str(Path.home())))
PROMPT_PATH: Path = prompts_mod.SYSTEM_PROMPT_PATH
CLAUDE_BIN_DEFAULT: str = "/usr/bin/claude"

TOOL_RESULT_TRUNCATE_BYTES: int = 16 * 1024
STDERR_TAIL_LINES: int = 100
STDERR_EVENT_LINES: int = 30
KEEPALIVE_INTERVAL_S: float = 15.0
CANCEL_GRACE_S: float = 2.0
SUBPROCESS_BUFFER_LIMIT: int = 4 * 1024 * 1024

# Maximum SSE events buffered per turn for replay/resume. Older events are
# silently evicted on overflow — a fresh-connect client (no Last-Event-ID)
# replays from whatever's left and may render an incomplete turn (e.g. miss
# the `init` event). Reconnects WITH Last-Event-ID always get only the newer
# tail, so they're unaffected. ~500 B/event → ~1 MB cap. Bump if pathological
# 50+ tool-call agentic loops become routine.
MAX_BUFFERED_EVENTS: int = 2000

# Grace period after subprocess exit before the runner is dropped from the
# active registry and its event buffer cleared. Lets a late-reconnecting
# client (e.g. tab reopened seconds after envelope-repair completed) still
# subscribe and replay the buffered tail. New turns for the same session
# aren't blocked during this window — the in-flight check looks at _done.
REAP_GRACE_S: float = 60.0

# One in-flight runner per session_id.
_active_runs: dict[str, "ClaudeRunner"] = {}

# Strong references for fire-and-forget push tasks spawned from _finalize.
# Without this set the asyncio loop may garbage-collect the task before
# send_to_all completes (Python's asyncio holds only weak refs to tasks).
# Tasks self-remove via add_done_callback once finished.
_push_tasks: set[asyncio.Task[None]] = set()


# ── turn-lifecycle events (persistent hub) ─────────────────────────
#
# An external orchestrator (e.g. an MCP client) can't watch the ephemeral
# turn /stream — it reaps ~60 s after a turn. So both runners mirror their
# TERMINAL transition onto the persistent SessionEventHub as one of three
# lifecycle events (`turn_started` is emitted once by the dispatcher):
#
#   turn_done  — the turn completed; carries cost/token/duration when claude
#                emitted a `result` event.
#   turn_error — the turn failed/cancelled; carries a message + stderr tail.
#
# These are SPARSE, must-deliver events → published with buffer=True so a
# brief Last-Event-ID reconnect replays a missed terminal frame. The emit is
# wired from inside each runner's single `_done`-guarded `_finalize`, NOT at
# the individual `_broadcast("done"/"error")` call sites: that structurally
# guarantees exactly one terminal event per turn and can't drift when a new
# error branch is added later. Each runner records its terminal `_broadcast`
# in `self._final_event` so `_finalize` knows done-vs-error + the payload.


def transcript_turn_idx(session_id: str) -> int:
    """Highest message ``turn_idx`` in the persisted transcript, or -1.

    The lifecycle/`wait`/pagination contract uses the transcript-global
    ``turn_idx`` (assigned by ``orchestrator_jsonl._build_messages``), NOT a
    runner's per-run ``self._turn_idx`` counter (which resets to 0 each turn).
    Best-effort: any read failure returns -1 so a turn with no assistant
    message yet is ``since_turn``-safe.
    """
    try:
        result = jsonl_mod.read_session(session_id)
        messages = result.get("messages") or []
        return max((int(m.get("turn_idx", -1)) for m in messages), default=-1)
    except Exception:  # noqa: BLE001 — lifecycle emit must never raise
        return -1


def emit_turn_lifecycle(
    session_id: str,
    *,
    final_event: tuple[str, dict[str, Any]] | None,
    cancelled: bool,
    stderr_tail: list[str] | None = None,
) -> None:
    """Map a runner's terminal ``_broadcast`` event → a hub lifecycle event.

    Shared by both ``ClaudeRunner`` and ``TmuxClaudeRunner`` (the latter
    imports this module as ``legacy_runner``) so the done/error mapping lives
    in one place. Best-effort: wrapped so a hub failure can never break
    ``_finalize`` (which still has to release the pool slot + reap the runner).
    """
    try:
        kind, data = final_event if final_event else ("error", {})
        idx = transcript_turn_idx(session_id)
        ts = time.time()
        hub = events_mod.get_hub()
        if kind == "done" and not cancelled:
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            total_tokens = None
            if usage:
                total_tokens = int(usage.get("input_tokens") or 0) + int(
                    usage.get("output_tokens") or 0
                )
            hub.publish(
                session_id,
                "turn_done",
                {
                    "session_id": session_id,
                    "turn_idx": idx,
                    "ts": ts,
                    "cost_usd": data.get("total_cost_usd") or data.get("cost_usd"),
                    "duration_ms": data.get("duration_ms"),
                    "total_tokens": total_tokens,
                    "num_turns": data.get("num_turns"),
                    "reason": data.get("reason") or "completed",
                },
            )
        else:
            message = data.get("message") or ("cancelled" if cancelled else "error")
            hub.publish(
                session_id,
                "turn_error",
                {
                    "session_id": session_id,
                    "turn_idx": idx,
                    "ts": ts,
                    "message": message,
                    "stderr_tail": data.get("stderr_tail") or list(stderr_tail or []),
                },
            )
    except Exception:  # noqa: BLE001 — lifecycle emit must never break finalize
        pass

# In-memory presence map: session_id → {client_id: last_seen_at_epoch_s}.
# Best-effort, intentionally not persisted — used only to suppress chat
# notifications when the user is actively watching a session.
_session_presence: dict[str, dict[str, float]] = {}
_PRESENCE_PRUNE_AFTER_S: float = 60.0
_PRESENCE_WATCH_WINDOW_S: float = 30.0


def record_presence(session_id: str, client_id: str, visible: bool) -> None:
    """Update or clear the presence timestamp for ``client_id`` on this session.

    ``visible=False`` removes that client's heartbeat (e.g. tab hidden); a
    visible heartbeat refreshes ``last_seen_at`` to wall-clock now. Entries
    older than 60 s are pruned on every write so the map can't accumulate.
    """
    if not isinstance(session_id, str) or not session_id:
        return
    if not isinstance(client_id, str) or not client_id:
        return
    now = time.time()
    bucket = _session_presence.get(session_id) or {}
    if visible:
        bucket = {**bucket, client_id: now}
    else:
        bucket = {k: v for k, v in bucket.items() if k != client_id}
    pruned = {k: v for k, v in bucket.items() if now - v <= _PRESENCE_PRUNE_AFTER_S}
    if pruned:
        _session_presence[session_id] = pruned
    else:
        _session_presence.pop(session_id, None)


def is_session_being_watched(session_id: str, within_s: float = _PRESENCE_WATCH_WINDOW_S) -> bool:
    """True iff some client has heartbeated for ``session_id`` within ``within_s``."""
    bucket = _session_presence.get(session_id)
    if not bucket:
        return False
    now = time.time()
    return any((now - ts) <= within_s for ts in bucket.values())


# ── helpers ────────────────────────────────────────────────────────


def _resolve_claude_bin() -> str:
    """Prefer the canonical Hetzner install; fall back to PATH lookup."""
    if Path(CLAUDE_BIN_DEFAULT).exists():
        return CLAUDE_BIN_DEFAULT
    found = shutil.which("claude")
    return found or CLAUDE_BIN_DEFAULT


def build_args(
    session_id: str,
    has_run_before: bool,
    user_text: str,
    model: str | None = None,
    append_system_prompt_paths: list[Path] | None = None,
    extra_prompt_path: Path | None = None,
    agent_skills_dir: Path | None = None,
) -> list[str]:
    """Compose the `claude -p ...` CLI invocation for one turn.

    When ``model`` is a non-empty string, append ``--model <model>`` so
    claude-cli routes the turn to that alias (e.g. ``opus`` / ``sonnet`` /
    ``haiku``) or full model id. ``None`` / empty string → no flag, claude
    picks its built-in default.

    Per-agent prompt stack: ``append_system_prompt_paths`` is the ORDERED list
    of files to forward as ``--append-system-prompt-file`` flags. The caller
    (``_post_message_handler`` via ``agent_prompts.prompts_for_session``)
    decides which layers apply (general / orchestrator / identity / custom).
    Each path is checked at runtime; missing files drop silently so a session
    pointing at a deleted prompt still runs.

    ``extra_prompt_path`` is kept for backwards compatibility with legacy
    sessions whose sidecar still carries a ``extra_prompt_path`` from the
    pre-stack era. When provided AND existing on disk it's appended LAST so
    it overrides nothing earlier in the chain — same as before the rewrite.
    New code paths should pass everything via ``append_system_prompt_paths``.

    ``agent_skills_dir`` (when supplied) is the parent dir of a per-agent
    symlink farm built by :func:`skills_per_agent.build_symlink_farm`. We emit
    a second ``--add-dir`` for it; claude-cli auto-discovers ``.claude/skills/``
    underneath. The legacy ``--add-dir ~/.claude`` is kept verbatim so the
    home config dir stays whitelisted for the .claude/ permission gate.
    """
    binary = _resolve_claude_bin()
    session_flag = "--resume" if has_run_before else "--session-id"
    args = [
        binary,
        "-p",
        user_text,
        session_flag,
        session_id,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        # `--permission-mode auto` (smart auto-approve) rather than
        # `--dangerously-skip-permissions` because the cli applies an
        # extra hardcoded gate to ANY path containing `.claude/` that
        # `--dangerously-skip-permissions` does NOT bypass — verified
        # empirically on 2026-05-05. `auto` is the only mode that lets
        # `mkdir .claude/skills/foo` go through. We pair it with
        # `--add-dir ~/.claude` so the HOME claude config dir is also
        # whitelisted (the same gate fires there even with `auto`).
        "--permission-mode",
        "auto",
        "--add-dir",
        str(Path.home() / ".claude"),
    ]
    if agent_skills_dir is not None:
        skills_path = Path(agent_skills_dir)
        if skills_path.is_dir():
            args.extend(["--add-dir", str(skills_path)])
    for raw in append_system_prompt_paths or []:
        if raw is None:
            continue
        p = Path(raw)
        if p.is_file():
            args.extend(["--append-system-prompt-file", str(p)])
    if extra_prompt_path is not None and Path(extra_prompt_path).is_file():
        args.extend(["--append-system-prompt-file", str(extra_prompt_path)])
    if isinstance(model, str) and model.strip():
        args.extend(["--model", model.strip()])
    return args


def _format_sse(event: str, data: dict[str, Any], seq: int | None = None) -> bytes:
    """Encode a single SSE message: optional `id:` + `event:` + `data:` + blank line."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    head = f"id: {seq}\n" if seq is not None else ""
    return (head + f"event: {event}\ndata: {payload}\n\n").encode("utf-8")


def _truncate_text(text: str, limit: int = TOOL_RESULT_TRUNCATE_BYTES) -> str:
    if not isinstance(text, str):
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="replace") + "\n…[truncated]"


# ── ClaudeRunner ────────────────────────────────────────────────────


class ClaudeRunner:
    """Wraps a single in-flight `claude -p` subprocess for one session."""

    def __init__(
        self,
        session_id: str,
        has_run_before: bool,
        model: str | None = None,
        cwd: Path | None = None,
        append_system_prompt_paths: list[Path] | None = None,
        extra_prompt_path: Path | None = None,
        agent_skills_dir: Path | None = None,
    ) -> None:
        self.session_id: str = session_id
        self.has_run_before: bool = has_run_before
        self.model: str | None = model.strip() if isinstance(model, str) and model.strip() else None
        # Per-agent overrides. ``None`` means "use the legacy global default":
        # cwd → HOME, no append-system-prompt-file flags.
        self._cwd: Path | None = Path(cwd) if cwd is not None else None
        # Ordered list of --append-system-prompt-file targets. Caller resolves
        # the stack via ``agent_prompts.prompts_for_session`` so this class
        # stays oblivious to which layers exist on a given turn.
        self._append_paths: list[Path] = (
            [Path(p) for p in append_system_prompt_paths]
            if append_system_prompt_paths
            else []
        )
        # Legacy session-scoped prompt — still honoured for sessions created
        # before the four-layer stack landed (their sidecar persists this).
        self._extra_prompt_path: Path | None = (
            Path(extra_prompt_path) if extra_prompt_path is not None else None
        )
        # Per-agent skill-farm parent dir (built by skills_per_agent.build_symlink_farm
        # before each spawn). Forwarded to build_args as a second --add-dir so
        # claude-cli auto-loads <that>/.claude/skills/ for this turn only.
        self._agent_skills_dir: Path | None = (
            Path(agent_skills_dir) if agent_skills_dir is not None else None
        )
        self.subscribers: list[asyncio.Queue[bytes | None]] = []
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._done: asyncio.Event = asyncio.Event()
        self._buffered_events: collections.deque[bytes] = collections.deque(maxlen=MAX_BUFFERED_EVENTS)
        self._seq: int = 0
        self._started_at_ms: int = int(time.time() * 1000)
        self._last_result: dict[str, Any] | None = None
        self._last_event_kind: str | None = None
        # The terminal ("done"/"error") event + payload captured in
        # `_broadcast`, consumed once by `_finalize` to emit the persistent
        # turn_done/turn_error lifecycle event for external orchestrators.
        self._final_event: tuple[str, dict[str, Any]] | None = None
        self._partial_text: dict[str, list[str]] = {}
        self._turn_idx: int = 0
        self._cancelled: bool = False
        self._keepalive_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # Tracked across the whole subprocess so the chat→Telegram push can
        # fire ONCE in `_finalize` only when the run actually did meaningful
        # work (any tool_use turn). Without this we'd ping per assistant
        # event in a multi-step agentic session — five tool calls in one
        # user turn would mean five Telegram messages.
        self._any_tool_used: bool = False
        # Most recent assistant text (after envelope normalisation) — used as
        # the body for the chat notification. Captured per-turn but only
        # consumed by `_finalize` so it always reflects the FINAL turn.
        self._last_assistant_text: str = ""

    # ── subscription ──────────────────────────────────────────────

    def subscribe(self, last_event_id: int | None = None) -> asyncio.Queue[bytes | None]:
        """Register a new subscriber queue; replays buffered events.

        When ``last_event_id`` is provided, only events whose ``id:`` is strictly
        greater than that value are replayed — used for SSE EventSource resume
        across client disconnects. Pre-seq events (no ``id:`` line) are skipped
        on resume; the client is assumed to be already past them.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        if last_event_id is None:
            for evt in self._buffered_events:
                queue.put_nowait(evt)
        else:
            for evt in self._buffered_events:
                head, _, _ = evt.partition(b"\n")
                if head.startswith(b"id: "):
                    try:
                        ev_seq = int(head[4:])
                    except ValueError:
                        continue
                    if ev_seq > last_event_id:
                        queue.put_nowait(evt)
        if self._done.is_set():
            queue.put_nowait(None)
        self.subscribers.append(queue)
        return queue

    def status_snapshot(self) -> dict[str, Any]:
        """Lightweight runtime status for the /status poll endpoint."""
        return {
            "in_flight": not self._done.is_set(),
            "started_at_ms": self._started_at_ms,
            "last_seq": self._seq,
        }

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        self._seq += 1
        formatted = _format_sse(event, data, seq=self._seq)
        self._buffered_events.append(formatted)
        self._last_event_kind = event
        if event in ("done", "error"):
            self._final_event = (event, data)
        for q in self.subscribers:
            try:
                q.put_nowait(formatted)
            except asyncio.QueueFull:
                # Unbounded queue — defensive only.
                pass

    def _broadcast_raw(self, raw: bytes) -> None:
        # Used for keepalive comments which are not stored in buffer.
        for q in self.subscribers:
            try:
                q.put_nowait(raw)
            except asyncio.QueueFull:
                pass

    def _close_subscribers(self) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ── lifecycle ─────────────────────────────────────────────────

    def _jsonl_exists(self) -> bool:
        """Return True if the claude-cli transcript JSONL is on disk.

        Path convention: ``~/.claude/projects/<cwd-slug>/<sid>.jsonl`` where
        ``<cwd-slug>`` is ``str(cwd).replace("/", "-")``. We check both the
        runner's configured cwd AND HOME (Global agent slug) because some
        legacy sessions were spawned with cwd=None and ended up under HOME.
        """
        cwd_path = self._cwd if self._cwd is not None and self._cwd.is_dir() else HOME
        candidates = {str(cwd_path), str(HOME)}
        for raw in candidates:
            slug = raw.replace("/", "-")
            jsonl = HOME / ".claude" / "projects" / slug / f"{self.session_id}.jsonl"
            if jsonl.is_file():
                return True
        return False

    async def start_turn(self, user_text: str) -> None:
        """Spawn the subprocess and pump NDJSON → SSE until exit."""
        # Defensive: claude-cli's --resume requires the JSONL transcript at
        # `~/.claude/projects/<cwd-slug>/<sid>.jsonl`. If it's missing
        # (e.g. dashboard sidecar survived a JSONL deletion or the user
        # archived the file out-of-band), --resume crashes with exit code
        # 1 and an opaque "session not found" stderr. Downgrade to
        # --session-id so we start a fresh conversation under the same
        # SID instead of leaving the user staring at "claude exited
        # with code 1".
        has_run_before = self.has_run_before
        if has_run_before and not self._jsonl_exists():
            print(
                f"[orchestrator_runner] JSONL missing for sid={self.session_id}; "
                f"downgrading --resume → --session-id (fresh start)"
            )
            has_run_before = False

        args = build_args(
            self.session_id,
            has_run_before,
            user_text,
            model=self.model,
            append_system_prompt_paths=self._append_paths,
            extra_prompt_path=self._extra_prompt_path,
            agent_skills_dir=self._agent_skills_dir,
        )
        # Pre-create the uploads dir for this session and surface its path
        # as an env var so the agent can persist generated artefacts (images,
        # audio, downloads) without having to guess the session id from
        # `ls -t`. The frontend's image/audio/download blocks resolve `path`
        # against this dir via `/api/orchestrator/uploads/{sid}/{filename}`.
        uploads_dir = uploads_mod.UPLOADS_ROOT / self.session_id
        try:
            uploads_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[orchestrator_runner] failed to pre-create uploads dir {uploads_dir}: {exc}")
        # scrubbed_env() strips ANTHROPIC_API_KEY/AUTH_TOKEN so this `-p` spawn
        # can't be forced onto raw API billing (see orchestrator_env).
        env = env_mod.scrubbed_env({
            "CLAUDE_CONFIG_DIR": str(HOME / ".claude"),
            "ORCHESTRATOR_SESSION_ID": self.session_id,
            "ORCHESTRATOR_UPLOADS_DIR": str(uploads_dir),
            # Let the `artifact` CLI skill auto-discover itself under the
            # programmatic runner too (the tmux runner injects these via -e).
            **artifacts_mod.session_env(
                self.session_id, meta_mod.get_meta(self.session_id).get("lib_id")
            ),
        })
        # Per-agent cwd: the runner inherits the agent's home directory so
        # claude's tool calls (Read/Edit/Bash) operate within that scope. If
        # the configured cwd has been deleted between session create and turn
        # start, fall back to HOME to keep the legacy/global behavior alive.
        cwd_path = self._cwd if self._cwd is not None and self._cwd.is_dir() else HOME

        # Layer scope-local .env onto subprocess env. Secrets the user manages
        # via the dashboard secrets UI live at `~/Areas/<id>/.env` or
        # `~/Projects/<id>/.env`; loading them here is what makes them visible
        # to skill scripts at runtime. Scope-local values win over the
        # systemd-loaded `os.environ` because they're more specific.
        scope_env = cwd_path / ".env"
        if cwd_path != HOME and scope_env.is_file():
            try:
                scope_values, _ = secrets_mgr_mod.parse_env(scope_env)
                # Re-scrub: a user .env must not reintroduce ANTHROPIC_API_KEY.
                env = env_mod.scrubbed_env(scope_values, base=env)
            except Exception as exc:  # noqa: BLE001 — never let a bad .env crash the turn
                print(f"[orchestrator_runner] failed to load {scope_env}: {exc}")
        env_mod.log_billing_path(f"runner sid={self.session_id}", interactive=False)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd_path),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_BUFFER_LIMIT,
            )
        except FileNotFoundError:
            self._broadcast("error", {
                "message": "claude binary not found; check CLAUDE_BIN path",
                "stderr_tail": [],
            })
            self._finalize()
            return
        except OSError as exc:
            self._broadcast("error", {
                "message": f"failed to spawn claude: {exc}",
                "stderr_tail": [],
            })
            self._finalize()
            return

        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            await self._pump_stdout()
            exit_code = await self.proc.wait()
        except asyncio.CancelledError:
            await self._terminate_proc()
            self._broadcast("error", {
                "message": "cancelled",
                "stderr_tail": list(self._stderr_tail)[-STDERR_EVENT_LINES:],
            })
            self._finalize()
            raise
        except Exception as exc:
            self._broadcast("error", {
                "message": f"runner error: {exc}",
                "stderr_tail": list(self._stderr_tail)[-STDERR_EVENT_LINES:],
            })
            self._finalize()
            return

        # Subprocess exited normally.
        natural_finish = False
        if self._cancelled:
            self._broadcast("error", {
                "message": "cancelled",
                "stderr_tail": list(self._stderr_tail)[-STDERR_EVENT_LINES:],
            })
        elif exit_code == 0 and self._last_event_kind == "result":
            payload = self._last_result or {}
            self._broadcast("done", payload)
            natural_finish = True
        elif exit_code == 0:
            self._broadcast("done", {"reason": "exited without result event"})
            natural_finish = True
        else:
            self._broadcast("error", {
                "message": f"claude exited with code {exit_code}",
                "stderr_tail": list(self._stderr_tail)[-STDERR_EVENT_LINES:],
            })

        # Fire-and-forget auto-title regen on every clean turn end. The titles
        # module gates on user-message-count thresholds + manual-title flag,
        # so most calls are no-ops; the actual Haiku call only happens at
        # milestones {1, 5, 25, 100}. Never bubble — title gen failure must
        # not affect the user-facing turn.
        if natural_finish:
            try:
                from . import orchestrator_titles as _titles_mod
                asyncio.create_task(_titles_mod.maybe_generate_title(self.session_id))
            except Exception as exc:  # noqa: BLE001 — defensive
                print(f"[orchestrator_runner] title-gen schedule failed: {exc}")

        self._finalize()

    async def _pump_stdout(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        while True:
            try:
                raw_line = await self.proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                self._stderr_tail.append(f"[parse] readline: {exc}")
                continue
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                self._stderr_tail.append(f"[parse] {exc}: {line[:200]}")
                continue
            self._handle_event(evt)

    async def _drain_stderr(self) -> None:
        assert self.proc is not None
        if self.proc.stderr is None:
            return
        while True:
            try:
                line = await self.proc.stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)

    async def _keepalive_loop(self) -> None:
        try:
            while not self._done.is_set():
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if self._done.is_set():
                    return
                self._broadcast_raw(b": ping\n\n")
        except asyncio.CancelledError:
            return

    # ── event mapping ─────────────────────────────────────────────

    def _handle_event(self, evt: dict[str, Any]) -> None:
        """Translate one Claude stream-json line into SSE event(s)."""
        etype = evt.get("type")
        if etype == "system":
            if evt.get("subtype") == "init":
                self._broadcast("init", {
                    "model": evt.get("model"),
                    "session_id": evt.get("session_id") or self.session_id,
                    "tools": evt.get("tools") or [],
                    "cwd": evt.get("cwd"),
                })
            return
        if etype == "stream_event":
            self._handle_stream_event(evt)
            return
        if etype == "assistant":
            self._handle_assistant(evt)
            return
        if etype == "user":
            self._handle_user(evt)
            return
        if etype == "result":
            self._last_result = {
                "cost_usd": evt.get("total_cost_usd") or evt.get("cost_usd") or 0.0,
                "duration_ms": evt.get("duration_ms") or 0,
                "total_tokens": (evt.get("usage") or {}).get("total_tokens") or 0,
                "num_turns": evt.get("num_turns") or 0,
                "session_id": evt.get("session_id") or self.session_id,
            }
            # Note: `done` is emitted from start_turn() after the proc exits
            # so we keep stderr_tail and exit-code semantics consistent.
            self._last_event_kind = "result"
            return
        # Unknown event type — drop silently.

    def _handle_stream_event(self, evt: dict[str, Any]) -> None:
        inner = evt.get("event") or {}
        sub = inner.get("type")
        if sub == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text") or ""
                if not text:
                    return
                self._broadcast("delta", {
                    "turn_idx": self._turn_idx,
                    "text": text,
                })
            return
        if sub == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._broadcast("tool_start", {
                    "tool_use_id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "input_partial": block.get("input") or {},
                })
            return
        # content_block_stop, message_start, message_stop are not surfaced.

    def _handle_assistant(self, evt: dict[str, Any]) -> None:
        msg = evt.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            return

        # Collect all text fragments from the assistant message; thinking and
        # tool_use blocks are surfaced verbatim alongside.
        text_parts: list[str] = []
        thinking_blocks: list[dict[str, Any]] = []
        tool_use_blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if text:
                    text_parts.append(text)
            elif btype == "thinking":
                thought = block.get("thinking") or block.get("text") or ""
                if thought:
                    thinking_blocks.append({"kind": "thinking", "text": thought})
            elif btype == "tool_use":
                tool_use_blocks.append({
                    "kind": "tool_use",
                    "tool_use_id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                })

        full_text = "".join(text_parts)

        # Always emit thinking + tool_use blocks via the existing
        # `assistant_message` channel — they are not part of the user-facing
        # JSON envelope and the frontend already handles them.
        if thinking_blocks or tool_use_blocks:
            self._broadcast("assistant_message", {
                "turn_idx": self._turn_idx,
                "blocks": thinking_blocks + tool_use_blocks,
            })

        if full_text:
            # Plain-markdown reply (envelope removed — Claude writes prose, not
            # a {"blocks":[…]} JSON object). The frontend's assistant_message →
            # FINALIZE_TURN path renders the markdown directly.
            self._broadcast("assistant_message", {
                "turn_idx": self._turn_idx,
                "blocks": [{"kind": "markdown", "text": full_text}],
            })

        if not (thinking_blocks or tool_use_blocks or full_text):
            return
        self._turn_idx += 1

        # Track tool use + latest text across the run — the chat→Telegram
        # push fires once in ``_finalize`` (not per turn) so a multi-step
        # agentic session sends ONE notification, not one-per-tool-turn.
        if tool_use_blocks:
            self._any_tool_used = True
        if full_text:
            self._last_assistant_text = full_text

    def _handle_user(self, evt: dict[str, Any]) -> None:
        # User events from the stream typically wrap tool_result blocks.
        msg = evt.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            if isinstance(raw, list):
                output = "\n".join(
                    b.get("text", "") for b in raw
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            elif isinstance(raw, str):
                output = raw
            else:
                output = ""
            self._broadcast("tool_result", {
                "tool_use_id": block.get("tool_use_id") or "",
                "stdout": _truncate_text(output),
                "is_error": bool(block.get("is_error", False)),
                "ms": block.get("duration_ms") or 0,
            })

    # ── cancel ────────────────────────────────────────────────────

    async def cancel(self) -> None:
        """SIGTERM, then SIGKILL after grace period."""
        self._cancelled = True
        await self._terminate_proc()

    async def _terminate_proc(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except OSError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=CANCEL_GRACE_S)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            except OSError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass

    # ── teardown ──────────────────────────────────────────────────

    def _spawn_chat_notify(self, full_text: str) -> None:
        """Fire-and-forget push for a tool-completed turn while the user is away.

        Soft-imports the ``notify`` primitive (Agent A) so a missing module
        only logs to stderr_tail instead of breaking the runner.
        """
        try:
            from . import notify as notify_mod
        except Exception as exc:  # noqa: BLE001
            self._stderr_tail.append(f"[notify] notify module unavailable: {exc}")
            return

        session_title = self._derive_session_title() or "Orchestrator"
        body = self._derive_chat_notify_body(full_text)
        title = f"Agent finished — {session_title}"
        click_url = public_link(f"/chat/{self.session_id}")

        async def _dispatch() -> None:
            try:
                await notify_mod.notify(
                    topic="chat",
                    title=title,
                    message=body or "Agent completed a tool-driven turn.",
                    click=click_url,
                    priority=3,
                )
            except Exception as exc:  # noqa: BLE001 — push must never break the runner
                self._stderr_tail.append(f"[notify] chat publish failed: {exc}")

        try:
            task = asyncio.create_task(_dispatch())
            _push_tasks.add(task)
            task.add_done_callback(_push_tasks.discard)
        except RuntimeError as exc:
            self._stderr_tail.append(f"[notify] no running loop for chat notify: {exc}")

    def _derive_session_title(self) -> str | None:
        # Shared precedence (issue #85): manual rename → native Claude Code
        # ai-title → stored title → first-message preview. Reads the (mtime-
        # cached) JSONL summary so a push notification carries the same title
        # the session list shows, not just a hand-set sidecar override.
        try:
            from . import orchestrator_meta as _meta_mod
            from . import orchestrator_jsonl as _jsonl_mod
            meta = _meta_mod.get_meta(self.session_id) or {}
            summary = _jsonl_mod._summary_for(_jsonl_mod.jsonl_path(self.session_id))
            title = _meta_mod.resolve_title(meta, summary)
        except Exception:  # noqa: BLE001
            return None
        return title.strip() or None

    def _derive_chat_notify_body(self, full_text: str) -> str:
        text = (full_text or "").strip()
        if text:
            return text[:120]
        derived = self._notification_title_body()[1]
        return (derived or "")[:120]

    def _notification_title_body(self) -> tuple[str | None, str]:
        """Derive a push title + body from the latest assistant text block.

        Title is always ``"Orchestrator"``; body is the first ~120 chars of the
        most recent assistant text block found in ``_buffered_events``. If no
        text is found (e.g. cancelled or empty turn), return ``(None, "")`` so
        the caller suppresses the push entirely.

        Best-effort — wrapped in try/except; any failure returns ``(None, "")``.
        """
        from . import orchestrator_oneshot as _oneshot
        try:
            for evt in reversed(self._buffered_events):
                # Shared frame decoder — single home for the SSE envelope shape.
                name, payload = _oneshot.decode_sse_frame(evt)
                if payload is None or name not in ("structured_blocks", "assistant_message"):
                    continue
                blocks = payload.get("blocks")
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("kind")
                    if kind in ("markdown", "text"):
                        content = block.get("content") or block.get("text") or ""
                        if isinstance(content, str) and content.strip():
                            body = content.strip()[:120]
                            return ("Orchestrator", body)
            return (None, "")
        except Exception:  # noqa: BLE001 — never let title derivation crash finalize
            return (None, "")

    def _finalize(self) -> None:
        """Mark done, close subscribers, defer registry pop + buffer release.

        Active subscribers get the `None` sentinel via `_close_subscribers`.
        New subscribers in the REAP_GRACE_S window after this point still find
        the runner via `_active_runs.get(...)`, replay the buffered tail, and
        immediately receive `None` (because `_done` is set). This protects
        against late reconnects missing envelope-repair events.
        """
        if self._done.is_set():
            return
        self._done.set()
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        try:
            jsonl_mod.invalidate_cache(self.session_id)
        except Exception:
            pass
        # Emit the persistent turn_done/turn_error lifecycle event for external
        # orchestrators. After invalidate_cache so transcript_turn_idx re-reads
        # the just-written final turn. Best-effort inside the helper.
        emit_turn_lifecycle(
            self.session_id,
            final_event=self._final_event,
            cancelled=self._cancelled,
            stderr_tail=list(self._stderr_tail)[-STDERR_EVENT_LINES:],
        )
        self._close_subscribers()
        # Push notification: best-effort; the frontend SW decides whether to show.
        try:
            title, body = self._notification_title_body()
            if title:
                task = asyncio.create_task(
                    notifs_module.send_to_all(
                        title=title,
                        body=body,
                        data={
                            "session_id": self.session_id,
                            "turn_idx": self._turn_idx,
                        },
                    )
                )
                _push_tasks.add(task)
                task.add_done_callback(_push_tasks.discard)
        except Exception as exc:  # noqa: BLE001 — push must never break finalize
            self._stderr_tail.append(f"[push] _finalize hook raised: {exc}")

        # Mobile (Telegram) chat notification: fire ONCE per run, only when
        # the run actually did work (any tool_use turn) AND nobody is
        # currently watching the session. The body reflects the LAST turn,
        # which for tool-using agents is typically the textual summary.
        if self._any_tool_used and not is_session_being_watched(self.session_id):
            try:
                self._spawn_chat_notify(self._last_assistant_text)
            except Exception as exc:  # noqa: BLE001
                self._stderr_tail.append(f"[notify] chat notify dispatch failed: {exc}")
        # Active subscribers got their None sentinel; clear the list so we
        # don't broadcast to dead queues if anything broadcasts post-finalize.
        self.subscribers.clear()
        # Defer registry pop + buffer release; allow late reconnects to replay.
        try:
            asyncio.get_running_loop().call_later(REAP_GRACE_S, self._reap)
        except RuntimeError:
            self._reap()

    def _reap(self) -> None:
        """Drop runner from registry + release buffer ~REAP_GRACE_S after finalize."""
        if _active_runs.get(self.session_id) is self:
            _active_runs.pop(self.session_id, None)
        self._buffered_events.clear()
