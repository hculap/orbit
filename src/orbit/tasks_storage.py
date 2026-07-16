"""tasks_storage — sidecar at ``~/.orchestrator/tasks.json``.

Stores **reminder config + per-fire delivery state only**. Issue/project data
live in GitHub.

Schema v2::

    {
      "_v": 2,
      "tasks": {
        "<issue_node_id>": {
          "reminders": [<Reminder>, ...],
          "fired":     {"<reminder_index>": "<iso_local_ts>"},
          "due_at_local": "2026-05-12T09:00:00+02:00",
          "due_time":     "17:30",          # optional task-level hour (HH:MM)
          "updated_at":   "2026-05-10T12:30:00+02:00"
        }
      },
      "standalone": {
        "<uuid>": {
          "title":    "Zadzwonić do X",
          "body":     "...",                # optional
          "fire_at":  "2026-05-12T17:00:00+02:00",   # required
          "fired_at": "2026-05-12T17:00:03+02:00",   # null until fired
          "priority": "P1-Must",            # optional
          "area_slug": null,
          "proj_slug": null,
          "task_link": null,                # optional issue_node_id
          "created_at": "...",
          "updated_at": "..."
        }
      }
    }

Legacy v1 (flat ``{<issue_node_id>: entry}``) is migrated to v2 on first load.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".orchestrator" / "tasks.json"
SCHEMA_VERSION = 2

# Module-level state. Tests monkeypatch _TASKS_PATH for isolation.
_TASKS_PATH: Path = DEFAULT_PATH
_data: dict[str, Any] | None = None
_loaded_mtime: float | None = None
_lock = asyncio.Lock()


def _warn(msg: str) -> None:
    print(f"[tasks_storage] {msg}", file=sys.stderr)


def set_storage_path(path: Path) -> None:
    """Override on-disk location (tests) and drop the cached payload."""
    global _TASKS_PATH, _data, _loaded_mtime
    _TASKS_PATH = path
    _data = None
    _loaded_mtime = None


def _ensure_dir() -> None:
    _TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _empty_root() -> dict[str, Any]:
    return {"_v": SCHEMA_VERSION, "tasks": {}, "standalone": {}}


def _normalize_task_entry(raw: dict[str, Any]) -> dict[str, Any]:
    reminders = raw.get("reminders") if isinstance(raw.get("reminders"), list) else []
    fired = raw.get("fired") if isinstance(raw.get("fired"), dict) else {}
    cleaned_fired: dict[str, str] = {}
    for idx_key, ts in fired.items():
        try:
            int(idx_key)
        except (TypeError, ValueError):
            continue
        if isinstance(ts, str) and ts:
            cleaned_fired[str(idx_key)] = ts
    return {
        "reminders": [r for r in reminders if isinstance(r, dict)],
        "fired": cleaned_fired,
        "due_at_local": raw.get("due_at_local") if isinstance(raw.get("due_at_local"), str) else None,
        "due_time": raw.get("due_time") if isinstance(raw.get("due_time"), str) else None,
        "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
    }


def _normalize_standalone_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    title = raw.get("title")
    fire_at = raw.get("fire_at")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(fire_at, str) or not fire_at.strip():
        return None
    return {
        "title": title.strip(),
        "body": raw.get("body") if isinstance(raw.get("body"), str) else None,
        "fire_at": fire_at,
        "fired_at": raw.get("fired_at") if isinstance(raw.get("fired_at"), str) else None,
        "priority": raw.get("priority") if isinstance(raw.get("priority"), str) else None,
        "area_slug": raw.get("area_slug") if isinstance(raw.get("area_slug"), str) else None,
        "proj_slug": raw.get("proj_slug") if isinstance(raw.get("proj_slug"), str) else None,
        "task_link": raw.get("task_link") if isinstance(raw.get("task_link"), str) else None,
        "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else None,
        "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
    }


def _load_from_disk() -> dict[str, Any]:
    if not _TASKS_PATH.exists():
        return _empty_root()
    try:
        with _TASKS_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt or unreadable {_TASKS_PATH.name}: {exc}; treating as empty")
        return _empty_root()
    if not isinstance(payload, dict):
        _warn(f"{_TASKS_PATH.name} is not an object; treating as empty")
        return _empty_root()

    if payload.get("_v") == SCHEMA_VERSION:
        tasks_raw = payload.get("tasks") if isinstance(payload.get("tasks"), dict) else {}
        sa_raw = payload.get("standalone") if isinstance(payload.get("standalone"), dict) else {}
        return {
            "_v": SCHEMA_VERSION,
            "tasks": {
                k: _normalize_task_entry(v)
                for k, v in tasks_raw.items()
                if isinstance(k, str) and isinstance(v, dict)
            },
            "standalone": {
                k: norm
                for k, v in sa_raw.items()
                if isinstance(k, str) and isinstance(v, dict)
                and (norm := _normalize_standalone_entry(v)) is not None
            },
        }

    # Legacy v1 — flat dict of issue_node_id → entry. Migrate.
    migrated_tasks: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if key.startswith("_"):
            continue
        migrated_tasks[key] = _normalize_task_entry(value)
    return {"_v": SCHEMA_VERSION, "tasks": migrated_tasks, "standalone": {}}


def _ensure_loaded() -> dict[str, Any]:
    """Lazy-load from disk; re-read if the file changed under us.

    The cron tick runs as a separate subprocess and writes to the same
    sidecar; checking ``st_mtime`` lets the long-lived dashboard process
    see those writes without restarting.
    """
    global _data, _loaded_mtime
    try:
        mtime = _TASKS_PATH.stat().st_mtime if _TASKS_PATH.exists() else 0.0
    except OSError:
        mtime = 0.0
    if _data is None or mtime != _loaded_mtime:
        _ensure_dir()
        _data = _load_from_disk()
        _loaded_mtime = mtime
    return _data


def _atomic_write(payload: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(_TASKS_PATH.parent),
        prefix=f".{_TASKS_PATH.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, _TASKS_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _default_task_entry() -> dict[str, Any]:
    return {"reminders": [], "fired": {}, "due_at_local": None, "due_time": None, "updated_at": None}


# ── Task-attached reminders API ────────────────────────────────


def get_entry(issue_node_id: str) -> dict[str, Any]:
    """Return the sidecar entry for an issue (defaults for unknowns)."""
    data = _ensure_loaded()
    entry = (data.get("tasks") or {}).get(issue_node_id)
    if not entry:
        return _default_task_entry()
    return {
        "reminders": list(entry.get("reminders") or []),
        "fired": dict(entry.get("fired") or {}),
        "due_at_local": entry.get("due_at_local"),
        "due_time": entry.get("due_time"),
        "updated_at": entry.get("updated_at"),
    }


def all_entries() -> dict[str, dict[str, Any]]:
    """Snapshot of every task entry — caller owns the copy."""
    data = _ensure_loaded()
    return {k: dict(v) for k, v in (data.get("tasks") or {}).items()}


def is_fired(entry: dict[str, Any], reminder_index: int) -> bool:
    fired = entry.get("fired") or {}
    return str(reminder_index) in fired


def _reminders_equal(a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None) -> bool:
    if (a or []) == (b or []):
        return True
    try:
        return json.dumps(a or [], sort_keys=True) == json.dumps(b or [], sort_keys=True)
    except TypeError:
        return False


async def upsert_reminders(
    issue_node_id: str,
    reminders: list[dict[str, Any]],
    due_at_local: str | None,
    due_time: str | None = None,
) -> dict[str, Any]:
    """Set reminders + due_at_local snapshot. Resets ``fired`` if either changed."""
    async with _lock:
        data = _ensure_loaded()
        tasks_dict = dict(data.get("tasks") or {})
        prev = tasks_dict.get(issue_node_id) or _default_task_entry()
        same_reminders = _reminders_equal(prev.get("reminders"), reminders)
        same_due = prev.get("due_at_local") == due_at_local
        # Preserve previously-stored due_time if caller passed None
        stored_due_time = prev.get("due_time") if due_time is None else due_time
        new_entry: dict[str, Any] = {
            "reminders": list(reminders or []),
            "fired": dict(prev.get("fired") or {}) if (same_reminders and same_due) else {},
            "due_at_local": due_at_local,
            "due_time": stored_due_time,
            "updated_at": _now_iso(),
        }
        tasks_dict[issue_node_id] = new_entry
        new_data = {**data, "tasks": tasks_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None
        return new_entry


async def set_due_time(issue_node_id: str, due_time: str | None) -> None:
    """Update the local sidecar's optional 'HH:MM' time component for a task."""
    async with _lock:
        data = _ensure_loaded()
        tasks_dict = dict(data.get("tasks") or {})
        prev = tasks_dict.get(issue_node_id) or _default_task_entry()
        new_entry = {**prev, "due_time": due_time, "updated_at": _now_iso()}
        tasks_dict[issue_node_id] = new_entry
        new_data = {**data, "tasks": tasks_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None


async def mark_fired(
    issue_node_id: str,
    reminder_index: int,
    fired_at_iso: str | None = None,
) -> None:
    ts = fired_at_iso or _now_iso()
    async with _lock:
        data = _ensure_loaded()
        tasks_dict = dict(data.get("tasks") or {})
        prev = tasks_dict.get(issue_node_id) or _default_task_entry()
        fired = dict(prev.get("fired") or {})
        fired.setdefault(str(reminder_index), ts)
        new_entry = {**prev, "fired": fired, "updated_at": _now_iso()}
        tasks_dict[issue_node_id] = new_entry
        new_data = {**data, "tasks": tasks_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None


async def reset_fired(
    issue_node_id: str,
    *,
    keep_indices: set[int] | None = None,
) -> None:
    async with _lock:
        data = _ensure_loaded()
        tasks_dict = dict(data.get("tasks") or {})
        if issue_node_id not in tasks_dict:
            return
        prev = tasks_dict[issue_node_id]
        if keep_indices is None:
            new_fired: dict[str, str] = {}
        else:
            keep_keys = {str(i) for i in keep_indices}
            new_fired = {k: v for k, v in (prev.get("fired") or {}).items() if k in keep_keys}
        tasks_dict[issue_node_id] = {**prev, "fired": new_fired, "updated_at": _now_iso()}
        new_data = {**data, "tasks": tasks_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None


async def remove(issue_node_id: str) -> None:
    """Drop the task entry entirely (called when a task closes)."""
    async with _lock:
        data = _ensure_loaded()
        tasks_dict = dict(data.get("tasks") or {})
        if issue_node_id not in tasks_dict:
            return
        del tasks_dict[issue_node_id]
        new_data = {**data, "tasks": tasks_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None


# ── Standalone reminders API ───────────────────────────────────


def _default_standalone() -> dict[str, Any]:
    return {
        "title": "", "body": None, "fire_at": None, "fired_at": None,
        "priority": None, "area_slug": None, "proj_slug": None, "task_link": None,
        "created_at": None, "updated_at": None,
    }


def get_standalone(rid: str) -> dict[str, Any] | None:
    data = _ensure_loaded()
    entry = (data.get("standalone") or {}).get(rid)
    return dict(entry) if entry else None


def all_standalone() -> dict[str, dict[str, Any]]:
    data = _ensure_loaded()
    return {k: dict(v) for k, v in (data.get("standalone") or {}).items()}


async def create_standalone(
    *,
    title: str,
    fire_at: str,
    body: str | None = None,
    priority: str | None = None,
    area_slug: str | None = None,
    proj_slug: str | None = None,
    task_link: str | None = None,
) -> tuple[str, dict[str, Any]]:
    rid = uuid.uuid4().hex
    now = _now_iso()
    entry = {
        "title": title.strip(),
        "body": body,
        "fire_at": fire_at,
        "fired_at": None,
        "priority": priority,
        "area_slug": area_slug,
        "proj_slug": proj_slug,
        "task_link": task_link,
        "created_at": now,
        "updated_at": now,
    }
    async with _lock:
        data = _ensure_loaded()
        sa_dict = dict(data.get("standalone") or {})
        sa_dict[rid] = entry
        new_data = {**data, "standalone": sa_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None
    return rid, entry


async def update_standalone(rid: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Apply a partial update to a standalone reminder. Returns new entry or None."""
    allowed = {"title", "body", "fire_at", "priority", "area_slug", "proj_slug", "task_link"}
    async with _lock:
        data = _ensure_loaded()
        sa_dict = dict(data.get("standalone") or {})
        prev = sa_dict.get(rid)
        if not prev:
            return None
        merged = dict(prev)
        for k, v in patch.items():
            if k not in allowed:
                continue
            merged[k] = v
        # When the fire_at changes we re-arm the entry (clear fired_at).
        if "fire_at" in patch and patch["fire_at"] != prev.get("fire_at"):
            merged["fired_at"] = None
        merged["updated_at"] = _now_iso()
        sa_dict[rid] = merged
        new_data = {**data, "standalone": sa_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None
        return merged


async def mark_standalone_fired(rid: str, fired_at_iso: str | None = None) -> None:
    ts = fired_at_iso or _now_iso()
    async with _lock:
        data = _ensure_loaded()
        sa_dict = dict(data.get("standalone") or {})
        prev = sa_dict.get(rid)
        if not prev:
            return
        if prev.get("fired_at"):
            return  # idempotent
        sa_dict[rid] = {**prev, "fired_at": ts, "updated_at": _now_iso()}
        new_data = {**data, "standalone": sa_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None


async def remove_standalone(rid: str) -> bool:
    async with _lock:
        data = _ensure_loaded()
        sa_dict = dict(data.get("standalone") or {})
        if rid not in sa_dict:
            return False
        del sa_dict[rid]
        new_data = {**data, "standalone": sa_dict}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
        try:
            globals()["_loaded_mtime"] = _TASKS_PATH.stat().st_mtime
        except OSError:
            globals()["_loaded_mtime"] = None
        return True
