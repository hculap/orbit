"""Per-session git worktrees — name rules, path math, preflight, spawn target.

A session can opt into running its claude REPL inside a git worktree of the
agent's repo, using claude-cli's own ``--worktree <name>`` flag. This module is
the entire policy layer for that; it is pure + synchronous, imports nothing
from ``orchestrator.py``, and never raises out of the git-backed helpers.

THE INVARIANT
-------------
``meta.cwd`` **stays the agent directory** (the repo root). The worktree lives
in a SEPARATE ``worktree_path`` sidecar field. Rewriting ``cwd`` to the
worktree would break, all at once: the ``?cwd=`` exact-match session filter,
the frontend's ``filteredSessions`` match, ``_lib_id_from_cwd`` (it would
invent a phantom agent named after the worktree), ``artifacts_dir()``,
``prompts_for_session`` and the per-agent skills farm. Only the three runtime
sites that actually spawn/tail a REPL consume the worktree path, and they all
go through :func:`resolve_spawn_target` so they cannot drift apart.

VERIFIED BEHAVIOUR (claude-cli 2.1.220, probed end-to-end before this was
written — the probe drove real REPLs through a throwaway tmux socket):

* ``--worktree <name>`` creates ``<repo-toplevel>/.claude/worktrees/<name>`` on
  branch ``worktree-<name>``, based on ``origin/<default>`` when a remote
  exists and on local ``HEAD`` when it does not.
* The session transcript is written to the **worktree's** project slug
  (``~/.claude/projects/-home-…--claude-worktrees-<name>/<sid>.jsonl``),
  regardless of which directory claude was launched from. This is why
  :class:`SpawnTarget` carries ``transcript_cwd`` separately from ``cwd`` —
  anchoring the JSONL tail on the launch dir makes a turn that SUCCEEDED time
  out after 600 s with no output.
* ``--resume <sid>`` is forgiving about the launch dir: resuming from the repo
  root works and claude re-enters the worktree by itself. So ``cwd`` is a
  cosmetic/ergonomic choice (we spawn inside the worktree so the pane's shell
  cwd is honest), not a correctness one.
* Re-passing ``--worktree`` on an EXISTING worktree can ``git reset --hard``
  it back to the base ref, so the flag is emitted on the cold spawn only.
* claude locks the worktree (``git worktree lock``) and that lock **survives
  process death**. A plain ``git worktree remove`` after a session ends always
  fails with "cannot remove a locked working tree" — hence the stale-lock
  unlock dance in :func:`remove_worktree`.
* A non-repo cwd makes claude exit 1 with "Can only use --worktree in a git
  repository", which is why :func:`preflight`'s is-repo gate is load-bearing:
  most ``~/Areas/*`` are not repos and neither is the Global session's ``$HOME``.
"""
from __future__ import annotations

import os
import random
import re
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import library_git
from . import orchestrator_jsonl_tail as tail_mod

# Mirrors claude-cli's own cap. Kept in sync with static/worktree-name.js by a
# drift test (tests/test_orchestrator_worktree.py).
NAME_MAX = 64
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Names git itself refuses, or that resolve to a path segment with a special
# meaning. Compared case-insensitively.
RESERVED_NAMES = frozenset({".", "..", ".git"})
# Where claude puts them, relative to the repo TOPLEVEL (not the agent dir —
# an agent's cwd may be a subdirectory of the repo).
WORKTREE_SUBDIR = ".claude/worktrees"
BRANCH_PREFIX = "worktree-"

# claude's own lock marker, e.g.
#   "claude session feat-1 (pid 3723518 start 775048268)"
#   "claude agent refactorer (pid 42 start 1)"
# We only ever unlock a lock that matches this AND whose pid is dead.
_CLAUDE_LOCK_RE = re.compile(r"^claude (?:session|agent) .{1,255} \(pid (\d{1,10})[ )]")

# How long to wait for a just-killed claude to actually exit before treating
# its worktree lock as live. See _stale_claude_lock.
_LOCK_WAIT_S = 5.0
_LOCK_POLL_S = 0.1

# Gitignored things a fresh checkout will be missing. Their presence in the
# repo without a `.worktreeinclude` / `worktree.symlinkDirectories` is worth a
# warning — the session will spawn fine and then break at runtime.
_DEP_MARKERS = (".env", ".venv", "node_modules")

# Vocabulary for generated names. Deliberately short + unambiguous so a name
# read off a phone screen can be typed back correctly.
_ADJECTIVES = (
    "swift", "bright", "calm", "keen", "bold", "quiet", "brave", "warm",
    "clear", "quick", "solid", "light",
)
_NOUNS = (
    "fox", "owl", "elm", "oak", "ray", "fern", "lark", "reed",
    "pine", "wren", "moss", "dune",
)
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


# ── names ──────────────────────────────────────────────────────────


def generate_name() -> str:
    """A random ``<adjective>-<noun>-<4 chars>`` name, always NAME_RE-valid."""
    suffix = "".join(random.choice(_SUFFIX_ALPHABET) for _ in range(4))
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{suffix}"


def sanitize_name(raw: str | None) -> str:
    """Validate a user-supplied worktree name; blank generates one.

    Raises ``ValueError`` (caller maps to HTTP 400) with a message safe to show
    the user. Rules mirror claude-cli's, plus two of our own: no ``/`` (claude
    mangles it to ``+`` on disk, which would desync the path we predict) and no
    leading ``-``.
    """
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        return generate_name()
    if len(name) > NAME_MAX:
        raise ValueError(f"worktree name must be at most {NAME_MAX} characters")
    if name.lower() in RESERVED_NAMES:
        raise ValueError(f"worktree name {name!r} is reserved by git")
    if "/" in name:
        raise ValueError("worktree name cannot contain '/'")
    if not NAME_RE.match(name):
        raise ValueError(
            "worktree name must start with a letter or digit and use only "
            "letters, digits, '.', '-' and '_'"
        )
    return name


def branch_for(name: str) -> str:
    """The branch claude-cli creates for this worktree."""
    return f"{BRANCH_PREFIX}{name}"


def worktree_path_for(repo_root: Path, name: str) -> Path:
    """``<repo_root>/.claude/worktrees/<name>``.

    Re-validates the name so this can never become a traversal vector, even if
    a caller forgets to :func:`sanitize_name` first.
    """
    if not isinstance(name, str) or not NAME_RE.match(name) or name.lower() in RESERVED_NAMES:
        raise ValueError(f"invalid worktree name: {name!r}")
    return Path(repo_root) / WORKTREE_SUBDIR / name


# ── git-backed helpers (never raise) ───────────────────────────────


def repo_root_for(cwd: Path) -> Path | None:
    """The git toplevel containing ``cwd``, or None when it isn't a repo.

    Uses the toplevel rather than ``cwd`` itself because an agent directory can
    be a subdirectory of the repo, and claude always places the worktree under
    the toplevel.
    """
    try:
        if not Path(cwd).is_dir():
            return None
    except OSError:
        return None
    rc, out, _ = library_git._git_run(["rev-parse", "--show-toplevel"], Path(cwd))
    if rc != 0:
        return None
    top = out.strip()
    return Path(top) if top else None


def list_worktrees(repo_root: Path) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain``, excluding the main checkout.

    Each entry: ``{name, path, branch, head, locked, lock_reason}``.
    """
    rc, out, _ = library_git._git_run(["worktree", "list", "--porcelain"], Path(repo_root))
    if rc != 0:
        return []

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    # Records are blank-line separated; each starts with a `worktree <path>`.
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = {
                "path": line[len("worktree "):].strip(),
                "branch": None,
                "head": None,
                "locked": False,
                "lock_reason": "",
            }
            entries.append(current)
        elif current is None:
            continue
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().replace("refs/heads/", "", 1)
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
            current["lock_reason"] = line[len("locked "):].strip() if len(line) > 6 else ""

    root = str(Path(repo_root))
    out_entries: list[dict[str, Any]] = []
    for entry in entries:
        # The first record is always the main checkout — drop it, and drop any
        # worktree living outside our own .claude/worktrees dir (someone else's
        # `git worktree add ../foo` is not ours to manage).
        if entry["path"] == root:
            continue
        parent = str(Path(entry["path"]).parent)
        if parent != str(Path(root) / WORKTREE_SUBDIR):
            continue
        entry["name"] = Path(entry["path"]).name
        out_entries.append(entry)
    return out_entries


def _pid_alive(pid: int) -> bool:
    """True when a process with this pid exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _stale_claude_lock(entry: dict[str, Any]) -> bool:
    """True when the entry carries a claude lock whose owning process is gone.

    Bounded wait: the caller releases the session's tmux slot immediately
    before this runs, and the claude process has typically NOT reaped yet — on
    the real box the pid was still alive microseconds later, so every
    "delete + remove worktree" failed with "cannot remove a locked working
    tree". Give it a moment to die before concluding the lock is live. A
    genuinely running session simply outlasts the wait and is left alone.
    """
    if not entry.get("locked"):
        return False
    match = _CLAUDE_LOCK_RE.match(entry.get("lock_reason") or "")
    if not match:
        return False  # foreign / manual lock — never touch it
    pid = int(match.group(1))
    deadline = time.monotonic() + _LOCK_WAIT_S
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_POLL_S)
    return True


def remove_worktree(repo_root: Path, path: Path, *, force: bool = False) -> dict[str, Any]:
    """``git worktree remove`` a session's worktree. Returns ``{ok, error}``.

    Deliberately conservative — deleting a *chat* must never silently discard a
    branch's work:

    * refuses any path outside ``<repo_root>/.claude/worktrees/``;
    * releases claude's STALE lock first (it survives process death and would
      otherwise fail every removal), but never a live or foreign lock;
    * runs without ``--force`` unless explicitly asked, so a worktree with
      modified/untracked files refuses and reports why;
    * leaves the ``worktree-<name>`` branch in place — ``git worktree remove``
      never deletes it, and a stray branch is recoverable while a deleted one
      is not.
    """
    repo_root = Path(repo_root)
    path = Path(path)
    expected_parent = repo_root / WORKTREE_SUBDIR
    if path.parent != expected_parent:
        return {"ok": False, "error": f"refusing to remove {path} — not under {expected_parent}"}

    entry = next((e for e in list_worktrees(repo_root) if e["path"] == str(path)), None)
    if entry is not None and _stale_claude_lock(entry):
        library_git._git_run(["worktree", "unlock", str(path)], repo_root)

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    rc, _, err = library_git._git_run(args, repo_root)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or f"rc={rc}")[:500]}
    return {"ok": True, "error": ""}


# ── preflight ──────────────────────────────────────────────────────


def preflight(agent_cwd: Path, name: str | None, *, reuse: bool = False) -> dict[str, Any]:
    """Can this agent dir host a worktree named ``name``? Never raises.

    Returns ``{ok, is_repo, repo_root, worktree_path, branch, suggested_name,
    existing, errors, warnings}``. ``errors`` block creation; ``warnings`` are
    advisory (shown in the modal) and leave ``ok`` true.

    Note there is deliberately NO check for an ``origin`` remote: a repo
    without one creates the worktree fine off local ``HEAD`` (verified).
    """
    result: dict[str, Any] = {
        "ok": False,
        "is_repo": False,
        "repo_root": None,
        "worktree_path": None,
        "branch": None,
        "suggested_name": None,
        "existing": [],
        "errors": [],
        "warnings": [],
    }

    repo_root = repo_root_for(Path(agent_cwd))
    if repo_root is None:
        result["errors"].append(f"{agent_cwd} is not a git repository")
        return result
    result["is_repo"] = True
    result["repo_root"] = str(repo_root)

    try:
        resolved = sanitize_name(name)
    except ValueError as exc:
        result["suggested_name"] = generate_name()
        result["errors"].append(str(exc))
        return result
    result["suggested_name"] = resolved

    existing = list_worktrees(repo_root)
    result["existing"] = [e["name"] for e in existing]
    if not reuse and resolved in result["existing"]:
        result["errors"].append(f"a worktree named {resolved!r} already exists")
        return result

    result["worktree_path"] = str(worktree_path_for(repo_root, resolved))
    result["branch"] = branch_for(resolved)

    # A worktree is a fresh checkout: gitignored deps do not come along unless
    # the repo opts in. Warn rather than block — the session still starts.
    if not (repo_root / ".worktreeinclude").is_file() and not _has_symlink_dirs(repo_root):
        missing = [m for m in _DEP_MARKERS if (repo_root / m).exists()]
        if missing:
            result["warnings"].append(
                f"{', '.join(missing)} won't exist in a fresh worktree — add a "
                f".worktreeinclude file or worktree.symlinkDirectories in "
                f".claude/settings.json"
            )

    result["ok"] = True
    return result


def _has_symlink_dirs(repo_root: Path) -> bool:
    """True when the repo configures ``worktree.symlinkDirectories``."""
    settings = repo_root / ".claude" / "settings.json"
    try:
        import json

        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    worktree_cfg = data.get("worktree") if isinstance(data, dict) else None
    if not isinstance(worktree_cfg, dict):
        return False
    return bool(worktree_cfg.get("symlinkDirectories"))


# ── spawn target ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SpawnTarget:
    """Everything the spawn path needs, resolved together so it cannot drift.

    ``cwd`` is what goes into ``tmux new-session -c``; ``worktree_flag`` is the
    ``--worktree=<name>`` argument (cold spawn only); ``resume`` picks
    ``--resume`` over ``--session-id``; and ``transcript_cwd`` is what the
    JSONL slug must be keyed on — which is NOT always ``cwd``.
    """

    cwd: Path
    worktree_flag: str | None
    resume: bool
    transcript_cwd: Path
    worktree_missing: bool = False


def _transcript_exists(cwd: Path, session_id: str) -> bool:
    try:
        return tail_mod.jsonl_path_for(cwd, session_id).is_file()
    except OSError:
        return False


def resolve_spawn_target(sidecar: dict[str, Any], session_id: str) -> SpawnTarget:
    """Resolve (cwd, --worktree, --resume, transcript slug) for one spawn.

    The single decision point for the worktree feature. Four cases:

    1. No worktree on the session → today's behaviour, byte for byte.
    2. The worktree dir exists → spawn inside it (honest pane cwd), no flag
       (re-passing it can ``git reset --hard`` the branch).
    3. A worktree is recorded but the transcript already lives under the AGENT
       dir (worktree attached to an already-run session) → follow the
       transcript, so one session never ends up with two JSONLs under two slugs
       (``read_session`` unions them and the UI interleaves two conversations).
    4. Cold create → launch from the repo root with ``--worktree=<name>`` and
       let claude do the chdir; the transcript still lands in the worktree slug.
    """
    cwd_str = sidecar.get("cwd") if isinstance(sidecar, dict) else None
    agent_cwd = Path(cwd_str) if isinstance(cwd_str, str) and cwd_str else Path.home()

    wt_str = sidecar.get("worktree_path") if isinstance(sidecar, dict) else None
    wt_name = sidecar.get("worktree_name") if isinstance(sidecar, dict) else None
    if not (isinstance(wt_str, str) and wt_str) or not (isinstance(wt_name, str) and wt_name):
        return SpawnTarget(
            cwd=agent_cwd,
            worktree_flag=None,
            resume=_transcript_exists(agent_cwd, session_id),
            transcript_cwd=agent_cwd,
        )

    worktree = Path(wt_str)
    if worktree.is_dir():
        return SpawnTarget(
            cwd=worktree,
            worktree_flag=None,
            resume=_transcript_exists(worktree, session_id),
            transcript_cwd=worktree,
        )

    # The dir is gone (or was never created). If the session already ran in the
    # agent dir, stay there rather than splitting the transcript.
    if _transcript_exists(agent_cwd, session_id):
        return SpawnTarget(
            cwd=agent_cwd,
            worktree_flag=None,
            resume=True,
            transcript_cwd=agent_cwd,
        )

    # A transcript under the worktree slug with no dir means the worktree was
    # removed out of band. Do NOT silently recreate it: a fresh checkout under
    # a transcript that "remembers" edits makes the model contradict itself.
    # Surface it and let the user decide (POST .../worktree/recreate).
    if _transcript_exists(worktree, session_id):
        return SpawnTarget(
            cwd=agent_cwd,
            worktree_flag=None,
            resume=False,
            transcript_cwd=worktree,
            worktree_missing=True,
        )

    return SpawnTarget(
        cwd=agent_cwd,
        worktree_flag=wt_name,
        resume=False,
        transcript_cwd=worktree,
    )
