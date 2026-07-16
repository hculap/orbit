"""GitHub PR + Issue route handlers for the Areas/Projects library.

Extracted from ``library.py`` to keep that file under the 800-LOC ceiling
after Phase 2 already pushed it to 779. Route handlers here mirror the
shape of the rest of the library API (`/api/library/{kind}/{name:path}/...`).

Error mapping — ``library_github`` returns ``{ok: False, error: "..."}`` on
gh-side failures (auth, network, 404 from GitHub, rate-limit). We surface
these as HTTP 424 Failed Dependency so the frontend can render a clean
inline error without making it look like a server bug.
"""
from __future__ import annotations
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException

from . import library as library_mod
from . import library_github as library_gh


def _resolve_item_path(kind: str, name: str) -> Path:
    """Map ``(kind, name)`` to an absolute item dir under Areas/ or Projects/."""
    if kind == "areas":
        return library_mod._safe_area_path(name)
    if kind == "projects":
        return library_mod._safe_project_path(name)
    raise HTTPException(400, detail="kind must be 'areas' or 'projects'")


def _bail(result: dict) -> None:
    """If a library_github call returned an error envelope, raise 424."""
    if not isinstance(result, dict) or not result.get("ok"):
        msg = (result or {}).get("error") or "github failed"
        raise HTTPException(424, detail=msg)


def _ensure_state(state: str) -> Literal["open", "closed", "all"]:
    if state not in ("open", "closed", "all"):
        raise HTTPException(400, detail="invalid state (open|closed|all)")
    return state  # type: ignore[return-value]


def _resolve_existing(kind: str, name: str) -> Path:
    """Resolve + assert the item directory exists. Maps ValueError → 400."""
    try:
        item = _resolve_item_path(kind, name)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if not item.is_dir():
        raise HTTPException(404, detail=f"{kind}/{name} not found")
    return item


def register_github_routes(app: FastAPI) -> None:
    """Mount /api/library/{kind}/{name:path}/github/* routes on ``app``."""

    # ── PRs ──────────────────────────────────────────────────────

    @app.get("/api/library/{kind}/{name:path}/github/prs")
    async def lib_gh_pr_list(
        kind: str, name: str, state: str = "open", limit: int = 50,
    ) -> dict:
        item = _resolve_existing(kind, name)
        st = _ensure_state(state)
        result = await library_gh.list_prs(item, state=st, limit=min(limit, 200))
        _bail(result)
        return result

    @app.get("/api/library/{kind}/{name:path}/github/prs/{number:int}")
    async def lib_gh_pr_get(kind: str, name: str, number: int) -> dict:
        item = _resolve_existing(kind, name)
        result = await library_gh.get_pr(item, number)
        _bail(result)
        return result

    @app.patch("/api/library/{kind}/{name:path}/github/prs/{number:int}")
    async def lib_gh_pr_patch(
        kind: str, name: str, number: int, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        body = payload.get("body")
        if not isinstance(body, str):
            raise HTTPException(400, detail="body must be a string")
        result = await library_gh.update_pr_body(item, number, body)
        _bail(result)
        return result

    # ── Issues ──────────────────────────────────────────────────

    @app.get("/api/library/{kind}/{name:path}/github/issues")
    async def lib_gh_issue_list(
        kind: str, name: str, state: str = "open", limit: int = 50,
    ) -> dict:
        item = _resolve_existing(kind, name)
        st = _ensure_state(state)
        result = await library_gh.list_issues(item, state=st, limit=min(limit, 200))
        _bail(result)
        return result

    @app.get("/api/library/{kind}/{name:path}/github/issues/{number:int}")
    async def lib_gh_issue_get(kind: str, name: str, number: int) -> dict:
        item = _resolve_existing(kind, name)
        result = await library_gh.get_issue(item, number)
        _bail(result)
        return result

    @app.post("/api/library/{kind}/{name:path}/github/issues")
    async def lib_gh_issue_create(
        kind: str, name: str, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        title = payload.get("title")
        body = payload.get("body", "")
        labels = payload.get("labels")
        if not isinstance(title, str) or not title.strip():
            raise HTTPException(400, detail="title is required")
        if not isinstance(body, str):
            raise HTTPException(400, detail="body must be a string")
        if labels is not None and not (
            isinstance(labels, list) and all(isinstance(s, str) for s in labels)
        ):
            raise HTTPException(400, detail="labels must be a list of strings")
        result = await library_gh.create_issue(
            item, title=title, body=body, labels=labels,
        )
        _bail(result)
        return result

    @app.patch("/api/library/{kind}/{name:path}/github/issues/{number:int}")
    async def lib_gh_issue_patch(
        kind: str, name: str, number: int, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        title = payload.get("title")
        body = payload.get("body")
        state = payload.get("state")
        add_labels = payload.get("add_labels")
        remove_labels = payload.get("remove_labels")

        if title is not None and not isinstance(title, str):
            raise HTTPException(400, detail="title must be a string")
        if body is not None and not isinstance(body, str):
            raise HTTPException(400, detail="body must be a string")
        if state is not None and state not in ("open", "closed"):
            raise HTTPException(400, detail="state must be 'open' or 'closed'")
        for arg_name, arg_val in (("add_labels", add_labels), ("remove_labels", remove_labels)):
            if arg_val is not None and not (
                isinstance(arg_val, list) and all(isinstance(s, str) for s in arg_val)
            ):
                raise HTTPException(400, detail=f"{arg_name} must be a list of strings")

        result = await library_gh.update_issue(
            item, number,
            title=title,
            body=body,
            state=state,
            add_labels=add_labels,
            remove_labels=remove_labels,
        )
        _bail(result)
        return result

    @app.post("/api/library/{kind}/{name:path}/github/issues/{number:int}/comments")
    async def lib_gh_issue_comment(
        kind: str, name: str, number: int, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            raise HTTPException(400, detail="body is required")
        result = await library_gh.add_comment(item, number, body)
        _bail(result)
        return result

    # ── Promote an issue onto the global Tasks board ("Make task") ──
    # Bridges the two GitHub-issue systems: this repo issue (gh CLI) → the
    # Projects v2 board (tasks_github). Gated on issues.make_task + a
    # configured tasks board. tasks_* imported lazily to avoid coupling the
    # base Issues view to the board subsystem at import time.

    @app.post("/api/library/{kind}/{name:path}/github/issues/{number:int}/make-task")
    async def lib_gh_issue_make_task(
        kind: str, name: str, number: int, payload: dict = Body(default={}),
    ) -> dict:
        from . import issues_config, tasks_config, tasks_github

        if not issues_config.load().make_task:
            raise HTTPException(
                503, detail="issues.make_task is disabled — set `issues.make_task: true` in override.yaml",
            )
        cfg = tasks_config.load()
        if not cfg.is_configured():
            raise HTTPException(
                503, detail="Tasks board not configured — set the `tasks:` block in override.yaml",
            )
        item = _resolve_existing(kind, name)
        node = await library_gh.get_issue_node_id(item, number)
        _bail(node)
        try:
            item_id = await tasks_github.add_to_project(cfg, node["id"])
            status = payload.get("status") if isinstance(payload, dict) else None
            if isinstance(status, str) and status.strip():
                # Best-effort initial Status — promotion already succeeded.
                try:
                    await tasks_github.update_fields(cfg, item_id, {"Status": status.strip()})
                except tasks_github.TasksGithubError:
                    pass
        except tasks_github.TasksGithubError as e:
            raise HTTPException(e.status_hint, detail=e.message) from e
        return {"ok": True, "item_id": item_id, "number": number}

    # ── One-click "Create GitHub repo" for a repo-less project/area ──
    # Lets the Issues tab bootstrap a repo (git init + initial commit +
    # `gh repo create --source --push`) so issues can exist. Gated on
    # issues.create_repo.

    @app.post("/api/library/{kind}/{name:path}/github/create-repo")
    async def lib_gh_create_repo(
        kind: str, name: str, payload: dict = Body(default={}),
    ) -> dict:
        from . import issues_config

        if not issues_config.load().create_repo:
            raise HTTPException(
                503, detail="issues.create_repo is disabled — set `issues.create_repo: true` in override.yaml",
            )
        item = _resolve_existing(kind, name)
        vis = payload.get("visibility") if isinstance(payload, dict) else None
        visibility = vis if vis in ("public", "private") else "private"
        result = await library_mod.create_github_repo_for_item(item, visibility)
        _bail(result)
        # Bust the 30s discovery cache so the next /api/data reports the new
        # has_github_remote and the tab flips from CTA → live issue list.
        try:
            from . import app as _app_mod
            _app_mod._cache["ts"] = 0.0
        except Exception:  # noqa: BLE001 — cache bust is best-effort
            pass
        return result
