"""Header-modal state extraction for the orchestrator session viewers.

Two pieces of derived session state surfaced through one combined endpoint:

1. Latest TodoWrite snapshot — same logic as
   ``orchestrator_compact._extract_latest_task_list`` but WITHOUT the status
   filter, so the modal can render completed entries with a checked checkbox
   alongside the pending / in_progress ones.
2. Latest plan file referenced — walks tool_use blocks backwards looking for
   a path under ``~/.claude/plans/*.md`` (Read / Edit / Write / EnterPlanMode
   / ExitPlanMode all carry the path on ``input.file_path`` or ``input.path``)
   and reads its on-disk content.

Plans live in a flat cwd-scoped directory; any session can reference any
plan, so we resolve content from disk rather than from the JSONL itself.
"""
from __future__ import annotations
import asyncio
from pathlib import Path

from . import orchestrator_jsonl as jsonl_mod

# Canonical plans directory — resolved once at import time. ``Path.home()``
# is cheap but we still keep it module-scoped for readability.
_PLANS_DIR = Path.home() / ".claude" / "plans"

# Hard cap on returned plan content. The modal viewer is a glorified textarea;
# multi-MB plans would choke the JSON serializer and the browser. 200 KB is
# ~50× the typical plan size we've seen.
_PLAN_CONTENT_MAX = 200 * 1024


def _extract_all_todos(session_id: str) -> list[dict]:
    """Return every todo from the most recent TodoWrite call (no status filter).

    Mirrors ``orchestrator_compact._extract_latest_task_list`` but keeps
    completed entries — the modal renders them with a checked checkbox so the
    user sees the full picture.

    Each entry: ``{"subject": str, "status": str, "description": str}``. The
    orchestrator's TodoWrite uses ``activeForm`` for the in-progress phrasing
    (e.g. "Wiring routes"); we fall back to ``description`` for older sessions
    that haven't been re-run since the schema change.

    Empty list when no TodoWrite ever ran or the session JSONL is missing.
    """
    parsed = jsonl_mod.read_session(session_id)
    if not parsed.get("ok"):
        return []
    messages = parsed.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("blocks") or []:
            if block.get("kind") != "tool_use":
                continue
            if block.get("name") != "TodoWrite":
                continue
            todos = (block.get("input") or {}).get("todos") or []
            if not isinstance(todos, list):
                continue
            out: list[dict] = []
            for t in todos:
                if not isinstance(t, dict):
                    continue
                status = t.get("status")
                if status not in ("pending", "in_progress", "completed"):
                    continue
                subject = (t.get("content") or "").strip()
                if not subject:
                    continue
                description = (t.get("activeForm") or t.get("description") or "").strip()
                out.append({
                    "subject": subject,
                    "status": status,
                    "description": description,
                })
            return out
    return []


def _plan_path_from_input(tool_input: object) -> str | None:
    """Return a normalized ``~/.claude/plans/*.md`` path from a tool_use input.

    Read / Edit / Write use ``file_path``; EnterPlanMode / ExitPlanMode use
    ``path`` for the plan file. We accept either key and reject anything that
    doesn't resolve under the plans directory or doesn't end in ``.md``.
    """
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    if not raw.endswith(".md"):
        return None
    plans_prefix = str(_PLANS_DIR) + "/"
    if not raw.startswith(plans_prefix):
        return None
    return raw


def _read_plan_file(path: Path) -> tuple[str | None, float]:
    """Read plan content from disk; return (content, mtime).

    Returns ``(None, 0.0)`` when the file is missing or unreadable so the
    frontend can show a "plan file deleted" state. Truncates to
    ``_PLAN_CONTENT_MAX`` bytes-equivalent of characters with a tail marker.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return (None, 0.0)
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (None, mtime)
    if len(content) > _PLAN_CONTENT_MAX:
        kb = len(content) // 1024
        content = content[:_PLAN_CONTENT_MAX] + f"\n\n…[truncated, file is {kb} KB]"
    return (content, mtime)


def _extract_latest_plan(session_id: str) -> dict | None:
    """Walk session backwards for the most recent plan-file reference.

    Returns ``{"path": str, "content": str | None, "mtime": float}`` for the
    first matching tool_use found while walking messages from the end. ``content``
    is ``None`` when the file no longer exists on disk (deleted between runs).
    Returns ``None`` when no tool_use in the session ever referenced a plan.
    """
    parsed = jsonl_mod.read_session(session_id)
    if not parsed.get("ok"):
        return None
    messages = parsed.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        # Within an assistant message, later blocks are more recent — scan
        # the block list in reverse so we pick the latest reference inside
        # the latest containing message.
        for block in reversed(msg.get("blocks") or []):
            if block.get("kind") != "tool_use":
                continue
            path_str = _plan_path_from_input(block.get("input"))
            if path_str is None:
                continue
            content, mtime = _read_plan_file(Path(path_str))
            return {"path": path_str, "content": content, "mtime": mtime}
    return None


async def get_session_state(session_id: str) -> dict:
    """Return ``{"todos": [...], "plan": {...} | None}`` for the modal viewers.

    Both extractors are I/O-bound (JSONL parse + plan read), so we offload to
    the default executor to keep the FastAPI worker responsive on big sessions.
    """
    loop = asyncio.get_running_loop()
    todos, plan = await asyncio.gather(
        loop.run_in_executor(None, _extract_all_todos, session_id),
        loop.run_in_executor(None, _extract_latest_plan, session_id),
    )
    return {"todos": todos, "plan": plan}
