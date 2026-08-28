"""HTTP route(s) for the notify primitive.

Provides ``POST /api/notify`` mirroring :func:`notify.notify`. Mounted by
:func:`library.register_routes` (Agent C wires the import).

Hand-rolled validators keep the conventions consistent with
:mod:`cron_routes` — no Pydantic for the body schema.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from fastapi import Body, FastAPI, HTTPException

from .public_url import public_link

_logger = logging.getLogger(__name__)

try:
    from . import notify as _notify_mod  # type: ignore[attr-defined]
    _MODULES_OK = True
except Exception as e:  # pragma: no cover — defensive
    _logger.warning(
        "notify_routes: notify module unavailable, routes disabled: %s", e,
    )
    _notify_mod = None  # type: ignore[assignment]
    _MODULES_OK = False

# Per-topic notification mutes (NOTIFY_DISABLE_TOPICS in ~/.env). These are
# topic NAMES, not a credential — so they get their own no-captcha read/write
# endpoints instead of the captcha-gated secrets API (which forced a captcha on
# every Settings mount + every toggle for zero human-gate value). The secrets
# manager is the persistence layer; we also update os.environ so notify.py's
# os.environ-backed mute check takes effect live (no restart) — strictly better
# than the old secrets-PUT path which only touched the file.
_MUTES_ENV_KEY = "NOTIFY_DISABLE_TOPICS"
# MUST mirror notify._TOPIC_RE so every topic notify.notify() can SEND is also
# mutable here — a stricter regex would silently leave some topics un-mutable
# (and a stray out-of-spec ~/.env entry would 400 the whole full-list PUT).
_TOPIC_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_MUTE_TOPICS = 64

try:
    from . import notify_topics as _topics  # type: ignore[attr-defined]
    _TOPICS_OK = True
except Exception as e:  # pragma: no cover — defensive
    _logger.warning("notify_routes: notify_topics unavailable, routing routes disabled: %s", e)
    _topics = None  # type: ignore[assignment]
    _TOPICS_OK = False

try:
    from . import secrets_manager as _sm  # type: ignore[attr-defined]
    from . import secrets_paths as _secrets_paths  # type: ignore[attr-defined]
    _MUTES_OK = True
except Exception as e:  # pragma: no cover — defensive
    _logger.warning("notify_routes: secrets modules unavailable, mute routes disabled: %s", e)
    _sm = None  # type: ignore[assignment]
    _secrets_paths = None  # type: ignore[assignment]
    _MUTES_OK = False


def _read_disabled_topics() -> list[str]:
    """Muted topics AS notify ACTUALLY HONORS THEM, so the Settings toggles never
    disagree with runtime behavior. ``notify._disabled_topics()`` reads
    ``os.environ`` (not the file), so we mirror it exactly — the ~/.env file is
    only the persistence layer, kept in sync on write. Reading the file instead
    would lie whenever ~/.env and the process env diverge (no EnvironmentFile /
    load_dotenv at boot, or a value set via systemd ``Environment=``)."""
    if _notify_mod is not None and hasattr(_notify_mod, "_disabled_topics"):
        try:
            return sorted(_notify_mod._disabled_topics())
        except Exception:  # noqa: BLE001 — fall back to a direct env read
            pass
    raw = os.environ.get(_MUTES_ENV_KEY, "")
    return sorted({t.strip() for t in raw.split(",") if t.strip()})


def _write_disabled_topics(topics: list[str]) -> list[str]:
    """Persist the muted-topics set to ~/.env AND apply it live to os.environ.
    Returns the normalized (deduped, sorted) list actually written."""
    cleaned = sorted({t for t in topics if isinstance(t, str) and _TOPIC_KEY_RE.match(t)})
    value = ",".join(cleaned)
    existing, lines = _sm.parse_env(_secrets_paths.GLOBAL_ENV)
    _sm.write_env(_secrets_paths.GLOBAL_ENV, {**existing, _MUTES_ENV_KEY: value}, lines)
    os.environ[_MUTES_ENV_KEY] = value  # live effect — notify._disabled_topics reads os.environ
    return cleaned


_MAX_MESSAGE_LEN = 4096
_MAX_TITLE_LEN = 256
_MAX_URL_LEN = 2048
_MAX_TAGS = 16
_MAX_TAG_LEN = 64
_MAX_ACTIONS = 3
_MAX_CODE_LEN = 4096   # notify.py truncates at 3500; this is just an upper bound


def _http_for(exc: Exception) -> HTTPException:
    """Map domain errors to HTTP. Mirrors :func:`cron_routes._http_for`."""
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return HTTPException(404, detail=str(exc))
    return HTTPException(500, detail=str(exc))


def _require_str(payload: dict, key: str, *, max_len: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise HTTPException(400, detail=f"{key} must be a string")
    stripped = value.strip()
    if not stripped:
        raise HTTPException(400, detail=f"{key} must be a non-empty string")
    if len(stripped) > max_len:
        raise HTTPException(
            400, detail=f"{key} exceeds max length ({max_len} chars)",
        )
    return stripped


def _optional_str(payload: dict, key: str, *, max_len: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, detail=f"{key} must be a string")
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_len:
        raise HTTPException(
            400, detail=f"{key} exceeds max length ({max_len} chars)",
        )
    return stripped


def _validate_priority(payload: dict) -> int:
    raw = payload.get("priority", 3)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(400, detail="priority must be an int 1..5")
    if raw not in (1, 2, 3, 4, 5):
        raise HTTPException(400, detail="priority must be one of 1..5")
    return raw


def _validate_tags(payload: dict) -> list[str] | None:
    tags = payload.get("tags")
    if tags is None:
        return None
    if not isinstance(tags, list) or len(tags) > _MAX_TAGS:
        raise HTTPException(
            400, detail=f"tags must be a list of up to {_MAX_TAGS} strings",
        )
    cleaned: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise HTTPException(400, detail="tags entries must be strings")
        stripped = tag.strip()
        if not stripped:
            continue
        if len(stripped) > _MAX_TAG_LEN:
            raise HTTPException(
                400, detail=f"tag exceeds max length ({_MAX_TAG_LEN} chars)",
            )
        cleaned.append(stripped)
    return cleaned or None


def _validate_actions(payload: dict) -> list[dict] | None:
    actions = payload.get("actions")
    if actions is None:
        return None
    if not isinstance(actions, list) or len(actions) > _MAX_ACTIONS:
        raise HTTPException(
            400, detail=f"actions must be a list of up to {_MAX_ACTIONS} entries",
        )
    out: list[dict] = []
    for entry in actions:
        if not isinstance(entry, dict):
            raise HTTPException(400, detail="actions entries must be objects")
        action_type = entry.get("action")
        if action_type not in ("view", "broadcast", "http"):
            raise HTTPException(
                400, detail="actions[].action must be 'view' | 'broadcast' | 'http'",
            )
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(400, detail="actions[].label must be a non-empty string")
        out.append(entry)
    return out


def _normalize_notify_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="body must be a JSON object")
    return {
        "topic": _require_str(payload, "topic", max_len=64),
        "message": _require_str(payload, "message", max_len=_MAX_MESSAGE_LEN),
        "title": _optional_str(payload, "title", max_len=_MAX_TITLE_LEN),
        "priority": _validate_priority(payload),
        "tags": _validate_tags(payload),
        "click": _optional_str(payload, "click", max_len=_MAX_URL_LEN),
        "actions": _validate_actions(payload),
        "attach": _optional_str(payload, "attach", max_len=_MAX_URL_LEN),
        "code": _optional_str(payload, "code", max_len=_MAX_CODE_LEN),
    }


def register(app: FastAPI) -> None:
    """Mount the notify routes on ``app``.

    No-op if the underlying notify module failed to import.
    """
    if not _MODULES_OK:
        _logger.warning(
            "notify_routes.register: skipped — notify module unavailable",
        )
        return

    @app.post("/api/notify")
    async def api_notify(payload: dict = Body(default={})) -> dict:
        kwargs = _normalize_notify_payload(payload)
        try:
            result = await _notify_mod.notify(**kwargs)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            _logger.exception("notify_routes: notify() failed")
            raise _http_for(exc) from exc

        if not result.get("ok"):
            return {"ok": False, **{k: v for k, v in result.items() if k != "ok"}}
        return {
            "ok": True,
            "topic": result.get("topic"),
            "message_id": result.get("message_id"),
        }

    # ── Telegram-specific setup helpers ──────────────────────────────
    # Used by the Settings → Notifications card to verify the bot/chat
    # credentials and to auto-detect chat_id after the user sends /start
    # to the bot.

    @app.get("/api/notify/probe")
    async def api_notify_probe() -> dict:
        if not hasattr(_notify_mod, "probe"):
            return {"ok": False, "error": "probe unavailable in this build"}
        try:
            return await _notify_mod.probe()
        except Exception as exc:  # noqa: BLE001 — never surface a 500 here
            _logger.exception("notify_routes: probe failed")
            return {"ok": False, "error": f"probe failed: {exc}"}

    @app.post("/api/notify/detect-chat-id")
    async def api_notify_detect_chat_id() -> dict:
        if not hasattr(_notify_mod, "detect_chat_id"):
            return {"ok": False, "error": "detect_chat_id unavailable"}
        try:
            return await _notify_mod.detect_chat_id()
        except Exception as exc:  # noqa: BLE001
            _logger.exception("notify_routes: detect_chat_id failed")
            return {"ok": False, "error": f"detect failed: {exc}"}

    @app.post("/api/notify/test")
    async def api_notify_test(payload: dict = Body(default={})) -> dict:
        topic = _optional_str(payload, "topic", max_len=64) or "system"
        msg = _optional_str(payload, "message", max_len=_MAX_MESSAGE_LEN) \
            or "Test push from orbit."
        try:
            result = await _notify_mod.notify(
                topic=topic,
                title="Test push",
                message=msg,
                priority=3,
                tags=["test"],
                click=public_link("/settings"),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("notify_routes: test push failed")
            return {"ok": False, "error": f"send failed: {exc}"}
        return result

    # ── per-topic routing: one supergroup, one forum thread per topic ────
    # Not credentials either (a group id is not a secret and is useless without
    # the bot token), so these skip the captcha for the same reason mutes do.

    @app.get("/api/notify/routes")
    async def api_notify_routes_get() -> dict:
        if not _TOPICS_OK:
            return {"ok": False, "error": "routing unavailable in this build",
                    "group_chat_id": "", "topics": {}, "known_topics": []}
        try:
            cfg = _topics.get_config()
        except Exception as exc:  # noqa: BLE001 — never 500 the Settings mount
            _logger.exception("notify_routes: read routing failed")
            return {"ok": False, "error": f"read failed: {exc}",
                    "group_chat_id": "", "topics": {}, "known_topics": []}
        return {
            "ok": True,
            "group_chat_id": cfg.get("group_chat_id", ""),
            "topics": cfg.get("topics", {}),
            "known_topics": list(_topics.KNOWN_TOPICS),
        }

    @app.put("/api/notify/routes")
    async def api_notify_routes_put(payload: dict = Body(default={})) -> dict:
        if not _TOPICS_OK:
            raise HTTPException(503, detail="routing unavailable in this build")
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="body must be a JSON object")
        try:
            saved = _topics.save_config(payload)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            _logger.exception("notify_routes: write routing failed")
            raise HTTPException(500, detail=f"write failed: {exc}") from exc
        return {"ok": True, **saved, "known_topics": list(_topics.KNOWN_TOPICS)}

    @app.post("/api/notify/routes/discover")
    async def api_notify_routes_discover() -> dict:
        if not hasattr(_notify_mod, "discover_forum_topics"):
            return {"ok": False, "error": "discover unavailable in this build", "groups": []}
        try:
            return await _notify_mod.discover_forum_topics()
        except Exception as exc:  # noqa: BLE001
            _logger.exception("notify_routes: discover failed")
            return {"ok": False, "error": f"discover failed: {exc}", "groups": []}

    @app.post("/api/notify/routes/create-topics")
    async def api_notify_routes_create(payload: dict = Body(default={})) -> dict:
        """Create a forum thread per requested topic, then persist the mapping.

        Partial success is the norm (the bot may lack *Manage Topics*, or one
        name may collide), so every topic reports its own outcome and whatever
        DID get created is saved — re-running only fills the gaps.
        """
        if not _TOPICS_OK or not hasattr(_notify_mod, "create_forum_topic"):
            raise HTTPException(503, detail="routing unavailable in this build")
        group = _require_str(payload, "group_chat_id", max_len=64)
        requested = payload.get("topics")
        if requested is None:
            requested = list(_topics.KNOWN_TOPICS)
        if not isinstance(requested, list) or not requested:
            raise HTTPException(400, detail="topics must be a non-empty list")
        if len(requested) > _MAX_MUTE_TOPICS:
            raise HTTPException(400, detail=f"too many topics (max {_MAX_MUTE_TOPICS})")
        for t in requested:
            if not isinstance(t, str) or not _TOPIC_KEY_RE.match(t):
                raise HTTPException(400, detail=f"invalid topic name: {t!r}")

        cfg = _topics.get_config()
        mapping = dict(cfg.get("topics") or {})
        results: list[dict] = []
        for index, topic in enumerate(requested):
            existing = (mapping.get(topic) or {}).get("thread_id")
            if existing:
                results.append({"topic": topic, "ok": True, "thread_id": existing,
                                "skipped": "already mapped"})
                continue
            # Plain name: Telegram renders the icon beside it, so an emoji in
            # the name shows up twice in the tab bar.
            title = topic
            icon_emoji = _notify_mod._FORUM_ICON_EMOJI.get(topic)
            icon_id = await _notify_mod.forum_icon_id(icon_emoji) if icon_emoji else None
            try:
                created = await _notify_mod.create_forum_topic(
                    chat_id=group, name=title,
                    icon_custom_emoji_id=icon_id,
                    icon_color=_notify_mod.forum_icon_color(index),
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("notify_routes: createForumTopic failed (%s)", topic)
                results.append({"topic": topic, "ok": False, "error": str(exc)})
                continue
            if created.get("ok"):
                mapping[topic] = {"thread_id": created["thread_id"], "title": title}
                results.append({"topic": topic, "ok": True, "thread_id": created["thread_id"]})
            else:
                results.append({"topic": topic, "ok": False, "error": created.get("error")})

        try:
            saved = _topics.save_config({"group_chat_id": group, "topics": mapping})
        except Exception as exc:  # noqa: BLE001
            _logger.exception("notify_routes: saving created topics failed")
            raise HTTPException(500, detail=f"write failed: {exc}") from exc
        return {"ok": any(r["ok"] for r in results), "results": results, **saved}

    # ── per-topic mutes (no captcha — topic names, not a credential) ──────
    # NOTIFY_DISABLE_TOPICS read/write without the captcha friction of the
    # secrets API. Read reflects the persisted ~/.env; write persists + applies
    # live to os.environ so the mute takes effect immediately.

    @app.get("/api/notify/mutes")
    async def api_notify_mutes_get() -> dict:
        if not _MUTES_OK:
            return {"ok": False, "error": "mutes unavailable in this build", "disabled": []}
        try:
            # Tiny ~/.env read; kept ON the loop so it stays serialized with
            # the (also on-loop) secrets-routes env writers — off-loading just
            # this one would race their read-modify-write of the same ~/.env.
            return {"ok": True, "disabled": _read_disabled_topics()}
        except Exception as exc:  # noqa: BLE001 — never 500 the Settings mount
            _logger.exception("notify_routes: read mutes failed")
            return {"ok": False, "error": f"read failed: {exc}", "disabled": []}

    @app.put("/api/notify/mutes")
    async def api_notify_mutes_put(payload: dict = Body(default={})) -> dict:
        if not _MUTES_OK:
            raise HTTPException(503, detail="mutes unavailable in this build")
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="body must be a JSON object")
        disabled = payload.get("disabled")
        if not isinstance(disabled, list):
            raise HTTPException(400, detail="disabled must be a list of topic strings")
        if len(disabled) > _MAX_MUTE_TOPICS:
            raise HTTPException(400, detail=f"too many topics (max {_MAX_MUTE_TOPICS})")
        for t in disabled:
            if not isinstance(t, str) or not _TOPIC_KEY_RE.match(t):
                raise HTTPException(400, detail=f"invalid topic name: {t!r}")
        try:
            # Tiny ~/.env read-modify-write; kept ON the loop so it stays
            # serialized with the (also on-loop) secrets-routes env writers —
            # off-loading just this one would race their write of the same file
            # and silently drop one side's change (lost update).
            written = _write_disabled_topics(disabled)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("notify_routes: write mutes failed")
            raise HTTPException(500, detail=f"write failed: {exc}") from exc
        return {"ok": True, "disabled": written}
