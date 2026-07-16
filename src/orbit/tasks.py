"""tasks — FastAPI routes for the Tasks feature.

Wired from :func:`orbit.app.create_app` via
:func:`register_routes`. Auto-discovers the live GH project schema, exposes
a CRUD-ish surface for issues on that project, and persists per-task
reminder configs to ``~/.orchestrator/tasks.json``.
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import discovery as discovery_mod
from . import tasks_config as tasks_config_mod
from . import tasks_github as tg
from . import tasks_reminders as tasks_reminders_mod
from . import tasks_storage as ts
from .tasks_github import TasksGithubError

_PERIODS = ("morning", "noon", "afternoon", "evening")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")

# ── Pydantic models ─────────────────────────────────────────────


class ReminderAtPeriod(BaseModel):
    kind: Literal["at_period"]
    offset_days: int = Field(ge=-365, le=0)
    period: Literal["morning", "noon", "afternoon", "evening"]


class ReminderAtTime(BaseModel):
    kind: Literal["at_time"]
    offset_days: int = Field(ge=-365, le=0)
    time: str = Field(pattern=r"^\d{2}:\d{2}$")


# Legacy kinds — UI no longer creates these but they're still accepted so
# pre-v2 sidecar entries don't fail validation when round-tripped through PATCH.
class ReminderBefore(BaseModel):
    kind: Literal["before"]
    value: int = Field(ge=1, le=10080)
    unit: Literal["min", "hour", "day"]


class ReminderMorningOf(BaseModel):
    kind: Literal["morning_of"]


class ReminderExact(BaseModel):
    kind: Literal["exact"]


Reminder = Annotated[
    Union[ReminderAtPeriod, ReminderAtTime, ReminderBefore, ReminderMorningOf, ReminderExact],
    Field(discriminator="kind"),
]


def _reminder_to_dict(r: BaseModel | dict) -> dict[str, Any]:
    if isinstance(r, BaseModel):
        return r.model_dump()
    return dict(r)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _slugify(name: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return out or "global"


def _validate_slug_or_none(slug: str | None) -> str | None:
    if slug is None:
        return None
    s = slug.strip().lower()
    if not s:
        return None
    if not _SLUG_RE.match(s):
        raise HTTPException(400, detail=f"invalid slug {slug!r} (expected ^[a-z0-9][a-z0-9-]*$)")
    return s


class TaskCreatePayload(BaseModel):
    title: str = Field(min_length=1, max_length=400)
    body: str = ""
    repo: str | None = None  # explicit override; otherwise resolved from area/proj
    area_slug: str | None = None
    proj_slug: str | None = None
    status: str = "Inbox"
    priority: str | None = None
    category: str | None = None
    due_date: str | None = None
    due_time: str | None = None  # optional "HH:MM" local time, stored in sidecar
    waiting_on: str | None = None
    source: str | None = None
    labels: list[str] = Field(default_factory=list)
    reminders: list[Reminder] | None = None


class TaskUpdatePayload(BaseModel):
    title: str | None = None
    body: str | None = None
    status: str | None = None
    priority: str | None = None
    category: str | None = None
    due_date: str | None = None  # empty string clears
    due_time: str | None = None  # "" clears, "HH:MM" sets
    waiting_on: str | None = None
    source: str | None = None
    area_slug: str | None = None
    proj_slug: str | None = None
    add_labels: list[str] | None = None
    remove_labels: list[str] | None = None
    reminders: list[Reminder] | None = None


# ── Standalone reminder payloads ───────────────────────────────


class StandaloneReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=400)
    body: str | None = None
    fire_at: str  # ISO datetime with or without tz
    priority: str | None = None
    area_slug: str | None = None
    proj_slug: str | None = None
    task_link: str | None = None  # optional issue_node_id


class StandaloneReminderUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    fire_at: str | None = None
    priority: str | None = None
    area_slug: str | None = None
    proj_slug: str | None = None
    task_link: str | None = None


# ── error mapping ───────────────────────────────────────────────


def _to_http(exc: TasksGithubError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_hint,
        detail={"error": exc.message, "kind": exc.kind, "detail": exc.detail},
    )


def _require_cfg() -> tasks_config_mod.TasksConfig:
    cfg = tasks_config_mod.load()
    if not cfg.is_configured():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "tasks feature is disabled — set tasks.enabled and tasks.project_url in config/override.yaml",
                "kind": "config",
            },
        )
    return cfg


# ── filter helpers ──────────────────────────────────────────────


def _bucket_for(due: str | None) -> str:
    if not due:
        return "no-due"
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return "no-due"
    today = date.today()
    delta = (d - today).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta <= 7:
        return "this-week"
    return "later"


def _apply_filters(
    items: list[dict[str, Any]],
    *,
    state: str,
    status: list[str] | None,
    priority: list[str] | None,
    category: list[str] | None,
    area: list[str] | None,
    project: list[str] | None,
    due_before: str | None,
    due_after: str | None,
    due_bucket: list[str] | None,
    q: str | None,
) -> list[dict[str, Any]]:
    state_lo = (state or "open").lower()
    out = []
    q_low = (q or "").strip().lower()
    for it in items:
        item_state = (it.get("state") or "OPEN").upper()
        if state_lo == "open" and item_state != "OPEN":
            continue
        if state_lo == "closed" and item_state != "CLOSED":
            continue
        if status and it.get("status") not in status:
            continue
        if priority and it.get("priority") not in priority:
            continue
        if category and it.get("category") not in category:
            continue
        if area and it.get("area_slug") not in area:
            continue
        if project and it.get("proj_slug") not in project:
            continue
        due = it.get("due_date")
        if due_before:
            try:
                if not due or date.fromisoformat(due) >= date.fromisoformat(due_before):
                    continue
            except ValueError:
                continue
        if due_after:
            try:
                if not due or date.fromisoformat(due) <= date.fromisoformat(due_after):
                    continue
            except ValueError:
                continue
        if due_bucket and _bucket_for(due) not in due_bucket:
            continue
        if q_low:
            hay = " ".join([
                (it.get("title") or ""),
                " ".join(it.get("labels") or []),
            ]).lower()
            if q_low not in hay:
                continue
        out.append(it)
    return out


def _enrich(item: dict[str, Any]) -> dict[str, Any]:
    """Add server-side derived bits the frontend needs (reminder count + due_time)."""
    node_id = item.get("issue_node_id")
    entry = ts.get_entry(node_id) if isinstance(node_id, str) else None
    reminders = (entry or {}).get("reminders") or []
    due_time = (entry or {}).get("due_time")
    return {**item, "reminders_count": len(reminders), "due_time": due_time}


# ── Flatten reminders (task-attached + standalone) ─────────────


def _parse_fire_at(iso: str | None, tz: ZoneInfo) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _flatten_reminders(
    items: list[dict[str, Any]],
    cfg: tasks_config_mod.TasksConfig,
) -> list[dict[str, Any]]:
    """Return a flat list of reminders (task-attached + standalone), each with
    computed fire_at, fired_at, label, links."""
    tz = ZoneInfo(cfg.timezone)
    out: list[dict[str, Any]] = []
    items_by_id = {it["issue_node_id"]: it for it in items if it.get("issue_node_id")}
    # Task-attached
    for node_id, entry in ts.all_entries().items():
        item = items_by_id.get(node_id)
        if not item:
            continue  # task closed or filtered out
        due_iso = entry.get("due_at_local") or item.get("due_date")
        due_time = entry.get("due_time")
        for idx, rem in enumerate(entry.get("reminders") or []):
            fa = tasks_reminders_mod.compute_fire_time(due_iso, rem, cfg.fire_times, tz, due_time=due_time)
            fired_ts = (entry.get("fired") or {}).get(str(idx))
            out.append({
                "id": f"task:{node_id}:{idx}",
                "kind": "task",
                "spec": rem,
                "spec_label": tasks_reminders_mod.format_lead(rem),
                "fire_at": fa.isoformat(timespec="seconds") if fa else None,
                "fired_at": fired_ts,
                "title": item.get("title") or "",
                "body": None,
                "priority": item.get("priority"),
                "area_slug": item.get("area_slug"),
                "proj_slug": item.get("proj_slug"),
                "category": item.get("category"),
                "task": {
                    "issue_node_id": node_id,
                    "number": item.get("number"),
                    "repo": item.get("repo"),
                    "url": item.get("url"),
                    "due_date": item.get("due_date"),
                    "due_time": due_time,
                },
            })
    # Standalone
    for rid, entry in ts.all_standalone().items():
        fa = _parse_fire_at(entry.get("fire_at"), tz)
        fired_at = entry.get("fired_at")
        task_link = entry.get("task_link")
        linked_item = items_by_id.get(task_link) if task_link else None
        out.append({
            "id": f"standalone:{rid}",
            "kind": "standalone",
            "spec": None,
            "spec_label": "standalone",
            "fire_at": fa.isoformat(timespec="seconds") if fa else entry.get("fire_at"),
            "fired_at": fired_at,
            "title": entry.get("title") or "",
            "body": entry.get("body"),
            "priority": entry.get("priority"),
            "area_slug": entry.get("area_slug") or (linked_item.get("area_slug") if linked_item else None),
            "proj_slug": entry.get("proj_slug") or (linked_item.get("proj_slug") if linked_item else None),
            "category": None,
            "task": (
                {
                    "issue_node_id": task_link,
                    "number": linked_item.get("number") if linked_item else None,
                    "repo": linked_item.get("repo") if linked_item else None,
                    "url": linked_item.get("url") if linked_item else None,
                    "due_date": linked_item.get("due_date") if linked_item else None,
                    "due_time": None,
                } if task_link else None
            ),
        })
    return out


def _within_to_delta(within: str) -> timedelta | None:
    return {
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }.get(within)


def _filter_reminders(
    rows: list[dict[str, Any]],
    *,
    state: str,
    within: str,
    area: list[str] | None,
    project: list[str] | None,
    kind: str,
    q: str | None,
    tz: ZoneInfo,
    grace_hours: int,
) -> list[dict[str, Any]]:
    now = datetime.now(tz)
    delta = _within_to_delta(within)
    q_low = (q or "").strip().lower()
    grace = timedelta(hours=max(0, int(grace_hours)))
    out = []
    for r in rows:
        fired = r.get("fired_at") is not None
        if state == "pending" and fired:
            continue
        if state == "fired" and not fired:
            continue
        if kind == "task" and r.get("kind") != "task":
            continue
        if kind == "standalone" and r.get("kind") != "standalone":
            continue
        if area and r.get("area_slug") not in area:
            continue
        if project and r.get("proj_slug") not in project:
            continue
        if delta:
            try:
                fa_dt = datetime.fromisoformat((r.get("fire_at") or "").replace("Z", "+00:00"))
            except ValueError:
                fa_dt = None
            if fa_dt is None:
                continue
            if fa_dt.tzinfo is None:
                fa_dt = fa_dt.replace(tzinfo=tz)
            fa_dt = fa_dt.astimezone(tz)
            if state == "pending":
                # Same grace window the scan loop uses: pending rows older than
                # `grace` are stale (the tick would have silent-fired them by now),
                # so don't show them as still-pending in the UI.
                if fa_dt < now - grace or fa_dt > now + delta:
                    continue
            elif state == "fired":
                if not fired or fa_dt < now - delta:
                    continue
            else:  # all
                if fa_dt < now - delta or fa_dt > now + delta:
                    continue
        if q_low:
            hay = " ".join([(r.get("title") or ""), (r.get("body") or "")]).lower()
            if q_low not in hay:
                continue
        out.append(r)
    return out


# ── area/project listing ────────────────────────────────────────


def _list_areas_and_projects(cfg: tasks_config_mod.TasksConfig) -> dict[str, list[dict[str, str]]]:
    """Combine PARA discovery + configured maps so the create form has a single source.

    Each record now carries `path` (absolute, under HOME) so the frontend can
    spawn agent sessions with the correct cwd for an area / project.
    """
    from pathlib import Path as _P
    areas: dict[str, dict[str, str]] = {}
    projects: dict[str, dict[str, str]] = {}
    try:
        areas_root = discovery_mod.AREAS
        for a in discovery_mod.discover_areas() or []:
            name = a.get("name") or ""
            if not name:
                continue
            slug = _slugify(name)
            label = a.get("label") or name
            path = str(areas_root / name)
            areas[slug] = {
                "slug": slug, "label": label,
                "repo": cfg.area_repo_map.get(slug, ""),
                "path": path,
            }
        for p in discovery_mod.discover_projects({}) or []:
            name = p.get("name") or ""
            if not name:
                continue
            slug = _slugify(name)
            label = p.get("label") or name
            linked = (p.get("linked_areas") or [None])[0] if isinstance(p.get("linked_areas"), list) else None
            # `rel_path` is "Projects/<group>/<name>" relative to HOME (or just
            # "Projects/<name>" for top-level), so HOME / rel_path works for
            # both nested and direct PARA projects.
            rel = p.get("rel_path")
            path = str(_P.home() / rel) if isinstance(rel, str) and rel else ""
            projects[slug] = {
                "slug": slug,
                "label": label,
                "repo": cfg.proj_repo_map.get(slug, ""),
                "area_slug": _slugify(linked) if isinstance(linked, str) else "",
                "path": path,
            }
    except Exception as exc:  # noqa: BLE001 — discovery is optional
        print(f"[tasks] discovery failed: {exc}", file=sys.stderr)
    # Fold in any area/proj_repo_map slugs that don't have a PARA folder.
    for slug, repo in (cfg.area_repo_map or {}).items():
        if slug not in areas:
            areas[slug] = {"slug": slug, "label": slug.replace("-", " ").title(), "repo": repo, "path": ""}
    for slug, repo in (cfg.proj_repo_map or {}).items():
        if slug not in projects:
            projects[slug] = {"slug": slug, "label": slug.replace("-", " ").title(), "repo": repo, "area_slug": "", "path": ""}
    return {
        "areas": sorted(areas.values(), key=lambda x: x["label"].lower()),
        "projects": sorted(projects.values(), key=lambda x: x["label"].lower()),
    }


# ── route registration ──────────────────────────────────────────


def register_routes(app: FastAPI) -> None:
    """Mount /api/tasks/* routes onto the FastAPI app."""

    @app.get("/api/tasks/config")
    async def api_tasks_config_route() -> dict:
        cfg = _require_cfg()
        try:
            schema = await tg.fetch_project_schema(cfg)
        except TasksGithubError as exc:
            raise _to_http(exc)
        ap = _list_areas_and_projects(cfg)
        return {
            "ok": True,
            "config": {
                "project_url": cfg.project_url,
                "project_owner": cfg.project_owner,
                "project_number": cfg.project_number,
                "default_repo": cfg.default_repo,
                "fire_times": cfg.fire_times,
                "grace_window_hours": cfg.grace_window_hours,
                "timezone": cfg.timezone,
                "reminder_defaults": cfg.reminder_defaults,
                "status_options": schema.options_by_field.get("Status", []),
                "priority_options": schema.options_by_field.get("Priority", []),
                "category_options": schema.options_by_field.get("Category", []),
                "fields": sorted(schema.field_id_by_name.keys()),
                "areas": ap["areas"],
                "projects": ap["projects"],
            },
        }

    @app.get("/api/tasks/areas")
    async def api_tasks_areas_route() -> dict:
        cfg = _require_cfg()
        return {"ok": True, **_list_areas_and_projects(cfg)}

    @app.get("/api/tasks/repos")
    async def api_tasks_repos_route() -> dict:
        _require_cfg()
        try:
            repos = await tg.list_repos_for_user()
        except TasksGithubError as exc:
            raise _to_http(exc)
        return {"ok": True, "repos": repos}

    @app.post("/api/tasks/refresh")
    async def api_tasks_refresh_route() -> dict:
        cfg = _require_cfg()
        tg.invalidate_caches(schema=True, items=True, repos=True)
        try:
            listing = await tg.list_items(cfg, force=True)
        except TasksGithubError as exc:
            raise _to_http(exc)
        return {
            "ok": True,
            "items": [_enrich(it) for it in listing["items"]],
            "cached_at": listing["cached_at"],
            "fresh": listing["fresh"],
        }

    @app.get("/api/tasks/board")
    async def api_tasks_board_route(
        state: Literal["open", "closed", "all"] = "open",
        status: list[str] | None = Query(default=None),
        priority: list[str] | None = Query(default=None),
        category: list[str] | None = Query(default=None),
        area: list[str] | None = Query(default=None),
        project: list[str] | None = Query(default=None),
        due_bucket: list[str] | None = Query(default=None),
        q: str | None = None,
    ) -> dict:
        cfg = _require_cfg()
        try:
            # list_items and fetch_project_schema are independent gh round-trips
            # (up to ~20s each cold) — run them concurrently, not back-to-back.
            listing, schema = await asyncio.gather(
                tg.list_items(cfg), tg.fetch_project_schema(cfg)
            )
        except TasksGithubError as exc:
            raise _to_http(exc)
        items = _apply_filters(
            listing["items"],
            state=state, status=status, priority=priority, category=category,
            area=area, project=project, due_before=None, due_after=None,
            due_bucket=due_bucket, q=q,
        )
        items = [_enrich(it) for it in items]
        columns: dict[str, list[dict[str, Any]]] = {opt: [] for opt in schema.options_by_field.get("Status", [])}
        unassigned: list[dict[str, Any]] = []
        for it in items:
            st = it.get("status")
            if st and st in columns:
                columns[st].append(it)
            else:
                unassigned.append(it)
        if unassigned:
            columns["(no status)"] = unassigned
        return {"ok": True, "columns": columns, "cached_at": listing["cached_at"]}

    @app.get("/api/tasks")
    async def api_tasks_list_route(
        state: Literal["open", "closed", "all"] = "open",
        status: list[str] | None = Query(default=None),
        priority: list[str] | None = Query(default=None),
        category: list[str] | None = Query(default=None),
        area: list[str] | None = Query(default=None),
        project: list[str] | None = Query(default=None),
        due_before: str | None = None,
        due_after: str | None = None,
        due_bucket: list[str] | None = Query(default=None),
        q: str | None = None,
        limit: int = 500,
    ) -> dict:
        cfg = _require_cfg()
        try:
            listing = await tg.list_items(cfg)
        except TasksGithubError as exc:
            raise _to_http(exc)
        items = _apply_filters(
            listing["items"],
            state=state, status=status, priority=priority, category=category,
            area=area, project=project, due_before=due_before, due_after=due_after,
            due_bucket=due_bucket, q=q,
        )
        items = [_enrich(it) for it in items[: max(1, min(int(limit), 1000))]]
        return {"ok": True, "items": items, "cached_at": listing["cached_at"], "fresh": listing["fresh"]}

    @app.post("/api/tasks")
    async def api_tasks_create_route(payload: TaskCreatePayload) -> dict:
        cfg = _require_cfg()
        area = _validate_slug_or_none(payload.area_slug)
        proj = _validate_slug_or_none(payload.proj_slug)

        repo = payload.repo or tasks_config_mod.resolve_repo(area, proj, cfg)
        if not repo or "/" not in repo:
            raise HTTPException(400, detail={"error": "could not resolve target repo; configure tasks.default_repo or pass repo explicitly", "kind": "config"})

        labels = list(dict.fromkeys(payload.labels or []))
        if area:
            labels.append(tg.area_label(area))
        if proj:
            labels.append(tg.project_label(proj))
        if not area and not proj and "global" not in labels:
            labels.append("global")

        try:
            created = await tg.create_issue(cfg, repo=repo, title=payload.title, body=payload.body or "", labels=list(dict.fromkeys(labels)))
            item_id = await tg.add_to_project(cfg, created.issue_node_id)
            patch: dict[str, str | None] = {}
            if payload.status:
                patch["Status"] = payload.status
            if payload.priority:
                patch["Priority"] = payload.priority
            if payload.category:
                patch["Category"] = payload.category
            if payload.due_date:
                patch["Due Date"] = payload.due_date
            if payload.waiting_on is not None:
                patch["Waiting On"] = payload.waiting_on
            if payload.source is not None:
                patch["Source"] = payload.source
            if patch:
                await tg.update_fields(cfg, item_id, patch)
        except TasksGithubError as exc:
            raise _to_http(exc)

        reminders = payload.reminders if payload.reminders is not None else (
            [_reminder_to_dict(r) for r in cfg.reminder_defaults] if payload.due_date else []
        )
        reminder_dicts = [_reminder_to_dict(r) for r in (reminders or [])]
        if reminder_dicts or payload.due_date or payload.due_time is not None:
            due_time_clean = (payload.due_time or "").strip() or None
            if due_time_clean and not _TIME_RE.match(due_time_clean):
                raise HTTPException(400, detail={"error": "due_time must be HH:MM", "kind": "validation"})
            await ts.upsert_reminders(created.issue_node_id, reminder_dicts, payload.due_date, due_time=due_time_clean)

        item = await tg.get_item(cfg, issue_node_id=created.issue_node_id)
        if not item:
            return {"ok": True, "item": None}
        entry = ts.get_entry(created.issue_node_id)
        return {"ok": True, "item": {**_enrich(item), "reminders": entry["reminders"], "fired": entry["fired"]}}

    @app.get("/api/tasks/{issue_node_id}")
    async def api_tasks_get_route(issue_node_id: str) -> dict:
        cfg = _require_cfg()
        try:
            item = await tg.get_item(cfg, issue_node_id=issue_node_id)
        except TasksGithubError as exc:
            raise _to_http(exc)
        if not item:
            raise HTTPException(404, detail={"error": f"task {issue_node_id} not on configured project", "kind": "not-found"})
        body = ""
        try:
            body = await tg.fetch_issue_body(item["repo"], item["number"])
        except TasksGithubError as exc:
            raise _to_http(exc)
        entry = ts.get_entry(issue_node_id)
        return {
            "ok": True,
            "item": {**_enrich(item), "body": body, "reminders": entry["reminders"], "fired": entry["fired"]},
        }

    @app.patch("/api/tasks/{issue_node_id}")
    async def api_tasks_patch_route(issue_node_id: str, payload: TaskUpdatePayload) -> dict:
        cfg = _require_cfg()
        try:
            current = await tg.get_item(cfg, issue_node_id=issue_node_id)
        except TasksGithubError as exc:
            raise _to_http(exc)
        if not current:
            raise HTTPException(404, detail={"error": f"task {issue_node_id} not on configured project", "kind": "not-found"})

        # Cross-repo move guard
        new_area = _validate_slug_or_none(payload.area_slug) if payload.area_slug is not None else current.get("area_slug")
        new_proj = _validate_slug_or_none(payload.proj_slug) if payload.proj_slug is not None else current.get("proj_slug")
        if payload.area_slug is not None or payload.proj_slug is not None:
            would_resolve = tasks_config_mod.resolve_repo(new_area, new_proj, cfg)
            if would_resolve and would_resolve != current.get("repo"):
                raise HTTPException(422, detail={
                    "error": "cannot move task across repos; close and recreate",
                    "kind": "cross-repo",
                    "current_repo": current.get("repo"),
                    "would_resolve_to": would_resolve,
                })

        # Issue-level edit
        try:
            if payload.title is not None or payload.body is not None:
                await tg.edit_issue(cfg, current["repo"], current["number"],
                                    title=payload.title, body=payload.body)

            # Label changes — both explicit + area/proj sync
            add: list[str] = list(payload.add_labels or [])
            remove: list[str] = list(payload.remove_labels or [])
            cur_area = current.get("area_slug")
            cur_proj = current.get("proj_slug")
            if payload.area_slug is not None and new_area != cur_area:
                if cur_area:
                    remove.append(tg.area_label(cur_area))
                if new_area:
                    add.append(tg.area_label(new_area))
            if payload.proj_slug is not None and new_proj != cur_proj:
                if cur_proj:
                    remove.append(tg.project_label(cur_proj))
                if new_proj:
                    add.append(tg.project_label(new_proj))
            add = list(dict.fromkeys(add))
            remove = list(dict.fromkeys(remove))
            # One gh subprocess for both directions when both lists are
            # populated (typical area/project move) — halves wall time vs
            # the previous add-then-remove sequence.
            if add or remove:
                await tg.edit_labels(
                    cfg, current["repo"], current["number"],
                    add=add, remove=remove,
                )

            # Project field patch
            patch: dict[str, str | None] = {}
            if payload.status is not None:
                patch["Status"] = payload.status
            if payload.priority is not None:
                patch["Priority"] = payload.priority or None
            if payload.category is not None:
                patch["Category"] = payload.category or None
            if payload.due_date is not None:
                patch["Due Date"] = payload.due_date or None
            if payload.waiting_on is not None:
                patch["Waiting On"] = payload.waiting_on
            if payload.source is not None:
                patch["Source"] = payload.source
            if patch:
                item_id = current.get("project_item_id") or await tg.find_item_id_for_issue(cfg, current["repo"], current["number"])
                await tg.update_fields(cfg, item_id, patch)
        except TasksGithubError as exc:
            raise _to_http(exc)

        # Reminders / due_time persistence
        if payload.reminders is not None or payload.due_date is not None or payload.due_time is not None:
            new_due = payload.due_date if payload.due_date is not None else current.get("due_date")
            if payload.reminders is not None:
                reminders = [_reminder_to_dict(r) for r in payload.reminders]
            else:
                entry = ts.get_entry(issue_node_id)
                reminders = entry["reminders"]
            if payload.due_time is not None:
                dt_clean = (payload.due_time or "").strip() or None
                if dt_clean and not _TIME_RE.match(dt_clean):
                    raise HTTPException(400, detail={"error": "due_time must be HH:MM", "kind": "validation"})
                await ts.upsert_reminders(issue_node_id, reminders, new_due, due_time=dt_clean)
            else:
                await ts.upsert_reminders(issue_node_id, reminders, new_due)

        item = await tg.get_item(cfg, issue_node_id=issue_node_id)
        if not item:
            return {"ok": True, "item": None}
        entry = ts.get_entry(issue_node_id)
        return {"ok": True, "item": {**_enrich(item), "reminders": entry["reminders"], "fired": entry["fired"]}}

    @app.delete("/api/tasks/{issue_node_id}")
    async def api_tasks_delete_route(issue_node_id: str, archive: bool = False) -> dict:
        cfg = _require_cfg()
        try:
            current = await tg.get_item(cfg, issue_node_id=issue_node_id)
            if not current:
                raise HTTPException(404, detail={"error": f"task {issue_node_id} not on configured project", "kind": "not-found"})
            if archive:
                item_id = current.get("project_item_id") or await tg.find_item_id_for_issue(cfg, current["repo"], current["number"])
                await tg.update_fields(cfg, item_id, {"Status": "Archived"})
            await tg.close_issue(cfg, current["repo"], current["number"], reason="not_planned" if archive else "completed")
        except TasksGithubError as exc:
            raise _to_http(exc)
        await ts.remove(issue_node_id)
        return {"ok": True}

    @app.post("/api/tasks/{issue_node_id}/close")
    async def api_tasks_close_route(
        issue_node_id: str,
        reason: Literal["completed", "not_planned"] = "completed",
    ) -> dict:
        cfg = _require_cfg()
        try:
            current = await tg.get_item(cfg, issue_node_id=issue_node_id)
            if not current:
                raise HTTPException(404, detail={"error": f"task {issue_node_id} not on configured project", "kind": "not-found"})
            await tg.close_issue(cfg, current["repo"], current["number"], reason=reason)
        except TasksGithubError as exc:
            raise _to_http(exc)
        await ts.remove(issue_node_id)
        return {"ok": True}

    @app.post("/api/tasks/{issue_node_id}/reopen")
    async def api_tasks_reopen_route(issue_node_id: str) -> dict:
        cfg = _require_cfg()
        try:
            current = await tg.get_item(cfg, issue_node_id=issue_node_id)
            if not current:
                raise HTTPException(404, detail={"error": f"task {issue_node_id} not on configured project", "kind": "not-found"})
            await tg.reopen_issue(cfg, current["repo"], current["number"])
        except TasksGithubError as exc:
            raise _to_http(exc)
        await ts.reset_fired(issue_node_id)
        return {"ok": True}

    # ── Reminders (flat view: task-attached + standalone) ──────

    @app.get("/api/reminders")
    async def api_reminders_list_route(
        state: Literal["pending", "fired", "all"] = "pending",
        within: Literal["24h", "7d", "30d", "all"] = "7d",
        area: list[str] | None = Query(default=None),
        project: list[str] | None = Query(default=None),
        kind: Literal["task", "standalone", "all"] = "all",
        q: str | None = None,
    ) -> dict:
        cfg = _require_cfg()
        try:
            listing = await tg.list_items(cfg)
        except TasksGithubError as exc:
            raise _to_http(exc)
        tz = ZoneInfo(cfg.timezone)
        rows = _flatten_reminders(listing["items"], cfg)
        rows = _filter_reminders(
            rows,
            state=state, within=within, area=area, project=project,
            kind=kind, q=q, tz=tz, grace_hours=cfg.grace_window_hours,
        )
        rows.sort(key=lambda r: (r.get("fire_at") or ""))
        return {"ok": True, "reminders": rows, "cached_at": listing["cached_at"]}

    @app.post("/api/reminders")
    async def api_reminders_create_route(payload: StandaloneReminderCreate) -> dict:
        _require_cfg()
        # Validate fire_at is parseable
        try:
            datetime.fromisoformat(payload.fire_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, detail={"error": "fire_at must be ISO 8601 datetime", "kind": "validation"})
        rid, entry = await ts.create_standalone(
            title=payload.title,
            body=payload.body,
            fire_at=payload.fire_at,
            priority=payload.priority,
            area_slug=payload.area_slug,
            proj_slug=payload.proj_slug,
            task_link=payload.task_link,
        )
        return {"ok": True, "id": rid, "reminder": entry}

    @app.patch("/api/reminders/{rid}")
    async def api_reminders_update_route(rid: str, payload: StandaloneReminderUpdate) -> dict:
        _require_cfg()
        patch = payload.model_dump(exclude_unset=True)
        if "fire_at" in patch and patch["fire_at"]:
            try:
                datetime.fromisoformat(patch["fire_at"].replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(400, detail={"error": "fire_at must be ISO 8601 datetime", "kind": "validation"})
        updated = await ts.update_standalone(rid, patch)
        if not updated:
            raise HTTPException(404, detail={"error": f"standalone reminder {rid} not found", "kind": "not-found"})
        return {"ok": True, "reminder": updated}

    @app.delete("/api/reminders/{rid}")
    async def api_reminders_delete_route(rid: str) -> dict:
        _require_cfg()
        ok = await ts.remove_standalone(rid)
        if not ok:
            raise HTTPException(404, detail={"error": f"standalone reminder {rid} not found", "kind": "not-found"})
        return {"ok": True}
