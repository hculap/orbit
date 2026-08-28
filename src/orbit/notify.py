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
    # Results of runs that went fine, split off from `cron` so the alert
    # channel stays an alert channel.
    "cron-out": "📤",
    "agent":  "🤖",
    "chat":   "💬",
    # `tasks` shipped later than the original four and was missing here, so
    # every task reminder wore the generic 🔔 — including, once forum routing
    # landed, the name of its own thread.
    "tasks":  "✅",
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


def _route_for(topic: str) -> tuple[str | None, int | None]:
    """Destination chat + forum thread for ``topic``.

    Delegated to :mod:`notify_topics`, which falls back to the plain
    ``TELEGRAM_CHAT_ID`` whenever no routing is configured — so this stays a
    no-op for an install that never opens Settings. An import failure must not
    take notifications down with it: degrade to the legacy single chat.
    """
    try:
        from . import notify_topics  # local import: keeps notify importable alone

        route = notify_topics.resolve(topic)
        return (route.chat_id, route.thread_id)
    except Exception as exc:  # noqa: BLE001 — routing is an enhancement, not a dependency
        _logger.warning("notify: routing unavailable (topic=%s): %s", topic, exc)
        _, chat_id = _read_credentials()
        return (chat_id, None)


def _is_thread_missing(response: httpx.Response) -> bool:
    """True when Telegram rejected the send because the forum thread is gone.

    Deleting a topic in the Telegram client leaves its id in our config, and
    every message to it then 400s. That must not silently swallow a cron
    failure alert, so the caller retries in the group's General thread.
    """
    if response.status_code != 400:
        return False
    try:
        desc = str((response.json() or {}).get("description") or "")
    except ValueError:
        desc = response.text or ""
    lowered = desc.lower()
    return "thread not found" in lowered or "topic_deleted" in lowered


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
    thread_id: int | None = None,
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
    if thread_id is not None:
        # Multipart form fields are strings — an int here is sent as one anyway
        # by httpx, but being explicit keeps the payload obvious in a tcpdump.
        data["message_thread_id"] = str(thread_id)
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

    token, _ = _read_credentials()
    chat_id, thread_id = _route_for(topic)
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

    async def _post(thread: int | None) -> httpx.Response:
        if attach:
            return await _send_attachment(
                token=token, chat_id=chat_id, caption=text,
                attach=attach, reply_markup=keyboard,
                silent=silent, timeout=timeout, thread_id=thread,
            )
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread is not None:
            body["message_thread_id"] = thread
        if keyboard is not None:
            body["reply_markup"] = keyboard
        if silent:
            body["disable_notification"] = True
        url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, json=body)

    thread_fallback = False
    try:
        response = await _post(thread_id)
        if thread_id is not None and _is_thread_missing(response):
            _logger.warning(
                "notify: forum thread %s gone (topic=%s) — retrying in General; "
                "re-map the topic in Settings → Powiadomienia",
                thread_id, topic,
            )
            response = await _post(None)
            thread_fallback = True
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
    envelope = {"ok": True, "topic": topic, "message_id": msg_id}
    if thread_fallback:
        # Surfaced (not just logged) so the /api/notify caller and the Settings
        # test button can say "delivered, but your thread mapping is stale".
        envelope["thread_fallback"] = True
    return envelope


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


# ── forum-topic setup (per-topic routing; see notify_topics) ─────────


#: Telegram accepts only these six ``icon_color`` values for a forum topic;
#: anything else is rejected outright. Assigned round-robin so the threads are
#: visually distinct in the client without asking the user to pick colours.
_FORUM_ICON_COLORS = (0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F)

#: Topic icon per topic. Telegram draws this NEXT TO the name, so the name
#: itself stays plain — the same emoji in both renders twice in the tab bar.
#: These are NOT ``_TOPIC_EMOJI``: topic icons come from a closed set of ~112
#: emoji (``getForumTopicIconStickers``) which has no ⏰, 🛠 or 📤, so cron,
#: system and cron-out use the nearest available stand-in.
_FORUM_ICON_EMOJI = {
    "cron":     "❗️",   # awarie — no ⏰ in the allowed set
    "cron-out": "📈",   # wyniki / raporty
    "agent":    "🤖",
    "chat":     "💬",
    "tasks":    "✅",
    "system":   "💻",   # no 🛠 in the allowed set
}

_VARIATION_SELECTOR = "️"
_forum_icon_cache: dict[str, str] | None = None


def forum_icon_color(index: int) -> int:
    return _FORUM_ICON_COLORS[index % len(_FORUM_ICON_COLORS)]


def reset_forum_icon_cache() -> None:
    """Drop the memoized sticker list (tests; the set is static in practice)."""
    global _forum_icon_cache
    _forum_icon_cache = None


async def forum_icon_id(emoji: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> str | None:
    """``custom_emoji_id`` for a topic icon, or None if Telegram doesn't offer it.

    Matching ignores U+FE0F: the API returns "❗️" with the variation selector
    while source literals often lack it, and a literal comparison then misses a
    perfectly available icon.
    """
    global _forum_icon_cache
    if _forum_icon_cache is None:
        token, _ = _read_credentials()
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{TELEGRAM_API_BASE}/bot{token}/getForumTopicIconStickers",
                )
            data = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            _logger.warning("notify: forum icon list unavailable: %s", exc)
            return None
        if not data.get("ok"):
            return None
        _forum_icon_cache = {
            str(s.get("emoji", "")).replace(_VARIATION_SELECTOR, ""): str(s.get("custom_emoji_id"))
            for s in (data.get("result") or [])
            if s.get("emoji") and s.get("custom_emoji_id")
        }
    return _forum_icon_cache.get((emoji or "").replace(_VARIATION_SELECTOR, ""))


async def create_forum_topic(
    *,
    chat_id: str,
    name: str,
    icon_color: int | None = None,
    icon_custom_emoji_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Create one forum thread in ``chat_id``. Returns ``{ok, thread_id?, error?}``.

    Needed because the Bot API offers **no way to list** existing forum topics —
    a bot only learns a ``message_thread_id`` from an update it received in that
    thread. So either the bot creates the threads itself (this call, requiring
    admin + *Manage Topics*) or the user posts once in each thread and we read
    the ids back via :func:`discover_forum_topics`.
    """
    token, _ = _read_credentials()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}
    body: dict[str, Any] = {"chat_id": chat_id, "name": name[:128]}
    # An explicit icon wins; the colour is the fallback for topics whose emoji
    # Telegram does not offer as a topic icon.
    if icon_custom_emoji_id:
        body["icon_custom_emoji_id"] = icon_custom_emoji_id
    elif icon_color is not None:
        body["icon_color"] = icon_color
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{TELEGRAM_API_BASE}/bot{token}/createForumTopic", json=body,
            )
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "error": f"transport: {exc}"}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "error": f"non-json response (http {response.status_code})"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("description") or f"http {response.status_code}"}
    result = data.get("result") or {}
    thread_id = result.get("message_thread_id")
    if not isinstance(thread_id, int):
        return {"ok": False, "error": "telegram returned no message_thread_id"}
    return {"ok": True, "thread_id": thread_id, "name": result.get("name") or name}


def _thread_name(msg: dict) -> str | None:
    """Topic title from either shape Telegram uses.

    A message that CREATED the topic carries ``forum_topic_created`` directly;
    an ordinary message in the thread carries it on ``reply_to_message`` (the
    thread's root). Neither is guaranteed — an id with no name is still useful.
    """
    for holder in (msg, msg.get("reply_to_message") or {}):
        created = holder.get("forum_topic_created")
        if isinstance(created, dict) and created.get("name"):
            return str(created["name"])
    return None


async def discover_forum_topics(*, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Scan ``getUpdates`` for forum threads the bot has seen.

    Returns ``{ok, groups: [{chat_id, title, threads: [{thread_id, name?}]}]}``.
    Telegram keeps unacknowledged updates ~24 h, so this only sees threads
    posted in recently — the reason the Settings flow prefers creating the
    topics over discovering them.
    """
    token, _ = _read_credentials()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing", "groups": []}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{TELEGRAM_API_BASE}/bot{token}/getUpdates")
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "error": f"transport: {exc}", "groups": []}
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "error": "non-json response from telegram", "groups": []}
    if not data.get("ok"):
        return {
            "ok": False,
            "error": data.get("description") or "telegram api ok=false",
            "groups": [],
        }

    groups: dict[str, dict[str, Any]] = {}
    for update in data.get("result") or []:
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or chat.get("type") not in ("supergroup", "group"):
            continue
        key = str(chat_id)
        group = groups.setdefault(key, {
            "chat_id": key,
            "title": chat.get("title"),
            "is_forum": bool(chat.get("is_forum")),
            "threads": {},
        })
        group["is_forum"] = group["is_forum"] or bool(chat.get("is_forum"))
        thread_id = msg.get("message_thread_id")
        if isinstance(thread_id, int):
            name = _thread_name(msg)
            # Later updates win only when they actually carry a name — an
            # ordinary message must not blank a title we already learned.
            if name or thread_id not in group["threads"]:
                group["threads"][thread_id] = name

    out = [
        {
            "chat_id": g["chat_id"],
            "title": g["title"],
            "is_forum": g["is_forum"],
            "threads": [
                {"thread_id": tid, "name": name}
                for tid, name in sorted(g["threads"].items())
            ],
        }
        for g in groups.values()
    ]
    if not out:
        return {
            "ok": False,
            "groups": [],
            "error": (
                "brak świeżych update'ów — napisz cokolwiek w każdym wątku grupy "
                "(bot musi być jej członkiem), potem kliknij ponownie"
            ),
        }
    return {"ok": True, "groups": out}
