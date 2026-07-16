"""Async Telegram-Bot notification primitive.

Single async helper :func:`notify` posts to the Telegram Bot API
(``https://api.telegram.org/bot<TOKEN>/sendMessage``). Replaced the
self-hosted ntfy backend after iOS-app testing showed iOS requires a
publicly-reachable HTTPS endpoint AND ntfy.sh's APNs forwarding for any
backgrounded delivery — both of which break the dashboard's
Tailscale-only access boundary. Telegram solves that with a single
outbound POST and zero new infra.

Reads ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` from the environment
(typically ``~/.env`` managed via the Credentials surface). Both required;
missing either short-circuits with a WARNING log so a misconfigured bot
never wedges a caller on the cron / chat / scheduler hot path.

Per-topic mute via ``NOTIFY_DISABLE_TOPICS=cron,agent`` (comma-separated
logical topics). The helper accepts any topic string — the four wired
ones are ``cron``, ``agent``, ``chat``, ``system``; future event sources
just pick a name.
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_S = 5.0

_TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_VALID_PRIORITIES = (1, 2, 3, 4, 5)
_HASHTAG_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")

# Visual differentiation per topic + per-priority emoji prefix. Keeps the
# alert glanceable at a notification-shade glance without any per-call
# styling effort from the publisher.
_TOPIC_EMOJI = {
    "cron":   "⏰",
    "agent":  "🤖",
    "chat":   "💬",
    "system": "🛠️",
}
_PRIORITY_PREFIX = {
    5: "🚨 ",
    4: "⚠️ ",
}


def _disabled_topics() -> set[str]:
    raw = os.environ.get("NOTIFY_DISABLE_TOPICS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def _validate_topic(topic: Any) -> str:
    if not isinstance(topic, str) or not _TOPIC_RE.match(topic):
        raise ValueError(
            f"topic must match {_TOPIC_RE.pattern}; got {topic!r}",
        )
    return topic


def _validate_priority(priority: Any) -> int:
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError(f"priority must be int 1..5; got {priority!r}")
    if priority not in _VALID_PRIORITIES:
        raise ValueError(f"priority must be 1..5; got {priority}")
    return priority


def _read_credentials() -> tuple[str | None, str | None]:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return (token or None, chat_id or None)


def _format_message(
    *,
    topic: str,
    message: str,
    title: str | None,
    priority: int,
    tags: list[str] | None,
    click: str | None,
) -> str:
    """Build the Telegram HTML-formatted message body.

    Telegram's HTML mode tolerates exactly four tags (``<b>``, ``<i>``,
    ``<u>``, ``<a>``) and the bare amp/lt/gt entities. Everything else
    needs escaping — bot replies otherwise return ``Bad Request: can't
    parse entities``.
    """
    emoji = _TOPIC_EMOJI.get(topic, "🔔")
    prefix = _PRIORITY_PREFIX.get(priority, "")
    header_text = title if (title and title.strip()) else topic
    lines: list[str] = [
        f"{emoji} {prefix}<b>{html.escape(header_text)}</b>",
        html.escape(message),
    ]
    # `click` is rendered as an inline_keyboard button by the caller — not
    # repeated as a text link inside the body so the message stays clean.
    hashtags = [f"#{topic}"]
    if tags:
        for raw in tags:
            if not isinstance(raw, str):
                continue
            cleaned = _HASHTAG_SAFE_RE.sub("", raw)
            if cleaned:
                hashtags.append(f"#{cleaned}")
    lines.append(" ".join(hashtags))
    return "\n".join(lines)


def _topic_button_label(topic: str) -> str:
    """Per-topic button caption — short enough to render inside Telegram's
    narrow inline-keyboard cell while still being descriptive."""
    return {
        "cron":   "Open scheduler",
        "agent":  "Open chat",
        "chat":   "Open chat",
        "system": "Open dashboard",
        "tasks":  "Open task",
    }.get(topic, "Open")


def _build_inline_keyboard(
    *,
    topic: str,
    click: str | None,
    actions: list[dict] | None,
) -> dict | None:
    """Build a Telegram inline_keyboard payload from `click` + `actions`.

    Tap-on-notification on iOS / Android opens the URL in the system
    browser (or the app the URL is registered with — works for our
    Tailscale dashboard since the user typically has the dashboard PWA
    installed or just bookmarked).

    Each entry in `actions` may carry `{action: "view", label, url}` —
    these become extra buttons on the same row. Non-URL actions
    (callback, broadcast, http) are dropped silently for now; bidirectional
    bot support is a separate workstream.
    """
    buttons: list[dict] = []
    if isinstance(click, str) and click.strip():
        buttons.append({"text": _topic_button_label(topic), "url": click.strip()})
    if isinstance(actions, list):
        for entry in actions:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            label = entry.get("label")
            if not isinstance(url, str) or not url.strip():
                continue
            if not isinstance(label, str) or not label.strip():
                continue
            buttons.append({"text": label.strip()[:32], "url": url.strip()})
    if not buttons:
        return None
    # All on one row for now — Telegram wraps automatically when the row
    # is too wide. Future: split into multi-row when len > 3.
    return {"inline_keyboard": [buttons[:5]]}


# ── attachment dispatch ──────────────────────────────────────────────
# `attach` may be a filesystem path or an https:// URL. Telegram's docs
# split media into four endpoints with their own size caps; we pick the
# right one from the file extension. Unknown extensions fall back to
# sendDocument so the attachment still arrives just without preview.

_PHOTO_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_AUDIO_EXTS = frozenset({".mp3", ".m4a", ".ogg", ".opus", ".oga", ".wav", ".flac"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".mkv"})
# Telegram size caps (subject to change; conservative values).
_MAX_PHOTO_BYTES = 10 * 1024 * 1024     # 10 MB
_MAX_OTHER_BYTES = 50 * 1024 * 1024     # 50 MB

_HOME = Path(os.environ.get("HOME", str(Path.home()))).resolve()


def _attach_method(target: str) -> tuple[str, str, str]:
    """Pick ``(telegram_method, form_field, kind)`` for an attachment.

    Strips URL query/fragment before extension lookup so
    ``https://x/y/foo.png?token=...`` still routes to sendPhoto.
    """
    cleaned = target.lower().split("?")[0].split("#")[0]
    last = cleaned.rsplit("/", 1)[-1]
    ext = ""
    if "." in last:
        ext = "." + last.rsplit(".", 1)[-1]
    if ext in _PHOTO_EXTS:
        return "sendPhoto", "photo", "photo"
    if ext in _AUDIO_EXTS:
        return "sendAudio", "audio", "audio"
    if ext in _VIDEO_EXTS:
        return "sendVideo", "video", "video"
    return "sendDocument", "document", "document"


def _resolve_attach_path(target: str) -> Path:
    """Resolve a local-file `attach` arg.

    Restricts paths to ``$HOME`` or ``/tmp`` so a misbehaving caller
    (e.g. agent skill) can't smuggle ``/etc/passwd`` to Telegram. Raises
    ``FileNotFoundError`` / ``ValueError`` on any rejection.
    """
    p = Path(target).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"attach: not a file: {p}")
    home = _HOME
    tmp = Path("/tmp")
    if not (p.is_relative_to(home) or p.is_relative_to(tmp)):
        raise ValueError(f"attach: path must be under {home} or /tmp; got {p}")
    return p


async def _send_attachment(
    *,
    token: str, chat_id: str, caption: str,
    attach: str,
    reply_markup: dict | None,
    silent: bool,
    timeout: float,
) -> httpx.Response:
    """POST to the right media endpoint. Caller handles the response shape
    (Telegram returns the same envelope as sendMessage)."""
    method, field, kind = _attach_method(attach)
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        # Multipart form expects reply_markup as a JSON-encoded string,
        # not a nested object (different from sendMessage's JSON body).
        data["reply_markup"] = json.dumps(reply_markup)
    if silent:
        data["disable_notification"] = "true"

    async with httpx.AsyncClient(timeout=timeout) as client:
        if attach.startswith(("http://", "https://")):
            # Telegram fetches the URL itself — pass as a plain form field.
            data[field] = attach
            return await client.post(url, data=data)
        # Local file path — multipart upload.
        path = _resolve_attach_path(attach)
        size = path.stat().st_size
        cap = _MAX_PHOTO_BYTES if kind == "photo" else _MAX_OTHER_BYTES
        if size > cap:
            raise ValueError(
                f"attach: {path.name} is {size} bytes, exceeds Telegram's {cap}-byte cap for {kind}",
            )
        mime, _ = mimetypes.guess_type(str(path))
        files = {field: (path.name, path.read_bytes(), mime or "application/octet-stream")}
        return await client.post(url, files=files, data=data)


async def notify(
    *,
    topic: str,
    message: str,
    title: str | None = None,
    priority: int = 3,
    tags: list[str] | None = None,
    click: str | None = None,
    actions: list[dict] | None = None,
    attach: str | None = None,
    code: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Send a Telegram push to the configured bot/chat. Never raises.

    Returns ``{"ok": True, "topic", "message_id"}`` on success or
    ``{"ok": False, "topic", "reason"}`` on any failure (transport,
    misconfigured credentials, validation, Telegram API error). The
    caller is expected to fire-and-forget; failures are logged at
    WARNING.

    ``click`` becomes the primary inline-keyboard button under the
    message (per-topic label, e.g. cron → "Open scheduler"). ``actions``
    with ``{action:"view", label, url}`` entries become additional buttons
    on the same row (max 5). ``attach`` (file path under ``$HOME``/``/tmp``
    or ``https://`` URL) routes to sendPhoto / sendAudio / sendVideo /
    sendDocument based on extension; ``message`` becomes the caption.

    ``code`` is appended to the body wrapped in ``<pre>`` so the recipient
    sees a monospace block — useful for shell output, log tails, JSON
    dumps where alignment matters. Auto-escaped, so callers pass raw
    text without HTML concerns.
    """
    try:
        topic = _validate_topic(topic)
        priority = _validate_priority(priority)
    except ValueError as exc:
        _logger.warning("notify: invalid args: %s", exc)
        return {"ok": False, "topic": str(topic), "reason": str(exc)}

    if not isinstance(message, str) or not message.strip():
        return {"ok": False, "topic": topic, "reason": "message required"}

    if topic in _disabled_topics():
        return {"ok": False, "topic": topic, "reason": "topic disabled via NOTIFY_DISABLE_TOPICS"}

    token, chat_id = _read_credentials()
    if not token or not chat_id:
        _logger.warning(
            "notify: TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID missing — skipping (topic=%s)",
            topic,
        )
        return {"ok": False, "topic": topic, "reason": "telegram credentials missing"}

    text = _format_message(
        topic=topic, message=message, title=title,
        priority=priority, tags=tags, click=click,
    )
    if code and isinstance(code, str) and code.strip():
        # Cap before <pre> so we don't blow Telegram's 4096-char limit.
        clipped = code if len(code) <= 3500 else code[:3500] + "\n…(truncated)"
        # The hashtags line lives at the end of `text`; insert the code
        # block before it so the tags stay glued to the bottom for
        # consistent visual rhythm.
        lines = text.split("\n")
        hashtag_line = lines.pop() if lines and lines[-1].startswith("#") else None
        text = "\n".join(lines) + f"\n<pre>{html.escape(clipped)}</pre>"
        if hashtag_line:
            text += f"\n{hashtag_line}"
    keyboard = _build_inline_keyboard(topic=topic, click=click, actions=actions)
    silent = priority <= 1

    try:
        if attach:
            response = await _send_attachment(
                token=token, chat_id=chat_id, caption=text,
                attach=attach, reply_markup=keyboard,
                silent=silent, timeout=timeout,
            )
        else:
            body: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if keyboard is not None:
                body["reply_markup"] = keyboard
            if silent:
                body["disable_notification"] = True
            url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=body)
    except (FileNotFoundError, ValueError) as exc:
        _logger.warning("notify: attach rejected (topic=%s): %s", topic, exc)
        return {"ok": False, "topic": topic, "reason": f"attach: {exc}"}
    except (httpx.HTTPError, OSError) as exc:
        _logger.warning("notify: transport error (topic=%s): %s", topic, exc)
        return {"ok": False, "topic": topic, "reason": f"transport: {exc}"}
    except Exception as exc:  # noqa: BLE001 — defence in depth
        _logger.exception("notify: unexpected error (topic=%s)", topic)
        return {"ok": False, "topic": topic, "reason": f"unexpected: {exc}"}

    if response.status_code != 200:
        snippet = (response.text or "")[:200]
        _logger.warning(
            "notify: telegram http %s (topic=%s): %s",
            response.status_code, topic, snippet,
        )
        return {
            "ok": False, "topic": topic,
            "reason": f"telegram http {response.status_code}: {snippet}",
        }
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "topic": topic, "reason": "telegram returned non-json"}
    if not data.get("ok"):
        desc = data.get("description") or "telegram api ok=false"
        _logger.warning("notify: telegram error (topic=%s): %s", topic, desc)
        return {"ok": False, "topic": topic, "reason": desc}
    msg_id = (data.get("result") or {}).get("message_id")
    return {"ok": True, "topic": topic, "message_id": msg_id}


# ── setup helpers (used by /api/notify/{probe,detect-chat-id,test}) ──


async def probe(*, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Verify the bot token + chat_id are valid without sending a message.

    Returns ``{ok, bot_username?, chat_id?, error?}``. Used by Settings
    UI to display "configured" / "missing" before the user clicks Test.
    """
    token, chat_id = _read_credentials()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID missing"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{TELEGRAM_API_BASE}/bot{token}/getMe")
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "error": f"transport: {exc}"}
    if response.status_code != 200:
        return {"ok": False, "error": f"getMe http {response.status_code}"}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "error": "non-json response from telegram"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("description") or "telegram api ok=false"}
    username = (data.get("result") or {}).get("username")
    return {"ok": True, "bot_username": username, "chat_id": chat_id}


async def detect_chat_id(*, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Pull the latest /getUpdates and return the chat_id of the most
    recent private message to the bot.

    The user is expected to send ``/start`` in the Telegram client
    before clicking the Settings "Detect" button. Returns
    ``{ok, chat_id?, from?, error?}``.
    """
    token, _ = _read_credentials()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{TELEGRAM_API_BASE}/bot{token}/getUpdates")
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "error": f"transport: {exc}"}
    if response.status_code != 200:
        return {"ok": False, "error": f"getUpdates http {response.status_code}"}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "error": "non-json response from telegram"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("description") or "telegram api ok=false"}
    updates = data.get("result") or []
    for update in reversed(updates):
        msg = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            sender = msg.get("from") or {}
            who = sender.get("username") or sender.get("first_name") or chat.get("title")
            return {
                "ok": True,
                "chat_id": str(chat["id"]),
                "from": who,
                "chat_type": chat.get("type"),
            }
    return {
        "ok": False,
        "error": "no recent messages — send /start to the bot first, then click Detect",
    }
