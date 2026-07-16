"""Orchestrator — FastAPI route handlers (routes only).

Drives the Orchestrator chat panel HTTP surface. The subprocess lifecycle
(`ClaudeRunner`, NDJSON→SSE bridge, `_active_runs` registry) lives in the
sibling `orchestrator_runner` module. JSONL transcripts are owned by Claude
itself; this module reads them via `orchestrator_jsonl` and decorates with
sidecar metadata via `orchestrator_meta`.
"""
from __future__ import annotations
import asyncio
import collections
import hmac
import io
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import agent_prompts as agent_prompts_mod
from . import bundled_mcp as bundled_mcp_mod
from . import orchestrator_a2a as a2a_mod
from . import orchestrator_artifacts as artifacts_mod
from . import orchestrator_read_aloud as read_aloud_mod
from . import orchestrator_compact as compact_mod
from . import orchestrator_events as events_mod
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_jsonl_tail as tail_mod
from . import orchestrator_meta as meta_mod
from . import orchestrator_notifications as notifs_module
from . import orchestrator_prompts as prompts_mod
from . import orchestrator_runner as runner
from . import orchestrator_runner_tmux as runner_tmux_mod
from . import orchestrator_session_state as session_state_mod
from . import orchestrator_settings as settings_mod
from . import orchestrator_teleport as teleport_mod
from . import orchestrator_terminal as terminal_mod
from . import orchestrator_terminal_shortcuts as shortcuts_mod
from . import orchestrator_agent_tab_order as tab_order_mod
from . import orchestrator_tmux as tmux_mod
from . import orchestrator_ttyd as ttyd_mod
from . import orchestrator_uploads as uploads_module
from . import orchestrator_voice as voice_mod
from . import orchestrator_tts as tts_mod
from . import orchestrator_search as search_mod
from . import orchestrator_wait as wait_mod
from . import skills_per_agent as skills_per_agent_mod
from .discovery import HOME

_logger = logging.getLogger(__name__)

# Shared TmuxPool instance — started by ``register_routes`` and torn down on
# app shutdown. Lazy-created so importing this module doesn't spawn the
# evictor task before the asyncio loop is running.
_tmux_pool: tmux_mod.TmuxPool | None = None


def _get_tmux_pool() -> tmux_mod.TmuxPool:
    """Get the shared TmuxPool, lazy-creating with current server settings.

    On each call we refresh ``idle_ttl_s`` and ``pool_size`` from the live
    settings file so a PATCH on /api/orchestrator/settings takes effect
    immediately for subsequent eviction sweeps and capacity checks — no
    dashboard restart required.
    """
    global _tmux_pool
    if _tmux_pool is None:
        # Seed keep-alive (persistent) session IDs from the meta sidecar so
        # the flag survives a dashboard restart — the pool exempts these slots
        # from idle eviction.
        try:
            _persistent_ids = {
                sid for sid, m in meta_mod.all_meta().items()
                if isinstance(m, dict) and m.get("persistent")
            }
        except Exception as exc:  # noqa: BLE001 — never block pool creation on this
            print(f"[orchestrator] seeding persistent session ids failed: {exc}", file=sys.stderr)
            _persistent_ids = set()
        _tmux_pool = tmux_mod.TmuxPool(
            pool_size=int(settings_mod.get_flag("pool_size") or 4),
            idle_ttl_s=float(settings_mod.get_flag("pool_idle_ttl_s") or 600),
            persistent_ids=_persistent_ids,
        )
    else:
        # Pick up live edits to the tuning knobs without restarting.
        live_size = settings_mod.get_flag("pool_size")
        live_ttl = settings_mod.get_flag("pool_idle_ttl_s")
        if isinstance(live_size, int) and live_size > 0:
            _tmux_pool.pool_size = live_size
        if isinstance(live_ttl, (int, float)) and live_ttl > 0:
            _tmux_pool.idle_ttl_s = float(live_ttl)
    return _tmux_pool


def tmux_pool_snapshot() -> dict[str, Any]:
    """Diagnostic snapshot of the tmux pool for the System view.

    Reads the existing singleton WITHOUT creating it — if no slot has ever
    been acquired (programmatic mode, terminal never opened) the pool is
    ``None`` and we report an empty pool using the configured tuning knobs,
    so the System view doesn't spawn an evictor-less pool just by polling.
    """
    if _tmux_pool is None:
        return {
            "active": 0,
            "pool_size": int(settings_mod.get_flag("pool_size") or 4),
            "idle_ttl_s": float(settings_mod.get_flag("pool_idle_ttl_s") or 600),
            "slots": [],
        }
    snap = _tmux_pool.snapshot()
    # Enrich each slot with a human title + agent name so the System view
    # and the mobile session switcher show what the session IS, not just
    # its uuid. Title uses the shared `meta_mod.resolve_title` precedence
    # (same as the session list `_decorate_session`): manual rename → native
    # Claude Code ai-title → stored title → first-message preview → empty.
    # Agent name mirrors the chat header
    # (`agentNameRaw`/`agentName` in orchestrator.jsx). list_sessions() is
    # cached (5 s TTL) so this stays cheap on the poll. Best-effort.
    try:
        overlay = meta_mod.all_meta()
        summaries = {s.get("id"): s for s in jsonl_mod.list_sessions()}
        for slot in snap.get("slots", []):
            sid = slot.get("session_id")
            meta = overlay.get(sid) if isinstance(overlay, dict) else None
            summ = summaries.get(sid) or {}
            slot["title"] = meta_mod.resolve_title(meta, summ).strip()[:100]
            lib_id = meta.get("lib_id") if isinstance(meta, dict) else None
            lib_id = lib_id if isinstance(lib_id, str) and lib_id else None
            # Fall back to the cwd when the sidecar has no lib_id, so a legacy
            # cwd-rooted session still groups under its real agent (correct tab
            # + icon) instead of collapsing to Global.
            lib_id = lib_id or _lib_id_from_cwd(slot.get("cwd"))
            slot["agent"] = _agent_name_for(slot.get("cwd"), lib_id)
            # lib_id (e.g. "areas/Work") lets the /agents directory match a
            # slot to its agent card; "" → a Global (cwd-less) session.
            slot["lib_id"] = lib_id or ""
            # updated_at (last-message time) lets the tab strip pick the most
            # RECENT session as the agent's default target, not the least-idle
            # (warmest) one — see groupAgents.
            ts = summ.get("updated_at")
            slot["updated_at"] = float(ts) if isinstance(ts, (int, float)) else 0.0
    except Exception as exc:  # noqa: BLE001 — diagnostics never break the page
        print(f"[orchestrator] tmux snapshot enrich failed: {exc}")
    return snap


async def tmux_pool_snapshot_live() -> dict[str, Any]:
    """Like :func:`tmux_pool_snapshot` but reconciled against LIVE tmux.

    The plain snapshot reads the in-memory ``_slots`` dict, which over-reports a
    hot/persistent slot whose REPL died out-of-band (the idle evictor never
    reaps those, so the slot — and therefore its agent tab + session-list dot —
    lingers as a phantom). This drops any slot whose ``hd-<id>`` tmux session is
    no longer alive, so the UI only shows agents/sessions backed by a real tmux.

    Probe failures (no pool yet, ``tmux list-sessions`` errored) leave the
    snapshot UNFILTERED — diagnostics never hide live slots on a transient tmux
    hiccup; a confirmed-empty live set (genuinely no sessions) correctly folds
    every stale slot away.
    """
    snap = await asyncio.to_thread(tmux_pool_snapshot)
    slots = snap.get("slots") or []
    if not slots or _tmux_pool is None:
        return snap
    try:
        live = await _tmux_pool.live_session_ids()
    except Exception as exc:  # noqa: BLE001 — never hide live slots on a probe error
        print(f"[orchestrator] tmux live reconcile failed: {exc}")
        return snap
    kept = [s for s in slots if s.get("session_id") in live]
    if len(kept) == len(slots):
        return snap
    dropped = [s.get("session_id") for s in slots if s.get("session_id") not in live]
    print(f"[orchestrator] pool snapshot dropped {len(dropped)} dead slot(s): {dropped}")
    return {**snap, "slots": kept, "active": len(kept)}


def _humanize_slug(slug: str) -> str:
    """kebab/snake/camel → 'Title Case' — mirrors orchestrator.jsx agentName."""
    s = re.sub(r"[-_]+", " ", slug or "")
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return " ".join(w[:1].upper() + w[1:] for w in s.split() if w)


def _lib_id_from_cwd(cwd: str | None) -> str | None:
    """Best-effort agent ``lib_id`` from a session cwd, for legacy sessions whose
    sidecar predates lib_id tracking (or was created cwd-only).

    ``~/Areas/Health`` → ``areas/Health``; ``~/Projects/my-project`` →
    ``projects/my-project`` (nested projects keep their relpath). Areas collapse
    to their TOP-LEVEL dir (the area is the whole subtree). Anything outside
    Areas/Projects (home, /tmp, …) → ``None`` so Global stays Global. Without this
    a cwd-rooted session with no lib_id mis-attributes to Global (wrong tab/scope)
    and loses its agent icon.
    """
    if not cwd or not isinstance(cwd, str):
        return None
    try:
        # expanduser() ONLY — deliberately NOT resolve(): PARA areas contain
        # ``~/Areas/<Area>/projects/<X>`` symlinks into ``~/Projects/<X>``;
        # resolving would follow them and mis-file an area-scoped session under
        # the standalone project. A lexical path keeps it under ``areas/<Area>``.
        # (PurePath already collapses '.' and '//'.) Compare against unresolved
        # bases so both sides stay lexical.
        p = Path(cwd).expanduser()
    except (ValueError, TypeError):
        return None
    areas = HOME / "Areas"
    projects = HOME / "Projects"
    try:
        if p == areas or areas in p.parents:
            rel = p.relative_to(areas).parts
            return ("areas/" + rel[0]) if rel else None
        if p == projects or projects in p.parents:
            rel = str(p.relative_to(projects))
            return ("projects/" + rel) if rel != "." else None
    except (ValueError, OSError):
        return None
    return None


def _agent_name_for(cwd: str | None, lib_id: str | None) -> str:
    """Human agent label for a session, from its lib_id (preferred) or cwd.

    ``areas/Work`` → "Work"; ``projects/my-project`` → "My Project".
    Falls back to the cwd basename when there's no lib_id (more useful than
    the chat header's "Global" for a session clearly rooted in a folder),
    and to "Global" for home / no cwd.
    """
    raw: str | None = None
    if lib_id:
        m = re.match(r"^(areas|projects)/(.+)$", lib_id)
        raw = m.group(2).split("/")[-1] if m else lib_id
    elif cwd:
        base = cwd.rstrip("/").split("/")[-1]
        if base and base.lower() not in ("", "home", Path.home().name.lower()):
            raw = base
    if not raw:
        return "Global"
    return _humanize_slug(raw) or "Global"


async def _warm_session_slot(session_id: str, *, wait_ready: bool = True) -> bool:
    """Spawn (or no-op if already warm) the tmux slot for ``session_id``,
    using the exact flags a real message turn / terminal-open would.

    Shared by the ``/term/ensure`` endpoint and the startup pre-warm so the
    long-lived claude carries the same system-prompt / cwd / model context
    either way. Returns True on a cold spawn, False if already warm; raises
    on spawn failure (caller decides how loud to be).

    ``wait_ready=False`` returns as soon as the tmux session exists (skips the
    readiness poll). The terminal-open path uses it so ttyd can attach to the
    live pane immediately and the user drives claude's boot / resume picker
    themselves, instead of blocking the iframe on a 60 s poll a resume picker
    never satisfies.
    """
    pool = _get_tmux_pool()
    # Short-circuit ONLY on a verified-live slot. `has_warm_slot` is pure
    # in-memory (`session_id in _slots`) and cannot tell a live slot from one
    # whose tmux died out-of-band — trusting it made ensure/paste no-op 200 on
    # a stale slot, so ttyd then attached to a missing window
    # ("no such window: hd-<id>"). `has_live_slot` probes `tmux has-session`
    # (attach-agnostic → a live detached survivor still counts), and acquire()
    # below self-heals + re-spawns a confirmed-dead slot.
    if await pool.has_live_slot(session_id):
        return False

    sidecar = meta_mod.get_meta(session_id)
    cwd_str = sidecar.get("cwd") if isinstance(sidecar, dict) else None
    lib_id = (
        sidecar.get("lib_id")
        if isinstance(sidecar, dict) and isinstance(sidecar.get("lib_id"), str)
        else None
    )
    cwd_path = Path(cwd_str) if isinstance(cwd_str, str) and cwd_str else HOME
    model = sidecar.get("model") if isinstance(sidecar, dict) else None
    append_paths = agent_prompts_mod.prompts_for_session(cwd_str, lib_id)

    agent_skills_dir: Path | None = None
    try:
        farm_kind, farm_lib_id = skills_per_agent_mod.resolve_lib_id_from_session(cwd_str, lib_id)
        agent_skills_dir = skills_per_agent_mod.build_symlink_farm(farm_kind, farm_lib_id)
    except Exception as exc:  # noqa: BLE001 — never block warm-up on skills
        print(f"[orchestrator] warm-slot skills farm build failed: {exc}")
    add_dirs = [agent_skills_dir] if agent_skills_dir is not None else None

    # Match the runner's resume check: cwd-aware JSONL lookup (claude
    # organizes transcripts by cwd slug, not bare session id).
    try:
        existing_jsonl = tail_mod.jsonl_path_for(cwd_path, session_id)
        wants_resume = existing_jsonl.is_file()
    except Exception:  # noqa: BLE001 — fall back to fresh session
        wants_resume = False

    await pool.acquire(
        session_id=session_id,
        cwd=cwd_path,
        append_system_prompt_paths=append_paths,
        add_dirs=add_dirs,
        resume=wants_resume,
        model=model,
        env_extra=artifacts_mod.session_env(session_id, lib_id),
        wait_ready=wait_ready,
    )
    return True


# Max simultaneous cold spawns during startup pre-warm. 4 ≈ ~6 GB peak
# (claude idle ~1.3 GB) on the 12 GB box, fills the default 4-slot pool
# in one wave. Higher pool_size still warms, just in waves of this many.
PREWARM_CONCURRENCY = 4


async def prewarm_recent_sessions() -> None:
    """Fill the tmux pool with the most-recently-used sessions at startup.

    Run as a background task from the app lifespan when
    ``pool_prewarm_on_start`` is set, so after an app/server restart the
    first terminal (or interactive chat) open is instant instead of a
    10-20 s cold spawn. Warms up to ``pool_size`` sessions, newest first
    (the most likely to be reopened), CONCURRENTLY so all the hot slots
    fill in roughly one cold-spawn (~30-60 s) instead of stacking
    sequentially (4 × 60 s ≈ 4 min). Concurrency is capped at
    ``PREWARM_CONCURRENCY`` so a large pool_size can't trigger N
    simultaneous claude boots and OOM the box. Best-effort: a session
    whose cwd vanished or whose resume fails is logged and skipped.
    """
    pool = _get_tmux_pool()
    limit = pool.pool_size
    try:
        summaries = jsonl_mod.list_sessions()
    except Exception as exc:  # noqa: BLE001
        print(f"[orchestrator] prewarm: list_sessions failed: {exc}")
        return
    overlay = meta_mod.all_meta()

    def _archived(sid: str) -> bool:
        m = overlay.get(sid)
        return bool(m.get("archived")) if isinstance(m, dict) else False

    recent = [s for s in summaries if s.get("id") and not _archived(s["id"])]
    recent.sort(key=lambda s: -float(s.get("updated_at") or 0.0))
    targets = recent[:limit]
    if not targets:
        return
    print(f"[orchestrator] prewarm: warming {len(targets)} recent session(s)…")
    sem = asyncio.Semaphore(PREWARM_CONCURRENCY)

    async def _warm_one(sid: str) -> None:
        async with sem:
            try:
                spawned = await _warm_session_slot(sid)
                print(f"[orchestrator] prewarm: {sid[:8]} {'spawned' if spawned else 'already warm'}")
            except Exception as exc:  # noqa: BLE001 — skip + continue
                print(f"[orchestrator] prewarm: {sid[:8]} failed: {exc}")

    await asyncio.gather(*[_warm_one(s["id"]) for s in targets])


# Companion pool for the inline interactive terminal. Lazy-created so
# importing this module never spawns the evictor before the asyncio
# loop is running, and never spawns when ``ttyd_enabled=false`` (the
# default — see plan rollback story). The lifespan in ``app.py`` only
# instantiates this pool when the flag is on.
_ttyd_pool: ttyd_mod.TtydPool | None = None


def _get_ttyd_pool() -> ttyd_mod.TtydPool:
    """Get the shared TtydPool, lazy-creating with current server settings.

    Re-reads ``ttyd_idle_ttl_s`` on each call so a PATCH on
    /api/orchestrator/settings takes effect for the next eviction
    sweep without a restart. Port range edits require a restart
    because the pool's slot bookkeeping is keyed off them.
    """
    global _ttyd_pool
    if _ttyd_pool is None:
        port_min = settings_mod.get_flag("ttyd_port_min")
        port_max = settings_mod.get_flag("ttyd_port_max")
        _ttyd_pool = ttyd_mod.TtydPool(
            idle_ttl_s=float(
                settings_mod.get_flag("ttyd_idle_ttl_s")
                or ttyd_mod.DEFAULT_IDLE_TTL_S
            ),
            port_range=(
                int(port_min) if isinstance(port_min, int) else ttyd_mod.DEFAULT_PORT_MIN,
                int(port_max) if isinstance(port_max, int) else ttyd_mod.DEFAULT_PORT_MAX,
            ),
        )
    else:
        live_ttl = settings_mod.get_flag("ttyd_idle_ttl_s")
        if isinstance(live_ttl, (int, float)) and live_ttl > 0:
            _ttyd_pool.idle_ttl_s = float(live_ttl)
    return _ttyd_pool


# Per-session prompt files live here; one .md per session id with extra
# system-prompt text. Created on demand at session-create when an
# `extra_system_prompt` is supplied; unlinked on session delete.
SESSION_PROMPTS_DIR: Path = HOME / ".orchestrator" / "session-prompts"

# Mirror library_files.AGENT_PROMPT_MAX_BYTES — duplicated as a constant here
# so the orchestrator import surface doesn't pull library_files (which itself
# imports from library.py and would tangle the import graph).
EXTRA_PROMPT_MAX_BYTES = 8 * 1024

# Sentinel for the cwd query filter that selects "Global" sessions (those
# whose sidecar carries no cwd, i.e. legacy or non-agent sessions).
GLOBAL_CWD_SENTINEL = "__global__"

_DEVICE_ID_RE = notifs_module.DEVICE_ID_RE


# ── route handlers ─────────────────────────────────────────────────


def _decorate_session(summary: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    """Merge JSONL summary with sidecar overlay; immutable-style new dict."""
    meta = meta or {}
    title = meta_mod.resolve_title(meta, summary)
    raw_pins = meta.get("pinned_turn_idxs") or []
    pinned_turn_idxs = [int(i) for i in raw_pins if isinstance(i, int) and not isinstance(i, bool) and i >= 0]
    cf = meta.get("compacted_from")
    ct = meta.get("compacted_to")
    tf = meta.get("teleported_from")
    raw_model = meta.get("model")
    model = raw_model if isinstance(raw_model, str) and raw_model in meta_mod.ALLOWED_MODELS else None
    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) and meta.get("cwd") else None
    lib_id = meta.get("lib_id") if isinstance(meta.get("lib_id"), str) and meta.get("lib_id") else None
    # Legacy sessions created cwd-only (no lib_id) still resolve to their agent
    # so the chat scope shows e.g. "Health" (+ icon), not "Global".
    lib_id = lib_id or _lib_id_from_cwd(cwd)
    msg_count = int(summary.get("msg_count") or 0)
    raw_lr = meta.get("last_read_msg_count")
    last_read = int(raw_lr) if isinstance(raw_lr, int) and not isinstance(raw_lr, bool) and raw_lr >= 0 else 0
    unread_count = max(0, msg_count - last_read)
    return {
        **summary,
        "title": title,
        "archived": bool(meta.get("archived", False)),
        "pinned": bool(meta.get("pinned", False)),
        "persistent": bool(meta.get("persistent", False)),
        "pinned_turn_idxs": pinned_turn_idxs,
        "compacted_from": cf if isinstance(cf, str) and cf else None,
        "compacted_to": ct if isinstance(ct, str) and ct else None,
        "teleported_from": tf if isinstance(tf, str) and tf else None,
        "model": model,
        "cwd": cwd,
        "lib_id": lib_id,
        "last_read_msg_count": last_read,
        "unread_count": unread_count,
    }


# ---- Git branch cache --------------------------------------------------
# The session list shows the current branch next to each session's cwd as a
# quick "where are you?" indicator. Computing it on every list call would
# fork ``git symbolic-ref`` once per unique cwd per poll (~1s cadence with
# UAT-observed N≈12 active agents → 12 forks/s, ~5 ms each on the box).
# A 5-second TTL collapses that to one fork per cwd per 5 s, picks up
# manual ``git checkout`` within a single refresh interval, and survives
# concurrent list handlers safely (worst case: brief duplicate forks under
# burst — no correctness impact, the cache is a hint not a source of truth).
_BRANCH_CACHE_TTL_S = 5.0
_branch_cache: dict[str, tuple[float, str | None]] = {}


def _branch_for_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    now = time.monotonic()
    cached = _branch_cache.get(cwd)
    if cached is not None and (now - cached[0]) < _BRANCH_CACHE_TTL_S:
        return cached[1]
    # Lazy-import inside the function so a missing ``library_git`` (e.g.
    # during a partial test bootstrap) doesn't crash the module load.
    from . import library_git as _git
    try:
        branch = _git._current_branch(Path(cwd))
    except Exception:
        branch = None
    _branch_cache[cwd] = (now, branch)
    return branch


def _orphan_summary_stub(session_id: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a summary-shaped dict for a session that has a sidecar entry but
    no JSONL transcript yet (just created via POST /sessions, no first turn).

    Matches the keys produced by ``orchestrator_jsonl._build_summary`` so the
    downstream ``_decorate_session`` overlay and the frontend renderer don't
    have to special-case the orphan path. ``updated_at`` reads the sidecar's
    persisted ``created_at`` (stamped once at first ``set_meta`` write) so
    the orphan's sort position is stable across back-to-back list polls —
    previously ``time.time()`` regenerated on every call, which caused
    list-order churn + React key flicker on the sidebar between two rapid
    refreshes. Falls back to 0.0 (sorts to bottom) for entries that pre-
    date the ``created_at`` field — they'll get a stable timestamp on the
    next sidecar mutation.
    """
    ts = 0.0
    if meta:
        ca = meta.get("created_at")
        if isinstance(ca, (int, float)) and ca > 0:
            ts = float(ca)
    return {
        "id": session_id,
        "created_at": ts,
        "updated_at": ts,
        "msg_count": 0,
        "last_user_preview": "",
        "last_role": "",
        "first_user_preview": "",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "last_context_tokens": 0,
        "last_model": "",
        "corpus": "",
    }


async def _list_sessions_handler(
    cwd_filter: str | None = None,
    *,
    include_corpus: bool = False,
) -> list[dict[str, Any]]:
    """GET /api/orchestrator/sessions — JSONL summaries + sidecar overlay.

    Pinned sessions float to the top; ties broken by updated_at desc.

    Sidecar-only "orphan" sessions (created via POST but no JSONL yet,
    because ``claude -p`` only writes the transcript on the first turn) are
    folded in via stub summaries so the UI can resolve them between create
    and first-message.

    When ``cwd_filter`` is provided, only sessions whose sidecar ``cwd``
    matches are returned. The special value ``__global__`` returns sessions
    whose ``cwd is None`` (legacy + non-agent sessions). Any other value is
    matched as an exact string equality against the persisted ``cwd``.

    ``include_corpus`` (default False): strip the heavy ``corpus`` field
    from each session before returning. The field is only useful for the
    client-side MiniSearch index and is typically tens of KB per session;
    the UI fetches it lazily via ``/api/orchestrator/sessions/corpora``
    when the search box is opened.
    """
    # list_sessions() walks every project slug and full-reads each changed
    # JSONL (the active transcript grows on every turn) — off-load so this
    # ~1 s-polled handler never blocks the loop on a cache miss.
    summaries = await asyncio.to_thread(jsonl_mod.list_sessions)
    overlay = meta_mod.all_meta()
    seen_ids = {s["id"] for s in summaries}
    # Fold in sidecar-only sessions (no JSONL yet) so they're visible in the
    # list immediately after POST /sessions, before the user's first turn.
    # Archived orphans stay hidden — same posture as the rest of the list.
    orphan_stubs = [
        _orphan_summary_stub(sid, meta)
        for sid, meta in overlay.items()
        if sid not in seen_ids and not meta.get("archived", False)
    ]
    summaries = [*summaries, *orphan_stubs]
    decorated = [_decorate_session(s, overlay.get(s["id"])) for s in summaries]
    if cwd_filter is not None:
        if cwd_filter == GLOBAL_CWD_SENTINEL:
            decorated = [s for s in decorated if s.get("cwd") is None]
        else:
            # Normalise the filter the same way creates do (tilde + resolve)
            # so `?cwd=~/Areas/Home` matches the canonical persisted form.
            try:
                normalized = str(Path(cwd_filter).expanduser().resolve(strict=False))
            except (OSError, RuntimeError):
                normalized = cwd_filter
            decorated = [s for s in decorated if s.get("cwd") == normalized]
    decorated.sort(key=lambda s: (not s.get("pinned", False), -s.get("updated_at", 0.0)))
    if not include_corpus:
        # Trim the heavy corpus field from the list payload — the UI fetches
        # it lazily via /api/orchestrator/sessions/corpora when search opens.
        for s in decorated:
            if "corpus" in s:
                s["corpus"] = ""
    # Attach the current git branch for each session's cwd. Each lookup forks
    # a blocking `git` once per uncached cwd (TTL cache in _branch_for_cwd),
    # so resolve the UNIQUE cwds CONCURRENTLY off the event loop instead of
    # forking git serially per session on this ~1 s-polled handler.
    unique_cwds = {s.get("cwd") for s in decorated if s.get("cwd")}
    branch_map: dict[str, str | None] = {}
    if unique_cwds:
        cwds = list(unique_cwds)
        branches = await asyncio.gather(
            *(asyncio.to_thread(_branch_for_cwd, c) for c in cwds)
        )
        branch_map = dict(zip(cwds, branches))
    for s in decorated:
        s["git_branch"] = branch_map.get(s.get("cwd"))
    return decorated


async def _list_session_corpora_handler() -> list[dict[str, str]]:
    """GET /api/orchestrator/sessions/corpora — id+corpus pairs only.

    Companion to ``_list_sessions_handler`` for lazy MiniSearch index
    construction. Returned shape: ``[{"id": "<uuid>", "corpus": "<text>"}]``.
    Sidecar-only orphans (no JSONL yet) carry no corpus and are skipped.
    """
    summaries = await asyncio.to_thread(jsonl_mod.list_sessions)
    return [
        {"id": s["id"], "corpus": s.get("corpus") or ""}
        for s in summaries
        if isinstance(s, dict) and s.get("id")
    ]


def _validate_cwd_under_home(raw: str) -> str:
    """Validate that ``raw`` is an absolute path to an existing dir under HOME.

    Mirrors the path-traversal posture of ``library._safe_*_path``: explicit
    rejection of relative paths and ``..`` segments BEFORE resolve(), then a
    post-resolve assertion that the path is contained in HOME.

    Returns the normalized absolute path string. Raises ``ValueError`` on
    every failure mode (caller maps to HTTP 400).
    """
    if not isinstance(raw, str):
        raise ValueError("cwd must be a string")
    cwd_str = raw.strip()
    if not cwd_str:
        raise ValueError("cwd cannot be empty")
    # Accept `~` / `~/...` from clients (the frontend doesn't know HOME).
    # expanduser is a no-op on already-absolute paths.
    cwd_path = Path(cwd_str).expanduser()
    if not cwd_path.is_absolute():
        raise ValueError("cwd must be an absolute path")
    # Reject literal `..` segments BEFORE resolve so a path like
    # /home/user/Areas/../etc never even reaches the filesystem layer.
    if ".." in cwd_path.parts:
        raise ValueError("cwd cannot contain '..' segments")
    try:
        resolved = cwd_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cwd cannot be resolved: {exc}") from exc
    home_resolved = HOME.resolve()
    if resolved != home_resolved and home_resolved not in resolved.parents:
        raise ValueError("cwd must be under HOME")
    if not resolved.is_dir():
        raise ValueError("cwd must be an existing directory")
    return str(resolved)


def _write_extra_prompt(session_id: str, text: str) -> str:
    """Persist the per-session prompt suffix to disk; return its absolute path.

    Caller must have already enforced the size cap. We mkdir -p the parent
    once; the file itself is overwrite-on-create (one prompt per session id).
    """
    SESSION_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    target = SESSION_PROMPTS_DIR / f"{session_id}.md"
    target.write_text(text, encoding="utf-8")
    return str(target)


async def _create_session_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/orchestrator/sessions — generate a UUID; sidecar entry only if title set.

    Body shape (all fields optional):
        title: str | None — sidecar override; if non-empty also sets title_manual.
        cwd: str | None — abs path under HOME; the launched subprocess's cwd.
        lib_id: str | None — informational, e.g. "areas/Home".
        model: str | None — one of ALLOWED_MODELS; validated.
        extra_system_prompt: str | None — ≤8 KB; written to a per-session
            prompt file and the path persisted in sidecar.extra_prompt_path.

    Validation is strict: every field is checked before any disk write so a
    bad payload never produces a half-written session.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(400, detail="title must be a string")

    raw_cwd = payload.get("cwd")
    cwd: str | None = None
    if raw_cwd is not None and not (isinstance(raw_cwd, str) and not raw_cwd.strip()):
        if not isinstance(raw_cwd, str):
            raise HTTPException(400, detail="cwd must be a string or null")
        try:
            cwd = _validate_cwd_under_home(raw_cwd)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    raw_lib_id = payload.get("lib_id")
    lib_id: str | None = None
    if raw_lib_id is not None:
        if not isinstance(raw_lib_id, str):
            raise HTTPException(400, detail="lib_id must be a string or null")
        stripped = raw_lib_id.strip()
        lib_id = stripped or None

    raw_model = payload.get("model")
    model: str | None = None
    if raw_model is not None and not (isinstance(raw_model, str) and not raw_model.strip()):
        if not isinstance(raw_model, str):
            raise HTTPException(400, detail="model must be a string or null")
        candidate = raw_model.strip().lower()
        if candidate not in meta_mod.ALLOWED_MODELS:
            allowed = ", ".join(sorted(meta_mod.ALLOWED_MODELS))
            raise HTTPException(400, detail=f"model must be null or one of: {allowed}")
        model = candidate

    raw_extra = payload.get("extra_system_prompt")
    extra_prompt_text: str | None = None
    if raw_extra is not None:
        if not isinstance(raw_extra, str):
            raise HTTPException(400, detail="extra_system_prompt must be a string or null")
        if raw_extra:
            if len(raw_extra.encode("utf-8")) > EXTRA_PROMPT_MAX_BYTES:
                raise HTTPException(
                    400,
                    detail=f"extra_system_prompt too large (>{EXTRA_PROMPT_MAX_BYTES} bytes)",
                )
            extra_prompt_text = raw_extra

    session_id = str(uuid.uuid4())

    extra_prompt_path: str | None = None
    if extra_prompt_text is not None:
        try:
            extra_prompt_path = _write_extra_prompt(session_id, extra_prompt_text)
        except OSError as exc:
            raise HTTPException(500, detail=f"failed to write session prompt: {exc}") from exc

    # Persist sidecar only when at least one non-default field is supplied.
    # Empty strings on lib_id are silently ignored (treated as None) but
    # explicit non-empty values trigger a write so the GET filter sees them.
    needs_write = any(
        v is not None for v in (title, cwd, lib_id, model, extra_prompt_path)
    ) or bool(title)
    if needs_write:
        await meta_mod.set_meta(
            session_id,
            title=title or None,
            cwd=cwd,
            lib_id=lib_id,
            model=model,
            extra_prompt_path=extra_prompt_path,
        )

    return {
        "ok": True,
        "id": session_id,
        "title": title,
        "cwd": cwd,
        "lib_id": lib_id,
        "model": model,
        "extra_prompt_path": extra_prompt_path,
    }


async def _patch_session_handler(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/orchestrator/sessions/{id} — sidecar mutation only."""
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    title = payload.get("title")
    archived = payload.get("archived")
    pinned = payload.get("pinned")
    persistent = payload.get("persistent")
    pinned_turn_idxs = payload.get("pinned_turn_idxs")
    if title is not None and not isinstance(title, str):
        raise HTTPException(400, detail="title must be a string")
    if archived is not None and not isinstance(archived, bool):
        raise HTTPException(400, detail="archived must be a boolean")
    if pinned is not None and not isinstance(pinned, bool):
        raise HTTPException(400, detail="pinned must be a boolean")
    if persistent is not None and not isinstance(persistent, bool):
        raise HTTPException(400, detail="persistent must be a boolean")
    if pinned_turn_idxs is not None:
        if not isinstance(pinned_turn_idxs, list):
            raise HTTPException(400, detail="pinned_turn_idxs must be a list")
        for item in pinned_turn_idxs:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise HTTPException(400, detail="pinned_turn_idxs entries must be non-negative ints")
    if (title is None and archived is None and pinned is None
            and persistent is None and pinned_turn_idxs is None):
        return {"ok": True}
    # A non-empty title from the rename UI is a deliberate user choice — flip
    # `title_manual` so the auto-titler stops overwriting. An empty string
    # ("") clears the override and re-enables auto-titles.
    title_manual: bool | None = None
    if isinstance(title, str):
        title_manual = bool(title.strip())
    # Sync the live pool FIRST so keep-alive takes effect atomically: set_persistent
    # clears the slot's eviction deadline under the pool lock. Writing the durable
    # meta first would open a small window (between its await and the pool sync)
    # where the idle evictor could reap an already-cooling slot before the pool
    # learns it's keep-alive. The pool also seeds from meta on (re)creation, so a
    # not-yet-spawned session is still covered.
    if persistent is not None and _tmux_pool is not None:
        try:
            await _tmux_pool.set_persistent(session_id, bool(persistent))
        except Exception:  # noqa: BLE001 — best-effort; the meta write below backstops
            pass
    await meta_mod.set_meta(
        session_id,
        title=title,
        title_manual=title_manual,
        archived=archived,
        pinned=pinned,
        persistent=persistent,
        pinned_turn_idxs=pinned_turn_idxs,
    )
    return {"ok": True}


async def _patch_model_handler(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/orchestrator/sessions/{id}/model — set or clear `--model` alias.

    Body: ``{"model": "opus" | "sonnet" | "haiku" | null}``. ``null`` (or
    omitted) clears the override and lets claude-cli pick its built-in
    default for subsequent turns. Strings outside the allowlist are
    rejected with 400 so we never spawn a subprocess against an invalid
    alias.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    raw = payload.get("model")
    if raw is None:
        normalized = ""
    elif isinstance(raw, str):
        candidate = raw.strip().lower()
        if not candidate:
            normalized = ""
        elif candidate not in meta_mod.ALLOWED_MODELS:
            allowed = ", ".join(sorted(meta_mod.ALLOWED_MODELS))
            raise HTTPException(400, detail=f"model must be null or one of: {allowed}")
        else:
            normalized = candidate
    else:
        raise HTTPException(400, detail="model must be a string or null")
    await meta_mod.set_meta(session_id, model=normalized)
    return {"ok": True, "model": normalized or None}


async def _mark_session_read_handler(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/read — set ``last_read_msg_count``.

    Body: ``{"msg_count": <int>}``. The frontend passes the current
    ``msg_count`` from the sessions list at the moment of viewing — that
    becomes the new ``last_read_msg_count`` and zeroes the unread badge for
    every other client until a new turn lands.

    Tolerant of missing field (treated as 0). Negative values clear the
    field (back to "never read").
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    raw = payload.get("msg_count")
    if raw is None:
        v = 0
    else:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, detail="msg_count must be an integer") from None
    await meta_mod.set_meta(session_id, last_read_msg_count=v)
    return {"ok": True, "last_read_msg_count": max(0, v) if v >= 0 else None}


async def _release_session_slots(
    session_id: str, *, forget_persistent: bool, detach_tmux: bool = False
) -> None:
    """Cancel any in-flight runner and free the tmux + ttyd pool slots held
    for ``session_id``.

    Shared by delete (which then wipes the transcript) and close (which keeps
    it). Without the release, a slot survives the action and only dies via the
    10-min idle TTL, leaving a stale claude process in `tmux ls` plus a held
    pool slot even though the user explicitly ended the session. Cancel alone
    deliberately does NOT release (cold-start cost is too high to pay on every
    cancel), so an explicit release belongs here.

    ``forget_persistent`` drops the keep-alive tracking — True for delete (the
    id is gone for good), False for close (preserve the user's keep-alive
    choice for when the session is reopened). All cleanup is best-effort; the
    pools' idle TTLs are the backstop, so we never block the primary action.

    ``detach_tmux`` (close-only) drops just the in-memory pool slot and leaves
    the tmux session + claude REPL RUNNING detached, so "zamknij sesję" frees a
    slot WITHOUT sending ``/exit`` to claude — the conversation survives and is
    re-adopted on reopen. Only honoured when ``tmux_detached_sessions`` is on;
    with the flag off (legacy spawn) a detached session wouldn't survive, so we
    fall back to the killing ``release``.
    """
    active = runner._active_runs.get(session_id)
    if active is not None:
        await active.cancel()
    detach = detach_tmux and settings_mod.get_flag("tmux_detached_sessions")
    try:
        pool = _get_tmux_pool()
        if detach:
            await pool.detach(session_id)
        else:
            await pool.release(session_id)
    except Exception as exc:  # noqa: BLE001 — defensive; idle TTL is a backstop
        print(f"[orchestrator] pool.{'detach' if detach else 'release'}({session_id}) failed: {exc}")
    # Forget keep-alive tracking only when asked. Guard on the singleton so we
    # don't lazy-instantiate a pool just for this (release above used _get_,
    # but if no slot ever existed the set has nothing for this id anyway).
    if forget_persistent and _tmux_pool is not None:
        try:
            await _tmux_pool.forget_persistent(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            print(f"[orchestrator] forget_persistent({session_id}) failed: {exc}")
    # Mirror the tmux release for the ttyd subprocess so ending a session ALSO
    # kills the inline terminal's ttyd + frees its port. Guard on the module-
    # level singleton so we don't lazy-instantiate a pool just to call release
    # (no-op when ttyd_enabled=false OR no slot was acquired for this session).
    if _ttyd_pool is not None:
        try:
            await _get_ttyd_pool().release(session_id)
        except Exception as exc:  # noqa: BLE001 — same backstop reasoning as above
            print(f"[orchestrator] ttyd_pool.release({session_id}) failed: {exc}")


async def _close_session_handler(session_id: str, *, kill: bool = False) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/close — free the session's pool slot
    but KEEP the transcript so it can be reopened.

    Default (``kill=False``): with detached sessions on, the tmux/claude REPL is
    left RUNNING (no ``/exit``) and only the in-memory slot is dropped — reopening
    re-adopts the live session ("zamknij sesję" = park it, instant reopen).

    ``kill=True``: actually TEAR DOWN the REPL (``/exit`` + kill-session) instead
    of detaching. Used by "close agent" — there "I'm done" means end the work and
    reclaim RAM; detaching every session would leak claude REPLs (the idle evictor
    can't reap detached slots). The transcript is still kept (reopen cold-resumes).

    A keep-alive (persistent) session is NEVER torn down — a ``kill`` request for
    one is downgraded to a park (detach). Server-side backstop for the
    client-side close-agent / context-menu guards (the UI already skips them).
    """
    if kill and _tmux_pool is not None:
        try:
            if _tmux_pool._is_persistent(session_id):
                kill = False
        except Exception:  # noqa: BLE001 — never let the guard block the close
            pass
    await _release_session_slots(session_id, forget_persistent=False, detach_tmux=not kill)
    return {"ok": True}


async def _delete_session_handler(session_id: str) -> dict[str, Any]:
    """DELETE /api/orchestrator/sessions/{id} — JSONL + sidecar + uploads + prompt file."""
    await _release_session_slots(session_id, forget_persistent=True)
    # Read the per-session prompt path BEFORE wiping the sidecar so we can
    # unlink the file too — otherwise it'd be orphaned forever.
    prompt_path_str = meta_mod.get_meta(session_id).get("extra_prompt_path")
    jsonl_mod.delete_session(session_id)
    await meta_mod.remove_meta(session_id)
    # Best-effort upload cleanup — must not block primary deletion.
    try:
        uploads_module.delete_session_uploads(session_id)
    except Exception as exc:  # noqa: BLE001 — defensive, partially-corrupt dir is non-fatal
        print(f"[orchestrator] delete_session_uploads({session_id}) failed: {exc}")
    # Best-effort prompt file cleanup. Path must live under SESSION_PROMPTS_DIR
    # to prevent a hostile sidecar from pointing the unlink at /etc/passwd.
    if isinstance(prompt_path_str, str) and prompt_path_str:
        try:
            prompt_path = Path(prompt_path_str).resolve(strict=False)
            prompts_root = SESSION_PROMPTS_DIR.resolve()
            if prompts_root in prompt_path.parents and prompt_path.is_file():
                os.unlink(prompt_path)
        except OSError as exc:
            print(f"[orchestrator] unlink session prompt {session_id} failed: {exc}")
    return {"ok": True}


async def _read_messages_handler(
    session_id: str,
    *,
    limit: int | None = None,
    after_turn: int | None = None,
) -> dict[str, Any]:
    """GET /api/orchestrator/sessions/{id}/messages — parsed JSONL, optionally paged.

    ``after_turn`` keeps only messages whose transcript ``turn_idx`` exceeds it
    (the pagination cursor an orchestrator advances). ``limit`` then keeps the
    LAST ``limit`` of the remaining messages (newest window); ``limit=0`` or
    omitted means no cap. Bounds: ``limit`` must be ≥ 0 (negative → 400) and is
    clamped to 500. ``after_turn`` past the end simply yields ``[]`` — never an
    error. Response adds ``total`` (full transcript size) and ``truncated``
    (whether ``limit`` cut off older messages).
    """
    if limit is not None:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if limit > 500:
            limit = 500
    result = jsonl_mod.read_session(session_id)
    all_messages = (result.get("messages") or []) if result.get("ok") else []
    total = len(all_messages)
    messages = all_messages
    if after_turn is not None:
        messages = [m for m in messages if int(m.get("turn_idx", -1)) > after_turn]
    truncated = False
    if limit:  # non-zero, positive → tail window
        if len(messages) > limit:
            messages = messages[-limit:]
            truncated = True
    return {
        "ok": True,
        "messages": messages,
        "session_id": session_id,
        "total": total,
        "truncated": truncated,
    }


async def _post_message_handler(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/messages — kick off a runner."""
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    text = payload.get("text")
    if text is not None and not isinstance(text, str):
        raise HTTPException(400, detail="text must be a string")
    text = (text or "").strip()
    reply_to = payload.get("reply_to_turn_idx")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)):
        raise HTTPException(400, detail="reply_to_turn_idx must be an int or null")
    # In-flight guard. Returns HTTP 200 with a machine-readable busy envelope
    # (NOT 409) so the existing chat UI keeps working unchanged; an external
    # orchestrator branches on ``ok``/``status`` rather than the HTTP code.
    existing = runner._active_runs.get(session_id)
    if existing is not None and not existing._done.is_set():
        return {"ok": False, "status": "busy", "session_id": session_id,
                "error": "turn already in flight; cancel first"}

    # Reply-to: validate the referenced turn exists and is an assistant message,
    # then prepend a self-closing tag to the user text BEFORE the <attached>
    # block so the order is: reply tag, user text, attached block.
    if reply_to is not None and reply_to >= 0:
        session = jsonl_mod.read_session(session_id)
        target = None
        if session.get("ok"):
            for msg in session.get("messages") or []:
                if msg.get("turn_idx") == reply_to and msg.get("role") == "assistant":
                    target = msg
                    break
        if target is None:
            raise HTTPException(
                400, detail="reply_to_turn_idx must reference an existing assistant message"
            )
        text = f'<reply-to turn_idx="{reply_to}"/>\n\n' + text if text else f'<reply-to turn_idx="{reply_to}"/>'

    # Inject pending uploads as a trailing <attached> block so the runner's
    # `claude -p` invocation sees them as part of the user turn. File-only
    # sends are valid: claude infers from the system prompt that an empty
    # body + <attached> means "look at these".
    try:
        pending = uploads_module.pop_pending(session_id)
    except ValueError:
        pending = []
    if not text and not pending:
        raise HTTPException(400, detail="text or attachments required")
    if pending:
        attached = "<attached>\n" + "\n".join(f"- {p}" for p in pending) + "\n</attached>"
        text = (text + "\n\n" + attached) if text else attached

    # Transcript cursor the next turn will exceed. Returned as `expected_turn_idx`
    # so an orchestrator can pass it to GET /wait?since_turn=<this> unambiguously
    # — and a turn that errors before emitting any assistant message still
    # resolves /wait as status:"error" instead of timing out. Cheap (cached read).
    expected_turn_idx = await asyncio.to_thread(runner.transcript_turn_idx, session_id)
    started_ts = time.time()

    has_run_before = jsonl_mod.jsonl_path(session_id).exists()
    sidecar = meta_mod.get_meta(session_id)
    model = sidecar.get("model")
    cwd_str = sidecar.get("cwd")
    lib_id = sidecar.get("lib_id") if isinstance(sidecar.get("lib_id"), str) else None
    extra_prompt_str = sidecar.get("extra_prompt_path")
    cwd_path = Path(cwd_str) if isinstance(cwd_str, str) and cwd_str else None
    extra_prompt_path = (
        Path(extra_prompt_str) if isinstance(extra_prompt_str, str) and extra_prompt_str else None
    )
    # Resolve the four-layer per-agent prompt stack: general → orchestrator
    # (Global only) → identity (if generated) → custom (if user-edited).
    # Legacy ``extra_prompt_path`` is forwarded separately so old sessions
    # still get their per-session override appended after the stack.
    append_paths = agent_prompts_mod.prompts_for_session(cwd_str, lib_id)

    # Build the per-agent skills symlink farm fresh for this spawn. Resolution
    # mirrors prompts_for_session: Global session (no cwd / cwd==HOME) gets
    # ("global", "global"); per-agent sessions get their (kind, lib_id) parsed
    # from the sidecar lib_id hint. build_symlink_farm is idempotent — wipes
    # the farm dir and rebuilds it from the union of global + per-agent
    # allowlists. Failure is logged but never blocks the turn.
    agent_skills_dir: Path | None = None
    try:
        farm_kind, farm_lib_id = skills_per_agent_mod.resolve_lib_id_from_session(
            cwd_str, lib_id
        )
        agent_skills_dir = skills_per_agent_mod.build_symlink_farm(farm_kind, farm_lib_id)
    except Exception as exc:  # noqa: BLE001 — never block a turn on skills wiring
        print(f"[orchestrator] skills farm build failed: {exc}")

    # Dispatch: per-request override (`interactive_mode: true|false` in the
    # POST body) wins; otherwise fall back to the global `runner_mode` flag.
    # Default = "programmatic" so existing behavior is unchanged until the
    # user opts in.
    request_override = payload.get("interactive_mode")
    if isinstance(request_override, bool):
        use_interactive = request_override
    else:
        use_interactive = settings_mod.get_flag("runner_mode") == "interactive"

    if use_interactive:
        pool = _get_tmux_pool()
        active = runner_tmux_mod.TmuxClaudeRunner(
            session_id,
            pool=pool,
            cwd=cwd_path,
            append_system_prompt_paths=append_paths,
            has_run_before=has_run_before,
            model=model,
            extra_prompt_path=extra_prompt_path,
            agent_skills_dir=agent_skills_dir,
        )
    else:
        active = runner.ClaudeRunner(
            session_id,
            has_run_before,
            model=model,
            cwd=cwd_path,
            append_system_prompt_paths=append_paths,
            extra_prompt_path=extra_prompt_path,
            agent_skills_dir=agent_skills_dir,
        )
    runner._active_runs[session_id] = active
    turn_idx = int(time.time() * 1000)
    # Persistent turn_started lifecycle event for external orchestrators —
    # emitted once here so it covers BOTH runner classes (the terminal
    # turn_done/turn_error come from each runner's _finalize). Best-effort.
    # Oneshot/cron turns bypass this dispatcher, so they emit only the terminal
    # event, never turn_started — documented in /capabilities.
    try:
        events_mod.get_hub().publish(
            session_id,
            "turn_started",
            {"session_id": session_id, "turn_idx": expected_turn_idx, "ts": started_ts},
        )
    except Exception:  # noqa: BLE001 — never block a turn on the notification bus
        pass
    asyncio.create_task(active.start_turn(text))
    return {
        "ok": True,
        "turn_idx": turn_idx,
        "expected_turn_idx": expected_turn_idx,
        "turn_started_ts": started_ts,
        "runner_mode": "interactive" if use_interactive else "programmatic",
    }


async def _stream_handler(request: Request, session_id: str) -> StreamingResponse:
    """GET /api/orchestrator/sessions/{id}/stream — SSE for active turn.

    Honors the ``Last-Event-ID`` header (set automatically by reconnecting
    EventSource clients) so only events strictly newer than the supplied seq
    are replayed. Cleans up the subscriber queue on client disconnect to
    prevent leaks in ``runner.subscribers``.
    """
    last_id_hdr = request.headers.get("last-event-id")
    last_id: int | None = None
    if last_id_hdr is not None:
        try:
            last_id = int(last_id_hdr)
        except ValueError:
            last_id = None

    active = runner._active_runs.get(session_id)
    if active is None:
        async def empty():
            yield runner._format_sse("done", {"reason": "no active turn"})
        return StreamingResponse(
            empty(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    queue = active.subscribe(last_id)

    async def gen():
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    return
                yield evt
        except asyncio.CancelledError:
            return
        finally:
            try:
                active.subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _status_handler(session_id: str) -> dict[str, Any]:
    """GET /api/orchestrator/sessions/{id}/status — runner liveness for poll fallback."""
    active = runner._active_runs.get(session_id)
    if active is None:
        return {"in_flight": False}
    return active.status_snapshot()


async def _pane_stream_handler(request: Request, session_id: str) -> StreamingResponse:
    """GET /api/orchestrator/sessions/{id}/pane — SSE feed of tmux pane snapshots.

    Polls ``capture-pane -p -S - -E -`` on the live tmux slot for this
    session at ``PANE_TICK_S`` cadence and emits a ``pane_snapshot`` event
    whenever the captured text changes (sha1-based dedup). The frontend
    renders snapshots into a ``<pre>`` so the user can watch the live
    claude TUI without SSHing into the box.

    Quiet behavior:
    - No slot for this session (programmatic mode, or interactive but
      first turn hasn't fired yet): emits ``done`` and closes.
    - Pane capture fails (tmux dead, session torn down mid-stream): emits
      ``error`` and closes.
    - Client disconnect: generator's CancelledError exits cleanly.

    Bandwidth: dedup is critical — claude's TUI repaints on every
    keystroke + cursor blink, but the actual screen contents only change
    when there's real output. Without sha1 dedup we'd push ~10 KB every
    tick to every viewer.
    """
    pane_tick_s = 0.75  # 1.33 Hz — feels live without hammering subprocess

    async def gen():
        import hashlib
        pool = _get_tmux_pool()
        session_name = f"{tmux_mod.SESSION_PREFIX}{session_id}"
        seq = 0
        last_hash: str | None = None
        try:
            # Probe tmux directly rather than the pool's `_slots` dict —
            # ephemeral runners (cron's `_run_llm_isolated_interactive`)
            # call `pool.release()` after each turn which evicts the slot
            # from `_slots`, but the tmux session can linger briefly and
            # the user-chat slots stay forever. `has_session` reflects
            # ground truth: ask tmux server "is this session live?".
            if not await pool._runner.has_session(session_name):
                seq += 1
                yield runner._format_sse(
                    "done", {"reason": "tmux session not running"}, seq=seq,
                )
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    text = await pool._runner.capture_pane(session_name)
                except Exception as exc:  # noqa: BLE001
                    seq += 1
                    yield runner._format_sse(
                        "error", {"message": f"capture failed: {exc}"}, seq=seq,
                    )
                    return
                # Hash + dedup so identical re-paints don't spam the wire.
                h = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
                if h != last_hash:
                    last_hash = h
                    seq += 1
                    yield runner._format_sse(
                        "pane_snapshot",
                        {"text": text, "ts": time.time()},
                        seq=seq,
                    )
                await asyncio.sleep(pane_tick_s)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _cancel_handler(session_id: str) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/cancel — terminate the active runner."""
    active = runner._active_runs.get(session_id)
    if active is None:
        return {"ok": True, "note": "no active turn"}
    await active.cancel()
    return {"ok": True}


def _api_version() -> str:
    """Best-effort package version for the capabilities advertisement."""
    try:
        from importlib import metadata
        return metadata.version("orbit")
    except Exception:  # noqa: BLE001
        return "0.0.0"


async def _capabilities_handler() -> dict[str, Any]:
    """GET /api/orchestrator/capabilities — what this API supports, for an
    external orchestrator to feature-gate against. Advertises ONLY shipped
    features (deferred ones are False)."""
    semantic = bool(settings_mod.get_flag("semantic_search_enabled"))
    return {
        "ok": True,
        "version": _api_version(),
        "features": {
            "turn_lifecycle_events": True,
            "session_wait": True,
            "session_start_stop": True,
            "lexical_search": True,
            "vector_search": search_mod._get_sklearn() is not None,
            "semantic_search": semantic,
            "embedding_ready": search_mod.embedding_ready(),
            "messages_pagination": True,
            "global_events": False,
            "agent_context": False,
        },
        "sse_events": [
            "turn_started", "turn_done", "turn_error",
            "artifact_create", "artifact_open", "speak",
        ],
        "limits": {
            "wait_timeout_max_s": wait_mod.WAIT_TIMEOUT_MAX_S,
            "wait_max_concurrent": wait_mod.WAIT_MAX_CONCURRENT,
            "search_limit_max": search_mod._LIMIT_MAX,
            "messages_limit_max": 500,
        },
    }


async def _turns_running_handler() -> dict[str, Any]:
    """GET /api/orchestrator/turns/running — sessions with an in-flight turn.

    Snapshots ``_active_runs`` before iterating (``_finalize``/``_reap`` mutate
    it from ``call_later`` callbacks) and excludes already-finalized runners.
    Oneshot/cron turns aren't registered here — documented in /capabilities.
    Deliberately does NOT expose the per-run ``_turn_idx`` counter (it isn't the
    transcript turn index the rest of the contract uses)."""
    running: list[dict[str, Any]] = []
    for sid, active in list(runner._active_runs.items()):
        try:
            if active._done.is_set():
                continue
            is_tmux = isinstance(active, runner_tmux_mod.TmuxClaudeRunner)
            running.append({
                "session_id": sid,
                "started_at": getattr(active, "_started_at_ms", 0) / 1000.0,
                "runner": "tmux" if is_tmux else "programmatic",
            })
        except Exception:  # noqa: BLE001 — one bad runner must not break the snapshot
            continue
    return {"ok": True, "running": running}


async def _start_session_handler(session_id: str) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/start — bring the interactive
    (tmux) runtime online so the session is warm and ready to receive a turn.

    Distinct from POST /messages (which sends a prompt) and from /stop. Reuses
    the same warm-slot primitive a real message turn / terminal-open uses, so
    the long-lived claude carries the correct prompt/cwd/model context. Returns
    ``spawned: true`` on a cold spawn, ``false`` if the slot was already warm.
    Not gated on ttyd (that's only the browser terminal view).
    """
    try:
        spawned = await _warm_session_slot(session_id, wait_ready=True)
    except Exception as exc:  # noqa: BLE001 — surface as 503
        raise HTTPException(503, detail=f"failed to start session: {exc}")
    return {"ok": True, "started": True, "spawned": spawned}


async def _stop_session_handler(session_id: str) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/stop — tear down the session's live
    runtime (cancel any in-flight turn, release the tmux + ttyd slots) while
    KEEPING the transcript. The orchestrator-facing lifecycle counterpart to
    /start; same teardown primitive as /close, preserving keep-alive choice."""
    await _release_session_slots(session_id, forget_persistent=False)
    return {"ok": True, "stopped": True}


async def _compact_handler(session_id: str) -> dict[str, Any]:
    """POST /api/orchestrator/sessions/{id}/compact — fork to a new session.

    Runs a synchronous summary turn against the current session, extracts the
    markdown summary + active tasks, and seeds a new session with that content.
    Old session is preserved (sidecar marks it `compacted_to=<new_id>`); new
    session's sidecar carries `compacted_from=<old_id>`. Returns the new
    session id so the frontend can switch to it.

    Long-running (10–60s for typical sessions). Caller is expected to show a
    spinner. No streaming — the heavy work is server-side; we return a single
    JSON envelope when both turns finish.
    """
    if not jsonl_mod.jsonl_path(session_id).exists():
        raise HTTPException(404, detail="session not found")
    existing = runner._active_runs.get(session_id)
    if existing is not None and not existing._done.is_set():
        raise HTTPException(409, detail="turn already in flight on this session; cancel first")
    try:
        result = await compact_mod.compact_session(session_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, detail=f"compact failed: {exc}") from exc
    return result


# ── artifacts + persistent events ───────────────────────────────────


def _resolve_artifacts_dir(
    session_id: str | None, lib_id: str | None, cwd: str | None
) -> Path:
    """Resolve the artifacts dir for a request from query params.

    Session scope wins; otherwise agent scope (lib_id/cwd), with the
    ``__global__`` sentinel (matching the agents-directory convention) and a
    bare-empty value both mapping to the global agent dir.
    """
    if session_id:
        return artifacts_mod.artifacts_dir(session_id=session_id)
    lib = None if lib_id in (None, "", "__global__") else lib_id
    c = None if cwd in (None, "", "__global__") else cwd
    return artifacts_mod.artifacts_dir(cwd=c, lib_id=lib)


# Per-type Content-Type allowlists. A manifest's ``mime`` is agent-written and
# could be ``image/svg+xml`` or ``text/html`` — serving those on the dashboard's
# OWN origin would be an XSS vector (SVG/HTML execute script). We only honor
# known-safe, non-executable media types for inline rendering; anything else
# (svg, unknown, ``file`` artifacts) is forced to octet-stream + attachment so a
# direct navigation can't execute it. ``nosniff`` is always set.
_SAFE_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"})
_SAFE_AUDIO_MIMES = frozenset({"audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/mp4", "audio/aac", "audio/webm", "audio/flac"})
_SAFE_VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/ogg", "video/quicktime"})
_SAFE_MIMES_BY_TYPE = {"image": _SAFE_IMAGE_MIMES, "audio": _SAFE_AUDIO_MIMES, "video": _SAFE_VIDEO_MIMES}


def _safe_artifact_serving(artifact_type: str | None, mime: object) -> tuple[str, str]:
    """(media_type, disposition) for an artifact file — XSS-safe.

    Inline only for image/audio/video whose manifest mime is in the per-type
    allowlist; everything else (svg+xml, html, unknown, ``file``) downgrades to
    ``application/octet-stream`` + ``attachment`` so it can't execute.
    """
    allow = _SAFE_MIMES_BY_TYPE.get(artifact_type or "")
    if allow and isinstance(mime, str) and mime in allow:
        return mime, "inline"
    return "application/octet-stream", "attachment"


def _register_artifact_routes(app: FastAPI) -> None:
    """Mount /api/orchestrator/artifacts* + the persistent /events SSE channel.

    Only ``/notify`` (the CLI → server control channel that pops browser
    toasts/modals) is token-gated. The GETs and the gallery's
    duplicate/delete/edit are browser-origin on a single-user Tailscale box,
    so they stay open — that also keeps plain ``<img src>`` working.
    """

    def _require_token(request: Request) -> None:
        expected = artifacts_mod.read_token()
        got = request.headers.get("x-artifact-token") or ""
        if not expected or not hmac.compare_digest(got, expected):
            raise HTTPException(401, detail="invalid artifact token")

    @app.get("/api/orchestrator/artifacts")
    async def api_artifacts_list(
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List artifacts for a session (per-session view) or an agent (gallery)."""
        # Both list_* helpers scandir the .artifacts dir and json.load one
        # manifest per artifact (N file reads) — off-load so the orchestrator
        # panel / Agent gallery load never blocks the loop.
        if session_id:
            artifacts = await asyncio.to_thread(artifacts_mod.list_for_session, session_id)
            return {"artifacts": artifacts}
        if lib_id is None and cwd is None:
            raise HTTPException(400, detail="session_id or lib_id required")
        lib = None if lib_id in ("", "__global__") else lib_id
        c = None if cwd in ("", "__global__") else cwd
        artifacts = await asyncio.to_thread(artifacts_mod.list_for_agent, cwd=c, lib_id=lib)
        return {"artifacts": artifacts}

    @app.get("/api/orchestrator/artifacts/{artifact_id}/file")
    async def api_artifact_file(
        artifact_id: str,
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ):
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        try:
            manifest = artifacts_mod.get(dirp, artifact_id)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if manifest is None:
            raise HTTPException(404, detail="artifact not found")
        # Spec types (chart/map/youtube) carry the payload inline → return JSON.
        if manifest.get("type") in artifacts_mod.SPEC_TYPES or not manifest.get("src"):
            return JSONResponse(manifest)
        try:
            p = artifacts_mod.file_path(dirp, manifest)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if p is None:
            raise HTTPException(404, detail="artifact file missing")
        if manifest.get("type") == "html":
            # Serve as text/plain so direct navigation can't execute the doc;
            # the frontend fetches it and injects into a sandboxed iframe srcDoc.
            return FileResponse(
                p, media_type="text/plain; charset=utf-8",
                headers={
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": f'inline; filename="{p.name}"',
                },
            )
        # Never let the agent-written manifest mime pick an executable
        # Content-Type on the app origin (svg+xml / html → XSS). Allowlist +
        # nosniff; unknown/unsafe types download as octet-stream.
        media_type, disposition = _safe_artifact_serving(manifest.get("type"), manifest.get("mime"))
        return FileResponse(
            p, media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'{disposition}; filename="{p.name}"',
            },
        )

    @app.get("/api/orchestrator/artifacts/{artifact_id}/view")
    async def api_artifact_view(
        artifact_id: str,
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ):
        """Full-page render of an ``html`` artifact (new-tab / full-screen).

        ``/file`` serves html as ``text/plain`` so a bare navigation can't run
        it — good for the inline ``srcDoc`` iframe, useless for "open in a new
        tab". This route serves real ``text/html`` but pins the document into an
        opaque origin with a ``Content-Security-Policy: sandbox`` header:
        scripts/forms run, but the page gets NO same-origin access to the
        dashboard (no cookies, no localStorage, no credentialed fetch), and the
        header applies to the top-level document too — so a direct new-tab
        navigation is as safe as the embedded iframe. Non-html artifacts 404.
        """
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        try:
            manifest = artifacts_mod.get(dirp, artifact_id)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if manifest is None:
            raise HTTPException(404, detail="artifact not found")
        if manifest.get("type") != "html" or not manifest.get("src"):
            raise HTTPException(404, detail="not an html artifact")
        try:
            p = artifacts_mod.file_path(dirp, manifest)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if p is None:
            raise HTTPException(404, detail="artifact file missing")
        return FileResponse(
            p, media_type="text/html; charset=utf-8",
            headers={
                "X-Content-Type-Options": "nosniff",
                # Opaque-origin sandbox — omitting allow-same-origin is what
                # firewalls the doc off from the dashboard. Applies top-level.
                "Content-Security-Policy": (
                    "sandbox allow-scripts allow-forms allow-popups "
                    "allow-modals allow-downloads;"
                ),
                "Content-Disposition": f'inline; filename="{p.name}"',
            },
        )

    @app.get("/api/orchestrator/artifacts/{artifact_id}/path")
    async def api_artifact_path(
        artifact_id: str,
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Absolute filesystem path of an artifact's payload file.

        Used by the gallery's "Skomentuj" action so the path can be pasted
        into the live terminal — claude (running on the box) reads the file
        directly, exactly like an uploaded attachment. Spec types
        (chart/map/youtube) have no payload file → 404.
        """
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        try:
            manifest = artifacts_mod.get(dirp, artifact_id)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if manifest is None:
            raise HTTPException(404, detail="artifact not found")
        if manifest.get("type") in artifacts_mod.SPEC_TYPES or not manifest.get("src"):
            raise HTTPException(404, detail="artifact has no file (spec type)")
        try:
            p = artifacts_mod.file_path(dirp, manifest)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if p is None or not p.is_file():
            raise HTTPException(404, detail="artifact file missing")
        return {"path": str(p)}

    @app.get("/api/orchestrator/artifacts/{artifact_id}/thumb")
    async def api_artifact_thumb(
        artifact_id: str,
        size: int = Query(default=256),
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ) -> Response:
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        try:
            manifest = artifacts_mod.get(dirp, artifact_id)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if manifest is None or manifest.get("type") != "image":
            raise HTTPException(404, detail="no thumbnail")
        try:
            p = artifacts_mod.file_path(dirp, manifest)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if p is None:
            raise HTTPException(404)
        size = max(32, min(int(size), 1024))

        def _make_thumb() -> bytes:
            from PIL import Image, ImageOps
            img = Image.open(p)
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return buf.getvalue()

        try:
            # PIL decode + LANCZOS resample + JPEG encode is 100-500ms of
            # CPU-bound work on a 4K image — off the loop so concurrent
            # gallery thumbnail requests don't serialize through it.
            data = await asyncio.to_thread(_make_thumb)
            return Response(content=data, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600"})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, detail=f"thumbnail failed: {exc}")

    @app.post("/api/orchestrator/artifacts/notify")
    async def api_artifact_notify(
        request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """CLI → server: pop a toast (created) or a modal (open) in the browser."""
        _require_token(request)
        session_id = payload.get("session_id")
        kind = payload.get("kind")
        artifact = payload.get("artifact")
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(400, detail="session_id required")
        if kind not in ("created", "open"):
            raise HTTPException(400, detail="kind must be created|open")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("id"), str):
            raise HTTPException(400, detail="artifact manifest with id required")
        # Verify the artifact exists on disk before pushing, so we never pop a
        # modal for a dangling/never-written id (CLI race: file then notify).
        dirp = artifacts_mod.artifacts_dir(session_id=session_id)
        try:
            on_disk = artifacts_mod.get(dirp, artifact["id"])
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        if on_disk is None:
            raise HTTPException(404, detail="artifact not found on disk")
        events_mod.get_hub().publish(session_id, f"artifact_{kind}", {"artifact": on_disk})
        return {"ok": True}

    @app.post("/api/orchestrator/artifacts/{artifact_id}/duplicate")
    async def api_artifact_duplicate(
        artifact_id: str,
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ) -> dict[str, Any]:
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        try:
            return {"artifact": artifacts_mod.duplicate(dirp, artifact_id)}
        except FileNotFoundError:
            raise HTTPException(404, detail="artifact not found")
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @app.patch("/api/orchestrator/artifacts/{artifact_id}")
    async def api_artifact_edit(
        artifact_id: str,
        payload: dict[str, Any] = Body(default={}),
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ) -> dict[str, Any]:
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        title = payload.get("title")
        type_ = payload.get("type")
        try:
            return {"artifact": artifacts_mod.edit(
                dirp, artifact_id,
                title=title if isinstance(title, str) else None,
                type=type_ if isinstance(type_, str) else None,
            )}
        except FileNotFoundError:
            raise HTTPException(404, detail="artifact not found")
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @app.delete("/api/orchestrator/artifacts/{artifact_id}")
    async def api_artifact_delete(
        artifact_id: str,
        session_id: str | None = Query(default=None),
        lib_id: str | None = Query(default=None),
        cwd: str | None = Query(default=None),
    ) -> dict[str, Any]:
        dirp = _resolve_artifacts_dir(session_id, lib_id, cwd)
        try:
            return {"ok": artifacts_mod.delete(dirp, artifact_id)}
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))

    @app.get("/api/orchestrator/sessions/{session_id}/read-aloud")
    async def api_read_aloud(request: Request, session_id: str) -> StreamingResponse:
        """Passive read-aloud SSE — auto-speak assistant turns over tmux.

        Gated by ``read_aloud_tmux_enabled`` (404 when off; the client bounds
        its reconnects and gives up — EventSource can't read the status itself).
        Arms the per-session JSONL watcher for the lifetime of THIS connection
        (disarmed in ``finally``), and forwards only ``speak`` frames (plus
        keepalive pings) from the shared per-session hub — artifact frames on the
        same bus are dropped via ``read_aloud_mod.is_forwardable_frame``. Mirrors
        ``/events`` (Last-Event-ID resume, StreamingResponse). Separate
        EventSource so the client opens it only when a device wants auto-read
        (voiceMode == 'always').
        """
        if not read_aloud_mod.is_enabled():
            raise HTTPException(404, detail="read-aloud is disabled")
        if not uploads_module.SESSION_ID_RE.fullmatch(session_id or ""):
            raise HTTPException(400, detail="invalid session_id")
        last_id_hdr = request.headers.get("last-event-id")
        last_id: int | None = None
        if last_id_hdr is not None:
            try:
                last_id = int(last_id_hdr)
            except ValueError:
                last_id = None
        hub = events_mod.get_hub()
        mgr = read_aloud_mod.get_manager()
        mgr.arm(session_id)
        queue = hub.subscribe(session_id, last_id)
        _ip = request.client.host if request.client else "?"
        # Breadcrumb: turns the journal's bare SSE GET lines into a
        # connect/disconnect timeline (same IP) so a flap straddling a reply is
        # visible — the read-aloud audio path had ZERO observability when an
        # in-car attempt failed (2026-06-13).
        _logger.info("[read-aloud] connect %s ip=%s last_id=%s refcount=%d",
                     session_id, _ip, last_id, mgr.refcount(session_id))

        async def gen():
            try:
                while True:
                    evt = await queue.get()
                    if evt is None:
                        return
                    # Shared per-session bus also carries artifact frames; this
                    # channel only forwards speak frames + keepalive comments.
                    if read_aloud_mod.is_forwardable_frame(evt):
                        yield evt
            except asyncio.CancelledError:
                return
            finally:
                hub.unsubscribe(session_id, queue)
                mgr.disarm(session_id)
                _logger.info("[read-aloud] disconnect %s ip=%s refcount=%d",
                             session_id, _ip, mgr.refcount(session_id))

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/orchestrator/sessions/{session_id}/events")
    async def api_session_events(request: Request, session_id: str) -> StreamingResponse:
        """Persistent server-initiated SSE channel (artifact toasts/modals).

        Independent of the turn /stream — stays open while the panel is open,
        survives the turn runner's 60s reap, and honors Last-Event-ID resume.
        """
        if not uploads_module.SESSION_ID_RE.fullmatch(session_id or ""):
            raise HTTPException(400, detail="invalid session_id")
        last_id_hdr = request.headers.get("last-event-id")
        last_id: int | None = None
        if last_id_hdr is not None:
            try:
                last_id = int(last_id_hdr)
            except ValueError:
                last_id = None
        hub = events_mod.get_hub()
        queue = hub.subscribe(session_id, last_id)

        async def gen():
            try:
                while True:
                    evt = await queue.get()
                    if evt is None:
                        return
                    yield evt
            except asyncio.CancelledError:
                return
            finally:
                hub.unsubscribe(session_id, queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# ── A2A (agent-to-agent mail) ───────────────────────────────────────

# In-process LRU of message ids we've already enqueued, so a CLI retry within
# one process lifetime is a cheap no-op before we touch the maildir. The
# on-disk ``inbox_has`` check (inbox/ + cur/) is the restart-spanning backstop.
_A2A_SEEN_IDS: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_A2A_SEEN_MAX = 512


def _a2a_mark_seen(msg_id: str) -> None:
    """Record ``msg_id`` in the dedup LRU, evicting the oldest past the cap."""
    _A2A_SEEN_IDS[msg_id] = None
    _A2A_SEEN_IDS.move_to_end(msg_id)
    while len(_A2A_SEEN_IDS) > _A2A_SEEN_MAX:
        _A2A_SEEN_IDS.popitem(last=False)


def _register_a2a_routes(app: FastAPI) -> None:
    """Mount /api/orchestrator/a2a/{send,agents,whois}.

    ``/send`` is token-gated (the SAME ~/.orchestrator/artifact_token the
    ``artifact`` CLI already carries via ``session_env``) and flag-gated on
    ``a2a_enabled``. The server derives the ``from`` lib_id from the caller
    session's sidecar — a client can never spoof its own identity. ``/send`` is
    a PURE ENQUEUE (v2): it writes the envelope into the target's maildir and
    returns ``delivery="enqueued"`` — there is no push, no warm nudge, no cold
    auto-revive. The target's human later drains it (``a2a inbox --drain``).
    ``/agents`` + ``/whois`` are open GET directories (not credentials).
    """

    def _require_a2a_token(request: Request) -> None:
        expected = artifacts_mod.read_token()
        got = request.headers.get("x-a2a-token") or ""
        if not expected or not hmac.compare_digest(got, expected):
            raise HTTPException(403, detail="invalid a2a token")

    @app.post("/api/orchestrator/a2a/send")
    async def api_a2a_send(
        request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """CLI → server: route one agent message into the target's maildir.

        Body: ``{to, type?, correlation_id?, reply_to?, text|payload.text,
        session_id, session?}``. The ``from`` is server-set from the caller
        session's sidecar lib_id (NEVER the client). PURE ENQUEUE (v2): the
        envelope is written into the target's (optionally per-``session``)
        maildir and the route returns ``delivery="enqueued"`` — no liveness
        gate, no push, no cold auto-revive. The target's human drains it later.
        """
        _require_a2a_token(request)
        if not settings_mod.get_flag("a2a_enabled"):
            raise HTTPException(403, detail="a2a disabled")
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")

        caller_session_id = payload.get("session_id")
        if not isinstance(caller_session_id, str) or not uploads_module.SESSION_ID_RE.fullmatch(
            caller_session_id
        ):
            raise HTTPException(400, detail="invalid session_id")

        to_lib = payload.get("to")
        if not isinstance(to_lib, str) or not to_lib.strip():
            raise HTTPException(400, detail="to (target lib_id or 'global') required")

        msg_type = payload.get("type") or "message"
        if not isinstance(msg_type, str) or msg_type not in a2a_mod.ALLOWED_TYPES:
            allowed = ", ".join(sorted(a2a_mod.ALLOWED_TYPES))
            raise HTTPException(400, detail=f"type must be one of: {allowed}")

        correlation_id = payload.get("correlation_id")
        if correlation_id is not None and not isinstance(correlation_id, str):
            raise HTTPException(400, detail="correlation_id must be a string or null")
        reply_to = payload.get("reply_to")
        if reply_to is not None and not isinstance(reply_to, str):
            raise HTTPException(400, detail="reply_to must be a string or null")

        # Optional per-session targeting. ABSENT (None/empty) → agent-level
        # delivery (warm enqueue / cold auto-revive, unchanged). PRESENT → the
        # message is pinned to a SPECIFIC live session of the target agent;
        # validated below once we know the normalized `to` lib_id.
        target_session = payload.get("session")
        if target_session is not None:
            if not isinstance(target_session, str):
                raise HTTPException(400, detail="session must be a string or null")
            target_session = target_session.strip() or None
        if target_session is not None and not uploads_module.SESSION_ID_RE.fullmatch(
            target_session
        ):
            raise HTTPException(400, detail="invalid session")

        # text from the top level, or nested under payload.text (CLI sends one
        # or the other). Cap/non-empty are enforced by build_envelope.
        text = payload.get("text")
        if text is None and isinstance(payload.get("payload"), dict):
            text = payload["payload"].get("text")
        if not isinstance(text, str):
            raise HTTPException(400, detail="text required")

        # Server-set sender identity: the caller session's sidecar lib_id, or
        # "global" for a no-lib_id (home-rooted) session. The client cannot set
        # this — it's read from disk, keyed on the validated session_id.
        try:
            caller_meta = await asyncio.to_thread(meta_mod.get_meta, caller_session_id)
        except Exception as exc:  # noqa: BLE001 — fall back to global identity
            print(f"[orchestrator] a2a: get_meta failed for caller: {exc}")
            caller_meta = {}
        from_lib = caller_meta.get("lib_id") if isinstance(caller_meta, dict) else None
        from_lib = from_lib if isinstance(from_lib, str) and from_lib.strip() else "global"

        # Mint + validate the envelope (raises ValueError on a bad to/type/text).
        try:
            envelope = a2a_mod.build_envelope(
                from_lib=from_lib,
                to_lib=to_lib,
                type=msg_type,
                text=text,
                correlation_id=correlation_id,
                reply_to=reply_to,
                to_session=target_session,
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

        msg_id = envelope["id"]
        target_lib = envelope["to"]

        # PURE ENQUEUE (v2): validate + dedup + write into the target's maildir,
        # then return. No liveness gate, no warm nudge, no cold auto-revive — the
        # target's human drains it later with `a2a inbox --drain`. A `--session`
        # simply routes the envelope into that session's sub-maildir (the uuid
        # shape was validated above; build_envelope re-validates it).

        # Dedup: in-mem LRU fast path, then the restart-spanning on-disk check.
        # The on-disk probe is agent-level (inbox/ + cur/); a session-targeted
        # send routes to the session inbox, but the in-mem LRU + the unique
        # minted id still guard a same-process retry.
        already = msg_id in _A2A_SEEN_IDS
        if not already:
            try:
                already = await asyncio.to_thread(a2a_mod.inbox_has, target_lib, msg_id)
            except Exception as exc:  # noqa: BLE001 — never block send on a dedup probe
                print(f"[orchestrator] a2a: inbox_has probe failed: {exc}")
                already = False
        if not already:
            try:
                # envelope.to_session (set when target_session is present) is
                # authoritative; enqueue auto-routes to the session inbox.
                await asyncio.to_thread(a2a_mod.enqueue, envelope, session=target_session)
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc)) from exc
            except OSError as exc:
                raise HTTPException(500, detail=f"failed to enqueue: {exc}") from exc
        _a2a_mark_seen(msg_id)

        delivery = "enqueued"

        # Best-effort SSE toast back to the SENDER's panel; a publish failure
        # must never break the send.
        try:
            events_mod.get_hub().publish(
                caller_session_id,
                "a2a_sent",
                {"id": msg_id, "to": target_lib, "delivery": delivery},
            )
        except Exception:  # noqa: BLE001 — notification bus is decorative here
            pass

        return {"ok": True, "id": msg_id, "to": target_lib, "delivery": delivery}

    @app.get("/api/orchestrator/a2a/agents")
    async def api_a2a_agents() -> dict[str, Any]:
        """Open directory of every known agent, reconciled with live sessions.

        Each entry: ``{lib_id, name, warm, session_id, last_active, sessions}``
        where ``sessions`` is every LIVE session of that agent (for --session
        targeting). The global agent is always present. No token — a directory,
        not a secret. Fields flow straight through from ``list_agents`` (no
        stripping).
        """
        pool = _get_tmux_pool()
        try:
            live = await pool.live_session_ids()
        except Exception as exc:  # noqa: BLE001 — a probe error → treat all as cold
            print(f"[orchestrator] a2a: live_session_ids failed: {exc}")
            live = set()
        agents = await asyncio.to_thread(a2a_mod.list_agents, live)
        return {"ok": True, "agents": agents}

    @app.get("/api/orchestrator/a2a/whois")
    async def api_a2a_whois(request: Request) -> dict[str, Any]:
        """Open directory record for a SINGLE agent (``?lib_id=<lib_id>``).

        Returns ``{ok, agent}`` where ``agent`` is the full ``whois`` record:
        the agent's ``name`` / PARA ``dir`` / full ``identity.md`` + EVERY
        session (live and cold) with its title, ``last_active``, ``live`` flag
        and absolute ``.jsonl`` transcript path. ``lib_id`` is a query param (not
        a path capture) so a slash in ``areas/Home`` needs no URL-encoding. No
        token — a directory, not a secret. Reconciled against the live tmux set.
        """
        lib_id = request.query_params.get("lib_id") or "global"
        pool = _get_tmux_pool()
        try:
            live = await pool.live_session_ids()
        except Exception as exc:  # noqa: BLE001 — a probe error → treat all as cold
            print(f"[orchestrator] a2a: live_session_ids failed: {exc}")
            live = set()
        agent = await asyncio.to_thread(a2a_mod.whois, lib_id, live)
        return {"ok": True, "agent": agent}


# ── route registration ─────────────────────────────────────────────


def register_routes(app: FastAPI) -> None:
    """Mount /api/orchestrator/* routes on the given FastAPI app.

    Calls `orchestrator_prompts.ensure_prompts()` once at registration so
    the system prompt file exists before the first claude invocation.
    """
    try:
        prompts_mod.ensure_prompts()
    except Exception as exc:  # noqa: BLE001 — startup should not crash app
        print(f"[orchestrator] ensure_prompts failed: {exc}")

    # Warm VAPID keys so the first subscribe request doesn't pay keygen cost.
    try:
        notifs_module.ensure_vapid_keys()
    except Exception as exc:  # noqa: BLE001 — push is best-effort, never block boot
        print(f"[orchestrator] ensure_vapid_keys failed: {exc}")

    # Mint the shared `artifact` CLI auth token so the first CLI notify works.
    try:
        artifacts_mod.ensure_token()
    except Exception as exc:  # noqa: BLE001 — never block boot on token mint
        print(f"[orchestrator] ensure_artifact_token failed: {exc}")

    # Wire the bundled `dashboard` MCP server for the Global agent (issue #95).
    try:
        bundled_mcp_mod.ensure_dashboard_mcp()
    except Exception as exc:  # noqa: BLE001 — MCP wiring must never block boot
        print(f"[orchestrator] ensure_dashboard_mcp failed: {exc}")

    voice_mod.register_routes(app)
    tts_mod.register_routes(app)
    _register_artifact_routes(app)
    _register_a2a_routes(app)
    # Inline interactive terminal (ttyd-backed). Routes are mounted
    # unconditionally; the kill-switch ``ttyd_enabled`` lives inside each
    # route so the user can toggle the feature from Settings without a
    # restart. With the flag off, the routes return 503 (HTTP) / close
    # 1011 (WS) and the frontend falls back to the legacy SSE preview.
    terminal_mod.register_terminal_routes(app, _get_ttyd_pool)

    @app.post("/api/orchestrator/sessions/{session_id}/term/ensure")
    async def api_term_ensure(session_id: str) -> dict[str, Any]:
        """Pre-warm the tmux slot for this chat before the iframe loads.

        The terminal modal calls this first so a chat whose slot has
        evicted (idle TTL) or never spawned (programmatic mode without
        a message yet) still produces a working live terminal. Mirrors
        the dispatch logic in ``_post_message_handler`` so the cold
        spawn uses the exact same flags a real message turn would —
        otherwise the warm slot's long-lived claude would carry stale
        system-prompt context when the user later sends a message.

        Fast-path for an already-warm slot: returns in milliseconds
        with ``spawned: false``. Cold path returns as soon as the tmux
        session exists (``terminal_instant_attach`` on, the default) so the
        iframe mounts and the user watches claude boot / picks its resume
        mode in the live terminal — no readiness wait. Flip the flag off to
        restore the legacy blocking spawn (waits out wait_until_ready before
        returning).
        """
        if not settings_mod.get_flag("ttyd_enabled"):
            raise HTTPException(503, detail="ttyd terminal disabled")
        instant = bool(settings_mod.get_flag("terminal_instant_attach"))
        try:
            spawned = await _warm_session_slot(session_id, wait_ready=not instant)
        except Exception as exc:  # noqa: BLE001 — surface as 503
            raise HTTPException(
                status_code=503,
                detail=f"failed to spawn interactive session: {exc}",
            )
        return {"ok": True, "spawned": spawned}

    @app.post("/api/orchestrator/sessions/{session_id}/term/paste")
    async def api_term_paste(
        session_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Paste text into the session's live tmux pane WITHOUT submitting.

        Powers the gallery "Skomentuj" → terminal flow: the artifact path is
        injected server-side at claude's input prompt (no Enter), so the user
        adds context and submits themselves. Pasting into the pane (vs poking
        the browser xterm) is timing-independent — ttyd just reflects the pane,
        so it works the instant the terminal view (re)mounts.
        """
        if not settings_mod.get_flag("ttyd_enabled"):
            raise HTTPException(503, detail="ttyd terminal disabled")
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise HTTPException(400, detail="text required")
        if len(text) > 8192:
            raise HTTPException(400, detail="text too long")
        pool = _get_tmux_pool()
        # Route through _warm_session_slot UNCONDITIONALLY: it short-circuits in
        # ms for a verified-live slot (the common Skomentuj-from-open-terminal
        # case) and self-heals a stale one via acquire() (re-spawns + waits
        # ready). Trusting has_warm_slot here would paste into a dead pane
        # ("no such window: hd-<id>") when the slot went stale out-of-band.
        try:
            await _warm_session_slot(session_id)
        except Exception as exc:  # noqa: BLE001 — surface as 503
            raise HTTPException(503, detail=f"failed to spawn session: {exc}")
        try:
            await pool.paste_into(session_id, text)
        except KeyError:
            raise HTTPException(503, detail="session slot unavailable")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, detail=f"paste failed: {exc}")
        return {"ok": True}

    @app.get("/api/orchestrator/sessions/{session_id}/copy-mode")
    async def api_term_copy_mode(session_id: str) -> dict[str, Any]:
        """Whether the session's tmux pane is in copy-mode (scrolled up).

        Powers the terminal scroll-to-bottom FAB: scrolling the pane enters
        tmux copy-mode (the ``[N/M]`` indicator) rather than moving the browser
        xterm's own scrollbar, so the FAB can't watch a DOM scrollbar — it polls
        this instead. Cheap + read-only: never spawns a slot, never touches the
        LRU (so polling can't keep an idle session warm). Returns
        ``in_mode: false`` whenever the terminal is off, the slot is cold, or the
        query fails — a UI poll must never surface an error.
        """
        if not settings_mod.get_flag("ttyd_enabled"):
            return {"in_mode": False}
        pool = _get_tmux_pool()
        if not pool.has_warm_slot(session_id):
            return {"in_mode": False}
        try:
            in_mode = await pool.pane_in_mode(session_id)
        except Exception:  # noqa: BLE001 — a poll must never 500
            return {"in_mode": False}
        return {"in_mode": bool(in_mode)}

    @app.post("/api/orchestrator/sessions/{session_id}/copy-mode")
    async def api_term_cancel_copy_mode(session_id: str) -> dict[str, Any]:
        """Exit tmux copy-mode → snap the pane back to the live bottom.

        The click target of the terminal scroll-to-bottom FAB. Idempotent /
        safe: a no-op (still ``ok``) when the terminal is off, the slot is cold,
        or the pane isn't actually in a mode (``send-keys -X cancel`` types
        nothing in that case).
        """
        if not settings_mod.get_flag("ttyd_enabled"):
            return {"ok": True}
        pool = _get_tmux_pool()
        if not pool.has_warm_slot(session_id):
            return {"ok": True}
        try:
            await pool.cancel_copy_mode(session_id)
        except KeyError:
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, detail=f"scroll-bottom failed: {exc}")
        return {"ok": True}

    @app.post("/api/orchestrator/sessions/{session_id}/mouse")
    async def api_term_set_mouse(
        session_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Toggle tmux mouse-mode for the session's pane.

        Powers the terminal "select text" button: with mouse OFF a browser drag
        does a native xterm selection (so the user can drag-copy a URL/token)
        instead of the drag being captured by tmux's copy-mode. Body: ``{on: bool}``.
        No-op (still ``ok``) when the terminal is off or the slot is cold.
        """
        if not settings_mod.get_flag("ttyd_enabled"):
            return {"ok": True}
        on = bool(payload.get("on"))
        pool = _get_tmux_pool()
        if not pool.has_warm_slot(session_id):
            return {"ok": True}
        try:
            await pool.set_mouse(session_id, on)
        except KeyError:
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, detail=f"set mouse failed: {exc}")
        return {"ok": True, "mouse": on}

    @app.get("/api/orchestrator/global-agent/prompt")
    async def api_global_agent_prompt_get() -> dict[str, Any]:
        """Prompt layers for the Global agent (the meta-area "Global").

        Mirrors the per-agent ``/agent/prompts`` shape: ``general`` +
        ``orchestrator`` are read-only (managed/version-migrated), ``custom`` is
        the user-editable layer (``orchestrator-custom.md``) appended for global
        sessions.
        """
        def _read(p) -> str:
            try:
                return p.read_text(encoding="utf-8")
            except (OSError, FileNotFoundError):
                return ""
        return {
            "general": {"content": _read(agent_prompts_mod.general_prompt_path()), "readonly": True},
            "orchestrator": {"content": _read(agent_prompts_mod.orchestrator_prompt_path()), "readonly": True},
            "custom": {"content": agent_prompts_mod.read_global_custom()},
        }

    @app.patch("/api/orchestrator/global-agent/prompt")
    async def api_global_agent_prompt_patch(
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Write the Global agent's editable custom prompt layer."""
        if not isinstance(payload, dict) or "custom" not in payload:
            raise HTTPException(400, detail="custom field required")
        custom = payload.get("custom")
        if not isinstance(custom, str):
            raise HTTPException(400, detail="custom must be a string")
        if len(custom.encode("utf-8")) > 100_000:
            raise HTTPException(400, detail="custom too large (>100k bytes)")
        try:
            agent_prompts_mod.write_global_custom(custom)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        return {"ok": True, "custom": {"content": agent_prompts_mod.read_global_custom()}}

    @app.get("/api/orchestrator/pool")
    async def api_pool() -> dict[str, Any]:
        """Active interactive (tmux) sessions, enriched with title + agent.

        Powers the System-view diagnostics card, the desktop agent-tab strip,
        AND the mobile terminal's "Sesje" soft-key view (a session switcher).
        Lightweight — reads the existing pool snapshot (no spawn) + cached
        session summaries, reconciled against a single ``tmux list-sessions``
        probe so a slot whose REPL died out-of-band can't show as a phantom
        agent/session.
        """
        return await tmux_pool_snapshot_live()

    @app.get("/api/orchestrator/capabilities")
    async def api_capabilities() -> dict[str, Any]:
        """Feature/limits advertisement for an external orchestrator."""
        return await _capabilities_handler()

    @app.get("/api/orchestrator/turns/running")
    async def api_turns_running() -> dict[str, Any]:
        """Sessions with an in-flight turn right now."""
        return await _turns_running_handler()

    @app.get("/api/orchestrator/sessions")
    async def api_sessions(
        cwd: str | None = Query(default=None),
        include_corpus: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        """List sessions with sidecar overlay, newest first.

        Optional ``?cwd=<abs_path>`` filters to sessions whose sidecar cwd
        matches exactly. The sentinel ``__global__`` returns sessions whose
        cwd is None (legacy / non-agent sessions).

        Optional ``?include_corpus=true`` returns the heavy ``corpus`` field
        (~tens of KB per session) for client-side MiniSearch indexing.
        Default is False — the UI lazy-fetches corpora via
        ``/api/orchestrator/sessions/corpora`` when search opens.
        """
        try:
            return await _list_sessions_handler(cwd_filter=cwd, include_corpus=include_corpus)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.get("/api/orchestrator/sessions/corpora")
    async def api_sessions_corpora() -> list[dict[str, str]]:
        """Per-session corpus text for client-side fuzzy search.

        Fetched lazily by the UI when the search box opens. Splitting this
        out of the main list endpoint cuts the per-poll payload by a couple
        hundred KB on a 50+ session box (since the UI poll only needs
        metadata, not the haystack)."""
        return await _list_session_corpora_handler()

    @app.get("/api/orchestrator/sessions/search")
    async def api_sessions_search(
        q: str = Query(...),
        limit: int = Query(default=search_mod._LIMIT_DEFAULT),
        cwd: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Content search over session transcripts ("find the session about X").

        Hybrid BM25 ⊕ TF-IDF cosine over the existing per-session corpora;
        ``mode`` reports ``"hybrid"`` (sklearn present) or ``"lexical"``.
        Optional ``?cwd=`` restricts to one agent's sessions (validated under
        HOME). ``limit`` clamps to 1..100. 429 when the search cap is hit.
        """
        cwd_filter: str | None = None
        if cwd:
            try:
                cwd_filter = _validate_cwd_under_home(cwd)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))
        try:
            return await search_mod.search(q, limit, cwd_filter)
        except search_mod.SearchSaturated:
            raise HTTPException(429, detail="search busy; retry shortly")

    @app.post("/api/orchestrator/sessions")
    async def api_create_session(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Create a new session id; JSONL appears on first turn."""
        try:
            return await _create_session_handler(payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.patch("/api/orchestrator/sessions/{session_id}")
    async def api_patch_session(
        session_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Mutate sidecar entry (title/archived)."""
        try:
            return await _patch_session_handler(session_id, payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/orchestrator/sessions/read-all")
    async def api_mark_all_read() -> dict[str, Any]:
        """Mark EVERY session read (last_read = msg_count) in one shot.

        Clears the unread backlog so the red "new activity" dot only lights
        for turns that land FROM NOW ON. Declared before the
        ``{session_id}/read`` route so the static path wins the match.
        """
        try:
            summaries = jsonl_mod.list_sessions()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, detail=f"list_sessions failed: {e}")
        marked = 0
        for s in summaries:
            sid = s.get("id")
            mc = int(s.get("msg_count") or 0)
            if not sid or mc <= 0:
                continue
            try:
                await meta_mod.set_meta(sid, last_read_msg_count=mc)
                marked += 1
            except Exception as exc:  # noqa: BLE001 — best-effort, skip + continue
                print(f"[orchestrator] read-all: {sid[:8]} failed: {exc}")
        return {"ok": True, "marked": marked}

    @app.post("/api/orchestrator/sessions/{session_id}/read")
    async def api_mark_session_read(
        session_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Mark all messages up to ``msg_count`` as read for this session.

        Body: ``{"msg_count": <int>}``. Pass the count returned by the
        sessions list (``msg_count`` field) at the moment of viewing. Stored
        as ``last_read_msg_count`` in the sidecar; ``unread_count`` derives
        from ``msg_count - last_read_msg_count`` on subsequent list polls.

        Cross-device by design — every client opening this session sees the
        same unread number.
        """
        try:
            return await _mark_session_read_handler(session_id, payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.patch("/api/orchestrator/sessions/{session_id}/model")
    async def api_patch_session_model(
        session_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Set or clear the per-session `--model` override."""
        try:
            return await _patch_model_handler(session_id, payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.delete("/api/orchestrator/sessions/{session_id}")
    async def api_delete_session(session_id: str) -> dict[str, Any]:
        """Delete JSONL transcript and sidecar entry."""
        try:
            return await _delete_session_handler(session_id)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/orchestrator/sessions/{session_id}/close")
    async def api_close_session(
        session_id: str, kill: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Free the session's pool slot; keep the transcript. ``?kill=true`` ends
        the REPL outright (used by close-agent) instead of detaching it."""
        if not uploads_module.SESSION_ID_RE.fullmatch(session_id or ""):
            raise HTTPException(400, detail="invalid session_id")
        try:
            return await _close_session_handler(session_id, kill=kill)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/orchestrator/sessions/{session_id}/start")
    async def api_start_session(session_id: str) -> dict[str, Any]:
        """Bring the session's interactive (tmux) runtime online + warm."""
        if not uploads_module.SESSION_ID_RE.fullmatch(session_id or ""):
            raise HTTPException(400, detail="invalid session_id")
        return await _start_session_handler(session_id)

    @app.post("/api/orchestrator/sessions/{session_id}/stop")
    async def api_stop_session(session_id: str) -> dict[str, Any]:
        """Tear down the session's live runtime (keep the transcript)."""
        if not uploads_module.SESSION_ID_RE.fullmatch(session_id or ""):
            raise HTTPException(400, detail="invalid session_id")
        return await _stop_session_handler(session_id)

    @app.get("/api/orchestrator/sessions/{session_id}/messages")
    async def api_read_messages(
        session_id: str,
        limit: int | None = Query(default=None),
        after_turn: int | None = Query(default=None),
    ) -> dict[str, Any]:
        """Return parsed transcript; optional ``?limit`` (tail) + ``?after_turn``."""
        try:
            return await _read_messages_handler(
                session_id, limit=limit, after_turn=after_turn
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/orchestrator/sessions/{session_id}/messages")
    async def api_post_message(
        session_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Kick off a turn (background ClaudeRunner)."""
        try:
            return await _post_message_handler(session_id, payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.get("/api/orchestrator/sessions/{session_id}/stream")
    async def api_stream(request: Request, session_id: str) -> StreamingResponse:
        """SSE feed for the active turn (honors Last-Event-ID for resume)."""
        try:
            return await _stream_handler(request, session_id)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.get("/api/orchestrator/sessions/{session_id}/status")
    async def api_session_status(session_id: str) -> dict[str, Any]:
        """Runner liveness snapshot for the frontend's poll fallback."""
        try:
            return await _status_handler(session_id)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.get("/api/orchestrator/sessions/{session_id}/wait")
    async def api_session_wait(
        session_id: str,
        since_turn: int = Query(default=-1),
        timeout: float = Query(default=wait_mod.WAIT_TIMEOUT_DEFAULT_S),
    ) -> dict[str, Any]:
        """Long-poll until the next assistant turn past ``since_turn`` lands.

        The request/response primitive for an external orchestrator: pass the
        ``expected_turn_idx`` returned by ``POST /messages`` as ``since_turn``.
        Resolves with ``status`` ∈ ``{"done","error","timeout"}``; ``timeout``
        is clamped to ``(0, 60]`` seconds (default 25). Returns 429 when the
        process-wide waiter cap is hit. Rides the persistent ``turn_done`` /
        ``turn_error`` hub events, so it survives the turn /stream's 60 s reap.
        """
        if not uploads_module.SESSION_ID_RE.fullmatch(session_id or ""):
            raise HTTPException(400, detail="invalid session_id")
        try:
            return await wait_mod.await_turn_guarded(
                session_id,
                since_turn,
                timeout,
                hub=events_mod.get_hub(),
                read_session=jsonl_mod.read_session,
            )
        except wait_mod.WaitSaturated:
            raise HTTPException(429, detail="too many concurrent waiters; retry shortly")

    @app.get("/api/orchestrator/sessions/{session_id}/pane")
    async def api_pane_stream(request: Request, session_id: str) -> StreamingResponse:
        """SSE feed of tmux pane snapshots for the live preview modal."""
        try:
            return await _pane_stream_handler(request, session_id)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/orchestrator/presence/{session_id}")
    async def api_presence(
        session_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, bool]:
        """Heartbeat endpoint so the runner can suppress chat push when watched.

        Body: ``{"client_id": "<uuid>", "visible": <bool>}``. Best-effort —
        unknown sessions still return 200 so the frontend can fire-and-forget
        without coordinating with /sessions list state.
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")
        client_id = payload.get("client_id")
        visible = payload.get("visible")
        if not isinstance(client_id, str) or not client_id.strip():
            raise HTTPException(400, detail="client_id must be a non-empty string")
        if not isinstance(visible, bool):
            raise HTTPException(400, detail="visible must be a boolean")
        try:
            runner.record_presence(session_id, client_id.strip(), visible)
        except Exception:  # noqa: BLE001 — presence is best-effort
            pass
        return {"ok": True}

    @app.post("/api/orchestrator/sessions/{session_id}/uploads")
    async def api_upload(
        session_id: str,
        files: list[UploadFile] = File(...),
        stage_only: bool = Query(False),
    ) -> dict[str, Any]:
        """Persist uploaded files under the session's upload dir.

        ``stage_only=1`` skips the ``.pending.json`` queue — the interactive
        terminal uses it so the file is saved + the path returned for pasting
        into tmux, without also auto-attaching it to the next chat turn.
        """
        try:
            saved = await uploads_module.save_uploads(
                session_id, files, queue_pending=not stage_only,
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        return {"ok": True, "saved": saved}

    @app.get("/api/orchestrator/sessions/{session_id}/uploads")
    async def api_list_uploads(session_id: str) -> dict[str, Any]:
        """List previously persisted uploads for a session."""
        try:
            items = uploads_module.list_uploads(session_id)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        return {"ok": True, "items": items}

    @app.get("/api/orchestrator/uploads/{session_id}/{filename:path}")
    async def api_serve_upload(session_id: str, filename: str) -> FileResponse:
        """Serve a single upload file inline."""
        try:
            p = uploads_module.safe_upload_path(session_id, filename)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        if not p.is_file():
            raise HTTPException(404, detail="not found")
        return FileResponse(p, headers={"Content-Disposition": f'inline; filename="{p.name}"'})

    @app.get("/api/orchestrator/sessions/{session_id}/state")
    async def api_session_state(session_id: str) -> dict:
        """Return current todos + latest plan for the session viewers.

        Orphan sessions (sidecar entry exists but JSONL hasn't been written
        yet — `claude -p` only creates the transcript on first turn) return
        empty state instead of 404, so the frontend's `useSessionState`
        hook doesn't log an error for legitimately-empty sessions.
        """
        if not jsonl_mod.jsonl_path(session_id).exists():
            overlay = meta_mod.all_meta()
            if session_id in overlay:
                return {"todos": [], "plan": None}
            raise HTTPException(404, detail="session not found")
        return await session_state_mod.get_session_state(session_id)

    @app.post("/api/orchestrator/sessions/{session_id}/cancel")
    async def api_cancel(session_id: str) -> dict[str, Any]:
        """Terminate the active runner."""
        try:
            return await _cancel_handler(session_id)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/orchestrator/sessions/{session_id}/compact")
    async def api_compact(session_id: str) -> dict[str, Any]:
        """V2 stub — always returns deferred."""
        return await _compact_handler(session_id)

    # ── session teleport (issue #91): export/import a session as a file ──
    @app.get("/api/orchestrator/sessions/{session_id}/teleport")
    async def api_teleport_export(session_id: str) -> JSONResponse:
        """Download a self-contained teleport bundle for this session."""
        try:
            envelope = await asyncio.to_thread(teleport_mod.export_session, session_id)
        except FileNotFoundError:
            raise HTTPException(404, detail="session not found")
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        filename = f"teleport-{session_id}.json"
        return JSONResponse(
            envelope,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/orchestrator/sessions/teleport")
    async def api_teleport_import(
        request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """Plug an uploaded teleport bundle into a chosen agent. Token-gated."""
        expected = artifacts_mod.read_token()
        got = request.headers.get("x-artifact-token") or ""
        if not expected or not hmac.compare_digest(got, expected):
            raise HTTPException(401, detail="invalid artifact token")
        if not isinstance(payload, dict) or "lib_id" not in payload:
            raise HTTPException(400, detail="lib_id is required (specify the target agent)")
        try:
            return await teleport_mod.import_session(
                payload.get("envelope"),
                lib_id=payload.get("lib_id"),
                title=payload.get("title"),
                model=payload.get("model"),
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(404, detail=str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, detail=f"teleport import failed: {e}")

    # ── teleport skill distribution (install on a local/remote agent) ──
    def _teleport_base_url(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        )
        return f"{proto}://{host}".rstrip("/")

    @app.get("/api/orchestrator/teleport/skill.tar.gz")
    async def api_teleport_skill_tarball() -> Response:
        """Download the teleport skill as a tarball (for local self-install)."""
        try:
            data = await asyncio.to_thread(teleport_mod.build_skill_tarball)
        except FileNotFoundError:
            raise HTTPException(404, detail="teleport skill not available")
        return Response(
            content=data,
            media_type="application/gzip",
            headers={"Content-Disposition": 'attachment; filename="teleport.tar.gz"'},
        )

    @app.get("/api/orchestrator/teleport/install")
    async def api_teleport_install(request: Request) -> Response:
        """Markdown install + usage doc; the URL a user pastes to a local agent."""
        doc = teleport_mod.install_doc(_teleport_base_url(request))
        return Response(content=doc, media_type="text/markdown; charset=utf-8")

    @app.get("/api/orchestrator/teleport/info")
    async def api_teleport_info(request: Request) -> dict[str, Any]:
        """URLs + the artifact token Settings shows for local-agent setup."""
        base = _teleport_base_url(request)
        return {
            "base_url": base,
            "install_url": f"{base}/api/orchestrator/teleport/install",
            "skill_url": f"{base}/api/orchestrator/teleport/skill.tar.gz",
            "install_prompt": teleport_mod.install_prompt(base),
            "token": artifacts_mod.read_token() or "",
        }

    @app.get("/api/orchestrator/settings")
    async def api_get_settings() -> dict[str, Any]:
        """Return server-side flags (with defaults filled in)."""
        return settings_mod.get_settings()

    @app.patch("/api/orchestrator/settings")
    async def api_patch_settings(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Merge known boolean flags into the on-disk settings."""
        try:
            return await settings_mod.set_settings(payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    # ── terminal soft-keyboard layout ──────────────────────────────
    # Opt-in manager (gated by the `terminal_shortcuts_enabled` flag). The
    # `enabled` field rides along so the frontend learns the flag state from a
    # single GET. `layout` is the FULL views+buttons layout tree (seeded from
    # defaults when no file). See orchestrator_terminal_shortcuts.py.

    def _shortcuts_enabled() -> bool:
        return bool(settings_mod.get_flag("terminal_shortcuts_enabled"))

    @app.get("/api/orchestrator/terminal-shortcuts")
    async def api_get_terminal_shortcuts() -> dict[str, Any]:
        """Return {enabled, layout} for the mobile soft-keyboard manager."""
        return {"enabled": _shortcuts_enabled(), "layout": shortcuts_mod.get_layout()}

    @app.put("/api/orchestrator/terminal-shortcuts")
    async def api_put_terminal_shortcuts(
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Replace the layout (full state, sanitized) and echo it back."""
        try:
            layout = await shortcuts_mod.set_layout(payload)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        return {"enabled": _shortcuts_enabled(), "layout": layout}

    @app.post("/api/orchestrator/terminal-shortcuts/reset")
    async def api_reset_terminal_shortcuts() -> dict[str, Any]:
        """Reseed the canonical default layout."""
        layout = await shortcuts_mod.reset_layout()
        return {"enabled": _shortcuts_enabled(), "layout": layout}

    # ── agent-tab custom order ─────────────────────────────────────
    # Persisted (server-side, cross-device) drag-and-drop order for the agent
    # tab strip. Dumb key-list store; the frontend merges it with the live pool
    # (new agents append, absent saved keys are skipped). See
    # orchestrator_agent_tab_order.py.

    @app.get("/api/orchestrator/agent-tab-order")
    async def api_get_agent_tab_order() -> dict[str, Any]:
        """Return {order: [agent_key, …]} — the saved tab order (may be empty)."""
        order = await asyncio.to_thread(tab_order_mod.get_order)
        return {"order": order}

    @app.put("/api/orchestrator/agent-tab-order")
    async def api_put_agent_tab_order(
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Replace the whole saved order (sanitized) and echo it back."""
        order = await asyncio.to_thread(tab_order_mod.set_order, payload.get("order"))
        return {"order": order}

    # ── push notifications ─────────────────────────────────────────

    @app.get("/api/notifications/vapid-key")
    async def api_vapid_key() -> dict:
        """Return the VAPID public key for the frontend's PushManager.subscribe."""
        return {"public_key": notifs_module.get_public_key()}

    @app.post("/api/notifications/subscribe")
    async def api_subscribe(request: Request) -> dict:
        """Persist a Web Push subscription keyed by the device's stable UUID."""
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")
        device_id = payload.get("device_id")
        subscription = payload.get("subscription")
        if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
            raise HTTPException(400, detail="invalid device_id")
        if not isinstance(subscription, dict):
            raise HTTPException(400, detail="subscription must be an object")
        if not isinstance(subscription.get("endpoint"), str):
            raise HTTPException(400, detail="subscription.endpoint required")
        keys = subscription.get("keys")
        if (
            not isinstance(keys, dict)
            or not isinstance(keys.get("p256dh"), str)
            or not isinstance(keys.get("auth"), str)
        ):
            raise HTTPException(400, detail="subscription.keys.{p256dh,auth} required")
        await notifs_module.add_subscription(device_id, subscription)
        return {"ok": True}

    @app.delete("/api/notifications/subscribe/{device_id}")
    async def api_unsubscribe(device_id: str) -> dict:
        """Remove a device's subscription. Idempotent."""
        if not _DEVICE_ID_RE.match(device_id):
            raise HTTPException(400, detail="invalid device_id")
        await notifs_module.remove_subscription(device_id)
        return {"ok": True}
