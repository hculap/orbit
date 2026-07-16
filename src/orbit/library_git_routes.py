"""Git + file mutation routes for the Areas/Projects library.

Extracted from ``library.py`` (which already sits at the 800-LOC ceiling
after Phase 1+2+3) to host the Round 2 additions:

- ``DELETE /file`` and ``GET /file/download`` (file mutations)
- ``POST  /git/init``         — initialise repo
- ``POST  /git/attach-remote`` — set/replace ``origin``
- ``POST  /git/commit``       — ``add -A`` + commit
- ``POST  /git/push``         — push current branch
- ``GET   /git/status``       — porcelain v1 status, structured
- ``GET   /git/recent``       — recent commits for Overview tab
- ``POST  /git/open-pr``      — ``gh pr create`` (mirrors ``library_github_routes`` naming)

We deliberately namespace the PR-open route under ``/git/`` (not
``/github/``) because the underlying implementation is a single
``gh pr create`` shell-out wrapped in :mod:`library_git`, not part of the
async ``library_github`` PR/issue CRUD. ``library_github_routes`` continues
to own ``/github/prs`` (list + view + edit) as before.

Error mapping mirrors ``library.py``:
  ValueError → 400, FileExistsError → 409, FileNotFoundError → 404,
  PermissionError → 403, generic → 500. ``gh`` failures (open_pr) become
  424 Failed Dependency with sanitised error message.
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Callable, Literal

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import library as library_mod
from . import library_files as files_mod
from . import library_git as git_mod


def _resolve_item_path(kind: str, name: str) -> Path:
    """Map ``(kind, name)`` to absolute item dir under Areas/ or Projects/."""
    if kind == "areas":
        return library_mod._safe_area_path(name)
    if kind == "projects":
        return library_mod._safe_project_path(name)
    raise HTTPException(400, detail="kind must be 'areas' or 'projects'")


def _resolve_existing(kind: str, name: str) -> Path:
    """Resolve + assert the item directory exists. Maps ValueError → 400."""
    try:
        item = _resolve_item_path(kind, name)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if not item.is_dir():
        raise HTTPException(404, detail=f"{kind}/{name} not found")
    return item


def _http_for(exc: Exception) -> HTTPException:
    """Domain → HTTP. Mirrors library._http_for plus PermissionError → 403."""
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(403, detail=str(exc))
    return HTTPException(500, detail=str(exc))


def _bail_on_gh_failure(result: dict) -> None:
    """If ``open_pr`` (or similar gh wrapper) returned ``ok: False``, raise 424."""
    if not isinstance(result, dict) or not result.get("ok"):
        msg = (result or {}).get("error") or "gh failed"
        raise HTTPException(424, detail=msg)


def _kind_param(kind: str) -> Literal["area", "project"]:
    if kind == "areas":
        return "area"
    if kind == "projects":
        return "project"
    raise HTTPException(400, detail="kind must be 'areas' or 'projects'")


def register_git_and_file_routes(app: FastAPI, drop_cache: Callable[[], None]) -> None:
    """Mount Round-2 file mutation + git routes on ``app``.

    ``drop_cache`` is the closure exposed by ``library.register_routes`` so
    every write op invalidates the discovery cache.
    """

    # ── file mutations ──────────────────────────────────────────

    @app.delete("/api/library/{kind}/{name:path}/file")
    async def api_delete_file(kind: str, name: str, rel: str) -> dict:
        single_kind = _kind_param(kind)
        try:
            result = files_mod.delete_file(single_kind, name, rel)
        except Exception as e:
            raise _http_for(e) from e
        drop_cache()
        return result

    @app.get("/api/library/{kind}/{name:path}/file/download")
    async def api_download_file(kind: str, name: str, rel: str) -> FileResponse:
        single_kind = _kind_param(kind)
        try:
            target, basename = files_mod.file_for_download(single_kind, name, rel)
        except Exception as e:
            raise _http_for(e) from e
        return FileResponse(
            path=str(target),
            filename=basename,
            media_type="application/octet-stream",
        )

    # ── git ops ──────────────────────────────────────────────────

    @app.post("/api/library/{kind}/{name:path}/git/init")
    async def api_git_init(kind: str, name: str) -> dict:
        item = _resolve_existing(kind, name)
        try:
            result = await asyncio.to_thread(git_mod.git_init, item)
        except Exception as e:
            raise _http_for(e) from e
        drop_cache()
        return result

    @app.post("/api/library/{kind}/{name:path}/git/attach-remote")
    async def api_git_attach_remote(
        kind: str, name: str, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        url = payload.get("url")
        fetch = bool(payload.get("fetch", False))
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(400, detail="url required")
        try:
            result = await asyncio.to_thread(
                git_mod.attach_remote, item, url.strip(), fetch=fetch
            )
        except Exception as e:
            raise _http_for(e) from e
        drop_cache()
        return result

    @app.post("/api/library/{kind}/{name:path}/git/commit")
    async def api_git_commit(
        kind: str, name: str, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        message = payload.get("message")
        if not isinstance(message, str):
            raise HTTPException(400, detail="message must be a string")
        try:
            result = await asyncio.to_thread(git_mod.commit_all, item, message)
        except Exception as e:
            raise _http_for(e) from e
        if not result.get("ok"):
            # commit_all returns {ok:False, error:...} for "nothing to commit"
            # and other recoverable git states — surface as 400.
            raise HTTPException(400, detail=result.get("error") or "commit failed")
        drop_cache()
        return result

    @app.post("/api/library/{kind}/{name:path}/git/push")
    async def api_git_push(kind: str, name: str) -> dict:
        item = _resolve_existing(kind, name)
        try:
            result = await asyncio.to_thread(git_mod.push_current, item)
        except Exception as e:
            raise _http_for(e) from e
        if not result.get("ok"):
            # Push failures (auth, no remote, network) → 424.
            raise HTTPException(424, detail=result.get("error") or "push failed")
        return result

    @app.post("/api/library/{kind}/{name:path}/git/fetch")
    async def api_git_fetch(kind: str, name: str) -> dict:
        item = _resolve_existing(kind, name)
        try:
            result = await asyncio.to_thread(git_mod.git_fetch, item)
        except Exception as e:
            raise _http_for(e) from e
        if not result.get("ok"):
            raise HTTPException(424, detail=result.get("error") or "fetch failed")
        return result

    @app.get("/api/library/{kind}/{name:path}/git/status")
    async def api_git_status_porcelain(kind: str, name: str) -> dict:
        item = _resolve_existing(kind, name)
        try:
            result = await asyncio.to_thread(git_mod.status_porcelain, item)
        except Exception as e:
            raise _http_for(e) from e
        if not result.get("ok"):
            raise HTTPException(400, detail=result.get("error") or "status failed")
        return result

    @app.get("/api/library/{kind}/{name:path}/git/recent")
    async def api_git_recent(kind: str, name: str, limit: int = 5) -> dict:
        item = _resolve_existing(kind, name)
        try:
            result = await asyncio.to_thread(
                git_mod.list_recent_commits, item, limit=limit
            )
        except Exception as e:
            raise _http_for(e) from e
        if not result.get("ok"):
            raise HTTPException(400, detail=result.get("error") or "log failed")
        return result

    @app.post("/api/library/{kind}/{name:path}/git/open-pr")
    async def api_git_open_pr(
        kind: str, name: str, payload: dict = Body(...),
    ) -> dict:
        item = _resolve_existing(kind, name)
        title = payload.get("title")
        body = payload.get("body", "")
        base = payload.get("base") or "main"
        if not isinstance(title, str) or not title.strip():
            raise HTTPException(400, detail="title required")
        if not isinstance(body, str):
            raise HTTPException(400, detail="body must be a string")
        if not isinstance(base, str) or not base.strip():
            raise HTTPException(400, detail="base must be a non-empty string")
        try:
            result = await asyncio.to_thread(
                git_mod.open_pr, item, title=title, body=body, base=base
            )
        except Exception as e:
            raise _http_for(e) from e
        _bail_on_gh_failure(result)
        return result
