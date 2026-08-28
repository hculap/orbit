"""Forum-thread icons.

Telegram already draws an icon next to a topic's name, so putting the same
emoji INSIDE the name renders it twice ("🛠️ 🛠️ system" in the tab bar). The
emoji therefore belongs in ``icon_custom_emoji_id`` and the name stays plain.
The catch: only ~112 emoji are allowed as topic icons and the set does NOT
include ⏰ / 🛠 / 📤, so the mapping here is a deliberate second choice per
topic, not the message emoji reused.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from orbit import notify as notify_mod
from orbit import notify_topics


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Client:
    posts: list[dict] = []
    get_body: dict = {}

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, **_kw):
        return _Resp(type(self).get_body)

    async def post(self, url, *, json=None, **_kw):  # noqa: A002
        type(self).posts.append({"url": url, "json": json})
        return _Resp({"ok": True, "result": {"message_thread_id": 3, "name": "cron"}})


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    _Client.posts = []
    _Client.get_body = {"ok": True, "result": [
        {"emoji": "🤖", "custom_emoji_id": "111"},
        {"emoji": "❗️", "custom_emoji_id": "222"},
    ]}
    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", lambda *a, **kw: _Client())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    notify_mod.reset_forum_icon_cache()
    yield
    notify_mod.reset_forum_icon_cache()


def test_every_known_topic_has_an_icon_emoji():
    """A topic with no mapping would silently fall back to a random colour."""
    for topic in notify_topics.KNOWN_TOPICS:
        assert topic in notify_mod._FORUM_ICON_EMOJI, topic


def test_icon_lookup_matches_ignoring_variation_selector():
    """Telegram returns ❗️ with U+FE0F; a literal comparison misses it."""
    assert _run(notify_mod.forum_icon_id("❗")) == "222"
    assert _run(notify_mod.forum_icon_id("❗️")) == "222"


def test_unavailable_emoji_returns_none_not_error():
    assert _run(notify_mod.forum_icon_id("⏰")) is None


def test_icon_list_is_fetched_once(monkeypatch):
    calls = {"n": 0}
    real_get = _Client.get

    async def counting_get(self, url, **kw):
        calls["n"] += 1
        return await real_get(self, url, **kw)

    monkeypatch.setattr(_Client, "get", counting_get)
    _run(notify_mod.forum_icon_id("🤖"))
    _run(notify_mod.forum_icon_id("❗️"))
    assert calls["n"] == 1


def test_create_forum_topic_sends_icon_id_and_plain_name():
    _run(notify_mod.create_forum_topic(
        chat_id="-100123", name="cron", icon_custom_emoji_id="222"))
    body = _Client.posts[0]["json"]
    assert body["name"] == "cron"          # no emoji in the name
    assert body["icon_custom_emoji_id"] == "222"


def test_create_forum_topic_without_icon_id_falls_back_to_colour():
    _run(notify_mod.create_forum_topic(
        chat_id="-100123", name="cron", icon_color=notify_mod.forum_icon_color(0)))
    body = _Client.posts[0]["json"]
    assert "icon_custom_emoji_id" not in body
    assert body["icon_color"] in notify_mod._FORUM_ICON_COLORS
