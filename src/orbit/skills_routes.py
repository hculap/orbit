"""HTTP routes for the Skills Register.

Mounted by :func:`orbit.library.register_routes` BEFORE the
catch-all ``PATCH /api/library/{kind}/{name:path}`` so the more specific
``.../agent/skills`` route isn't shadowed (FastAPI uses first-match).

The routes are thin shells over three sibling modules:

* :mod:`skills_registry`    — canonical on-disk registry under ``~/.orchestrator/skills-registry/``
* :mod:`skills_install`     — install/update flows (github/zip/custom/shorthand)
* :mod:`skills_per_agent`   — per-agent allowlist + symlink farm

If any of those modules fail to import (e.g. Agent A's branch hasn't
landed yet) we log a warning and skip registration entirely so the rest
of the dashboard keeps booting. This module is best-effort glue.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi import UploadFile as _FastAPIUploadFile
from starlette.datastructures import UploadFile as _StarletteUploadFile

# fastapi 0.136+ and starlette 1.0+ ship divergent UploadFile classes;
# `request.form()` returns the starlette one, so isinstance against the
# fastapi import alone misses real multipart uploads. Accept both.
_UPLOAD_FILE_TYPES = (_FastAPIUploadFile, _StarletteUploadFile)

_logger = logging.getLogger(__name__)

# ── soft imports ──────────────────────────────────────────────────
# Agent A is implementing skills_registry / skills_install / skills_per_agent
# in parallel. If they're not on disk yet, we register no routes and the
# rest of the app keeps working. The contracts below are the source of
# truth shared between Agents A/B/C.

try:
    from . import skills_registry  # type: ignore[attr-defined]
    from . import skills_install  # type: ignore[attr-defined]
    from . import skills_per_agent  # type: ignore[attr-defined]
    _MODULES_OK = True
except Exception as e:  # pragma: no cover — defensive
    _logger.warning(
        "skills_routes: dependent modules not available, routes disabled: %s", e,
    )
    skills_registry = None  # type: ignore[assignment]
    skills_install = None  # type: ignore[assignment]
    skills_per_agent = None  # type: ignore[assignment]
    _MODULES_OK = False


# ── helpers ───────────────────────────────────────────────────────


def _http_for(exc: Exception) -> HTTPException:
    """Map domain errors to HTTP. Mirrors :func:`library._http_for`."""
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return HTTPException(404, detail=str(exc))
    return HTTPException(500, detail=str(exc))


def _validate_skill_name(name: str) -> str:
    """Run name through ``skills_registry.safe_skill_name`` → 400 on bad name."""
    try:
        return skills_registry.safe_skill_name(name)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e


def _parse_enable_for(value: Any) -> list[str]:
    """Validate ``enable_for`` is ``list[str]`` of well-formed targets.

    Accepted entries:
      * ``"all"``                           — shorthand for the global pool
      * ``"global"``                        — same as above (explicit)
      * ``"<kind>:<lib_id>"`` where kind ∈ {areas, projects, resources}

    Empty list is valid (caller chose to install without enabling anywhere).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(400, detail="enable_for must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(400, detail="enable_for entries must be non-empty strings")
        out.append(item.strip())
    return out


def _apply_enablement(skill_name: str, enable_for: list[str]) -> None:
    """Add the freshly-installed skill to the chosen scopes.

    "all"             → registry-wide ``.global-enabled.json`` (every agent sees it)
    "global"          → Global agent's per-agent allowlist (Global only)
    "<kind>:<lib_id>" → that agent's per-agent allowlist

    Best-effort: any individual scope error is logged but does not abort
    the install (the skill is already on disk; we don't want to dangle
    the registry entry over a typo'd agent id).
    """
    for entry in enable_for:
        try:
            if entry == "all":
                current = skills_registry.read_global_enabled()
                skills_registry.write_global_enabled(set(current) | {skill_name})
                continue
            if entry == "global":
                current = skills_per_agent.read_allowlist("global", "global")
                skills_per_agent.write_allowlist(
                    "global", "global", set(current) | {skill_name},
                )
                continue
            if ":" not in entry:
                _logger.warning(
                    "skills_routes: ignoring malformed enable_for entry %r "
                    "(expected 'all', 'global', or '<kind>:<lib_id>')", entry,
                )
                continue
            kind, lib_id = entry.split(":", 1)
            kind = kind.strip()
            lib_id = lib_id.strip()
            if not kind or not lib_id:
                _logger.warning(
                    "skills_routes: empty kind/lib_id in %r", entry,
                )
                continue
            current = skills_per_agent.read_allowlist(kind, lib_id)
            skills_per_agent.write_allowlist(
                kind, lib_id, set(current) | {skill_name},
            )
        except Exception as e:  # never abort the install on a single scope
            _logger.warning(
                "skills_routes: failed to enable %s for %s: %s",
                skill_name, entry, e,
            )


def _agents_state_for(skill_name: str, *, discovered_data: dict | None = None) -> list[dict]:
    """Return ``[{kind, lib_id, label, enabled}]`` for every known agent.

    Used by ``GET /api/skills/<name>``. Reads all allowlists in one batched
    pass via :func:`skills_per_agent.enabled_for_all_agents` instead of
    re-reading global-enabled + allowlist per agent.
    """
    rows: list[dict] = []
    try:
        agent_keys = skills_per_agent.list_all_agent_keys(discovered_data=discovered_data)
        enabled_by_agent = skills_per_agent.enabled_for_all_agents(agent_keys)
    except Exception as e:
        _logger.warning("skills_routes: list_all_agent_keys failed: %s", e)
        return rows

    for entry in agent_keys:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        lib_id = entry.get("lib_id") if isinstance(entry, dict) else None
        label = entry.get("label") if isinstance(entry, dict) else None
        if not kind or not lib_id:
            continue
        enabled_set = enabled_by_agent.get((kind, lib_id), set())
        rows.append({
            "kind": kind,
            "lib_id": lib_id,
            "label": label or lib_id,
            "enabled": skill_name in enabled_set,
        })
    return rows


def _icon_from_frontmatter(fm: dict) -> str | None:
    """Extract an emoji icon from a SKILL.md frontmatter (clawdbot metadata).

    Falls back to ``None`` so callers can default to the puzzle-piece icon.
    """
    if not isinstance(fm, dict):
        return None
    meta = fm.get("metadata")
    if isinstance(meta, dict):
        clawd = meta.get("clawdbot")
        if isinstance(clawd, dict) and isinstance(clawd.get("emoji"), str):
            return clawd["emoji"].strip() or None
    return None


# NOTE: legacy ``_count_agents_with_skill(skill_name, agent_keys)`` was removed
# in favour of the batched ``skills_per_agent.enabled_for_all_agents`` call
# in ``api_list_skills`` — it triggered O(skills × agents) file reads.


async def _read_install_payload(request: Request) -> tuple[str, dict, list[str], bytes | None]:
    """Parse install request (JSON or multipart).

    Returns ``(source, payload, enable_for, zip_bytes)``. ``zip_bytes`` is
    non-None only for multipart uploads with ``source=zip``.
    """
    ctype = (request.headers.get("content-type") or "").lower()

    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        source = str(form.get("source") or "zip")
        enable_for_raw = form.get("enable_for")
        enable_for: list[str] = []
        if enable_for_raw is not None:
            try:
                parsed = json.loads(str(enable_for_raw))
            except json.JSONDecodeError as e:
                raise HTTPException(
                    400, detail=f"enable_for must be JSON-encoded list: {e}",
                ) from e
            enable_for = _parse_enable_for(parsed)

        upload = form.get("file")
        zip_bytes: bytes | None = None
        if isinstance(upload, _UPLOAD_FILE_TYPES):
            zip_bytes = await upload.read()

        # For multipart we don't really have a payload dict beyond name_hint,
        # but accept one as JSON in a `payload` form field if the client sends it.
        payload: dict = {}
        payload_raw = form.get("payload")
        if payload_raw is not None:
            try:
                parsed_payload = json.loads(str(payload_raw))
            except json.JSONDecodeError as e:
                raise HTTPException(400, detail=f"payload must be JSON: {e}") from e
            if not isinstance(parsed_payload, dict):
                raise HTTPException(400, detail="payload must be an object")
            payload = parsed_payload

        return source, payload, enable_for, zip_bytes

    # Default: JSON body
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, detail=f"invalid JSON body: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(400, detail="body must be a JSON object")

    source = str(body.get("source") or "").strip()
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="payload must be an object")
    # Frontend sends fields flat alongside `source`/`enable_for`; backend
    # historically expected them nested under `payload`. Merge flat top-level
    # keys (except the few reserved ones) into `payload` so both shapes work.
    _RESERVED = {"source", "payload", "enable_for"}
    for key, value in body.items():
        if key in _RESERVED:
            continue
        payload.setdefault(key, value)
    enable_for = _parse_enable_for(body.get("enable_for"))
    return source, payload, enable_for, None


# ── route registration ────────────────────────────────────────────


def register(app: FastAPI) -> None:
    """Mount the skills routes on ``app``.

    No-op if dependent modules failed to import (logged once at module
    import time).
    """
    if not _MODULES_OK:
        _logger.warning(
            "skills_routes.register: skipped — dependent modules unavailable",
        )
        return

    # ── list / detail ──────────────────────────────────────────────

    @app.get("/api/skills")
    async def api_list_skills() -> list[dict]:
        try:
            skills = skills_registry.list_skills()
            # Reuse the app-wide 30 s discovery cache instead of triggering
            # three fresh PARA walks inside list_all_agent_keys().
            discovered = getattr(app, "_cache", {}).get("data") if hasattr(app, "_cache") else None
            agent_keys = skills_per_agent.list_all_agent_keys(discovered_data=discovered)
            # One bulk read of allowlists + global-enabled + existing-skills set
            # instead of O(skills × agents) per-pair file reads + is_dir stats.
            enabled_by_agent = skills_per_agent.enabled_for_all_agents(agent_keys)
        except Exception as e:
            _logger.exception("skills_routes: list_skills failed")
            raise _http_for(e) from e

        out: list[dict] = []
        for skill in skills:
            name = skill.get("name") if isinstance(skill, dict) else None
            if not name:
                continue
            # Pure dict-set membership; no I/O per skill.
            count = sum(1 for enabled in enabled_by_agent.values() if name in enabled)
            register = skill.get("register") or {}
            frontmatter = skill.get("frontmatter") or {}
            out.append({
                **skill,
                "source": register.get("source"),
                "version": register.get("version"),
                "icon": register.get("icon") or _icon_from_frontmatter(frontmatter),
                "description": register.get("description") or frontmatter.get("description"),
                "installed_at": register.get("installed_at"),
                "agents_enabled_count": count,
            })
        return out

    @app.get("/api/skills/{name}")
    async def api_get_skill(name: str) -> dict:
        safe = _validate_skill_name(name)
        try:
            skill = skills_registry.read_skill(safe)
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"skill not found: {safe}") from e
        except Exception as e:
            _logger.exception("skills_routes: read_skill failed for %s", safe)
            raise _http_for(e) from e

        if not skill:
            raise HTTPException(404, detail=f"skill not found: {safe}")

        discovered = getattr(app, "_cache", {}).get("data") if hasattr(app, "_cache") else None
        return {
            **skill,
            "agents": _agents_state_for(safe, discovered_data=discovered),
        }

    # ── install / uninstall ────────────────────────────────────────

    @app.post("/api/skills/install")
    async def api_install_skill(request: Request) -> dict:
        source, payload, enable_for, zip_bytes = await _read_install_payload(request)

        if not source:
            raise HTTPException(400, detail="source is required")
        if source not in {"github", "shorthand", "zip", "custom"}:
            raise HTTPException(
                400,
                detail=f"unknown source: {source!r} (allowed: github, shorthand, zip, custom)",
            )

        try:
            if source == "github":
                url = str(payload.get("url") or "").strip()
                if not url:
                    raise HTTPException(400, detail="payload.url is required for source=github")
                ref = str(payload.get("ref") or "main").strip() or "main"
                # git clone (up to 120s) — off-load so a single install never
                # freezes the whole server (incl. the 5s system poll).
                result = await asyncio.to_thread(
                    skills_install.install_from_github, url, ref=ref
                )
            elif source == "shorthand":
                repo = str(payload.get("repo") or "").strip()
                if not repo:
                    raise HTTPException(400, detail="payload.repo is required for source=shorthand")
                ref = str(payload.get("ref") or "main").strip() or "main"
                result = await asyncio.to_thread(
                    skills_install.install_from_shorthand, repo, ref=ref
                )
            elif source == "zip":
                if zip_bytes is None:
                    raise HTTPException(
                        400,
                        detail="source=zip requires multipart/form-data with a 'file' field",
                    )
                if not zip_bytes:
                    raise HTTPException(400, detail="uploaded zip is empty")
                name_hint = payload.get("name_hint")
                if name_hint is not None and not isinstance(name_hint, str):
                    raise HTTPException(400, detail="payload.name_hint must be a string")
                # extractall + shutil.copytree — off-load off the loop.
                result = await asyncio.to_thread(
                    skills_install.install_from_zip,
                    zip_bytes, name_hint=name_hint or None,
                )
            else:  # custom
                description = payload.get("description")
                if isinstance(description, str) and description.strip():
                    result = await skills_install.generate_skill_from_description(description)
                else:
                    custom_name = str(payload.get("name") or "").strip()
                    skill_md = payload.get("skill_md")
                    if not custom_name:
                        raise HTTPException(
                            400,
                            detail="payload.name (or payload.description) is required for source=custom",
                        )
                    if not isinstance(skill_md, str) or not skill_md.strip():
                        raise HTTPException(400, detail="payload.skill_md must be a non-empty string")
                    icon = payload.get("icon")
                    if icon is not None and not isinstance(icon, str):
                        raise HTTPException(400, detail="payload.icon must be a string")
                    result = skills_install.install_from_custom(
                        custom_name,
                        skill_md,
                        icon=icon or None,
                    )
        except HTTPException:
            raise
        except Exception as e:
            _logger.exception("skills_routes: install failed (source=%s)", source)
            raise _http_for(e) from e

        # Normalise installer result to ``installed: list[dict]`` so
        # marketplace expansions (one repo → many skills) and single-skill
        # installs share the same envelope.
        if isinstance(result, dict) and isinstance(result.get("installed"), list):
            installed = list(result["installed"])
        elif isinstance(result, list):
            installed = list(result)
        elif isinstance(result, dict):
            installed = [result]
        else:
            installed = []

        # Apply enablement to every freshly-installed skill.
        for entry in installed:
            name = entry.get("name") if isinstance(entry, dict) else None
            if name and enable_for:
                _apply_enablement(name, enable_for)

        return {"ok": True, "installed": installed}

    @app.delete("/api/skills/{name}")
    async def api_delete_skill(name: str) -> dict:
        safe = _validate_skill_name(name)
        try:
            skills_registry.delete_skill(safe)
        except FileNotFoundError as e:
            raise HTTPException(404, detail=f"skill not found: {safe}") from e
        except Exception as e:
            _logger.exception("skills_routes: delete_skill failed for %s", safe)
            raise _http_for(e) from e
        return {"ok": True}

    # ── update detection / apply ───────────────────────────────────

    @app.post("/api/skills/{name}/check-update")
    async def api_check_update(name: str) -> dict:
        safe = _validate_skill_name(name)
        try:
            # git fetch (up to 60s) — off the event loop.
            return await asyncio.to_thread(skills_install.check_update, safe)
        except FileNotFoundError as e:
            raise HTTPException(404, detail=f"skill not found: {safe}") from e
        except Exception as e:
            _logger.exception("skills_routes: check_update failed for %s", safe)
            raise _http_for(e) from e

    @app.post("/api/skills/{name}/update")
    async def api_update_skill(name: str) -> dict:
        safe = _validate_skill_name(name)
        try:
            # git fetch + reset --hard (up to 120s) — off the event loop.
            return await asyncio.to_thread(skills_install.update_skill, safe)
        except FileNotFoundError as e:
            raise HTTPException(404, detail=f"skill not found: {safe}") from e
        except Exception as e:
            _logger.exception("skills_routes: update_skill failed for %s", safe)
            raise _http_for(e) from e

    # ── per-skill bulk enable/disable ──────────────────────────────

    @app.patch("/api/skills/{name}/agents")
    async def api_patch_skill_agents(
        name: str, payload: dict = Body(default={}),
    ) -> dict:
        safe = _validate_skill_name(name)
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")

        enable_for = _parse_enable_for(payload.get("enable_for", []))
        disable_for = _parse_enable_for(payload.get("disable_for", []))

        try:
            # Sanity: skill must exist before we wire it onto any agent.
            if not skills_registry.read_skill(safe):
                raise HTTPException(404, detail=f"skill not found: {safe}")
        except HTTPException:
            raise
        except (FileNotFoundError, KeyError) as e:
            raise HTTPException(404, detail=f"skill not found: {safe}") from e
        except Exception as e:
            _logger.exception("skills_routes: read_skill failed for %s", safe)
            raise _http_for(e) from e

        for entry in enable_for:
            try:
                if entry == "all":
                    current = skills_registry.read_global_enabled()
                    skills_registry.write_global_enabled(set(current) | {safe})
                elif entry == "global":
                    current = skills_per_agent.read_allowlist("global", "global")
                    skills_per_agent.write_allowlist(
                        "global", "global", set(current) | {safe},
                    )
                else:
                    kind, lib_id = entry.split(":", 1) if ":" in entry else ("", "")
                    if not kind or not lib_id:
                        raise HTTPException(
                            400, detail=f"invalid enable_for entry: {entry!r}",
                        )
                    current = skills_per_agent.read_allowlist(kind, lib_id)
                    skills_per_agent.write_allowlist(
                        kind, lib_id, set(current) | {safe},
                    )
            except HTTPException:
                raise
            except Exception as e:
                _logger.exception(
                    "skills_routes: enable %s for %s failed", safe, entry,
                )
                raise _http_for(e) from e

        for entry in disable_for:
            try:
                if entry == "all":
                    current = skills_registry.read_global_enabled()
                    skills_registry.write_global_enabled(set(current) - {safe})
                elif entry == "global":
                    current = skills_per_agent.read_allowlist("global", "global")
                    skills_per_agent.write_allowlist(
                        "global", "global", set(current) - {safe},
                    )
                else:
                    kind, lib_id = entry.split(":", 1) if ":" in entry else ("", "")
                    if not kind or not lib_id:
                        raise HTTPException(
                            400, detail=f"invalid disable_for entry: {entry!r}",
                        )
                    current = skills_per_agent.read_allowlist(kind, lib_id)
                    skills_per_agent.write_allowlist(
                        kind, lib_id, set(current) - {safe},
                    )
            except HTTPException:
                raise
            except Exception as e:
                _logger.exception(
                    "skills_routes: disable %s for %s failed", safe, entry,
                )
                raise _http_for(e) from e

        return {"ok": True}

    # ── rescan webhook (used by create-skill meta) ─────────────────

    @app.post("/api/skills/rescan")
    async def api_rescan_skills() -> dict:
        try:
            skills = skills_registry.list_skills()
        except Exception as e:
            _logger.exception("skills_routes: rescan failed")
            raise _http_for(e) from e
        return {"ok": True, "count": len(skills)}

    # ── per-agent allowlist (mounted under /api/library/...) ───────

    @app.get("/api/library/{kind}/{name:path}/agent/skills")
    async def api_get_agent_skills(kind: str, name: str) -> dict:
        try:
            safe_kind = skills_per_agent.safe_kind(kind)
            safe_lib = skills_per_agent.safe_lib_id(name, safe_kind)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e

        try:
            allowlist = sorted(skills_per_agent.read_allowlist(safe_kind, safe_lib))
            global_enabled = sorted(skills_registry.read_global_enabled())
            effective = sorted(skills_per_agent.enabled_for_agent(safe_kind, safe_lib))
        except Exception as e:
            _logger.exception(
                "skills_routes: get_agent_skills failed for %s/%s",
                safe_kind, safe_lib,
            )
            raise _http_for(e) from e

        return {
            "enabled": effective,
            "allowlist": allowlist,
            "global_enabled": global_enabled,
        }

    @app.patch("/api/library/{kind}/{name:path}/agent/skills")
    async def api_patch_agent_skills(
        kind: str, name: str, payload: dict = Body(default={}),
    ) -> dict:
        # The {kind} arg here is the URL-level plural kind (areas/projects/
        # resources). skills_per_agent.safe_kind validates that.
        try:
            safe_kind = skills_per_agent.safe_kind(kind)
            safe_lib = skills_per_agent.safe_lib_id(name, safe_kind)
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e

        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")

        enabled_raw = payload.get("enabled")
        if not isinstance(enabled_raw, list):
            raise HTTPException(400, detail="enabled must be a list of skill names")

        # Validate each name is a known skill *and* well-formed before we
        # touch disk. This avoids leaving the allowlist in a half-written
        # state on a bad payload.
        cleaned: set[str] = set()
        for entry in enabled_raw:
            if not isinstance(entry, str):
                raise HTTPException(
                    400, detail="enabled entries must be strings",
                )
            try:
                cleaned.add(skills_registry.safe_skill_name(entry))
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e

        try:
            skills_per_agent.write_allowlist(safe_kind, safe_lib, cleaned)
        except Exception as e:
            _logger.exception(
                "skills_routes: write_allowlist failed for %s/%s",
                safe_kind, safe_lib,
            )
            raise _http_for(e) from e

        return {"ok": True}

    # ── Global agent's allowlist (kind=global, lib_id=global) ──────

    @app.patch("/api/orchestrator/global/skills")
    async def api_patch_global_skills(payload: dict = Body(default={})) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")

        enabled_raw = payload.get("enabled")
        if not isinstance(enabled_raw, list):
            raise HTTPException(400, detail="enabled must be a list of skill names")

        cleaned: set[str] = set()
        for entry in enabled_raw:
            if not isinstance(entry, str):
                raise HTTPException(
                    400, detail="enabled entries must be strings",
                )
            try:
                cleaned.add(skills_registry.safe_skill_name(entry))
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e

        try:
            skills_per_agent.write_allowlist("global", "global", cleaned)
        except Exception as e:
            _logger.exception("skills_routes: write_allowlist(global) failed")
            raise _http_for(e) from e

        return {"ok": True}
