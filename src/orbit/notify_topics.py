"""Per-topic Telegram routing: one supergroup, one forum THREAD per topic.

Before this module every notification — cron runs, agent turns, chat replies,
task reminders — landed in the single ``TELEGRAM_CHAT_ID``; the ``topic`` only
picked an emoji and a hashtag (:mod:`notify`). Here the topic also picks a
DESTINATION: a ``message_thread_id`` inside a forum-enabled supergroup, so the
phone shows one chat that opens into per-topic threads with their own unread
counts and their own mute switch.

Schema (version 1) at ``~/.orchestrator/notify_routes.json``::

    {
      "version": 1,
      "group_chat_id": "-1001234567890",     # the forum supergroup ("" = off)
      "topics": {
        "cron":  {"thread_id": 5, "title": "⏰ cron"},
        "tasks": {"thread_id": 9, "title": "✅ tasks"},
        "alerts": {"chat_id": "-1009999999999"}   # escape hatch, see below
      }
    }

``resolve(topic)`` is the ONLY read path and the whole contract lives in it:

* no ``group_chat_id`` ⇒ ``(TELEGRAM_CHAT_ID, None)`` — byte-identical to the
  pre-feature payload. The file is opt-in and arrives empty, so an install that
  never visits Settings notices nothing. Blanking the group in Settings is
  therefore also the level-1 rollback.
* group set, topic mapped ⇒ ``(group, thread_id)``.
* group set, topic UNMAPPED ⇒ ``(group, None)`` — the group's General thread.
  Delivering an unrouted alert to the wrong thread is recoverable; dropping it
  is not, and a new topic string (any caller may invent one) must never be able
  to silence itself by not being in this file.
* a per-topic ``chat_id`` outranks the group — the hybrid case (one topic in its
  own chat, the rest as threads). No UI writes it; hand-edit only.
* ``thread_id`` without a ``group_chat_id`` is DISCARDED rather than applied to
  the legacy 1:1 chat, where every send would 400 "message thread not found".

Everything is re-sanitized on read as well as on write: the file is small,
hand-editable, and feeds an outbound API call, so a bad ``chat_id`` must fail
here rather than as an opaque Telegram 400 on the cron hot path.

ROLLBACK
  1. blank ``group_chat_id`` (Settings → Powiadomienia) ⇒ legacy single-chat
     behaviour, file kept.
  2. drop the routing block from ``settings-notifications.jsx`` + the
     ``/api/notify/routes*`` endpoints ⇒ backend keeps routing headlessly.
  3. delete this module, its tests, the ``_route_for`` call in :mod:`notify`
     and ``~/.orchestrator/notify_routes.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

_logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_MAX_FILE_BYTES = 32 * 1024  # gate BEFORE json.load
_MAX_TOPICS = 64
_MAX_TITLE_LEN = 64

ROUTES_PATH = Path(os.environ.get("HOME", "/tmp")) / ".orchestrator" / "notify_routes.json"

# MUST mirror notify._TOPIC_RE — a topic notify() can send is a topic this file
# must be able to route; a stricter regex here would silently strand it in
# General with no way to map it.
_TOPIC_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Telegram accepts a numeric id (supergroups are negative, ~-100…) or an
# @username for public groups/channels. Anything else is a typo or an injection
# attempt and is dropped rather than pasted into an outbound URL/body.
_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
_AT_NAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")

#: The topics the dashboard itself publishes — the seed list Settings offers.
#: Callers may invent others (``notify`` accepts any matching string); those
#: resolve to the group's General thread until someone maps them.
KNOWN_TOPICS: tuple[str, ...] = ("cron", "cron-out", "agent", "chat", "tasks", "system")

_data: dict[str, Any] | None = None
_loaded_mtime: float | None = None


class Route(NamedTuple):
    """Where one topic's messages go. ``thread_id`` None = the chat itself."""

    chat_id: str | None
    thread_id: int | None


def _warn(msg: str) -> None:
    _logger.warning("notify_topics: %s", msg)


def _empty() -> dict[str, Any]:
    return {"version": _SCHEMA_VERSION, "group_chat_id": "", "topics": {}}


def _clean_chat_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if _CHAT_ID_RE.match(value) or _AT_NAME_RE.match(value):
        return value
    _warn(f"ignoring malformed chat id {value!r}")
    return ""


def _clean_thread_id(raw: Any) -> int | None:
    # bool is an int subclass — reject it explicitly or True becomes thread 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        if raw not in (None, ""):
            _warn(f"ignoring non-integer thread id {raw!r}")
        return None
    if raw < 1 or raw > 2**31 - 1:
        _warn(f"ignoring out-of-range thread id {raw!r}")
        return None
    return raw


def _sanitize(payload: Any) -> dict[str, Any]:
    """Rebuild the config key by key — never trust or merge the input dict."""
    if not isinstance(payload, dict):
        return _empty()
    out = _empty()
    out["group_chat_id"] = _clean_chat_id(payload.get("group_chat_id"))
    raw_topics = payload.get("topics")
    if not isinstance(raw_topics, dict):
        return out
    topics: dict[str, dict[str, Any]] = {}
    for key, value in list(raw_topics.items())[:_MAX_TOPICS]:
        if not isinstance(key, str) or not _TOPIC_KEY_RE.match(key):
            _warn(f"dropping out-of-spec topic key {key!r}")
            continue
        if not isinstance(value, dict):
            continue
        entry: dict[str, Any] = {"thread_id": _clean_thread_id(value.get("thread_id"))}
        chat_id = _clean_chat_id(value.get("chat_id"))
        if chat_id:
            entry["chat_id"] = chat_id
        title = value.get("title")
        if isinstance(title, str) and title.strip():
            entry["title"] = title.strip()[:_MAX_TITLE_LEN]
        topics[key] = entry
    out["topics"] = topics
    return out


def _load_from_disk() -> dict[str, Any]:
    if not ROUTES_PATH.exists():
        return _empty()
    try:
        size = ROUTES_PATH.stat().st_size
    except OSError as exc:
        _warn(f"cannot stat {ROUTES_PATH.name}: {exc}; routing off")
        return _empty()
    if size > _MAX_FILE_BYTES:
        _warn(f"{ROUTES_PATH.name} is {size}B > {_MAX_FILE_BYTES}B cap; routing off")
        return _empty()
    try:
        with ROUTES_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt or unreadable {ROUTES_PATH.name}: {exc}; routing off")
        return _empty()
    if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
        _warn(f"{ROUTES_PATH.name} not schema v{_SCHEMA_VERSION}; routing off")
        return _empty()
    return _sanitize(payload)


def invalidate() -> None:
    """Drop the cache — called after a write and by tests."""
    global _data, _loaded_mtime
    _data = None
    _loaded_mtime = None


def get_config() -> dict[str, Any]:
    """Sanitized config, cached until the file's mtime moves.

    The mtime check keeps a hand-edited file live without a restart, which
    matters because the alternative (reading on every send) puts a stat + parse
    on the cron hot path for a file that changes a few times a year.
    """
    global _data, _loaded_mtime
    try:
        mtime = ROUTES_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _data is None or mtime != _loaded_mtime:
        _data = _load_from_disk()
        _loaded_mtime = mtime
    return _data


def save_config(payload: Any) -> dict[str, Any]:
    """Sanitize + atomically write. Returns what was actually stored."""
    if not isinstance(payload, dict):
        raise ValueError("routing config must be an object")
    clean = _sanitize(payload)
    ROUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(ROUTES_PATH.parent),
        prefix=".notify_routes.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(clean, tmp, indent=2, sort_keys=True, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, ROUTES_PATH)
    except Exception:
        try:
            tmp.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    invalidate()
    return clean


def resolve(topic: str) -> Route:
    """Destination for ``topic``. See the module docstring for the ladder."""
    cfg = get_config()
    group = cfg.get("group_chat_id") or ""
    entry = (cfg.get("topics") or {}).get(topic) or {}
    override = entry.get("chat_id") or ""
    if override:
        # An explicit per-topic chat: its thread_id (if any) belongs to THAT
        # chat, so carry it along rather than silently flattening to General.
        return Route(override, entry.get("thread_id"))
    if not group:
        env_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        return Route(env_chat or None, None)
    return Route(group, entry.get("thread_id"))
