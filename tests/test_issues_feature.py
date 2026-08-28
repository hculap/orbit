"""Tests for the per-project/area Issues feature additions.

Covers the new, network-free surface:
- ``issues_config`` flag parsing (defaults off, reads, malformed-safe)
- ``library_github.get_issue_node_id`` / ``add_comment`` gh wrappers (``_gh_run``
  + ``gh_resolve_repo`` mocked, so no network / no gh CLI needed)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


# ── issues_config ───────────────────────────────────────────────


def test_issues_config_defaults_off(monkeypatch):
    from orbit import issues_config, config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_overrides", lambda: {})
    c = issues_config.load()
    assert c.make_task is False
    assert c.create_repo is False


def test_issues_config_reads_flags(monkeypatch):
    from orbit import issues_config, config as cfg_mod
    monkeypatch.setattr(
        cfg_mod, "load_overrides",
        lambda: {"issues": {"make_task": True, "create_repo": True}},
    )
    c = issues_config.load()
    assert c.make_task is True
    assert c.create_repo is True


def test_issues_config_malformed_block_is_safe(monkeypatch):
    from orbit import issues_config, config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_overrides", lambda: {"issues": "not-a-dict"})
    c = issues_config.load()
    assert c.make_task is False
    assert c.create_repo is False


# ── library_github.get_issue_node_id ────────────────────────────


def test_get_issue_node_id_ok(monkeypatch, tmp_path):
    from orbit import library_github as gh

    async def fake_resolve(_path):
        return ("owner", "repo")

    captured = {}

    async def fake_run(args, cwd=None, stdin_input=None, timeout_s=None):
        captured["args"] = args
        captured["cwd"] = cwd
        return (0, json.dumps({"id": "I_node123"}).encode(), b"")

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    monkeypatch.setattr(gh, "_gh_run", fake_run)

    res = _run(gh.get_issue_node_id(tmp_path, 7))
    assert res == {"ok": True, "id": "I_node123"}
    assert captured["args"] == ["issue", "view", "7", "--json", "id"]
    assert captured["cwd"] == tmp_path


def test_get_issue_node_id_bad_number():
    from orbit import library_github as gh
    res = _run(gh.get_issue_node_id(Path("/nope"), 0))
    assert res["ok"] is False


def test_get_issue_node_id_gh_error(monkeypatch, tmp_path):
    from orbit import library_github as gh

    async def fake_resolve(_path):
        return ("o", "r")

    async def fake_run(args, cwd=None, stdin_input=None, timeout_s=None):
        return (1, b"", b"gh boom")

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    monkeypatch.setattr(gh, "_gh_run", fake_run)
    res = _run(gh.get_issue_node_id(tmp_path, 7))
    assert res["ok"] is False
    assert "boom" in res["error"]


def test_get_issue_node_id_missing_id(monkeypatch, tmp_path):
    from orbit import library_github as gh

    async def fake_resolve(_path):
        return ("o", "r")

    async def fake_run(args, cwd=None, stdin_input=None, timeout_s=None):
        return (0, b"{}", b"")  # no id field

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    monkeypatch.setattr(gh, "_gh_run", fake_run)
    res = _run(gh.get_issue_node_id(tmp_path, 7))
    assert res["ok"] is False


# ── library_github.add_comment ──────────────────────────────────


def test_add_comment_ok(monkeypatch, tmp_path):
    from orbit import library_github as gh

    async def fake_resolve(_path):
        return ("o", "r")

    seen = {}

    async def fake_run(args, cwd=None, stdin_input=None, timeout_s=None):
        seen["args"] = args
        seen["stdin"] = stdin_input
        return (0, b"https://github.com/o/r/issues/7#issuecomment-1\n", b"")

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    monkeypatch.setattr(gh, "_gh_run", fake_run)

    res = _run(gh.add_comment(tmp_path, 7, "hello world"))
    assert res["ok"] is True
    assert res["url"].startswith("https://github.com/")
    assert seen["args"][:3] == ["issue", "comment", "7"]
    assert seen["stdin"] == b"hello world"


def test_add_comment_empty_body_rejected():
    from orbit import library_github as gh
    res = _run(gh.add_comment(Path("/nope"), 7, "   "))
    assert res["ok"] is False


# ── library.create_github_repo_for_item (Phase 4 guards) ────────


def test_create_repo_for_item_refuses_when_remote_exists(monkeypatch, tmp_path):
    from orbit import library as lib
    from orbit import library_github as gh

    async def fake_resolve(_path):
        return ("owner", "repo")  # a github origin already exists

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    res = _run(lib.create_github_repo_for_item(tmp_path, "private"))
    assert res["ok"] is False
    assert "already has a GitHub remote" in res["error"]


def test_create_repo_for_item_rejects_non_dir():
    from orbit import library as lib
    res = _run(lib.create_github_repo_for_item(Path("/nope/does-not-exist"), "private"))
    assert res["ok"] is False


def test_create_repo_for_item_refuses_when_nothing_to_commit(monkeypatch, tmp_path):
    """An empty dir / no commit must be refused BEFORE `gh repo create` so we
    never leave an orphaned empty remote on GitHub (real git, no network)."""
    from orbit import library as lib
    from orbit import library_github as gh

    async def fake_resolve(_path):
        raise ValueError("not a github repo")  # no remote yet → past the guard

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    res = _run(lib.create_github_repo_for_item(tmp_path, "private"))
    assert res["ok"] is False
    assert "nothing to commit" in res["error"]


# ── issue LIST body: the search haystack ────────────────────────
#
# The panel's search box matches issue DESCRIPTIONS client-side, which only
# works because the list request asks gh for `body`. Nothing renders that
# field, so every failure mode here is silent: drop it from the field list and
# search quietly degrades to title+number; splice prose into it and the user
# gets hits they cannot see. Hence the guards.


def test_issue_list_requests_body_for_search():
    """`body` on the LIST is what makes description search possible.

    A payload-size cleanup that drops it leaves the panel rendering and the
    search box accepting input — it just stops finding anything in a
    description. Nothing else in the repo would go red, so this does.
    """
    from orbit import library_github as gh
    assert "body" in gh._ISSUE_LIST_FIELDS.split(",")


def test_clip_issue_body_truncates_without_polluting_the_haystack():
    from orbit import library_github as gh

    cap = gh._ISSUE_LIST_BODY_CAP
    src = {"number": 1, "title": "t", "body": "x" * (cap + 1)}
    out = gh._clip_issue_body(src)

    assert out is not src                       # copied, never mutated
    assert src["body"] == "x" * (cap + 1)       # input untouched
    assert out["body"] == "x" * cap             # cut exactly at the cap…
    assert len(out["body"]) == cap              # …and not one char over
    assert out["body_truncated"] is True        # the fact rides out of band
    assert out["number"] == 1 and out["title"] == "t"


def test_clip_issue_body_adds_no_searchable_marker():
    """No prose in the haystack.

    `body` exists on the list ONLY as something to substring-match against, so
    a truncation marker would be matchable text with nothing on screen to
    explain the hit — typing "obcięte" used to return exactly the issues whose
    bodies happened to be long.
    """
    from orbit import library_github as gh

    out = gh._clip_issue_body({"body": "a" * (gh._ISSUE_LIST_BODY_CAP + 500)})
    assert set(out["body"]) == {"a"}            # nothing but the original text
    assert "…" not in out["body"]
    assert "obcięte" not in out["body"]


def test_clip_issue_body_passes_short_and_odd_items_through():
    """Under the cap nothing is copied — identity, not equality, on purpose."""
    from orbit import library_github as gh

    exactly = {"body": "y" * gh._ISSUE_LIST_BODY_CAP}
    for item in (
        {"number": 3},                       # missing body (gh omits it)
        {"body": None},                      # empty body arrives as null
        {"body": "short"},
        exactly,                             # boundary: == cap is NOT over
    ):
        assert gh._clip_issue_body(item) is item
    assert gh._clip_issue_body("not-a-dict") == "not-a-dict"


def test_list_issues_clips_bodies(monkeypatch, tmp_path):
    from orbit import library_github as gh

    async def fake_resolve(_path):
        return ("o", "r")

    seen = {}
    long_body = "z" * (gh._ISSUE_LIST_BODY_CAP + 42)

    async def fake_run(args, cwd=None, stdin_input=None, timeout_s=None):
        seen["args"] = args
        payload = [
            {"number": 1, "title": "long", "body": long_body},
            {"number": 2, "title": "short", "body": "ok"},
        ]
        return (0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(gh, "gh_resolve_repo", fake_resolve)
    monkeypatch.setattr(gh, "_gh_run", fake_run)

    res = _run(gh.list_issues(tmp_path, state="open", limit=50))
    assert res["ok"] is True
    assert "body" in seen["args"][seen["args"].index("--json") + 1]
    first, second = res["items"]
    assert len(first["body"]) == gh._ISSUE_LIST_BODY_CAP
    assert first.get("body_truncated") is True
    assert second["body"] == "ok"
    assert "body_truncated" not in second
