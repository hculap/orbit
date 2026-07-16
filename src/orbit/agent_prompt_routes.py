"""Per-agent prompt-stack routes.

Three endpoints registered under ``/api/library/{kind}/{name:path}/agent/...``
that must come BEFORE the catch-all ``PATCH /api/library/{kind}/{name:path}``
in :func:`orbit.library.register_routes` (FastAPI uses
first-match).

Storage lives centrally in ``~/.orchestrator/agents/<kind>/<lib_id>/``
(see :mod:`agent_prompts`); this module is purely the HTTP layer.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException

from . import agent_identity_generator
from . import agent_prompts
from . import library_files
from .library import _http_for, _safe_area_path, _safe_project_path, _invalidate_cache

# Caps (bytes — UTF-8 encoded). Match plan section 4 + sidecar limits.
IDENTITY_MAX_BYTES = 8 * 1024
CUSTOM_MAX_BYTES = 8 * 1024

# Icon: single grapheme-ish, allow ZWJ sequences (emoji families etc.) up to
# 8 unicode codepoints. Empty string clears the icon.
ICON_MAX_CHARS = 8


def _resolve_item(kind: str, name: str) -> tuple[Literal["area", "project"], str, Path]:
    """Validate ``kind``/``name`` and return ``(single_kind, lib_id, abs_path)``.

    ``lib_id`` is the disk-relative identifier used by :mod:`agent_prompts`
    (equals ``name`` for areas; for nested projects it's ``group/name`` —
    matches the existing ``{name:path}`` URL convention).

    Raises ``HTTPException(400)`` for bad kind/name; ``HTTPException(404)``
    if the area/project directory doesn't exist on disk.
    """
    if kind not in ("areas", "projects"):
        raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
    single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
    try:
        item_path = (
            _safe_area_path(name) if single_kind == "area" else _safe_project_path(name)
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    if not item_path.is_dir():
        raise HTTPException(404, detail=f"{kind}/{name} not found")
    # lib_id matches the URL `{name:path}` segment; agent_prompts treats it
    # as a relative subpath and creates dirs as needed.
    return single_kind, name, item_path


def _read_text_safe(path: Path) -> str:
    """Read UTF-8 text; missing or unreadable file returns ``""``."""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _atomic_write_text(path: Path, content: str) -> None:
    """tempfile + os.replace in the same directory. Creates parent dirs."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".prompt.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _validate_icon(raw: str) -> str:
    """Reject icon longer than ``ICON_MAX_CHARS`` codepoints. Empty allowed."""
    if not isinstance(raw, str):
        raise ValueError("icon must be a string")
    # Empty string = clear the icon. Otherwise: at least 1 char, at most 8.
    if raw == "":
        return ""
    n = len(raw)
    if n < 1 or n > ICON_MAX_CHARS:
        raise ValueError(
            f"icon must be 1-{ICON_MAX_CHARS} unicode characters (got {n})"
        )
    return raw


def register(app: FastAPI) -> None:
    """Mount the three /agent/prompts routes on ``app``.

    MUST be called before the catch-all ``PATCH /api/library/{kind}/{name:path}``
    or the ``/agent/prompts`` PATCH gets shadowed.
    """

    @app.get("/api/library/{kind}/{name:path}/agent/prompts")
    async def api_get_agent_prompts(kind: str, name: str) -> dict:
        single_kind, lib_id, _item_path = _resolve_item(kind, name)
        try:
            general_path = agent_prompts.general_prompt_path()
            orchestrator_path = agent_prompts.orchestrator_prompt_path()
            identity_path = agent_prompts.agent_identity_path(single_kind, lib_id)
            custom_path = agent_prompts.agent_custom_path(single_kind, lib_id)
            icon = agent_prompts.read_icon(single_kind, lib_id) or ""
        except Exception as e:
            raise _http_for(e) from e

        return {
            "ok": True,
            "general": {
                "content": _read_text_safe(general_path),
                "path": str(general_path),
                "readonly": True,
            },
            "orchestrator": {
                "content": _read_text_safe(orchestrator_path),
                "path": str(orchestrator_path),
                "readonly": True,
            },
            "identity": {
                "content": _read_text_safe(identity_path),
                "path": str(identity_path),
            },
            "custom": {
                "content": _read_text_safe(custom_path),
                "path": str(custom_path),
            },
            "icon": icon,
        }

    @app.patch("/api/library/{kind}/{name:path}/agent/prompts")
    async def api_patch_agent_prompts(
        kind: str, name: str, payload: dict = Body(default={}),
    ) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")
        allowed = {"identity", "custom", "icon"}
        extra = set(payload.keys()) - allowed
        if extra:
            raise HTTPException(
                400, detail=f"unknown fields: {sorted(extra)}",
            )

        single_kind, lib_id, _item_path = _resolve_item(kind, name)

        # Validate every field BEFORE any disk write so a bad payload is
        # all-or-nothing.
        identity_val: str | None = None
        custom_val: str | None = None
        icon_val: str | None = None

        if "identity" in payload:
            raw = payload["identity"]
            if not isinstance(raw, str):
                raise HTTPException(400, detail="identity must be a string")
            if len(raw.encode("utf-8")) > IDENTITY_MAX_BYTES:
                raise HTTPException(
                    400, detail=f"identity too large (>{IDENTITY_MAX_BYTES} bytes)",
                )
            identity_val = raw

        if "custom" in payload:
            raw = payload["custom"]
            if not isinstance(raw, str):
                raise HTTPException(400, detail="custom must be a string")
            if len(raw.encode("utf-8")) > CUSTOM_MAX_BYTES:
                raise HTTPException(
                    400, detail=f"custom too large (>{CUSTOM_MAX_BYTES} bytes)",
                )
            custom_val = raw

        if "icon" in payload:
            try:
                icon_val = _validate_icon(payload["icon"])
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e

        try:
            if identity_val is not None:
                _atomic_write_text(
                    agent_prompts.agent_identity_path(single_kind, lib_id),
                    identity_val,
                )
            if custom_val is not None:
                _atomic_write_text(
                    agent_prompts.agent_custom_path(single_kind, lib_id),
                    custom_val,
                )
            if icon_val is not None:
                agent_prompts.write_icon(single_kind, lib_id, icon_val)
                # Mirror into the library sidecar so /api/data + the agents
                # directory + chat header (which read from discovery, not
                # from icon.txt) reflect the new icon without requiring a
                # second round-trip. Invalidate the discovery cache too so
                # the next /api/data poll picks it up.
                try:
                    library_files.write_agent(
                        single_kind, lib_id, {"icon": icon_val},
                    )
                    _invalidate_cache(app)
                except Exception:
                    # Best-effort mirror — icon.txt write already succeeded;
                    # don't fail the whole request if the sidecar write
                    # races with another writer.
                    pass
        except Exception as e:
            raise _http_for(e) from e

        # Return current state (re-read so we never lie about the on-disk
        # content if the caller raced with another writer).
        return {
            "ok": True,
            "identity": _read_text_safe(
                agent_prompts.agent_identity_path(single_kind, lib_id)
            ),
            "custom": _read_text_safe(
                agent_prompts.agent_custom_path(single_kind, lib_id)
            ),
            "icon": agent_prompts.read_icon(single_kind, lib_id) or "",
        }

    @app.post("/api/library/{kind}/{name:path}/agent/regenerate-identity")
    async def api_regenerate_identity(kind: str, name: str) -> dict:
        single_kind, lib_id, item_path = _resolve_item(kind, name)
        # Synchronous (10-30s blocking) — accepted by the plan.
        # Don't fail the route on generator error: surface {ok: false, error}
        # so the UI can show inline feedback.
        try:
            result = await agent_identity_generator.generate_identity(
                single_kind, lib_id, item_path,
            )
        except Exception as e:  # never crash the route — surface it
            return {"ok": False, "identity": "", "icon": "", "error": str(e)}

        if not isinstance(result, dict):
            return {
                "ok": False, "identity": "", "icon": "",
                "error": "generator returned non-dict",
            }
        return {
            "ok": bool(result.get("ok")),
            "identity": result.get("identity") or "",
            "icon": result.get("icon") or "",
            "error": result.get("error"),
        }
