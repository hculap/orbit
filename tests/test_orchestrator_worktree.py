"""Unit tests for :mod:`orbit.orchestrator_worktree`.

Everything here is either pure (name rules, path math) or drives the module
through a monkeypatched ``library_git._git_run``, so no test forks a real
``git`` and no test touches a real worktree. The one exception is the
``resolve_spawn_target`` group, which uses tmp_path to make real dirs/files
because the resolver's whole job is answering "does this exist?".

Behaviour pinned here was verified end-to-end against claude-cli 2.1.220 in a
throwaway tmux session before the code was written (see the plan's "Ground
truth"); the comments call out which assertions encode a probe result.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from orbit import orchestrator_worktree as wt


# ── name rules ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["a", "A1", "feat-x", "v1.2.3", "a_b", "a" * 64])
def test_sanitize_name_accepts_valid(name: str) -> None:
    assert wt.sanitize_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "-x",  # leading dash — our own extra rule
        ".x",
        "_x",
        "feat/x",  # our own extra rule: keeps the on-disk path predictable
        "ma-ły",  # Polish diacritic — a very live risk for this user
        "a b",
        "a+b",
        "a" * 65,
        ".",
        "..",
        ".git",
        ".GIT",
    ],
)
def test_sanitize_name_rejects_invalid(name: str) -> None:
    with pytest.raises(ValueError):
        wt.sanitize_name(name)


def test_sanitize_name_trims_surrounding_whitespace() -> None:
    assert wt.sanitize_name("  feat-x  ") == "feat-x"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_sanitize_name_blank_generates_a_valid_name(blank: str | None) -> None:
    generated = wt.sanitize_name(blank)
    assert generated
    # A generated name must itself survive validation, or "leave it blank"
    # would produce a name the server then rejects.
    assert wt.sanitize_name(generated) == generated


def test_generate_name_shape_and_variety() -> None:
    names = {wt.generate_name() for _ in range(50)}
    # Collisions are possible but 50 draws landing on <10 distinct names would
    # mean the suffix isn't doing its job.
    assert len(names) > 10
    for name in names:
        assert wt.NAME_RE.match(name), name
        assert name.count("-") == 2, name  # adjective-noun-suffix


def test_name_regex_matches_the_frontend_literal() -> None:
    """Drift guard: worktree-name.js and this module must share one rule.

    A client/server split here means the user gets a green form and a 4xx.
    """
    js = (
        Path(__file__).resolve().parents[1]
        / "src/orbit/static/worktree-name.js"
    ).read_text(encoding="utf-8")
    m = re.search(r"const NAME_RE = /(.+?)/;", js)
    assert m, "NAME_RE literal not found in worktree-name.js"
    assert m.group(1) == wt.NAME_RE.pattern


def test_max_len_matches_the_frontend_literal() -> None:
    js = (
        Path(__file__).resolve().parents[1]
        / "src/orbit/static/worktree-name.js"
    ).read_text(encoding="utf-8")
    assert re.search(r"const MAX_LEN = (\d+);", js).group(1) == str(wt.NAME_MAX)


# ── path math ──────────────────────────────────────────────────────


def test_worktree_path_and_branch_mirror_claude_cli() -> None:
    """Pinned by probe 1: <repo>/.claude/worktrees/<name>, branch worktree-<name>."""
    repo = Path("/home/u/Projects/x")
    assert wt.worktree_path_for(repo, "feat-1") == repo / ".claude/worktrees/feat-1"
    assert wt.branch_for("feat-1") == "worktree-feat-1"


def test_worktree_path_rejects_an_invalid_name() -> None:
    # Defense in depth: the path helper must not be a traversal vector even if
    # a caller forgets to sanitize first.
    with pytest.raises(ValueError):
        wt.worktree_path_for(Path("/home/u/Projects/x"), "../../etc")


# ── git-backed helpers (monkeypatched _git_run) ────────────────────


@pytest.fixture()
def fake_git(monkeypatch):
    """Record git invocations and replay canned (rc, stdout, stderr)."""
    calls: list[list[str]] = []
    responses: dict[str, tuple[int, str, str]] = {}

    def _run(args, cwd, timeout_s=30.0):
        calls.append(list(args))
        for key, resp in responses.items():
            if key in " ".join(args):
                return resp
        return (0, "", "")

    monkeypatch.setattr(wt.library_git, "_git_run", _run)
    return type("FakeGit", (), {"calls": calls, "responses": responses})()


WORKTREE_LIST = """worktree /home/u/Projects/x
HEAD abc
branch refs/heads/main

worktree /home/u/Projects/x/.claude/worktrees/feat-1
HEAD def
branch refs/heads/worktree-feat-1
locked claude session feat-1 (pid 4242 start 775048268)

worktree /home/u/Projects/x/.claude/worktrees/feat-2
HEAD ghi
branch refs/heads/worktree-feat-2
"""


def test_list_worktrees_parses_porcelain(fake_git) -> None:
    fake_git.responses["worktree list"] = (0, WORKTREE_LIST, "")
    entries = wt.list_worktrees(Path("/home/u/Projects/x"))
    assert [e["name"] for e in entries] == ["feat-1", "feat-2"]  # main checkout excluded
    assert entries[0]["branch"] == "worktree-feat-1"
    assert entries[0]["locked"] is True
    assert entries[0]["lock_reason"].startswith("claude session")
    assert entries[1]["locked"] is False


def test_repo_root_for_uses_show_toplevel(fake_git, tmp_path) -> None:
    """An agent dir may be a SUBDIR of the repo; claude uses the toplevel."""
    sub = tmp_path / "sub"
    sub.mkdir()
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    assert wt.repo_root_for(sub) == tmp_path


def test_repo_root_for_returns_none_outside_a_repo(fake_git, tmp_path) -> None:
    fake_git.responses["rev-parse --show-toplevel"] = (128, "", "not a git repository")
    assert wt.repo_root_for(tmp_path) is None


def test_repo_root_for_returns_none_for_a_missing_dir(fake_git, tmp_path) -> None:
    """Guard before forking: _git_run does not catch subprocess's
    FileNotFoundError for a non-existent cwd, so it would escape uncaught."""
    assert wt.repo_root_for(tmp_path / "does-not-exist") is None
    assert fake_git.calls == []


# ── remove_worktree ────────────────────────────────────────────────


def test_remove_worktree_unlocks_a_stale_claude_lock_first(fake_git, monkeypatch) -> None:
    """Probe 6: claude's lock survives process death and blocks `remove`.

    Without the unlock step, deleting a session's worktree would ALWAYS fail.
    """
    fake_git.responses["worktree list"] = (0, WORKTREE_LIST, "")
    monkeypatch.setattr(wt, "_pid_alive", lambda pid: False)

    result = wt.remove_worktree(
        Path("/home/u/Projects/x"), Path("/home/u/Projects/x/.claude/worktrees/feat-1")
    )

    assert result["ok"] is True
    joined = [" ".join(c) for c in fake_git.calls]
    assert any(c.startswith("worktree unlock") for c in joined), joined
    assert any(c.startswith("worktree remove") for c in joined), joined
    # Never force: dirty/untracked must still refuse (probe 7).
    assert not any("--force" in c for c in joined), joined


def test_remove_worktree_waits_briefly_for_a_dying_claude(fake_git, monkeypatch) -> None:
    """UAT: deleting a session releases its tmux slot, but the claude process
    has NOT reaped by the time removal runs — so the lock still looks LIVE and
    every "delete + remove worktree" failed with "cannot remove a locked
    working tree". Observed on the real box with pid 3743272 still alive
    microseconds after the slot was released.

    So a claude-owned lock whose pid is still alive gets a short bounded wait
    before we conclude it's genuinely in use.
    """
    fake_git.responses["worktree list"] = (0, WORKTREE_LIST, "")
    alive = iter([True, True, False, False, False, False])
    monkeypatch.setattr(wt, "_pid_alive", lambda pid: next(alive, False))
    monkeypatch.setattr(wt, "_LOCK_WAIT_S", 1.0)
    monkeypatch.setattr(wt, "_LOCK_POLL_S", 0.01)

    result = wt.remove_worktree(
        Path("/home/u/Projects/x"), Path("/home/u/Projects/x/.claude/worktrees/feat-1")
    )

    assert result["ok"] is True
    joined = [" ".join(c) for c in fake_git.calls]
    assert any(c.startswith("worktree unlock") for c in joined), joined


def test_remove_worktree_leaves_a_live_lock_alone(fake_git, monkeypatch) -> None:
    fake_git.responses["worktree list"] = (0, WORKTREE_LIST, "")
    fake_git.responses["worktree remove"] = (128, "", "cannot remove a locked working tree")
    monkeypatch.setattr(wt, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(wt, "_LOCK_WAIT_S", 0.05)
    monkeypatch.setattr(wt, "_LOCK_POLL_S", 0.01)

    result = wt.remove_worktree(
        Path("/home/u/Projects/x"), Path("/home/u/Projects/x/.claude/worktrees/feat-1")
    )

    assert result["ok"] is False
    assert not any(" ".join(c).startswith("worktree unlock") for c in fake_git.calls)


def test_remove_worktree_leaves_a_foreign_lock_alone(fake_git, monkeypatch) -> None:
    foreign = WORKTREE_LIST.replace(
        "locked claude session feat-1 (pid 4242 start 775048268)", "locked manual hold"
    )
    fake_git.responses["worktree list"] = (0, foreign, "")
    fake_git.responses["worktree remove"] = (128, "", "cannot remove a locked working tree")

    result = wt.remove_worktree(
        Path("/home/u/Projects/x"), Path("/home/u/Projects/x/.claude/worktrees/feat-1")
    )

    assert result["ok"] is False
    assert not any(" ".join(c).startswith("worktree unlock") for c in fake_git.calls)


def test_remove_worktree_reports_failure_instead_of_raising(fake_git) -> None:
    fake_git.responses["worktree list"] = (0, WORKTREE_LIST, "")
    fake_git.responses["worktree remove"] = (
        128,
        "",
        "contains modified or untracked files, use --force to delete it",
    )
    result = wt.remove_worktree(
        Path("/home/u/Projects/x"), Path("/home/u/Projects/x/.claude/worktrees/feat-2")
    )
    assert result["ok"] is False
    assert "untracked" in result["error"]


def test_remove_worktree_refuses_a_path_outside_the_repos_worktree_dir(fake_git) -> None:
    result = wt.remove_worktree(Path("/home/u/Projects/x"), Path("/home/u/Projects/x"))
    assert result["ok"] is False
    assert not any("worktree remove" in " ".join(c) for c in fake_git.calls)


# ── preflight ──────────────────────────────────────────────────────


def test_preflight_errors_when_not_a_repo(fake_git, tmp_path) -> None:
    """Probe 9: claude hard-exits with "Can only use --worktree in a git repository"."""
    fake_git.responses["rev-parse --show-toplevel"] = (128, "", "not a git repository")
    result = wt.preflight(tmp_path, "feat-1")
    assert result["ok"] is False
    assert result["is_repo"] is False
    assert result["errors"]


def test_preflight_ok_without_an_origin_remote(fake_git, tmp_path) -> None:
    """Probe 8: a remote-less repo creates the worktree fine off local HEAD.

    The original plan wanted to hard-fail here; the probe proved that wrong.
    """
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    fake_git.responses["worktree list"] = (0, f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/main\n", "")
    fake_git.responses["remote get-url"] = (128, "", "No such remote 'origin'")
    result = wt.preflight(tmp_path, "feat-1")
    assert result["ok"] is True
    assert not result["errors"]


def test_preflight_reports_name_branch_and_existing(fake_git, tmp_path) -> None:
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    fake_git.responses["worktree list"] = (
        0,
        f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/main\n\n"
        f"worktree {tmp_path}/.claude/worktrees/other\nHEAD def\nbranch refs/heads/worktree-other\n",
        "",
    )
    result = wt.preflight(tmp_path, "feat-1")
    assert result["ok"] is True
    assert result["repo_root"] == str(tmp_path)
    assert result["worktree_path"] == str(tmp_path / ".claude/worktrees/feat-1")
    assert result["branch"] == "worktree-feat-1"
    assert result["existing"] == ["other"]


def test_preflight_errors_on_a_name_already_registered(fake_git, tmp_path) -> None:
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    fake_git.responses["worktree list"] = (
        0,
        f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/main\n\n"
        f"worktree {tmp_path}/.claude/worktrees/feat-1\nHEAD def\nbranch refs/heads/worktree-feat-1\n",
        "",
    )
    assert wt.preflight(tmp_path, "feat-1")["ok"] is False
    # ...unless the caller explicitly opts into reuse.
    assert wt.preflight(tmp_path, "feat-1", reuse=True)["ok"] is True


def test_preflight_errors_on_an_invalid_name(fake_git, tmp_path) -> None:
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    result = wt.preflight(tmp_path, "feat/x")
    assert result["ok"] is False
    assert any("/" in e for e in result["errors"])


def test_preflight_suggests_a_name_when_blank(fake_git, tmp_path) -> None:
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    result = wt.preflight(tmp_path, None)
    assert result["ok"] is True
    assert wt.NAME_RE.match(result["suggested_name"])


def test_preflight_warns_when_gitignored_deps_would_be_missing(fake_git, tmp_path) -> None:
    """A fresh checkout has no .env/.venv — warn so other repos self-announce."""
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    result = wt.preflight(tmp_path, "feat-1")
    assert result["ok"] is True
    assert result["warnings"]
    assert any(".worktreeinclude" in w for w in result["warnings"])


def test_preflight_no_warning_when_worktreeinclude_exists(fake_git, tmp_path) -> None:
    fake_git.responses["rev-parse --show-toplevel"] = (0, f"{tmp_path}\n", "")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
    assert wt.preflight(tmp_path, "feat-1")["warnings"] == []


# ── resolve_spawn_target ───────────────────────────────────────────


SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def jsonl_home(monkeypatch, tmp_path):
    """Point the slug helper at a tmp ~/.claude so transcripts can be faked."""
    claude_home = tmp_path / "dot-claude"
    monkeypatch.setattr(wt.tail_mod, "_CLAUDE_HOME", claude_home)

    def _touch(cwd: Path) -> Path:
        path = wt.tail_mod.jsonl_path_for(cwd, SID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return path

    return _touch


def test_resolve_no_worktree_is_todays_behaviour(jsonl_home, tmp_path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    target = wt.resolve_spawn_target({"cwd": str(agent)}, SID)
    assert (target.cwd, target.worktree_flag, target.resume) == (agent, None, False)
    assert target.transcript_cwd == agent

    jsonl_home(agent)
    assert wt.resolve_spawn_target({"cwd": str(agent)}, SID).resume is True


def test_resolve_cold_create_launches_from_the_repo_with_the_flag(jsonl_home, tmp_path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    sidecar = {
        "cwd": str(agent),
        "worktree_name": "feat-1",
        "worktree_path": str(agent / ".claude/worktrees/feat-1"),
    }
    target = wt.resolve_spawn_target(sidecar, SID)
    # The worktree does not exist yet, so claude must create it — launch from
    # the repo root and let --worktree do the chdir.
    assert target.cwd == agent
    assert target.worktree_flag == "feat-1"
    assert target.resume is False
    # ...but the transcript will land in the WORKTREE slug (probe 2). Getting
    # this wrong is the silent 600 s turn timeout.
    assert target.transcript_cwd == agent / ".claude/worktrees/feat-1"


def test_resolve_steady_state_spawns_inside_the_worktree(jsonl_home, tmp_path) -> None:
    agent = tmp_path / "agent"
    worktree = agent / ".claude/worktrees/feat-1"
    worktree.mkdir(parents=True)
    jsonl_home(worktree)
    sidecar = {
        "cwd": str(agent),
        "worktree_name": "feat-1",
        "worktree_path": str(worktree),
    }
    target = wt.resolve_spawn_target(sidecar, SID)
    assert target.cwd == worktree
    assert target.worktree_flag is None  # never re-pass: it can reset the branch
    assert target.resume is True
    assert target.transcript_cwd == worktree


def test_resolve_worktree_exists_but_no_transcript_yet(jsonl_home, tmp_path) -> None:
    agent = tmp_path / "agent"
    worktree = agent / ".claude/worktrees/feat-1"
    worktree.mkdir(parents=True)
    sidecar = {
        "cwd": str(agent),
        "worktree_name": "feat-1",
        "worktree_path": str(worktree),
    }
    target = wt.resolve_spawn_target(sidecar, SID)
    assert (target.cwd, target.worktree_flag, target.resume) == (worktree, None, False)


def test_resolve_follows_an_existing_repo_root_transcript(jsonl_home, tmp_path) -> None:
    """A worktree attached to an already-run session must not split the transcript."""
    agent = tmp_path / "agent"
    agent.mkdir()
    jsonl_home(agent)
    sidecar = {
        "cwd": str(agent),
        "worktree_name": "feat-1",
        "worktree_path": str(agent / ".claude/worktrees/feat-1"),
    }
    target = wt.resolve_spawn_target(sidecar, SID)
    assert target.cwd == agent
    assert target.worktree_flag is None
    assert target.resume is True
    assert target.transcript_cwd == agent


def test_resolve_missing_flag_when_the_worktree_vanished(jsonl_home, tmp_path) -> None:
    """Never silently recreate: a fresh checkout under an edit-remembering
    transcript makes the model contradict itself."""
    agent = tmp_path / "agent"
    agent.mkdir()
    worktree = agent / ".claude/worktrees/feat-1"
    jsonl_home(worktree)  # transcript exists, dir does not
    sidecar = {
        "cwd": str(agent),
        "worktree_name": "feat-1",
        "worktree_path": str(worktree),
    }
    target = wt.resolve_spawn_target(sidecar, SID)
    assert target.worktree_missing is True


def test_resolve_defaults_to_home_when_the_sidecar_has_no_cwd(jsonl_home) -> None:
    target = wt.resolve_spawn_target({}, SID)
    assert target.cwd == Path.home()
    assert target.worktree_flag is None
