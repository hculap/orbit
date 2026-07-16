"""Orchestrator sidecar — per-session `archived` flag + optional title override.

Plain dict-on-disk at `~/.orchestrator/sessions_meta.json`. JSONL is the
source of truth; this file only carries metadata Claude doesn't write
itself. Atomic writes (tempfile + os.replace), asyncio lock for
concurrent FastAPI access. If the file is corrupt we log and treat as
empty — never delete it (keep for forensic).
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

META_PATH = Path.home() / ".orchestrator" / "sessions_meta.json"

_DEFAULT = {
    "archived": False,
    "title": None,
    "title_manual": False,
    "pinned": False,
    # Keep-alive: when True the session's tmux slot is exempt from the idle
    # eviction reap (see TmuxPool._persistent_ids). Distinct from `pinned`,
    # which is favorite/ordering only.
    "persistent": False,
    "pinned_turn_idxs": [],
    "compacted_from": None,
    "compacted_to": None,
    # Provenance for a session minted by the teleport importer — the
    # source session id the transcript was teleported from. Cosmetic /
    # forensic only (like compacted_from); never duplicates JSONL data.
    "teleported_from": None,
    "model": None,
    # Per-agent fields. Sessions launched from a Library item carry the cwd
    # of that item plus the lib_id (e.g. "areas/Home") for filter UI. Legacy
    # sessions predating this feature carry None for all three (treated as
    # "Global" sessions in the directory view).
    "cwd": None,
    "lib_id": None,
    "extra_prompt_path": None,
    # Unix timestamp of the FIRST sidecar write for this id. Used by the
    # orphan-summary stub so a session created via POST /sessions has a
    # stable `updated_at` between back-to-back list polls (was: `time.time()`
    # regenerated every call → orphan kept floating around the sort).
    "created_at": 0.0,
    # Count of messages the user has acknowledged ("seen") for this session.
    # `unread_count = max(0, msg_count - last_read_msg_count)`. None means
    # never read (treated as 0 — every existing message is unread). Mutated
    # by POST /api/orchestrator/sessions/<sid>/read.
    "last_read_msg_count": None,
}

# Whitelist of accepted `--model` aliases. None means "no flag → CLI default".
ALLOWED_MODELS: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})

_data: dict[str, dict] | None = None
_lock = asyncio.Lock()


def _warn(msg: str) -> None:
    print(f"[orchestrator_meta] {msg}", file=sys.stderr)


def _ensure_dir() -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_from_disk() -> dict[str, dict]:
    if not META_PATH.exists():
        return {}
    try:
        with META_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        _warn(f"corrupt or unreadable {META_PATH.name}: {e}; treating as empty")
        return {}
    if not isinstance(payload, dict):
        _warn(f"{META_PATH.name} is not an object; treating as empty")
        return {}
    cleaned: dict[str, dict] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        title_val = value.get("title") if isinstance(value.get("title"), str) else None
        # Legacy migration: entries created before the auto-title feature
        # don't carry `title_manual`. If they have a non-empty title we treat
        # it as manual so the auto-titler doesn't suddenly overwrite long-
        # standing user-renamed sessions or compact-derived "Compact: …"
        # titles. New auto-set entries always carry the flag explicitly.
        if "title_manual" in value:
            title_manual_val = bool(value.get("title_manual"))
        else:
            title_manual_val = bool(title_val)
        ca_raw = value.get("created_at")
        try:
            created_at_val = float(ca_raw) if isinstance(ca_raw, (int, float)) else 0.0
        except (TypeError, ValueError):
            created_at_val = 0.0
        cleaned[key] = {
            "archived": bool(value.get("archived", False)),
            "title": title_val,
            "title_manual": title_manual_val,
            "pinned": bool(value.get("pinned", False)),
            "persistent": bool(value.get("persistent", False)),
            "pinned_turn_idxs": _normalize_turn_idxs(value.get("pinned_turn_idxs")),
            "compacted_from": _normalize_session_ref(value.get("compacted_from")),
            "compacted_to": _normalize_session_ref(value.get("compacted_to")),
            "teleported_from": _normalize_session_ref(value.get("teleported_from")),
            "model": _normalize_model(value.get("model")),
            "cwd": _normalize_path_str(value.get("cwd")),
            "lib_id": _normalize_session_ref(value.get("lib_id")),
            "extra_prompt_path": _normalize_path_str(value.get("extra_prompt_path")),
            "created_at": created_at_val,
        }
    return cleaned


def _normalize_path_str(raw: object) -> str | None:
    """Coerce to a non-empty path string, or None.

    No filesystem checks here — the writer (orchestrator session create) is
    responsible for validating cwd existence + path-traversal guards. This
    normalizer just guards against garbage in the on-disk JSON (numbers,
    nested dicts etc).
    """
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _normalize_model(raw: object) -> str | None:
    """Coerce to a whitelisted alias or None (no `--model` flag)."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip().lower()
    if not stripped:
        return None
    return stripped if stripped in ALLOWED_MODELS else None


def _normalize_session_ref(raw: object) -> str | None:
    """Coerce to a non-empty string session id, or None."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _normalize_turn_idxs(raw: object) -> list[int]:
    """Coerce to sorted, deduped list of non-negative ints; drop garbage."""
    if not isinstance(raw, list):
        return []
    seen: set[int] = set()
    for item in raw:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item >= 0:
            seen.add(item)
    return sorted(seen)


def _ensure_loaded() -> dict[str, dict]:
    global _data
    if _data is None:
        _ensure_dir()
        _data = _load_from_disk()
    return _data


def _atomic_write(payload: dict[str, dict]) -> None:
    _ensure_dir()
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(META_PATH.parent),
        prefix=".sessions_meta.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, META_PATH)
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


def get_meta(session_id: str) -> dict:
    """Return sidecar fields for session_id; defaults for missing entry."""
    data = _ensure_loaded()
    entry = data.get(session_id)
    if not entry:
        return {**_DEFAULT, "pinned_turn_idxs": []}
    return {
        "archived": bool(entry.get("archived", False)),
        "title": entry.get("title") if isinstance(entry.get("title"), str) else None,
        "title_manual": bool(entry.get("title_manual", False)),
        "pinned": bool(entry.get("pinned", False)),
        "persistent": bool(entry.get("persistent", False)),
        "pinned_turn_idxs": _normalize_turn_idxs(entry.get("pinned_turn_idxs")),
        "compacted_from": _normalize_session_ref(entry.get("compacted_from")),
        "compacted_to": _normalize_session_ref(entry.get("compacted_to")),
        "teleported_from": _normalize_session_ref(entry.get("teleported_from")),
        "model": _normalize_model(entry.get("model")),
        "cwd": _normalize_path_str(entry.get("cwd")),
        "lib_id": _normalize_session_ref(entry.get("lib_id")),
        "extra_prompt_path": _normalize_path_str(entry.get("extra_prompt_path")),
    }


def all_meta() -> dict[str, dict]:
    """Return a copy of the entire sidecar dict."""
    data = _ensure_loaded()
    return {k: dict(v) for k, v in data.items()}


def resolve_title(meta: dict | None, summary: dict | None) -> str:
    """Single source of truth for a session's DISPLAY title (issue #85).

    Precedence — highest first:
      1. Manual rename — ``meta.title`` when ``meta.title_manual`` is set. User
         intent always wins (and legacy stored titles load as manual, so a
         pre-existing hand-set title is preserved).
      2. Native Claude Code title — ``summary.ai_title``: the ``{"type":
         "ai-title"}`` record Claude writes into the session JSONL (the same
         string shown in ``/resume``). Free, no API call, and present in every
         session that ever ran interactively → auto-backfills old sessions.
      3. Any other stored ``meta.title`` (e.g. a legacy/programmatic Haiku
         auto-title written with ``title_manual=False``).
      4. First user-message preview from the JSONL.
      5. ``""``.

    Pure function over two plain dicts (either may be ``None``) so every title
    producer — ``_decorate_session``, ``tmux_pool_snapshot``, compact naming,
    push notifications — shares ONE precedence and can't drift.
    """
    meta = meta or {}
    summary = summary or {}
    stored = meta.get("title")
    stored = stored.strip() if isinstance(stored, str) and stored.strip() else None
    if stored and meta.get("title_manual"):
        return stored
    ai = summary.get("ai_title")
    if isinstance(ai, str) and ai.strip():
        return ai.strip()
    if stored:
        return stored
    fup = summary.get("first_user_preview")
    if isinstance(fup, str) and fup:
        return fup
    return ""


async def set_meta(
    session_id: str,
    *,
    archived: bool | None = None,
    title: str | None = None,
    title_manual: bool | None = None,
    pinned: bool | None = None,
    persistent: bool | None = None,
    pinned_turn_idxs: list[int] | None = None,
    compacted_from: str | None = None,
    compacted_to: str | None = None,
    teleported_from: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
    lib_id: str | None = None,
    extra_prompt_path: str | None = None,
    last_read_msg_count: int | None = None,
) -> None:
    """Mutate sidecar entry; `None` leaves a field unchanged.

    `pinned_turn_idxs` is full-array replace: non-None means "the new
    canonical list" (after dedup + sort); pass `[]` to clear.

    `compacted_from` / `compacted_to`: pass a non-empty string to set,
    pass `""` to clear (normalizes to None on disk). `None` leaves the
    existing value untouched.

    `model`: pass an alias (`"opus"` / `"sonnet"` / `"haiku"`) to set, pass
    `""` to clear (CLI default). `None` leaves the existing value untouched.

    `cwd` / `lib_id` / `extra_prompt_path`: pass non-empty string to set,
    `""` to clear, `None` to leave unchanged. cwd/extra_prompt_path are
    normalized to None when blank; the caller (orchestrator session create)
    is expected to have already validated path safety.
    """
    import time as _time
    async with _lock:
        data = _ensure_loaded()
        is_new = session_id not in data
        current = dict(data.get(session_id) or _DEFAULT)
        # Ensure the field exists for legacy entries that pre-date it.
        current.setdefault("pinned_turn_idxs", [])
        current.setdefault("persistent", False)
        current.setdefault("compacted_from", None)
        current.setdefault("compacted_to", None)
        current.setdefault("teleported_from", None)
        current.setdefault("model", None)
        current.setdefault("title_manual", False)
        current.setdefault("cwd", None)
        current.setdefault("lib_id", None)
        current.setdefault("extra_prompt_path", None)
        current.setdefault("created_at", 0.0)
        current.setdefault("last_read_msg_count", None)
        # Stamp created_at on first write so orphan-stub `updated_at` is
        # stable across list polls. Existing entries with created_at=0.0
        # (legacy + brand-new merge) get stamped on next set_meta call,
        # which is fine — stable from that point on.
        if is_new or not current.get("created_at"):
            current["created_at"] = _time.time()
        if archived is not None:
            current["archived"] = bool(archived)
        if title is not None:
            current["title"] = title
        if title_manual is not None:
            current["title_manual"] = bool(title_manual)
        if pinned is not None:
            current["pinned"] = bool(pinned)
        if persistent is not None:
            current["persistent"] = bool(persistent)
        if pinned_turn_idxs is not None:
            current["pinned_turn_idxs"] = _normalize_turn_idxs(pinned_turn_idxs)
        if compacted_from is not None:
            current["compacted_from"] = _normalize_session_ref(compacted_from)
        if compacted_to is not None:
            current["compacted_to"] = _normalize_session_ref(compacted_to)
        if teleported_from is not None:
            current["teleported_from"] = _normalize_session_ref(teleported_from)
        if model is not None:
            current["model"] = _normalize_model(model)
        if cwd is not None:
            current["cwd"] = _normalize_path_str(cwd)
        if lib_id is not None:
            current["lib_id"] = _normalize_session_ref(lib_id)
        if extra_prompt_path is not None:
            current["extra_prompt_path"] = _normalize_path_str(extra_prompt_path)
        if last_read_msg_count is not None:
            # Clamp to non-negative int. Pass -1 (or any negative) to clear.
            try:
                v = int(last_read_msg_count)
            except (TypeError, ValueError):
                v = 0
            current["last_read_msg_count"] = max(0, v) if v >= 0 else None
        new_data = {**data, session_id: current}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data


async def remove_meta(session_id: str) -> None:
    """Remove the entry for this session_id."""
    async with _lock:
        data = _ensure_loaded()
        if session_id not in data:
            return
        new_data = {k: v for k, v in data.items() if k != session_id}
        await asyncio.to_thread(_atomic_write, new_data)
        globals()["_data"] = new_data
