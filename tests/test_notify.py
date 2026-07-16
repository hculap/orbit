"""Unit tests for orbit.notify (Telegram backend)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from orbit import notify as notify_mod


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: dict | None = None,
                 raw_text: str | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 42}}
        self.text = raw_text if raw_text is not None else json.dumps(self._body)

    def json(self) -> dict:
        if not isinstance(self._body, dict):
            raise ValueError("not a dict")
        return self._body


class _FakeClient:
    """Async-context client that records the last POST/GET call."""

    last_post: dict[str, Any] = {}
    last_get_url: str = ""

    def __init__(
        self,
        *,
        post_response: _FakeResponse | None = None,
        get_response: _FakeResponse | None = None,
        raise_on_post: Exception | None = None,
        raise_on_get: Exception | None = None,
        timeout: float | None = None,
    ):
        self._post_resp = post_response or _FakeResponse()
        self._get_resp = get_response or _FakeResponse()
        self._raise_post = raise_on_post
        self._raise_get = raise_on_get

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, *, json: dict | None = None,  # noqa: A002
                   **_kwargs) -> _FakeResponse:
        type(self).last_post = {"url": url, "json": json}
        if self._raise_post is not None:
            raise self._raise_post
        return self._post_resp

    async def get(self, url: str, **_kwargs) -> _FakeResponse:
        type(self).last_get_url = url
        if self._raise_get is not None:
            raise self._raise_get
        return self._get_resp


def _install_fake_client(monkeypatch, **fake_kwargs):
    def _factory(*_a, **_kw):
        return _FakeClient(**fake_kwargs)
    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", _factory)
    _FakeClient.last_post = {}
    _FakeClient.last_get_url = ""


def _set_creds(monkeypatch, *, token: str = "TESTTOKEN", chat_id: str = "12345"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)


# ── notify() happy path + envelope ────────────────────────────────


def test_notify_happy_path_posts_to_sendmessage(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)

    result = _run(notify_mod.notify(
        topic="cron",
        title="Garden brain weekly",
        message="ok in 3.2s",
        priority=3,
    ))

    assert result["ok"] is True
    assert result["topic"] == "cron"
    assert result["message_id"] == 42

    call = _FakeClient.last_post
    assert call["url"] == "https://api.telegram.org/botTESTTOKEN/sendMessage"
    body = call["json"]
    assert body["chat_id"] == "12345"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    assert "<b>Garden brain weekly</b>" in body["text"]
    assert "ok in 3.2s" in body["text"]
    assert "#cron" in body["text"]
    assert "⏰" in body["text"]  # cron emoji
    assert "disable_notification" not in body  # priority=3 doesn't silence


def test_notify_priority_min_silences(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(topic="system", message="boring", priority=1))
    body = _FakeClient.last_post["json"]
    assert body["disable_notification"] is True


def test_notify_priority_max_prepends_alarm(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(topic="system", title="DOWN", message="x", priority=5))
    body = _FakeClient.last_post["json"]
    assert "🚨" in body["text"]


def test_notify_click_renders_inline_keyboard_button(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(
        topic="cron", message="x",
        click="https://dashboard.example/scheduler/foo",
    ))
    body = _FakeClient.last_post["json"]
    # No more text-link inside the body — that turned into a real button.
    assert "<a href=" not in body["text"]
    assert "reply_markup" in body
    rows = body["reply_markup"]["inline_keyboard"]
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0]["text"] == "Open scheduler"  # cron-specific label
    assert rows[0][0]["url"] == "https://dashboard.example/scheduler/foo"


def test_notify_no_click_omits_keyboard(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(topic="cron", message="x"))
    body = _FakeClient.last_post["json"]
    assert "reply_markup" not in body


def test_notify_actions_become_extra_buttons(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(
        topic="cron", message="x",
        click="https://example.com/main",
        actions=[
            {"action": "view", "label": "Logs", "url": "https://example.com/logs"},
            {"action": "view", "label": "Retry", "url": "https://example.com/retry"},
            {"action": "broadcast", "label": "Skipped — no url"},  # no url → drop
        ],
    ))
    rows = _FakeClient.last_post["json"]["reply_markup"]["inline_keyboard"]
    assert len(rows[0]) == 3  # main + Logs + Retry
    labels = [b["text"] for b in rows[0]]
    assert labels[0] == "Open scheduler"
    assert "Logs" in labels and "Retry" in labels


def test_notify_html_escapes_user_content(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(
        topic="cron",
        title="<script>alert(1)</script>",
        message="2 < 3 & 4 > 1",
    ))
    body = _FakeClient.last_post["json"]
    assert "<script>" not in body["text"]
    assert "&lt;script&gt;" in body["text"]
    assert "&lt; 3 &amp; 4 &gt; 1" in body["text"]


def test_notify_code_wraps_in_pre_block(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(
        topic="cron", message="status=ok",
        code="line1\nline2 with <html> & ampersand",
    ))
    body = _FakeClient.last_post["json"]
    text = body["text"]
    # Code rendered inside <pre> with HTML-escaped content
    assert "<pre>" in text and "</pre>" in text
    assert "&lt;html&gt;" in text
    assert "&amp;" in text
    # Hashtags should still be at the bottom (after the <pre>)
    pre_end = text.find("</pre>")
    hashtag_pos = text.find("#cron")
    assert hashtag_pos > pre_end, f"hashtags should follow </pre>; text was: {text}"


def test_notify_no_code_omits_pre_block(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(topic="cron", message="status=ok"))
    body = _FakeClient.last_post["json"]
    assert "<pre>" not in body["text"]


def test_notify_code_truncates_at_3500(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    big = "x" * 5000
    _run(notify_mod.notify(topic="cron", message="big", code=big))
    text = _FakeClient.last_post["json"]["text"]
    # Should contain truncation marker, not the full 5000 chars
    assert "(truncated)" in text
    assert text.count("x") <= 3500


def test_notify_extra_tags_become_hashtags(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _run(notify_mod.notify(
        topic="cron", message="x", tags=["failed", "garden brain"],
    ))
    body = _FakeClient.last_post["json"]
    # "garden brain" → "gardenbrain" (whitespace stripped by safe regex)
    assert "#cron" in body["text"]
    assert "#failed" in body["text"]
    assert "#gardenbrain" in body["text"]


# ── notify() failure modes (never raise) ──────────────────────────


def test_notify_missing_token_returns_failure(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _install_fake_client(monkeypatch)
    result = _run(notify_mod.notify(topic="cron", message="x"))
    assert result["ok"] is False
    assert "credentials" in result["reason"]
    assert _FakeClient.last_post == {}  # never called the network


def test_notify_missing_chat_id_returns_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    _install_fake_client(monkeypatch)
    result = _run(notify_mod.notify(topic="cron", message="x"))
    assert result["ok"] is False
    assert "credentials" in result["reason"]


def test_notify_topic_in_disabled_list_short_circuits(monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setenv("NOTIFY_DISABLE_TOPICS", "chat,cron")
    _install_fake_client(monkeypatch)
    result = _run(notify_mod.notify(topic="cron", message="x"))
    assert result["ok"] is False
    assert "disabled" in result["reason"]
    assert _FakeClient.last_post == {}  # never POSTed


def test_notify_invalid_topic_rejected(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    for bad in ["", "with/slash", "with space", "ąś", "_leadingunderscore"]:
        result = _run(notify_mod.notify(topic=bad, message="x"))
        assert result["ok"] is False, f"expected failure for topic={bad!r}"


def test_notify_invalid_priority_rejected(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    for bad in [0, 6, -1, True, "3"]:
        result = _run(notify_mod.notify(topic="cron", message="x", priority=bad))  # type: ignore[arg-type]
        assert result["ok"] is False, f"expected failure for priority={bad!r}"


def test_notify_empty_message_rejected(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(monkeypatch)
    for bad in ["", "   ", None]:
        result = _run(notify_mod.notify(topic="cron", message=bad))  # type: ignore[arg-type]
        assert result["ok"] is False


def test_notify_telegram_returns_non_200(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(
        monkeypatch,
        post_response=_FakeResponse(
            status_code=400,
            body={"ok": False, "description": "Bad Request: chat not found"},
            raw_text='{"ok":false,"description":"Bad Request: chat not found"}',
        ),
    )
    result = _run(notify_mod.notify(topic="cron", message="x"))
    assert result["ok"] is False
    assert "400" in result["reason"]


def test_notify_telegram_returns_ok_false(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(
        monkeypatch,
        post_response=_FakeResponse(
            status_code=200,
            body={"ok": False, "description": "Forbidden: bot was blocked by the user"},
        ),
    )
    result = _run(notify_mod.notify(topic="cron", message="x"))
    assert result["ok"] is False
    assert "blocked" in result["reason"].lower()


def test_notify_transport_error_swallowed(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(
        monkeypatch,
        raise_on_post=httpx.ConnectError("connection refused"),
    )
    result = _run(notify_mod.notify(topic="cron", message="x"))
    assert result["ok"] is False
    assert "transport" in result["reason"]


# ── probe() ───────────────────────────────────────────────────────


def test_probe_missing_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    result = _run(notify_mod.probe())
    assert result["ok"] is False
    assert "TELEGRAM_BOT_TOKEN" in result["error"]


def test_probe_happy_path_returns_username(monkeypatch):
    _set_creds(monkeypatch)
    _install_fake_client(
        monkeypatch,
        get_response=_FakeResponse(
            status_code=200,
            body={"ok": True, "result": {"username": "test_bot"}},
        ),
    )
    result = _run(notify_mod.probe())
    assert result["ok"] is True
    assert result["bot_username"] == "test_bot"
    assert result["chat_id"] == "12345"
    assert _FakeClient.last_get_url.endswith("/botTESTTOKEN/getMe")


def test_probe_invalid_token(monkeypatch):
    _set_creds(monkeypatch, token="BAD")
    _install_fake_client(
        monkeypatch,
        get_response=_FakeResponse(
            status_code=401,
            body={"ok": False, "description": "Unauthorized"},
        ),
    )
    result = _run(notify_mod.probe())
    assert result["ok"] is False
    assert "401" in result["error"]


# ── detect_chat_id() ──────────────────────────────────────────────


def test_detect_chat_id_finds_most_recent_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    _install_fake_client(
        monkeypatch,
        get_response=_FakeResponse(
            status_code=200,
            body={
                "ok": True,
                "result": [
                    {"update_id": 1, "message": {
                        "chat": {"id": 111, "type": "private"},
                        "from": {"username": "older"},
                    }},
                    {"update_id": 2, "message": {
                        "chat": {"id": 999, "type": "private"},
                        "from": {"username": "newer"},
                    }},
                ],
            },
        ),
    )
    result = _run(notify_mod.detect_chat_id())
    assert result["ok"] is True
    assert result["chat_id"] == "999"
    assert result["from"] == "newer"


# ── attach (sendPhoto / sendAudio / sendDocument) ────────────────


def test_attach_local_png_uses_send_photo(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    # `_HOME` was captured at import time; patch it for the path-safety check.
    monkeypatch.setattr(notify_mod, "_HOME", tmp_path)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    captured: dict[str, Any] = {}

    class _MultipartClient(_FakeClient):
        async def post(self, url, *, files=None, data=None, json=None, **_kw):  # noqa: A002
            captured["url"] = url
            captured["files"] = files
            captured["data"] = data
            return _FakeResponse()

    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", lambda *a, **kw: _MultipartClient())

    result = _run(notify_mod.notify(
        topic="agent", message="screenshot", attach=str(img),
    ))
    assert result["ok"] is True
    assert "/sendPhoto" in captured["url"]
    assert "photo" in captured["files"]
    name, content, mime = captured["files"]["photo"]
    assert name == "shot.png"
    assert content == b"\x89PNG\r\n\x1a\nfake"
    assert "image/png" in (mime or "")
    assert captured["data"]["caption"]  # caption is the formatted message
    assert "agent" in captured["data"]["caption"]


def test_attach_url_passes_string_not_multipart(monkeypatch):
    _set_creds(monkeypatch)
    captured: dict[str, Any] = {}

    class _UrlClient(_FakeClient):
        async def post(self, url, *, files=None, data=None, json=None, **_kw):  # noqa: A002
            captured["url"] = url
            captured["files"] = files
            captured["data"] = data
            return _FakeResponse()

    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", lambda *a, **kw: _UrlClient())
    result = _run(notify_mod.notify(
        topic="cron", message="x",
        attach="https://example.com/dashboard-state.png",
    ))
    assert result["ok"] is True
    assert "/sendPhoto" in captured["url"]
    assert captured["files"] is None
    assert captured["data"]["photo"] == "https://example.com/dashboard-state.png"


def test_attach_extension_picks_method(monkeypatch):
    cases = {
        "/tmp/foo.mp3":     ("sendAudio",    "audio"),
        "/tmp/foo.opus":    ("sendAudio",    "audio"),
        "/tmp/foo.mp4":     ("sendVideo",    "video"),
        "/tmp/foo.png":     ("sendPhoto",    "photo"),
        "/tmp/foo.jpg":     ("sendPhoto",    "photo"),
        "/tmp/foo.txt":     ("sendDocument", "document"),
        "/tmp/foo.log":     ("sendDocument", "document"),
        "/tmp/no_ext":      ("sendDocument", "document"),
    }
    for target, (method, field) in cases.items():
        m, f, _ = notify_mod._attach_method(target)
        assert m == method, f"{target}: expected {method}, got {m}"
        assert f == field, f"{target}: expected {field}, got {f}"


def test_attach_url_with_query_still_routes_correctly(monkeypatch):
    method, field, _ = notify_mod._attach_method("https://x/y/avatar.JPG?token=abc&ts=1")
    assert method == "sendPhoto"
    assert field == "photo"


def test_attach_path_outside_home_or_tmp_rejected(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    monkeypatch.setattr(notify_mod, "_HOME", tmp_path / "fake-home")
    # Real /etc/hosts (or any file outside the allowed roots).
    forbidden = "/etc/hosts"
    result = _run(notify_mod.notify(
        topic="cron", message="x", attach=forbidden,
    ))
    assert result["ok"] is False
    assert "attach" in result["reason"]


def test_attach_missing_file_returns_failure(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    monkeypatch.setattr(notify_mod, "_HOME", tmp_path)
    result = _run(notify_mod.notify(
        topic="cron", message="x", attach=str(tmp_path / "nonexistent.png"),
    ))
    assert result["ok"] is False
    assert "not a file" in result["reason"]


def test_route_payload_passes_code_through():
    """`POST /api/notify` must forward the `code` field to notify.notify(...)
    — earlier the router stripped it and the agent skill's documented
    monospace-block feature silently sent plain text. Issue from PR #36
    code review."""
    from orbit.notify_routes import _normalize_notify_payload
    payload = {
        "topic": "agent",
        "message": "build summary",
        "code": "src/foo.py:12: W: unused import",
    }
    kwargs = _normalize_notify_payload(payload)
    assert kwargs.get("code") == "src/foo.py:12: W: unused import"


def test_route_payload_omits_code_when_absent():
    from orbit.notify_routes import _normalize_notify_payload
    kwargs = _normalize_notify_payload({"topic": "agent", "message": "x"})
    assert kwargs.get("code") is None


def test_detect_chat_id_no_messages_returns_helpful_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    _install_fake_client(
        monkeypatch,
        get_response=_FakeResponse(
            status_code=200,
            body={"ok": True, "result": []},
        ),
    )
    result = _run(notify_mod.detect_chat_id())
    assert result["ok"] is False
    assert "/start" in result["error"]
