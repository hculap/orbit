"""Persistent custom order for the orchestrator agent-tab strip.

A deliberately tiny sibling of ``orchestrator_terminal_shortcuts.py``: the agent
tabs (Global / Finance / My Project / …) default to Global-first-then-alpha,
but the user can drag them into any order. That order is a personal layout that
should follow the user across devices, so it lives server-side in
``~/.orchestrator/agent_tab_order.json`` (schema ``{"version":1,"order":[key,…]}``)
rather than per-device localStorage.

The store is dumb on purpose: it persists an ordered list of agent KEYS
(``lib_id`` like ``areas/Work`` / ``projects/my-project``, or ``@Global`` for
the cwd-less agent). It does NOT know which agents currently have tabs — the
frontend merges this saved order with the live pool (keys not in the list append
at the end, saved keys for absent agents are skipped). Disappeared agents are
kept in the list so spinning them back up restores their slot.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

ORDER_PATH = Path.home() / ".orchestrator" / "agent_tab_order.json"

_SCHEMA_VERSION = 1
_MAX_FILE_BYTES = 64 * 1024          # gate BEFORE json.load
_MAX_KEYS = 256                       # silent tail-trim beyond this
# An agent key is an OPAQUE ordering token (lib_id like ``areas/Work``, or
# ``@Human Name`` for a cwd-rooted agent with no lib_id — which CAN contain
# spaces/unicode). It is never used as a filesystem path, so we only forbid
# control chars and bound the length; everything else (spaces, '/', '@', unicode)
# is allowed so a multi-word agent's reorder actually persists.
_KEY_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")

_lock = threading.Lock()


def _warn(msg: str) -> None:
    print(f"[agent_tab_order] {msg}")


def _sanitize(order: Any) -> list[str]:
    """Coerce arbitrary input into a clean, deduped, capped list of agent keys."""
    if not isinstance(order, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in order:
        if not isinstance(item, str):
            continue
        key = item.strip()
        if not key or key in seen or not _KEY_RE.match(key):
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= _MAX_KEYS:
            break
    return out


def _load_from_disk() -> list[str]:
    if not ORDER_PATH.exists():
        return []
    try:
        size = ORDER_PATH.stat().st_size
    except OSError as exc:
        _warn(f"cannot stat {ORDER_PATH.name}: {exc}")
        return []
    if size > _MAX_FILE_BYTES:
        _warn(f"{ORDER_PATH.name} is {size}B > {_MAX_FILE_BYTES}B cap; ignoring")
        return []
    try:
        with ORDER_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        _warn(f"corrupt or unreadable {ORDER_PATH.name}: {exc}; ignoring")
        return []
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        return []
    return _sanitize(payload.get("order"))


def _atomic_write(order: list[str]) -> None:
    ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _SCHEMA_VERSION, "order": order}
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(ORDER_PATH.parent),
        prefix=".agent_tab_order.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, ORDER_PATH)
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


def get_order() -> list[str]:
    """Saved tab-key order (sanitized). Empty list when nothing is stored yet."""
    with _lock:
        return _load_from_disk()


def set_order(order: Any) -> list[str]:
    """Replace the whole saved order (full PUT, not a delta). Returns the
    sanitized list that was persisted."""
    clean = _sanitize(order)
    with _lock:
        _atomic_write(clean)
    return clean
