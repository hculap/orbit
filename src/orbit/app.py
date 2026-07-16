"""FastAPI app — the dashboard."""
from __future__ import annotations
import asyncio
import io
import os
import subprocess
import sys
import time
import zipfile
from contextlib import asynccontextmanager

# Strong refs to fire-and-forget startup tasks (e.g. pool pre-warm) so the
# event loop doesn't GC them mid-flight. Entries self-remove on completion.
_BOOT_TASKS: set = set()
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .discovery import discover_all
from .config import load_overrides
from .system_status import all_status as system_status
from . import system_processes
from . import agent_prompts as agent_prompts_mod
from . import bundled_skills as bundled_skills_mod
from . import cron_scheduler as cron_scheduler_mod
from . import logs as logs_module
from . import share as share_module
from . import orchestrator as orchestrator_module
from . import library as library_mod
from . import tasks as tasks_module

PKG_DIR = Path(__file__).parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"
REPO_ROOT = PKG_DIR.parent.parent


def _get_git_sha() -> str:
    """Best-effort short git SHA — read once at import time. systemd restart
    after deploy re-reads it. Falls back to a process-start timestamp so the
    SW always has SOMETHING to version against."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=2,
        )
        sha = (out.stdout or "").strip()[:12]
        if sha:
            return sha
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    # No git available (e.g. running from a tarball) — fall back to a value
    # that at least changes between restarts, so the SW still detects deploys.
    return f"start-{int(time.time())}"


GIT_SHA: str = _get_git_sha()


class _NoCacheStatic(StaticFiles):
    """StaticFiles that forces revalidation on `.jsx` / `.html` / `.js`.

    Browsers (especially iOS PWA) hold these in heuristic cache for hours
    when the server only sends an `ETag`. Clients then miss a fresh deploy
    because they never even issue the conditional GET. ``no-cache`` keeps
    the cache (so 304s still spare bytes) but forces revalidation each load
    so a deploy is picked up on next page open. Static media (.png/.svg…)
    keeps default caching since those don't change between deploys.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if path.endswith((".jsx", ".js", ".html", ".css")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _filter_relative_time(ts: float) -> str:
    """'2h ago', '3d ago', 'just now'."""
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta / 86400)}d ago"
    if delta < 86400 * 365:
        return f"{int(delta / 86400 / 30)}mo ago"
    return f"{int(delta / 86400 / 365)}y ago"


def _filter_iso_date(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _filter_format_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        h, m = divmod(int(seconds / 60), 60)
        return f"{h}h {m}m"
    d, rem = divmod(int(seconds), 86400)
    h = rem // 3600
    return f"{d}d {h}h"

# Cache discovery for this many seconds (filesystem reads are cheap, but no point per-request)
DISCOVERY_TTL_S = 30

_cache: dict = {"data": None, "ts": 0.0}


def _get_data(force: bool = False) -> dict:
    now = time.time()
    # ``force`` bypasses the TTL for an explicit, user-triggered refresh (e.g.
    # opening the Agents view after the agent just created a project on disk).
    # Without it the 30s cache can hide a brand-new ~/Projects/<x> until the
    # window lapses — the "muszę odświeżyć serwer" bug.
    if force or _cache["data"] is None or (now - _cache["ts"]) > DISCOVERY_TTL_S:
        raw = discover_all()
        overrides = load_overrides()
        _cache["data"] = _apply_overrides(raw, overrides)
        _cache["ts"] = now
    return _cache["data"]


def _apply_overrides(data: dict, overrides: dict) -> dict:
    """Merge override.yaml on top of auto-discovered data.

    Override schema (all optional):
      areas:
        <name>:
          label: "Custom label"
          icon: "🏥"
          description: "..."
      projects:
        <name>:
          label: "..."
          icon: "..."
          hidden: true
      apps:
        <path>:
          label: "..."
          icon: "..."
      external_apps:
        - label: "Home Assistant"
          icon: "🏡"
          url: "http://homeassistant:8123/"
          description: "Smart home — Tailscale only"
      host:
        domain: "your-dashboard.example"
        region: "<region>"
      tab_order: [areas, apps, projects, resources]
    """
    # issues.create_repo flag — stamped onto each area/project item so the
    # detail view can show the one-click "Create GitHub repo" CTA without a
    # separate config fetch (mirrors how has_github_remote rides on the item).
    _issues_ov = overrides.get("issues")
    _issues_create_repo = bool(_issues_ov.get("create_repo", False)) if isinstance(_issues_ov, dict) else False
    for kind in ("areas", "projects", "resources", "apps"):
        items = data.get(kind, [])
        ov = overrides.get(kind, {}) or {}
        for it in items:
            if kind in ("areas", "projects"):
                it["issues_create_repo"] = _issues_create_repo
            key = it.get("name") or it.get("path")
            if key in ov:
                merge = ov[key]
                if merge.get("hidden"):
                    it["_hidden"] = True
                for k in ("label", "icon", "description"):
                    if k in merge:
                        it[k] = merge[k]
        # Drop hidden
        data[kind] = [it for it in items if not it.get("_hidden")]
    # Host metadata override (domain, region, ip…) — immutable merge
    host_ov = overrides.get("host", {}) or {}
    data["host"] = {
        **(data.get("host") or {}),
        **{k: host_ov[k] for k in ("domain", "region", "ip", "name") if host_ov.get(k)},
    }
    # External apps — apps not proxied by nginx, opened in a new tab via explicit URL
    external = overrides.get("external_apps") or []
    if isinstance(external, list):
        for ext in external:
            if not isinstance(ext, dict) or not ext.get("url"):
                continue
            data["apps"].append({
                "name": ext.get("name") or ext.get("label") or ext["url"],
                "label": ext.get("label") or ext.get("name") or ext["url"],
                "icon": ext.get("icon", "🔗"),
                "description": ext.get("description", ""),
                "url": ext["url"],
                "external": True,
            })
    if "tab_order" in overrides:
        data["tab_order"] = overrides["tab_order"]
    # Terminal access metadata (e.g. `terminal.ssh_host: my-server`) —
    # the orchestrator kebab uses it to build a copy-paste SSH+tmux attach
    # command. Validated minimally so a malformed YAML block can't inject
    # arbitrary keys into the frontend payload.
    term_ov = overrides.get("terminal") or {}
    if isinstance(term_ov, dict):
        ssh_host = term_ov.get("ssh_host")
        if isinstance(ssh_host, str) and ssh_host.strip():
            data["terminal"] = {"ssh_host": ssh_host.strip()}
    return data


async def _startup_tmux_pool() -> None:
    """Boot the tmux pool. CRITICAL ordering: ``ensure_tmux_socket_dir()`` pins
    TMUX_TMPDIR BEFORE the first tmux call (``recover_orphans``) — otherwise
    the survivor server (on the ~/.orchestrator socket) is resolved against the
    wrong /tmp socket and is invisible. Extracted from the lifespan so the
    ordering is unit-testable.
    """
    from . import orchestrator as _orch
    from . import orchestrator_tmux as _tmux
    from . import orchestrator_settings as _settings0
    _tmux.ensure_tmux_socket_dir()
    _pool = _orch._get_tmux_pool()
    await _pool.recover_orphans()
    await _pool.start()
    # Optional: pre-warm recent sessions in the background so the first open
    # after a restart is instant. Fire-and-forget — must NOT delay boot (each
    # cold spawn is 10-20 s). Strong ref kept so the task isn't GC'd mid-flight.
    # Skip prewarm on the transient standby holder (HD_STANDBY=1): it's killed
    # seconds after a deploy, so warming claude REPLs there is wasted churn.
    if os.environ.get("HD_STANDBY") != "1" and _settings0.get_flag("pool_prewarm_on_start"):
        _prewarm_task = asyncio.create_task(_orch.prewarm_recent_sessions())
        _BOOT_TASKS.add(_prewarm_task)
        _prewarm_task.add_done_callback(_BOOT_TASKS.discard)


async def _shutdown_tmux_pool() -> None:
    """Tear down the tmux pool on app shutdown — detached-aware.

    With ``tmux_detached_sessions`` ON (default) the REPLs must KEEP RUNNING so
    they outlive this restart, so we pass ``kill_sessions=False`` (drop only
    in-memory pool state; the next process re-attaches in ``acquire``). OFF
    restores the legacy graceful ``/exit`` + ``kill-session`` of every slot.
    Extracted from the lifespan so the flag→kwarg wiring is unit-testable.
    """
    from . import orchestrator as _orch
    from . import orchestrator_settings as _settings
    keep = _settings.get_flag("tmux_detached_sessions")
    await _orch._get_tmux_pool().shutdown(kill_sessions=not keep)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Boot the cron scheduler at app start; tear it down on shutdown.

    APScheduler's MemoryJobStore is rebuilt from ``~/.orchestrator/cron/jobs.json``
    here. Failures during start are logged but never block app boot — a broken
    sidecar shouldn't take down the whole dashboard.

    NOTE: Scheduler requires ``--workers 1`` on uvicorn. Multi-worker would
    instantiate N schedulers per process, each firing every job N times.
    """
    print(
        "[scheduler] orbit requires uvicorn --workers 1 "
        "(multi-worker would multiply cron fires)",
        file=sys.stderr,
    )
    try:
        from . import orchestrator_env as _env_mod
        _env_mod.warn_if_api_key_present()
    except Exception as exc:  # noqa: BLE001 — never block boot on the billing warning
        print(f"[orchestrator_env] api-key check failed: {exc}", file=sys.stderr)
    # Zero-downtime deploy: a transient STANDBY instance (HD_STANDBY=1, the green
    # holder spun up by deploy/deploy.sh) serves HTTP while the canonical blue
    # instance restarts onto new code. It MUST NOT run the cron scheduler — two
    # live schedulers would double-fire every job (scheduled agents, watchdog,
    # reminders); see the --workers 1 note above. The scheduler + its job seeds
    # + cron-job writes stay exclusively on blue. Standby still serves the API,
    # terminals, and turns (the tmux pool below runs in both — it's idempotent
    # against the shared external tmux server).
    standby = os.environ.get("HD_STANDBY") == "1"
    if standby:
        print("[scheduler] HD_STANDBY=1 — scheduler/seeds skipped (zero-downtime holder)", file=sys.stderr)
    else:
        try:
            await cron_scheduler_mod.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] start failed: {exc}", file=sys.stderr)
        try:
            from . import system_watchdog_seed as _watchdog_seed
            _watchdog_seed.seed_watchdog_job()
        except Exception as exc:  # noqa: BLE001 — soft-import; missing module ok
            print(f"[scheduler] watchdog seed failed: {exc}", file=sys.stderr)
        try:
            from . import tasks_reminders_seed as _tasks_seed
            _tasks_seed.seed_tasks_reminders_job()
        except Exception as exc:  # noqa: BLE001 — soft-import; missing module ok
            print(f"[scheduler] tasks-reminders seed failed: {exc}", file=sys.stderr)
        try:
            from . import orchestrator_a2a_gc_seed as _a2a_gc_seed
            _a2a_gc_seed.seed_a2a_gc_job()
        except Exception as exc:  # noqa: BLE001 — soft-import; missing module ok
            print(f"[scheduler] a2a-gc seed failed: {exc}", file=sys.stderr)
    # Tmux interactive-runner pool: recover orphans + start the idle evictor.
    # Soft-fail so a missing tmux on dev machines doesn't block app boot.
    try:
        await _startup_tmux_pool()
    except Exception as exc:  # noqa: BLE001
        print(f"[tmux-pool] start failed: {exc}", file=sys.stderr)
    # Persistent per-session SSE hub for artifact toasts/modals. Lives for the
    # whole process (independent of the turn runner, which reaps after 60s) so
    # an `artifact open` between turns still reaches the browser.
    try:
        from . import orchestrator_events as _events
        _events.get_hub().start()
    except Exception as exc:  # noqa: BLE001 — never block boot on the hub
        print(f"[events-hub] start failed: {exc}", file=sys.stderr)
    # ttyd inline-terminal pool: only spin up when the feature flag is on.
    # Reading the flag at lifespan-start means a flag-off dashboard never
    # spawns the evictor loop and never sweeps orphans (so it can't kill
    # ttyds the user might be running outside the dashboard for debug).
    try:
        from . import orchestrator as _orch
        from . import orchestrator_settings as _settings
        if _settings.get_flag("ttyd_enabled"):
            _ttyd = _orch._get_ttyd_pool()
            await _ttyd.recover_orphans()
            await _ttyd.start()
    except Exception as exc:  # noqa: BLE001 — never block boot on ttyd
        print(f"[ttyd-pool] start failed: {exc}", file=sys.stderr)
    try:
        yield
    finally:
        try:
            await cron_scheduler_mod.shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] shutdown failed: {exc}", file=sys.stderr)
        try:
            await _shutdown_tmux_pool()
        except Exception as exc:  # noqa: BLE001
            print(f"[tmux-pool] shutdown failed: {exc}", file=sys.stderr)
        try:
            from . import orchestrator_events as _events
            await _events.get_hub().shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[events-hub] shutdown failed: {exc}", file=sys.stderr)
        # Only shutdown ttyd pool if it was actually instantiated this run.
        # The lazy getter would otherwise create it just to tear it down,
        # which races with the start-side flag guard above.
        try:
            from . import orchestrator as _orch
            if _orch._ttyd_pool is not None:
                await _orch._get_ttyd_pool().shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[ttyd-pool] shutdown failed: {exc}", file=sys.stderr)


def create_app() -> FastAPI:
    # Seed ``~/.orchestrator/agent-prompts/{general,orchestrator}.md`` if
    # missing. Idempotent — never overwrites user edits to those files. Must
    # run before any session can spawn a runner so build_args has the files
    # available on its first --append-system-prompt-file flag.
    agent_prompts_mod.bootstrap()

    # Install repo-bundled skills (skills/<name>/) into the registry if missing,
    # so the `artifact` CLI + generate-image etc. ship with the code (no manual
    # rsync). Idempotent + best-effort — never overwrites an installed skill.
    try:
        bundled_skills_mod.seed_bundled_skills()
    except Exception as exc:  # noqa: BLE001 — never block app start on skill seeding
        print(f"[bundled_skills] seed failed: {exc}", file=sys.stderr)

    base_path = os.environ.get("BASE_PATH", "").rstrip("/")
    app = FastAPI(
        title="orbit",
        root_path=base_path,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["relative_time"] = _filter_relative_time
    templates.env.filters["iso_date"] = _filter_iso_date
    templates.env.filters["format_uptime"] = _filter_format_uptime

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        # _get_data() runs the full PARA + nginx filesystem scan on a cache
        # miss — off-load it so the homepage render never blocks the loop.
        data = await asyncio.to_thread(_get_data)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"data": data},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/api/version")
    async def api_version() -> dict:
        """Current build identifier — read by the service worker on a slow
        poll to decide whether to refresh its cache. Also used by the
        "new version" toast on the client."""
        return {"git_sha": GIT_SHA}

    @app.get("/sw.js")
    async def service_worker() -> Response:
        """Root-scoped service worker.

        Read the template from ``static/sw.js`` and substitute the build
        SHA into the placeholder so every deploy produces a byte-different
        response — that's what triggers the browser to install the new
        SW (and then our `update_available` postMessage fires).

        Served at the origin root (not under ``/static/``) so its scope
        covers the whole app. ``Service-Worker-Allowed`` header is not
        needed here because the file path IS the scope root.
        """
        sw_path = STATIC_DIR / "sw.js"
        try:
            text = sw_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(500, detail=f"sw.js missing: {exc}")
        text = text.replace("__SW_VERSION__", GIT_SHA)
        return Response(
            content=text,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache, must-revalidate",
                # Service-Worker-Allowed isn't required for a file at the
                # registration scope root, but setting it explicitly keeps
                # the header presence visible in tooling.
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/api/data")
    async def api_data(fresh: int = 0) -> dict:
        """JSON endpoint for the same payload — useful for debugging or future SPA.

        ``?fresh=1`` forces a filesystem re-scan, bypassing the discovery TTL,
        so the frontend can surface a just-created project/area immediately.
        """
        return await asyncio.to_thread(_get_data, bool(fresh))

    @app.get("/api/system")
    async def api_system() -> dict:
        """Live system metrics — CPU/RAM/disk/services/tailscale.

        Cached for ~3 s inside ``system_status.all_status`` so the
        ``systemctl`` + ``tailscale`` probes (the expensive part) don't run
        more than ~once per dashboard poll. Staleness window is well below
        the UI's 5 s refresh interval.

        Augmented with a live tmux-pool snapshot (active interactive
        sessions + their age / cooldown) for at-a-glance diagnostics.
        """
        # all_status fans 3 subprocess probes out concurrently internally, but
        # the whole thing is still blocking — run it off the event loop so the
        # ~5 s system poll never freezes concurrent requests on a cache miss.
        # Shallow-copy: all_status returns the cached snapshot object by
        # reference, so adding "tmux" below must not mutate the shared cache.
        status = dict(await asyncio.to_thread(system_status))
        try:
            from . import orchestrator as _orch
            # Live-reconciled snapshot: off-loads the synchronous filesystem
            # scan to a thread AND drops slots whose tmux died out-of-band, so
            # the System card matches the agent tabs (no phantom slots).
            status["tmux"] = await _orch.tmux_pool_snapshot_live()
        except Exception as exc:  # noqa: BLE001 — diagnostics must never 500 the page
            status["tmux"] = {"error": str(exc)[:200]}
        return status

    @app.get("/api/system/processes")
    async def api_system_processes(metric: str = "cpu", top: int = 12) -> dict:
        """Per-process attribution behind the clickable System cards (#104).

        ``metric`` is cpu | mem | disk. Off the event loop because the cpu
        path sleeps ~0.4 s for a delta sample and disk walks the filesystem;
        ``system_processes.top_for`` caches the result (4 s cpu/mem, 60 s
        disk) so this stays cheap on the drill-down's 5 s poll.
        """
        if metric not in ("cpu", "mem", "disk"):
            raise HTTPException(status_code=422, detail="metric must be cpu, mem, or disk")
        top = max(1, min(50, top))
        return await asyncio.to_thread(system_processes.top_for, metric, top)

    @app.get("/api/logs/sources")
    async def api_log_sources() -> list[dict]:
        return logs_module.list_sources()

    @app.get("/api/logs/{source_id}")
    async def api_logs(source_id: str, lines: int = 200) -> dict:
        # read_source shells out to tail/journalctl (up to 5-8s, blocking).
        # Off-load so opening the Logs tab never freezes the live header poll.
        return await asyncio.to_thread(logs_module.read_source, source_id, lines)

    # ── Share module ────────────────────────────────────────
    @app.get("/api/share")
    async def api_share_root(sizes: bool = True) -> dict:
        # list_dir with sizes=True does a recursive os.scandir walk per subdir
        # — off the loop so a large ~/Sync never freezes concurrent requests.
        return await asyncio.to_thread(
            share_module.list_dir, "", with_folder_sizes=sizes
        )

    @app.get("/api/share/size/{rel:path}")
    async def api_share_size(rel: str) -> dict:
        """On-demand single-folder size — for UI flows that want to skip
        recursive walks on the initial listing (?sizes=false) and fetch
        them lazily as the user expands folders."""
        try:
            p = share_module._safe_path(rel)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        if not p.is_dir():
            raise HTTPException(404, detail="not a directory")
        size = await asyncio.to_thread(share_module._folder_size, p)
        return {"rel": rel.strip("/"), "size": size}

    @app.get("/api/share/{rel:path}")
    async def api_share_dir(rel: str, sizes: bool = True) -> dict:
        try:
            return await asyncio.to_thread(
                share_module.list_dir, rel, with_folder_sizes=sizes
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.get("/share/download/{rel:path}")
    async def share_download(rel: str) -> FileResponse:
        try:
            p = share_module._safe_path(rel)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        if not p.is_file():
            raise HTTPException(404, detail="not a file")
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")

    @app.get("/share/preview/{rel:path}")
    async def share_preview(rel: str) -> FileResponse:
        try:
            p = share_module._safe_path(rel)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        if not p.is_file():
            raise HTTPException(404, detail="not a file")
        # FileResponse picks Content-Type from extension; inline disposition
        return FileResponse(p, headers={"Content-Disposition": f'inline; filename="{p.name}"'})

    @app.post("/share/upload")
    async def share_upload(
        rel_dir: str = Form(""),
        files: list[UploadFile] = File(...),
    ) -> dict:
        try:
            target_dir = share_module._safe_path(rel_dir)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        target_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            # Sanitize filename — strip path components
            name = (f.filename or "upload").split("/")[-1].split("\\")[-1]
            if not name or name.startswith("."):
                continue
            target = target_dir / name
            # Avoid overwrite
            i = 1
            while target.exists():
                stem, ext = target.stem, target.suffix
                target = target_dir / f"{stem} ({i}){ext}"
                i += 1
            written = 0
            with target.open("wb") as out:
                while True:
                    chunk = await f.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > share_module.MAX_UPLOAD_BYTES:
                        out.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(413, detail=f"file '{name}' exceeds {share_module.MAX_UPLOAD_BYTES} bytes")
                    out.write(chunk)
            saved.append({"name": target.name, "size": written, "rel": str(target.relative_to(share_module.SHARE_ROOT))})
        return {"saved": saved}

    @app.post("/api/share/mkdir")
    async def share_mkdir(payload: dict = Body(...)) -> dict:
        try:
            return share_module.mkdir(payload.get("rel_dir", ""), payload.get("name", ""))
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/api/share/rename")
    async def share_rename(payload: dict = Body(...)) -> dict:
        try:
            return share_module.rename(payload.get("rel", ""), payload.get("new_name", ""))
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.delete("/api/share/{rel:path}")
    async def share_delete(rel: str) -> dict:
        try:
            return share_module.delete(rel)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    @app.post("/share/download-zip")
    async def share_download_zip(payload: dict = Body(...)) -> StreamingResponse:
        """Stream a zip of selected paths (files or whole folders)."""
        rels = payload.get("paths") or []
        if not rels or not isinstance(rels, list):
            raise HTTPException(400, detail="paths required")
        # Validate all paths upfront
        resolved = []
        for rel in rels:
            try:
                p = share_module._safe_path(rel)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))
            if not p.exists():
                continue
            resolved.append(p)
        if not resolved:
            raise HTTPException(404, detail="no valid paths")

        def gen():
            buf = io.BytesIO()
            zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
            for p in resolved:
                if p.is_file():
                    zf.write(p, arcname=p.name)
                elif p.is_dir():
                    base = p.parent
                    for f in p.rglob("*"):
                        if f.is_file():
                            zf.write(f, arcname=str(f.relative_to(base)))
            zf.close()
            yield buf.getvalue()

        ts = time.strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            gen(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="share-{ts}.zip"'},
        )

    @app.get("/share/thumb/{rel:path}")
    async def share_thumb(rel: str, size: int = 256) -> Response:
        """Generate small JPEG thumbnail for an image. 256px default."""
        try:
            p = share_module._safe_path(rel)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))
        if not p.is_file():
            raise HTTPException(404)
        size = max(32, min(int(size), 1024))
        try:
            from PIL import Image, ImageOps
            img = Image.open(p)
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            return Response(content=buf.getvalue(), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600"})
        except Exception as e:
            raise HTTPException(500, detail=f"thumbnail failed: {e}")

    # ── Orchestrator module ─────────────────────────────────────
    orchestrator_module.register_routes(app)

    # ── Tasks module (GH Project v2 backlog + reminders) ────────
    tasks_module.register_routes(app)

    # ── Library module (Areas/Projects CRUD) ────────────────────
    # Expose the discovery cache on the app so library write ops can
    # invalidate it (forcing the next /api/data to re-run discover_all).
    app._cache = _cache  # type: ignore[attr-defined]
    library_mod.register_routes(app)

    # ── SPA fallback ────────────────────────────────────────────
    # Any GET that didn't match a specific route (and isn't /api/* or
    # /static/*) is the SPA's job — return the same index.html so the
    # frontend router can resolve the path. Registered LAST so all
    # specific routes take precedence (FastAPI uses first-match).
    @app.get("/{full_path:path}", response_class=HTMLResponse)
    async def spa_fallback(full_path: str, request: Request) -> HTMLResponse:
        if full_path.startswith(("api/", "static/")):
            raise HTTPException(404)
        # Same template + data envelope as the bare root route.
        data = await asyncio.to_thread(_get_data)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"data": data},
        )

    return app
