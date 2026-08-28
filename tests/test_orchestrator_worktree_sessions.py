"""Session-level wiring for per-session git worktrees.

Drives the orchestrator handlers directly (same idiom as
``test_orchestrator_close_kill.py``) with a fake sidecar + fake git, so nothing
here forks git, spawns tmux, or touches a real worktree.

The invariant most of these guard: **``meta.cwd`` stays the agent dir**. If a
worktree path ever replaces it, agent-scoped session lists, lib_id grouping,
artifacts, prompts and the skills farm all silently break at once.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from orbit import orchestrator as orch
from orbit import orchestrator_meta as meta_mod
from orbit import orchestrator_worktree as worktree_mod


@pytest.fixture()
def sidecar(monkeypatch, tmp_path):
    """In-memory sidecar so set_meta/get_meta/all_meta never touch disk."""
    store: dict[str, dict] = {}

    async def _set_meta(session_id, **kwargs):
        entry = store.setdefault(session_id, dict(meta_mod._DEFAULT))
        for key, value in kwargs.items():
            if value is not None:
                entry[key] = value or None

    monkeypatch.setattr(meta_mod, "set_meta", _set_meta)
    monkeypatch.setattr(
        meta_mod, "get_meta", lambda sid: dict(store.get(sid) or meta_mod._DEFAULT)
    )
    monkeypatch.setattr(meta_mod, "all_meta", lambda: {k: dict(v) for k, v in store.items()})
    return store


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A tmp dir that passes _validate_cwd_under_home and looks like a repo."""
    monkeypatch.setattr(orch, "HOME", tmp_path)
    agent_dir = tmp_path / "Projects" / "demo"
    agent_dir.mkdir(parents=True)

    monkeypatch.setattr(worktree_mod, "repo_root_for", lambda cwd: agent_dir)
    monkeypatch.setattr(worktree_mod, "list_worktrees", lambda root: [])
    return agent_dir


@pytest.fixture()
def flag_on(monkeypatch):
    from orbit import orchestrator_settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_flag",
        lambda name: True if name == "worktree_sessions_enabled" else False,
    )


def _create(payload):
    return asyncio.run(orch._create_session_handler(payload))


# ── create ─────────────────────────────────────────────────────────


def test_create_rejects_worktree_when_the_flag_is_off(monkeypatch, sidecar, repo):
    from orbit import orchestrator_settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_flag", lambda name: False)
    with pytest.raises(HTTPException) as exc:
        _create({"cwd": str(repo), "worktree": True})
    assert exc.value.status_code == 400
    # A silently-ignored flag would produce a session that CLAIMS isolation
    # while running in the main checkout — worse than an error.
    assert "disabled" in str(exc.value.detail)
    assert sidecar == {}


def test_create_with_worktree_keeps_cwd_as_the_agent_dir(sidecar, repo, flag_on):
    """THE invariant. cwd must remain the agent dir; the worktree is extra."""
    out = _create({"cwd": str(repo), "lib_id": "projects/demo", "worktree": True,
                   "worktree_name": "feat-1"})

    assert out["cwd"] == str(repo)
    assert out["worktree_name"] == "feat-1"
    assert out["worktree_path"] == str(repo / ".claude/worktrees/feat-1")
    assert out["worktree_branch"] == "worktree-feat-1"

    entry = sidecar[out["id"]]
    assert entry["cwd"] == str(repo), "cwd must NOT be rewritten to the worktree"
    assert entry["lib_id"] == "projects/demo"
    assert entry["worktree_name"] == "feat-1"
    assert entry["worktree_repo_root"] == str(repo)


def test_create_generates_a_name_when_blank(sidecar, repo, flag_on):
    """Blank means "server picks one" — and the client learns it from the
    response, which is the only place a random name surfaces."""
    out = _create({"cwd": str(repo), "worktree": True, "worktree_name": ""})
    assert worktree_mod.NAME_RE.match(out["worktree_name"])
    assert out["worktree_path"].endswith(out["worktree_name"])


def test_create_rejects_an_invalid_worktree_name(sidecar, repo, flag_on):
    with pytest.raises(HTTPException) as exc:
        _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat/x"})
    assert exc.value.status_code == 400
    assert sidecar == {}


def test_create_rejects_a_worktree_for_a_global_session(sidecar, repo, flag_on):
    """Global sessions have no agent dir, and $HOME is not a repo."""
    with pytest.raises(HTTPException) as exc:
        _create({"worktree": True})
    assert exc.value.status_code == 400


def test_create_409s_when_another_live_session_holds_the_worktree(sidecar, repo, flag_on):
    """Two REPLs in one tree — claude's own lock makes the second a non-owning
    guest rather than refusing, so we refuse here instead."""
    first = _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat-1"})
    assert first["worktree_name"] == "feat-1"

    with pytest.raises(HTTPException) as exc:
        _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat-1"})
    assert exc.value.status_code == 409


def test_create_allows_reusing_the_name_of_an_archived_session(sidecar, repo, flag_on):
    first = _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat-1"})
    sidecar[first["id"]]["archived"] = True
    second = _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat-1"})
    assert second["worktree_name"] == "feat-1"


def test_create_409s_when_the_worktree_already_exists_on_disk(
    monkeypatch, sidecar, repo, flag_on
):
    monkeypatch.setattr(
        worktree_mod, "list_worktrees",
        lambda root: [{"name": "feat-1", "path": str(repo / ".claude/worktrees/feat-1")}],
    )
    with pytest.raises(HTTPException) as exc:
        _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat-1"})
    assert exc.value.status_code == 409


def test_create_rejects_worktree_name_without_the_flag(sidecar, repo, flag_on):
    with pytest.raises(HTTPException) as exc:
        _create({"cwd": str(repo), "worktree_name": "feat-1"})
    assert exc.value.status_code == 400


def test_create_without_worktree_is_unchanged(sidecar, repo, flag_on):
    """The default path must stay byte-identical to pre-feature behaviour."""
    out = _create({"cwd": str(repo), "lib_id": "projects/demo"})
    assert out["worktree_name"] is None
    assert out["worktree_path"] is None
    assert sidecar[out["id"]]["worktree_name"] is None


def test_create_surfaces_preflight_warnings(monkeypatch, sidecar, repo, flag_on):
    (repo / ".env").write_text("SECRET=1", encoding="utf-8")
    out = _create({"cwd": str(repo), "worktree": True, "worktree_name": "feat-1"})
    assert out["ok"] is True
    assert any(".worktreeinclude" in w for w in out["warnings"])


# ── decorate / list ────────────────────────────────────────────────


def test_decorate_surfaces_worktree_fields(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    orch._worktree_exists_cache.clear()
    out = orch._decorate_session(
        {"id": "s1", "msg_count": 0},
        {"cwd": str(tmp_path), "worktree_name": "feat-1", "worktree_path": str(worktree)},
    )
    assert out["cwd"] == str(tmp_path), "cwd must survive decoration unchanged"
    assert out["worktree_name"] == "feat-1"
    assert out["worktree_missing"] is False


def test_decorate_flags_a_vanished_worktree(tmp_path):
    orch._worktree_exists_cache.clear()
    out = orch._decorate_session(
        {"id": "s1", "msg_count": 0},
        {"cwd": str(tmp_path), "worktree_name": "gone",
         "worktree_path": str(tmp_path / "nope")},
    )
    assert out["worktree_missing"] is True


def test_decorate_leaves_plain_sessions_without_a_worktree(tmp_path):
    out = orch._decorate_session({"id": "s1", "msg_count": 0}, {"cwd": str(tmp_path)})
    assert out["worktree_name"] is None
    assert out["worktree_missing"] is False


# ── delete ─────────────────────────────────────────────────────────


@pytest.fixture()
def delete_env(monkeypatch, sidecar):
    """Stub out everything DELETE touches except the worktree decision."""
    async def _noop_release(session_id, *, forget_persistent, detach_tmux=False):
        return None

    removals: list[tuple] = []

    def _fake_remove(repo_root, path, *, force=False):
        removals.append((Path(repo_root), Path(path), force))
        return {"ok": True, "error": ""}

    monkeypatch.setattr(orch, "_release_session_slots", _noop_release)
    monkeypatch.setattr(orch.jsonl_mod, "delete_session", lambda sid: None)

    async def _remove_meta(sid):
        sidecar.pop(sid, None)

    monkeypatch.setattr(meta_mod, "remove_meta", _remove_meta)
    monkeypatch.setattr(orch.uploads_module, "delete_session_uploads", lambda sid: None)
    monkeypatch.setattr(worktree_mod, "remove_worktree", _fake_remove)
    return removals


def _seed_worktree_session(sidecar, tmp_path, sid="sess-1"):
    sidecar[sid] = {
        **meta_mod._DEFAULT,
        "cwd": str(tmp_path),
        "worktree_name": "feat-1",
        "worktree_path": str(tmp_path / ".claude/worktrees/feat-1"),
        "worktree_repo_root": str(tmp_path),
    }
    return sid


def test_delete_keeps_the_worktree_by_default(sidecar, delete_env, tmp_path):
    """Deleting a CHAT must never silently discard a branch's work."""
    sid = _seed_worktree_session(sidecar, tmp_path)
    out = asyncio.run(orch._delete_session_handler(sid))
    assert out == {"ok": True}
    assert delete_env == [], "default delete must not touch the worktree"


def test_delete_removes_the_worktree_on_opt_in_without_force(sidecar, delete_env, tmp_path):
    sid = _seed_worktree_session(sidecar, tmp_path)
    out = asyncio.run(orch._delete_session_handler(sid, remove_worktree=True))
    assert out["worktree_removed"] is True
    assert len(delete_env) == 1
    _root, _path, force = delete_env[0]
    assert force is False, "never --force: a dirty tree must refuse, not be discarded"


def test_delete_reports_a_failed_removal_but_still_deletes_the_session(
    monkeypatch, sidecar, delete_env, tmp_path
):
    """Dirty/locked worktree → the session still goes, the tree stays."""
    monkeypatch.setattr(
        worktree_mod, "remove_worktree",
        lambda root, path, force=False: {"ok": False, "error": "contains modified files"},
    )
    sid = _seed_worktree_session(sidecar, tmp_path)
    out = asyncio.run(orch._delete_session_handler(sid, remove_worktree=True))
    assert out["ok"] is True
    assert out["worktree_removed"] is False
    assert "modified" in out["worktree_error"]


def test_delete_opt_in_is_a_noop_for_a_session_without_a_worktree(
    sidecar, delete_env, tmp_path
):
    sidecar["plain"] = {**meta_mod._DEFAULT, "cwd": str(tmp_path)}
    out = asyncio.run(orch._delete_session_handler("plain", remove_worktree=True))
    assert out == {"ok": True}
    assert delete_env == []


def test_close_never_touches_the_worktree(monkeypatch, sidecar, tmp_path):
    """Close keeps the transcript, so it must keep the worktree too."""
    calls: list = []

    async def _fake_release(session_id, *, forget_persistent, detach_tmux=False):
        calls.append(session_id)

    monkeypatch.setattr(orch, "_release_session_slots", _fake_release)
    monkeypatch.setattr(
        worktree_mod, "remove_worktree",
        lambda *a, **k: pytest.fail("close must never remove a worktree"),
    )
    _seed_worktree_session(sidecar, tmp_path)
    assert asyncio.run(orch._close_session_handler("sess-1")) == {"ok": True}
    assert calls == ["sess-1"]
