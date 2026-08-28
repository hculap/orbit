"""Unit tests for orbit.notify_topics (per-topic Telegram routing)
and the ``message_thread_id`` plumbing it adds to notify.notify().

The feature routes each logical topic (cron / agent / chat / tasks / system)
into its own FORUM THREAD of one supergroup. The invariant under test
everywhere: an unconfigured install must keep behaving exactly like before
(single ``TELEGRAM_CHAT_ID``, no thread), because the routing file is opt-in
and arrives empty.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from orbit import notify as notify_mod
from orbit import notify_topics as nt


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    """Point the store at a tmp file and drop the module cache between tests."""
    monkeypatch.setattr(nt, "ROUTES_PATH", tmp_path / "notify_routes.json")
    nt.invalidate()
    yield
    nt.invalidate()


def _write(payload: dict) -> None:
    nt.ROUTES_PATH.write_text(json.dumps(payload), encoding="utf-8")
    nt.invalidate()


# ── resolve(): fallback + routing ────────────────────────────────


def test_no_config_falls_back_to_env_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    route = nt.resolve("cron")
    assert route.chat_id == "12345"
    assert route.thread_id is None


def test_no_config_and_no_env_resolves_to_nothing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    route = nt.resolve("cron")
    assert route.chat_id is None
    assert route.thread_id is None


def test_group_and_thread_resolve(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5}, "tasks": {"thread_id": 9}},
    })
    assert nt.resolve("cron") == nt.Route("-1001234567890", 5)
    assert nt.resolve("tasks") == nt.Route("-1001234567890", 9)


def test_unmapped_topic_lands_in_group_general_thread():
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5}},
    })
    # A topic nobody wired yet must still be delivered — to the group's
    # General thread — never dropped and never sent to the old chat.
    assert nt.resolve("weather") == nt.Route("-1001234567890", None)


def test_per_topic_chat_id_override_wins():
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"tasks": {"chat_id": "-1009999999999"}},
    })
    assert nt.resolve("tasks") == nt.Route("-1009999999999", None)


def test_group_chat_id_absent_uses_env_even_with_threads(monkeypatch):
    """thread_id without a group is meaningless — must not be applied to the
    legacy 1:1 chat, or every message would 400 'thread not found'."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _write({"version": 1, "topics": {"cron": {"thread_id": 5}}})
    assert nt.resolve("cron") == nt.Route("12345", None)


# ── sanitizing an untrusted / hand-edited file ───────────────────


def test_bad_thread_id_dropped_but_group_kept():
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": "five"}, "chat": {"thread_id": -3}},
    })
    assert nt.resolve("cron") == nt.Route("-1001234567890", None)
    assert nt.resolve("chat") == nt.Route("-1001234567890", None)


def test_bad_group_chat_id_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _write({"version": 1, "group_chat_id": "'; DROP TABLE", "topics": {}})
    assert nt.resolve("cron") == nt.Route("12345", None)


def test_at_username_group_accepted():
    _write({"version": 1, "group_chat_id": "@orbit_alerts", "topics": {}})
    assert nt.resolve("cron").chat_id == "@orbit_alerts"


def test_out_of_spec_topic_key_dropped():
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"../etc/passwd": {"thread_id": 5}, "cron": {"thread_id": 7}},
    })
    cfg = nt.get_config()
    assert "../etc/passwd" not in cfg["topics"]
    assert cfg["topics"]["cron"]["thread_id"] == 7


def test_oversized_file_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    nt.ROUTES_PATH.write_text(
        json.dumps({"version": 1, "group_chat_id": "-100123", "pad": "x" * (nt._MAX_FILE_BYTES + 10)}),
        encoding="utf-8",
    )
    nt.invalidate()
    assert nt.resolve("cron") == nt.Route("12345", None)


def test_corrupt_json_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    nt.ROUTES_PATH.write_text("{not json", encoding="utf-8")
    nt.invalidate()
    assert nt.resolve("cron") == nt.Route("12345", None)


def test_wrong_schema_version_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _write({"version": 99, "group_chat_id": "-1001234567890", "topics": {"cron": {"thread_id": 5}}})
    assert nt.resolve("cron") == nt.Route("12345", None)


# ── save_config() ────────────────────────────────────────────────


def test_save_round_trip():
    saved = nt.save_config({
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5, "title": "⏰ cron"}},
    })
    assert saved["group_chat_id"] == "-1001234567890"
    nt.invalidate()
    assert nt.resolve("cron") == nt.Route("-1001234567890", 5)
    on_disk = json.loads(nt.ROUTES_PATH.read_text(encoding="utf-8"))
    assert on_disk["version"] == nt._SCHEMA_VERSION


def test_save_sanitizes_before_writing():
    nt.save_config({
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": "nope"}, "!!bad": {"thread_id": 2}},
    })
    on_disk = json.loads(nt.ROUTES_PATH.read_text(encoding="utf-8"))
    assert "!!bad" not in on_disk["topics"]
    assert on_disk["topics"]["cron"].get("thread_id") is None


def test_save_rejects_non_dict():
    with pytest.raises(ValueError):
        nt.save_config(["not", "a", "dict"])


def test_clearing_group_returns_to_legacy_chat(monkeypatch):
    """The rollback path: blank the group in Settings and every topic goes
    back to the single TELEGRAM_CHAT_ID with no thread."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    nt.save_config({"group_chat_id": "-1001234567890", "topics": {"cron": {"thread_id": 5}}})
    nt.save_config({"group_chat_id": "", "topics": {}})
    assert nt.resolve("cron") == nt.Route("12345", None)


# ── notify() plumbing ────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 42}}
        self.text = json.dumps(self._body)

    def json(self) -> dict:
        return self._body


class _FakeClient:
    calls: list[dict[str, Any]] = []
    responses: list[_FakeResponse] = []

    def __init__(self, **_kw):
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, *, json: dict | None = None,  # noqa: A002
                   data: dict | None = None, files: dict | None = None,
                   **_kwargs) -> _FakeResponse:
        type(self).calls.append({"url": url, "json": json, "data": data, "files": files})
        idx = len(type(self).calls) - 1
        resps = type(self).responses
        return resps[idx] if idx < len(resps) else _FakeResponse()


def _install_fake_client(monkeypatch, responses: list[_FakeResponse] | None = None):
    _FakeClient.calls = []
    _FakeClient.responses = responses or []
    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeClient())


def _creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.delenv("NOTIFY_DISABLE_TOPICS", raising=False)


def test_notify_sends_message_thread_id(monkeypatch):
    _creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5}},
    })

    result = _run(notify_mod.notify(topic="cron", message="backup ok"))

    assert result["ok"] is True
    body = _FakeClient.calls[0]["json"]
    assert body["chat_id"] == "-1001234567890"
    assert body["message_thread_id"] == 5


def test_notify_without_routing_omits_thread_id(monkeypatch):
    """Backwards compatibility: no routing file ⇒ byte-identical old payload."""
    _creds(monkeypatch)
    _install_fake_client(monkeypatch)

    _run(notify_mod.notify(topic="cron", message="backup ok"))

    body = _FakeClient.calls[0]["json"]
    assert body["chat_id"] == "12345"
    assert "message_thread_id" not in body


def test_notify_attachment_carries_thread_id(monkeypatch, tmp_path):
    _creds(monkeypatch)
    _install_fake_client(monkeypatch)
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"system": {"thread_id": 11}},
    })
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = _run(notify_mod.notify(topic="system", message="see this", attach=str(png)))

    assert result["ok"] is True
    data = _FakeClient.calls[0]["data"]
    assert data["chat_id"] == "-1001234567890"
    assert data["message_thread_id"] == "11"


def test_notify_thread_not_found_retries_in_group(monkeypatch):
    """A deleted forum topic must not swallow the alert: retry once WITHOUT the
    thread so it lands in General, and say so in the envelope + log."""
    _creds(monkeypatch)
    _install_fake_client(monkeypatch, responses=[
        _FakeResponse(status_code=400, body={
            "ok": False, "description": "Bad Request: message thread not found"}),
        _FakeResponse(),
    ])
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5}},
    })

    result = _run(notify_mod.notify(topic="cron", message="backup ok"))

    assert result["ok"] is True
    assert result["thread_fallback"] is True
    assert len(_FakeClient.calls) == 2
    assert "message_thread_id" not in _FakeClient.calls[1]["json"]
    assert _FakeClient.calls[1]["json"]["chat_id"] == "-1001234567890"


def test_notify_other_400_does_not_retry(monkeypatch):
    """Only thread-not-found earns a retry — a malformed body must fail once."""
    _creds(monkeypatch)
    _install_fake_client(monkeypatch, responses=[
        _FakeResponse(status_code=400, body={
            "ok": False, "description": "Bad Request: can't parse entities"}),
    ])
    _write({
        "version": 1,
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5}},
    })

    result = _run(notify_mod.notify(topic="cron", message="backup ok"))

    assert result["ok"] is False
    assert len(_FakeClient.calls) == 1


# ── setup helpers: create + discover forum threads ───────────────


class _FakeGetClient(_FakeClient):
    """Adds GET (getUpdates) on top of the POST recorder."""

    get_body: dict[str, Any] = {}

    async def get(self, url: str, **_kwargs) -> _FakeResponse:
        type(self).calls.append({"url": url, "json": None, "data": None, "files": None})
        return _FakeResponse(body=type(self).get_body)


def _install_get_client(monkeypatch, get_body: dict):
    _FakeGetClient.calls = []
    _FakeGetClient.responses = []
    _FakeGetClient.get_body = get_body
    monkeypatch.setattr(notify_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeGetClient())


def test_create_forum_topic_returns_thread_id(monkeypatch):
    _creds(monkeypatch)
    _install_fake_client(monkeypatch, responses=[
        _FakeResponse(body={"ok": True, "result": {"message_thread_id": 7, "name": "⏰ cron"}}),
    ])

    result = _run(notify_mod.create_forum_topic(chat_id="-1001234567890", name="⏰ cron"))

    assert result == {"ok": True, "thread_id": 7, "name": "⏰ cron"}
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/createForumTopic")
    assert call["json"]["chat_id"] == "-1001234567890"
    assert call["json"]["name"] == "⏰ cron"


def test_create_forum_topic_surfaces_missing_rights(monkeypatch):
    _creds(monkeypatch)
    _install_fake_client(monkeypatch, responses=[
        _FakeResponse(status_code=400, body={
            "ok": False, "description": "Bad Request: not enough rights to manage topics"}),
    ])

    result = _run(notify_mod.create_forum_topic(chat_id="-1001234567890", name="⏰ cron"))

    assert result["ok"] is False
    assert "manage topics" in result["error"]


def test_discover_forum_topics_groups_threads_by_chat(monkeypatch):
    _creds(monkeypatch)
    _install_get_client(monkeypatch, {"ok": True, "result": [
        {"message": {
            "chat": {"id": -1001234567890, "title": "Orbit", "type": "supergroup", "is_forum": True},
            "message_thread_id": 5,
            "reply_to_message": {"forum_topic_created": {"name": "⏰ cron"}},
        }},
        {"message": {
            "chat": {"id": -1001234567890, "title": "Orbit", "type": "supergroup", "is_forum": True},
            "message_thread_id": 9,
            "forum_topic_created": {"name": "✅ tasks"},
        }},
        {"message": {"chat": {"id": 4242, "title": None, "type": "private"}}},
    ]})

    result = _run(notify_mod.discover_forum_topics())

    assert result["ok"] is True
    groups = {g["chat_id"]: g for g in result["groups"]}
    assert "-1001234567890" in groups
    assert groups["-1001234567890"]["title"] == "Orbit"
    threads = {t["thread_id"]: t["name"] for t in groups["-1001234567890"]["threads"]}
    assert threads == {5: "⏰ cron", 9: "✅ tasks"}
    # A private chat is not a forum target and must not be offered as one.
    assert "4242" not in groups


def test_routes_get_returns_empty_config_and_known_topics(client):
    body = client.get("/api/notify/routes").json()
    assert body["ok"] is True
    assert body["group_chat_id"] == ""
    assert "cron" in body["known_topics"] and "tasks" in body["known_topics"]


def test_routes_put_round_trip(client):
    put = client.put("/api/notify/routes", json={
        "group_chat_id": "-1001234567890",
        "topics": {"cron": {"thread_id": 5}},
    })
    assert put.status_code == 200
    assert put.json()["topics"]["cron"]["thread_id"] == 5
    got = client.get("/api/notify/routes").json()
    assert got["group_chat_id"] == "-1001234567890"


def test_routes_put_rejects_non_object(client):
    # FastAPI's `payload: dict` annotation refuses a JSON array before the
    # handler runs — 422, not our hand-rolled 400. Either way it never reaches
    # the store; this pins that the request is rejected, not the exact code.
    assert client.put("/api/notify/routes", json=["nope"]).status_code == 422


def test_routes_create_topics_rejects_bad_topic_name(client):
    r = client.post("/api/notify/routes/create-topics", json={
        "group_chat_id": "-1001234567890", "topics": ["../etc"],
    })
    assert r.status_code == 400


def test_discover_forum_topics_without_updates_explains_next_step(monkeypatch):
    _creds(monkeypatch)
    _install_get_client(monkeypatch, {"ok": True, "result": []})

    result = _run(notify_mod.discover_forum_topics())

    assert result["ok"] is False
    assert "groups" in result and result["groups"] == []
    assert "wątk" in result["error"] or "thread" in result["error"].lower()
