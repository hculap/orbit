"""FastAPI routes for the env/secrets feature.

Mounted via :func:`register` from :func:`orbit.library.register_routes`,
next to ``cron_routes.register`` and ``skills_routes.register``.

Soft-import pattern (see :mod:`cron_routes` lines 34-44): if any sibling
module fails to import we log + skip registration so the rest of the app
keeps booting.

Captcha gate: every mutating endpoint AND every reveal endpoint expects a
``{"captcha": {"token", "code"}}`` block in the JSON body. The captcha is
verified BEFORE any file mutation. The PUT-on-insert case (creating a new
.env entry / new file) is the one exception per the plan, and the routes
below treat it as such.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException

_logger = logging.getLogger(__name__)

# Only these global-env key prefixes are mirrored into the live os.environ on
# write/delete (see _put_env). They are the notify credentials the Settings UI
# writes inline and that notify.py reads from os.environ; everything else keeps
# the file-only, restart-to-apply behavior so a UI write can't live-mutate the
# process env (and the subprocess envs derived from it).
_LIVE_SYNC_ENV_PREFIXES = ("TELEGRAM_", "NOTIFY_")

try:
    from . import secrets_captcha
    from . import secrets_manager as sm
    from . import secrets_paths
    from .discovery import AREAS, PROJECTS
    _MODULES_OK = True
except Exception as e:  # pragma: no cover — defensive
    _logger.warning(
        "secrets_routes: dependent modules not available, routes disabled: %s", e,
    )
    secrets_captcha = None  # type: ignore[assignment]
    sm = None  # type: ignore[assignment]
    secrets_paths = None  # type: ignore[assignment]
    AREAS = PROJECTS = None  # type: ignore[assignment]
    _MODULES_OK = False


# ── helpers ───────────────────────────────────────────────────────────


def _http_for(exc: Exception) -> HTTPException:
    """Map domain errors to HTTP. Mirrors :func:`library._http_for`."""
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return HTTPException(404, detail=str(exc))
    return HTTPException(500, detail=str(exc))


def _resolve(scope_kind: str, lib_id_or_global: str | None) -> "secrets_paths.ScopePaths":
    if scope_kind == "global":
        scope = "global"
    elif scope_kind in ("areas", "projects"):
        if not lib_id_or_global:
            raise HTTPException(400, detail="lib_id required for non-global scope")
        scope = f"{scope_kind}/{lib_id_or_global}"
    else:
        raise HTTPException(400, detail="scope kind must be 'global', 'areas' or 'projects'")
    try:
        return secrets_paths.resolve(scope)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e


def _enforce_captcha(payload: dict | None) -> None:
    """Verify captcha block; raise 400 on miss."""
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="captcha required")
    cap = payload.get("captcha")
    if not isinstance(cap, dict):
        raise HTTPException(400, detail="captcha required")
    token = cap.get("token")
    code = cap.get("code")
    if not isinstance(token, str) or not isinstance(code, str):
        raise HTTPException(400, detail="captcha token+code required")
    if not secrets_captcha.verify(token, code):
        raise HTTPException(400, detail="captcha invalid or expired")


def _require_str(payload: dict, key: str, *, max_len: int | None = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise HTTPException(400, detail=f"{key} must be a string")
    s = value.strip()
    if not s:
        raise HTTPException(400, detail=f"{key} must be a non-empty string")
    if max_len is not None and len(value) > max_len:
        raise HTTPException(400, detail=f"{key} exceeds max length ({max_len})")
    return s


def _scope_label(kind: str, lib_id: str | None) -> str:
    if kind == "global":
        return "Global"
    return f"{kind[:-1].capitalize()}: {lib_id}"


def _enumerate_scopes() -> list[dict]:
    """Walk Areas/ and Projects/ to surface valid per-scope tabs.

    Returns one entry per Area + per Project (top-level + group/name). Each
    entry has ``has_env`` / ``has_secrets_dir`` / ``has_ssh`` flags so the
    UI can render an empty-state hint when nothing exists yet.
    """
    out: list[dict] = []
    out.append({
        "kind": "global",
        "lib_id": None,
        "scope": "global",
        "label": "Global",
        "has_env": secrets_paths.GLOBAL_ENV.is_file(),
        "has_secrets_dir": secrets_paths.GLOBAL_SECRETS_DIR.is_dir(),
        "has_ssh": secrets_paths.GLOBAL_SSH_DIR.is_dir(),
    })
    if AREAS and AREAS.is_dir():
        for entry in sorted(AREAS.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            try:
                paths = secrets_paths.resolve(f"areas/{entry.name}")
            except ValueError:
                continue
            out.append({
                "kind": "areas",
                "lib_id": entry.name,
                "scope": f"areas/{entry.name}",
                "label": _scope_label("areas", entry.name),
                "has_env": paths.env.is_file(),
                "has_secrets_dir": paths.secrets_dir.is_dir(),
                "has_ssh": False,
            })
    if PROJECTS and PROJECTS.is_dir():
        for entry in sorted(PROJECTS.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            # Either a flat project, or a group whose children are projects.
            children = list(entry.iterdir()) if entry.is_dir() else []
            looks_like_group = any(
                c.is_dir()
                and not c.name.startswith(("_", "."))
                and (c / ".library.json").is_file()
                for c in children
            )
            candidates: list[str] = []
            if looks_like_group:
                for c in sorted(children):
                    if not c.is_dir() or c.name.startswith(("_", ".")):
                        continue
                    candidates.append(f"{entry.name}/{c.name}")
            else:
                candidates.append(entry.name)
            for lib_id in candidates:
                try:
                    paths = secrets_paths.resolve(f"projects/{lib_id}")
                except ValueError:
                    continue
                out.append({
                    "kind": "projects",
                    "lib_id": lib_id,
                    "scope": f"projects/{lib_id}",
                    "label": _scope_label("projects", lib_id),
                    "has_env": paths.env.is_file(),
                    "has_secrets_dir": paths.secrets_dir.is_dir(),
                    "has_ssh": False,
                })
    return out


def _env_list_payload(paths: "secrets_paths.ScopePaths") -> dict:
    """Return ``{has_env, items}`` so the UI can offer a "Create .env" CTA
    for scopes where the file doesn't exist yet (rather than rendering a
    deceptively-empty entry list)."""
    values, _lines = sm.parse_env(paths.env)
    last = sm.env_last_modified(paths.env)
    items = [
        {
            "key": key,
            "masked": sm.mask_value(value, kind="env"),
            "last_modified": last,
        }
        for key, value in values.items()
    ]
    return {"has_env": paths.env.is_file(), "items": items}


def _file_to_dict(info: "sm.SecretFileInfo") -> dict:
    return {
        "name": info.name,
        "size": info.size,
        "mode": info.mode,
        "masked": info.masked,
    }


# ── route registration ────────────────────────────────────────────────


def register(app: FastAPI) -> None:
    """Mount /api/secrets/* on ``app``. No-op if dependencies missing."""
    if not _MODULES_OK:
        _logger.warning("secrets_routes.register: skipped — dependent modules unavailable")
        return

    # ── captcha ──────────────────────────────────────────────────────

    @app.post("/api/secrets/captcha/issue")
    async def api_issue_captcha() -> dict:
        return secrets_captcha.issue()

    # ── scopes index ────────────────────────────────────────────────

    @app.get("/api/secrets/scopes")
    async def api_list_scopes() -> list[dict]:
        try:
            return _enumerate_scopes()
        except Exception as e:
            _logger.exception("secrets_routes: enumerate_scopes failed")
            raise _http_for(e) from e

    # ── env (global) ────────────────────────────────────────────────

    @app.get("/api/secrets/global/env")
    async def api_list_global_env() -> dict:
        return _env_list_global()

    @app.post("/api/secrets/global/env/init")
    async def api_init_global_env() -> dict:
        return _init_env("global", None)

    @app.put("/api/secrets/global/env/{key}")
    async def api_put_global_env(key: str, payload: dict = Body(default={})) -> dict:
        return _put_env("global", None, key, payload)

    @app.delete("/api/secrets/global/env/{key}")
    async def api_delete_global_env(key: str, payload: dict = Body(default={})) -> dict:
        return _delete_env("global", None, key, payload)

    @app.post("/api/secrets/global/env/{key}/reveal")
    async def api_reveal_global_env(key: str, payload: dict = Body(default={})) -> dict:
        return _reveal_env("global", None, key, payload)

    # ── env (per-library) ───────────────────────────────────────────

    @app.get("/api/secrets/{kind}/{lib_id:path}/env")
    async def api_list_lib_env(kind: str, lib_id: str) -> dict:
        paths = _resolve(kind, lib_id)
        return _env_list_payload(paths)

    @app.post("/api/secrets/areas/{lib_id:path}/env/init")
    async def api_init_areas_env(lib_id: str) -> dict:
        return _init_env("areas", lib_id)

    @app.post("/api/secrets/projects/{lib_id:path}/env/init")
    async def api_init_projects_env(lib_id: str) -> dict:
        return _init_env("projects", lib_id)

    @app.put("/api/secrets/{kind}/{lib_id:path}/env/{key}")
    async def api_put_lib_env(
        kind: str, lib_id: str, key: str, payload: dict = Body(default={}),
    ) -> dict:
        return _put_env(kind, lib_id, key, payload)

    @app.delete("/api/secrets/{kind}/{lib_id:path}/env/{key}")
    async def api_delete_lib_env(
        kind: str, lib_id: str, key: str, payload: dict = Body(default={}),
    ) -> dict:
        return _delete_env(kind, lib_id, key, payload)

    @app.post("/api/secrets/{kind}/{lib_id:path}/env/{key}/reveal")
    async def api_reveal_lib_env(
        kind: str, lib_id: str, key: str, payload: dict = Body(default={}),
    ) -> dict:
        return _reveal_env(kind, lib_id, key, payload)

    # ── secrets-dir files (global) ──────────────────────────────────

    @app.get("/api/secrets/global/files")
    async def api_list_global_files() -> dict:
        return _files_list("global", None)

    @app.post("/api/secrets/global/files/init")
    async def api_init_global_files() -> dict:
        return _init_files("global", None)

    @app.put("/api/secrets/global/files/{name}")
    async def api_put_global_file(name: str, payload: dict = Body(default={})) -> dict:
        return _put_file("global", None, name, payload)

    @app.delete("/api/secrets/global/files/{name}")
    async def api_delete_global_file(name: str, payload: dict = Body(default={})) -> dict:
        return _delete_file("global", None, name, payload)

    @app.post("/api/secrets/global/files/{name}/reveal")
    async def api_reveal_global_file(name: str, payload: dict = Body(default={})) -> dict:
        return _reveal_file("global", None, name, payload)

    # ── secrets-dir files (per-library) ─────────────────────────────

    @app.get("/api/secrets/{kind}/{lib_id:path}/files")
    async def api_list_lib_files(kind: str, lib_id: str) -> dict:
        return _files_list(kind, lib_id)

    @app.post("/api/secrets/areas/{lib_id:path}/files/init")
    async def api_init_areas_files(lib_id: str) -> dict:
        return _init_files("areas", lib_id)

    @app.post("/api/secrets/projects/{lib_id:path}/files/init")
    async def api_init_projects_files(lib_id: str) -> dict:
        return _init_files("projects", lib_id)

    @app.put("/api/secrets/{kind}/{lib_id:path}/files/{name}")
    async def api_put_lib_file(
        kind: str, lib_id: str, name: str, payload: dict = Body(default={}),
    ) -> dict:
        return _put_file(kind, lib_id, name, payload)

    @app.delete("/api/secrets/{kind}/{lib_id:path}/files/{name}")
    async def api_delete_lib_file(
        kind: str, lib_id: str, name: str, payload: dict = Body(default={}),
    ) -> dict:
        return _delete_file(kind, lib_id, name, payload)

    @app.post("/api/secrets/{kind}/{lib_id:path}/files/{name}/reveal")
    async def api_reveal_lib_file(
        kind: str, lib_id: str, name: str, payload: dict = Body(default={}),
    ) -> dict:
        return _reveal_file(kind, lib_id, name, payload)

    # ── ssh (global only) ───────────────────────────────────────────

    @app.get("/api/secrets/global/ssh")
    async def api_list_ssh() -> dict:
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            # list_ssh_dir forks ssh-keygen per public AND private key (N+1
            # blocking subprocesses, 5s timeout each) — off the loop.
            return await asyncio.to_thread(sm.list_ssh_dir, ssh_dir)
        except Exception as e:
            _logger.exception("secrets_routes: list_ssh_dir failed")
            raise _http_for(e) from e

    @app.post("/api/secrets/global/ssh/private/generate")
    async def api_generate_ssh(payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        name = _require_str(payload, "name", max_len=64)
        key_type = payload.get("type") or "ed25519"
        comment = payload.get("comment") or ""
        if not isinstance(comment, str):
            raise HTTPException(400, detail="comment must be a string")
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            # ssh-keygen (30s) + fingerprint (5s) — off the loop.
            generated = await asyncio.to_thread(
                sm.generate_ssh_key, ssh_dir, name, key_type=key_type, comment=comment
            )
        except Exception as e:
            raise _http_for(e) from e
        return {
            "name": generated.name,
            "public_key": generated.public_key,
            "fingerprint": generated.fingerprint,
        }

    @app.post("/api/secrets/global/ssh/private/{name}/reveal")
    async def api_reveal_ssh_private(name: str, payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            value = sm.read_private_key(ssh_dir, name)
        except Exception as e:
            raise _http_for(e) from e
        return {"value": value}

    # ── authorized_keys ─────────────────────────────────────────────

    @app.post("/api/secrets/global/ssh/authorized_keys")
    async def api_authorized_add(payload: dict = Body(default={})) -> dict:
        # Adding a line to authorized_keys grants SSH login to the box.
        # The PUT-on-insert exception applies only to .env / files; SSH
        # access changes are gated like every other mutation.
        _enforce_captcha(payload)
        line = _require_str(payload, "line", max_len=8192)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            idx = sm.append_line(ssh_dir / "authorized_keys", line)
        except Exception as e:
            raise _http_for(e) from e
        return {"idx": idx}

    @app.patch("/api/secrets/global/ssh/authorized_keys/{idx}")
    async def api_authorized_patch(idx: int, payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        line = _require_str(payload, "line", max_len=8192)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            sm.replace_line(ssh_dir / "authorized_keys", idx, line)
        except Exception as e:
            raise _http_for(e) from e
        return {"ok": True, "idx": idx}

    @app.delete("/api/secrets/global/ssh/authorized_keys/{idx}")
    async def api_authorized_delete(idx: int, payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            sm.delete_line(
                ssh_dir / "authorized_keys",
                idx,
                refuse_last=True,
            )
        except Exception as e:
            raise _http_for(e) from e
        return {"ok": True}

    # ── known_hosts ─────────────────────────────────────────────────

    @app.post("/api/secrets/global/ssh/known_hosts")
    async def api_known_hosts_add(payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        line = _require_str(payload, "line", max_len=8192)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            idx = sm.append_line(ssh_dir / "known_hosts", line)
        except Exception as e:
            raise _http_for(e) from e
        return {"idx": idx}

    @app.patch("/api/secrets/global/ssh/known_hosts/{idx}")
    async def api_known_hosts_patch(idx: int, payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        line = _require_str(payload, "line", max_len=8192)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            sm.replace_line(ssh_dir / "known_hosts", idx, line)
        except Exception as e:
            raise _http_for(e) from e
        return {"ok": True, "idx": idx}

    @app.delete("/api/secrets/global/ssh/known_hosts/{idx}")
    async def api_known_hosts_delete(idx: int, payload: dict = Body(default={})) -> dict:
        _enforce_captcha(payload)
        paths = _resolve("global", None)
        ssh_dir = secrets_paths.require_global(paths)
        try:
            sm.delete_line(ssh_dir / "known_hosts", idx)
        except Exception as e:
            raise _http_for(e) from e
        return {"ok": True}


# ── shared handler bodies (env / files) ──────────────────────────────
# These live at module scope so the Global and per-library route variants
# can share one implementation. They re-resolve the scope per call rather
# than capturing closures because FastAPI dispatches by raw path segments.


def _init_env(kind: str, lib_id: str | None) -> dict:
    """Idempotently create the .env file for a scope. No captcha — touching
    an empty file is reversible (rm) and the user explicitly clicked Create."""
    paths = _resolve(kind, lib_id)
    try:
        created = sm.init_env_file(paths.env)
    except Exception as e:
        raise _http_for(e) from e
    return {
        "ok": True,
        "created": created,
        "path": str(paths.env),
        **_env_list_payload(paths),
    }


def _init_files(kind: str, lib_id: str | None) -> dict:
    """Idempotently create the .secrets/ directory for a scope."""
    paths = _resolve(kind, lib_id)
    try:
        created = sm.init_secrets_dir(paths.secrets_dir)
    except Exception as e:
        raise _http_for(e) from e
    return {
        "ok": True,
        "created": created,
        "path": str(paths.secrets_dir),
        **_files_list(kind, lib_id),
    }


def _env_list_global() -> dict:
    paths = _resolve("global", None)
    try:
        return _env_list_payload(paths)
    except Exception as e:
        _logger.exception("secrets_routes: env list failed")
        raise _http_for(e) from e


def _put_env(kind: str, lib_id: str | None, key: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="body must be an object")
    paths = _resolve(kind, lib_id)
    try:
        safe_key = sm.validate_env_key(key)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    value = payload.get("value")
    if not isinstance(value, str):
        raise HTTPException(400, detail="value must be a string")
    if "\n" in value or "\r" in value:
        raise HTTPException(400, detail="value cannot contain newlines (multi-line .env not supported in v1)")

    try:
        existing, lines = sm.parse_env(paths.env)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    is_overwrite = safe_key in existing
    if is_overwrite:
        _enforce_captcha(payload)
    new_values = {**existing, safe_key: value}
    try:
        sm.write_env(paths.env, new_values, lines)
    except Exception as e:
        raise _http_for(e) from e
    # Sync os.environ so the write takes LIVE effect (no restart) — but ONLY for
    # the notify credentials the Settings UI legitimately writes inline
    # (TELEGRAM_*/NOTIFY_*), which notify.py reads straight from os.environ.
    # Syncing ANY global key would let a new PATH/LD_PRELOAD/NODE_OPTIONS (no
    # captcha on first-create) mutate the live process env inherited by claude/
    # gh/ssh subprocesses; those keep the prior file-only, restart-to-apply
    # behavior. Per-library scopes are never synced (they feed subprocess envs).
    if kind == "global" and safe_key.startswith(_LIVE_SYNC_ENV_PREFIXES):
        os.environ[safe_key] = value
    return {"ok": True, "key": safe_key, "overwritten": is_overwrite}


def _delete_env(kind: str, lib_id: str | None, key: str, payload: dict) -> dict:
    _enforce_captcha(payload)
    paths = _resolve(kind, lib_id)
    try:
        safe_key = sm.validate_env_key(key)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    try:
        existing, lines = sm.parse_env(paths.env)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if safe_key not in existing:
        raise HTTPException(404, detail=f"env key not found: {safe_key}")
    new_values = {k: v for k, v in existing.items() if k != safe_key}
    try:
        sm.write_env(paths.env, new_values, lines)
    except Exception as e:
        raise _http_for(e) from e
    # Clear the live value too (same notify-credential allowlist as _put_env) so a
    # deleted credential stops being honored without a restart.
    if kind == "global" and safe_key.startswith(_LIVE_SYNC_ENV_PREFIXES):
        os.environ.pop(safe_key, None)
    return {"ok": True, "key": safe_key}


def _reveal_env(kind: str, lib_id: str | None, key: str, payload: dict) -> dict:
    _enforce_captcha(payload)
    paths = _resolve(kind, lib_id)
    try:
        safe_key = sm.validate_env_key(key)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    try:
        existing, _lines = sm.parse_env(paths.env)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if safe_key not in existing:
        raise HTTPException(404, detail=f"env key not found: {safe_key}")
    return {"value": existing[safe_key]}


def _files_list(kind: str, lib_id: str | None) -> dict:
    """Return ``{has_secrets_dir, items}`` so the UI can offer a "Create
    .secrets/" CTA for scopes whose directory doesn't exist yet."""
    paths = _resolve(kind, lib_id)
    items = [_file_to_dict(info) for info in sm.list_secret_files(paths.secrets_dir)]
    return {"has_secrets_dir": paths.secrets_dir.is_dir(), "items": items}


def _put_file(kind: str, lib_id: str | None, name: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="body must be an object")
    paths = _resolve(kind, lib_id)
    try:
        safe_name = sm.validate_secret_file_name(name)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    is_overwrite = (paths.secrets_dir / safe_name).is_file()
    if is_overwrite:
        _enforce_captcha(payload)
    b64 = payload.get("content_b64")
    if not isinstance(b64, str):
        raise HTTPException(400, detail="content_b64 must be a base64 string")
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as e:
        raise HTTPException(400, detail=f"content_b64 not valid base64: {e}") from e
    try:
        info = sm.write_secret_file(paths.secrets_dir, safe_name, data)
    except Exception as e:
        raise _http_for(e) from e
    return {"ok": True, "overwritten": is_overwrite, **_file_to_dict(info)}


def _delete_file(kind: str, lib_id: str | None, name: str, payload: dict) -> dict:
    _enforce_captcha(payload)
    paths = _resolve(kind, lib_id)
    try:
        sm.delete_secret_file(paths.secrets_dir, name)
    except Exception as e:
        raise _http_for(e) from e
    return {"ok": True}


def _reveal_file(kind: str, lib_id: str | None, name: str, payload: dict) -> dict:
    _enforce_captcha(payload)
    paths = _resolve(kind, lib_id)
    try:
        data = sm.read_secret_file(paths.secrets_dir, name)
    except Exception as e:
        raise _http_for(e) from e
    return {"content_b64": base64.b64encode(data).decode("ascii")}
