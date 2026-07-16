"""Settings routes for the shared general/orchestrator prompts.

These two files (``~/.orchestrator/agent-prompts/{general,orchestrator}.md``)
are the global envelope shared by every agent. Editing happens here; per-agent
identity/custom layers are handled by :mod:`agent_prompt_routes`.

Cap is 32 KB per file (vs 8 KB on per-agent identity/custom) — the global
prompts carry the format envelope, response schema, and block-type rules,
so they're naturally larger.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException

from . import agent_prompts
from .library import _http_for

PROMPT_MAX_BYTES = 32 * 1024


def _read_text_safe(path: Path) -> str:
    """Read UTF-8 text; missing or unreadable returns ``""``."""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _atomic_write_text(path: Path, content: str) -> None:
    """tempfile + os.replace in same dir; creates parent dirs as needed."""
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


def register(app: FastAPI) -> None:
    """Mount /api/settings/prompts GET + PATCH on ``app``."""

    @app.get("/api/settings/prompts")
    async def api_get_settings_prompts() -> dict:
        try:
            general_path = agent_prompts.general_prompt_path()
            orchestrator_path = agent_prompts.orchestrator_prompt_path()
        except Exception as e:
            raise _http_for(e) from e
        return {
            "ok": True,
            "general": _read_text_safe(general_path),
            "orchestrator": _read_text_safe(orchestrator_path),
        }

    @app.patch("/api/settings/prompts")
    async def api_patch_settings_prompts(payload: dict = Body(default={})) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")
        allowed = {"general", "orchestrator"}
        extra = set(payload.keys()) - allowed
        if extra:
            raise HTTPException(400, detail=f"unknown fields: {sorted(extra)}")

        general_val: str | None = None
        orchestrator_val: str | None = None

        if "general" in payload:
            raw = payload["general"]
            if not isinstance(raw, str):
                raise HTTPException(400, detail="general must be a string")
            if len(raw.encode("utf-8")) > PROMPT_MAX_BYTES:
                raise HTTPException(
                    400, detail=f"general too large (>{PROMPT_MAX_BYTES} bytes)",
                )
            general_val = raw

        if "orchestrator" in payload:
            raw = payload["orchestrator"]
            if not isinstance(raw, str):
                raise HTTPException(400, detail="orchestrator must be a string")
            if len(raw.encode("utf-8")) > PROMPT_MAX_BYTES:
                raise HTTPException(
                    400, detail=f"orchestrator too large (>{PROMPT_MAX_BYTES} bytes)",
                )
            orchestrator_val = raw

        try:
            if general_val is not None:
                _atomic_write_text(agent_prompts.general_prompt_path(), general_val)
            if orchestrator_val is not None:
                _atomic_write_text(
                    agent_prompts.orchestrator_prompt_path(), orchestrator_val,
                )
        except Exception as e:
            raise _http_for(e) from e

        return {
            "ok": True,
            "general": _read_text_safe(agent_prompts.general_prompt_path()),
            "orchestrator": _read_text_safe(agent_prompts.orchestrator_prompt_path()),
        }
