"""Git ops for the Areas/Projects library.

All ops shell out to ``/usr/bin/git`` via ``subprocess`` / ``asyncio``; no
python git library dep. Hardening for clone-from-url is critical: remote
repos can include ``.git/hooks/*`` that run on checkout / merge / pull, so
we always pass ``-c core.hooksPath=/dev/null`` to neutralise hooks both
during clone and any subsequent op we run.

URL allowlist for clone: only HTTPS GitHub/GitLab and the canonical SSH
shortcut form (``git@host:owner/repo``). No ``file://``, no ``ext::``,
no ``ssh://`` to arbitrary hosts.

Refer to ``library.py`` for ``_safe_area_path`` / ``_safe_project_path``;
those resolve the cwd of every git subprocess we run here.
"""
from __future__ import annotations
import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIT_BIN = "/usr/bin/git"
CLONE_TIMEOUT_S = 90.0
DEFAULT_TIMEOUT_S = 30.0
PUSH_TIMEOUT_S = 60.0
MAX_CLONE_BYTES = 500 * 1024 * 1024  # 500 MB post-clone size cap
MAX_CLONE_FILES = 50_000
DEFAULT_DOWNLOAD_BYTES_MAX = 50 * 1024 * 1024  # mirror of library_files.MAX_DOWNLOAD_BYTES

# Reasonable bounds on user-supplied PR fields.
PR_TITLE_MAX = 256
PR_BASE_MAX = 200
COMMIT_MSG_MAX = 4096

# Defensive global args: neutralise repo-local hooks for every op we run.
_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")

_URL_RE = re.compile(
    r"^(?:"
    r"https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?/?|"
    r"https://gitlab\.com/[\w.-]+/[\w.-]+(?:\.git)?/?|"
    r"git@github\.com:[\w.-]+/[\w.-]+(?:\.git)?|"
    r"git@gitlab\.com:[\w.-]+/[\w.-]+(?:\.git)?"
    r")$"
)

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def validate_clone_url(url: str) -> str:
    """Return ``url`` if it matches the allowlist; raise ``ValueError`` otherwise."""
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    url = url.strip()
    if not url:
        raise ValueError("url required")
    if not _URL_RE.match(url):
        raise ValueError(
            "url not allowed (only https://github.com/.. or https://gitlab.com/.. "
            "or git@github.com:owner/repo are accepted)"
        )
    return url


def _validate_branch(branch: str) -> str:
    """Allow ``[A-Za-z0-9._/-]`` only — no whitespace, no shell meta."""
    if not isinstance(branch, str):
        raise ValueError("branch must be a string")
    branch = branch.strip()
    if not branch:
        raise ValueError("branch required")
    if not _BRANCH_RE.match(branch):
        raise ValueError(f"invalid branch name: {branch!r}")
    if branch.startswith("-"):
        raise ValueError("branch cannot start with '-'")
    return branch


def _git_env() -> dict[str, str]:
    """Subprocess env: drop GIT_DIR-style overrides, disable prompts."""
    blocked = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE"}
    e = {k: v for k, v in os.environ.items() if k not in blocked}
    e["GIT_TERMINAL_PROMPT"] = "0"
    e["GIT_ASKPASS"] = "/bin/echo"
    e["SSH_ASKPASS"] = "/bin/echo"
    e["LC_ALL"] = "C"
    return e


def _git_run(
    args: list[str],
    cwd: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, str, str]:
    """Run git synchronously. Returns ``(returncode, stdout, stderr)``.

    All invocations are prefixed with ``-c core.hooksPath=/dev/null``.
    """
    cmd = [GIT_BIN, *_NO_HOOKS, *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout after {timeout_s}s: {e}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ── status / branches ────────────────────────────────────────────


def _is_repo(path: Path) -> bool:
    rc, out, _ = _git_run(["rev-parse", "--is-inside-work-tree"], path)
    return rc == 0 and out.strip() == "true"


def _parse_branch_status(stdout: str) -> tuple[str | None, int, int, bool]:
    """Parse ``git status --porcelain=v2 --branch`` output.

    Returns ``(branch, ahead, behind, dirty)``. ``dirty`` is True if any
    non-header line exists.
    """
    branch: str | None = None
    ahead = 0
    behind = 0
    dirty = False
    for raw in stdout.splitlines():
        if not raw:
            continue
        if raw.startswith("# branch.head "):
            val = raw[len("# branch.head "):].strip()
            branch = None if val == "(detached)" else val
        elif raw.startswith("# branch.ab "):
            # format: "# branch.ab +N -M"
            parts = raw[len("# branch.ab "):].split()
            for p in parts:
                if p.startswith("+"):
                    try:
                        ahead = int(p[1:])
                    except ValueError:
                        pass
                elif p.startswith("-"):
                    try:
                        behind = int(p[1:])
                    except ValueError:
                        pass
        elif raw.startswith("#"):
            continue
        else:
            dirty = True
    return branch, ahead, behind, dirty


def git_branches_full(path: Path) -> list[str]:
    """Local + remote-tracking branch names, with ``origin/`` stripped, deduped."""
    rc, out, _ = _git_run(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes"],
        path,
    )
    if rc != 0:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for raw in out.splitlines():
        name = raw.strip()
        if not name:
            continue
        if name.startswith("origin/"):
            short = name[len("origin/"):]
            if short == "HEAD":
                continue
            name = short
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def git_status(path: Path) -> dict[str, Any]:
    """Full library-shaped status for one item dir."""
    if not path.is_dir():
        return {"is_repo": False, "branch": None, "branches": [],
                "remote_url": None, "dirty": False, "ahead": 0, "behind": 0}
    if not _is_repo(path):
        return {"is_repo": False, "branch": None, "branches": [],
                "remote_url": None, "dirty": False, "ahead": 0, "behind": 0}

    rc, out, _ = _git_run(["status", "--porcelain=v2", "--branch"], path)
    if rc != 0:
        return {"is_repo": True, "branch": None, "branches": [],
                "remote_url": None, "dirty": False, "ahead": 0, "behind": 0}
    branch, ahead, behind, dirty = _parse_branch_status(out)

    branches = git_branches_full(path)

    remote_url: str | None = None
    rc, out, _ = _git_run(["remote", "get-url", "origin"], path)
    if rc == 0:
        remote_url = out.strip() or None

    # Upstream tracking — rc=0 if @{u} resolves to anything (e.g. origin/main).
    upstream: str | None = None
    rc, out, _ = _git_run(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], path,
    )
    if rc == 0:
        upstream = out.strip() or None

    return {
        "is_repo": True,
        "branch": branch,
        "branches": branches,
        "remote_url": remote_url,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "upstream": upstream,
        "has_upstream": upstream is not None,
    }


# ── init / checkout ──────────────────────────────────────────────


def git_init(path: Path) -> dict[str, Any]:
    """``git init -q`` if ``.git/`` doesn't exist."""
    if not path.is_dir():
        raise FileNotFoundError(f"not a directory: {path}")
    if (path / ".git").exists():
        return {"ok": True, "already_initialized": True}
    rc, _, err = _git_run(["init", "-q"], path)
    if rc != 0:
        raise RuntimeError(f"git init failed: {err.strip() or rc}")
    return {"ok": True, "already_initialized": False}


def git_checkout(
    path: Path,
    branch: str,
    *,
    create: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Switch to ``branch``, optionally creating it. Refuses dirty unless ``force``."""
    if not path.is_dir():
        raise FileNotFoundError(f"not a directory: {path}")
    if not _is_repo(path):
        raise ValueError("not a git repo")
    branch = _validate_branch(branch)

    # Refuse on dirty unless force
    if not force:
        rc, out, _ = _git_run(["status", "--porcelain"], path)
        if rc == 0 and out.strip():
            raise FileExistsError("working tree dirty; pass force=true to override")

    args: list[str] = ["switch"]
    if create:
        args.append("-c")
    if force:
        args.append("-f")
    args.append(branch)

    rc, _, err = _git_run(args, path)
    if rc != 0:
        msg = err.strip() or f"git switch failed (rc={rc})"
        # Detect "already exists" as 409
        if "already exists" in msg.lower():
            raise FileExistsError(msg)
        raise ValueError(msg)
    return {"ok": True, "branch": branch}


# ── clone (async) ───────────────────────────────────────────────


def _post_clone_check(dest: Path) -> tuple[int, int]:
    """Walk ``dest`` to count files + bytes. Skips ``.git/objects/pack``."""
    file_count = 0
    total_bytes = 0
    skip_prefix = (dest / ".git" / "objects" / "pack").resolve()
    for root, dirs, files in os.walk(dest):
        # Skip the pack dir entirely (pack files dominate size, not user content)
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            continue
        if root_resolved == skip_prefix or skip_prefix in root_resolved.parents:
            continue
        for fname in files:
            file_count += 1
            try:
                total_bytes += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
            if file_count > MAX_CLONE_FILES or total_bytes > MAX_CLONE_BYTES:
                return file_count, total_bytes
    return file_count, total_bytes


async def git_clone_async(url: str, dest: Path) -> dict[str, Any]:
    """Clone a validated URL into ``dest``. ``dest`` must NOT exist.

    Hardening:
    - URL pre-validated by ``validate_clone_url``.
    - ``-c core.hooksPath=/dev/null`` to neutralise hooks during clone.
    - ``--no-local --depth=1 --no-tags --single-branch --no-hardlinks``.
    - 90s timeout. ``GIT_TERMINAL_PROMPT=0``, ``GIT_ASKPASS=/bin/echo``.
    - Post-clone size + file-count check; on overflow, ``rmtree(dest)``.
    - Any failure → ``rmtree(dest)`` (best-effort) before re-raising.
    """
    url = validate_clone_url(url)
    if dest.exists():
        raise FileExistsError(f"destination exists: {dest}")
    parent = dest.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"parent dir missing: {parent}")

    cmd = [
        GIT_BIN, *_NO_HOOKS,
        "clone",
        "--no-local", "--depth=1", "--no-tags",
        "--single-branch", "--no-hardlinks",
        "--", url, str(dest),
    ]

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(parent),
            env=_git_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=CLONE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.sleep(0)  # let kill propagate
            _safe_rmtree(dest)
            raise ValueError(f"clone timed out after {CLONE_TIMEOUT_S}s")

        if proc.returncode != 0:
            err = (stderr_b or b"").decode("utf-8", errors="replace").strip()
            _safe_rmtree(dest)
            raise ValueError(f"git clone failed (rc={proc.returncode}): {err}")
    except Exception:
        _safe_rmtree(dest)
        raise

    # Post-clone size guard
    file_count, total_bytes = _post_clone_check(dest)
    if file_count > MAX_CLONE_FILES or total_bytes > MAX_CLONE_BYTES:
        _safe_rmtree(dest)
        raise ValueError(
            f"cloned repo exceeds caps (files={file_count}/{MAX_CLONE_FILES}, "
            f"bytes={total_bytes}/{MAX_CLONE_BYTES})"
        )

    # Read back branch + remote
    branch: str | None = None
    rc, out, _ = _git_run(["symbolic-ref", "--short", "HEAD"], dest)
    if rc == 0:
        branch = out.strip() or None
    remote_url: str | None = None
    rc, out, _ = _git_run(["remote", "get-url", "origin"], dest)
    if rc == 0:
        remote_url = out.strip() or None

    return {
        "ok": True,
        "branch": branch,
        "remote_url": remote_url,
        "files": file_count,
        "bytes": total_bytes,
    }


def _safe_rmtree(p: Path) -> None:
    """Best-effort removal of ``p``. Never raises."""
    try:
        if p.is_symlink() or p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


# ── github metadata helpers ─────────────────────────────────────


_GH_PARSE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


def parse_github_owner_repo(url: str) -> dict[str, str] | None:
    """Extract ``{owner, repo}`` from a GitHub URL; ``None`` for non-GitHub URLs."""
    if not isinstance(url, str):
        return None
    m = _GH_PARSE_RE.match(url.strip())
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2)}


# ── attach remote / commit / push / status / pr ──────────────────


def _current_branch(path: Path) -> str | None:
    """Return current branch short name or ``None`` (detached HEAD / not repo)."""
    rc, out, _ = _git_run(["symbolic-ref", "--short", "HEAD"], path)
    if rc != 0:
        return None
    name = out.strip()
    return name or None


def attach_remote(path: Path, url: str, *, fetch: bool = False) -> dict[str, Any]:
    """Attach (or replace) ``origin`` remote on the repo at ``path``.

    Auto-runs ``git init -q`` first if ``.git/`` does not exist.
    URL is validated by :func:`validate_clone_url`.

    Behaviour:
    - existing origin same URL → no-op
    - existing origin different URL → ``git remote set-url origin <url>``
    - no origin → ``git remote add origin <url>``
    - ``fetch=True`` → best-effort ``git fetch origin --depth=1 --no-tags``
      (failures captured but don't propagate)

    Returns ``{ok, branch, remote_url, action, fetched, fetch_error}``.
    """
    if not path.is_dir():
        raise FileNotFoundError(f"not a directory: {path}")
    url = validate_clone_url(url)

    if not (path / ".git").exists():
        git_init(path)

    rc, out, _ = _git_run(["remote", "get-url", "origin"], path)
    existing = out.strip() if rc == 0 else ""
    if rc == 0 and existing == url:
        action = "noop"
    elif rc == 0 and existing:
        rc2, _, err2 = _git_run(["remote", "set-url", "origin", url], path)
        if rc2 != 0:
            raise RuntimeError(f"git remote set-url failed: {err2.strip() or rc2}")
        action = "replaced"
    else:
        rc2, _, err2 = _git_run(["remote", "add", "origin", url], path)
        if rc2 != 0:
            raise RuntimeError(f"git remote add failed: {err2.strip() or rc2}")
        action = "added"

    fetched = False
    fetch_error: str | None = None
    if fetch:
        rc3, _, err3 = _git_run(
            ["fetch", "origin", "--depth=1", "--no-tags"],
            path,
            timeout_s=DEFAULT_TIMEOUT_S,
        )
        if rc3 == 0:
            fetched = True
        else:
            fetch_error = (err3 or "").strip()[:500] or f"fetch rc={rc3}"

    return {
        "ok": True,
        "branch": _current_branch(path),
        "remote_url": url,
        "action": action,
        "fetched": fetched,
        "fetch_error": fetch_error,
    }


_CTRL_ONLY_RE = re.compile(r"^[\x00-\x1f\x7f\s]+$")


def commit_all(path: Path, message: str) -> dict[str, Any]:
    """Stage all changes (``add -A``) and commit with ``message``.

    Returns ``{ok: True, sha, files_changed}`` on success;
    ``{ok: False, error}`` on failure. The string ``"nothing to commit"``
    is mapped to a clean error envelope so the route can return 400.
    """
    if not path.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    if not _is_repo(path):
        return {"ok": False, "error": "not a git repo"}

    if not isinstance(message, str):
        return {"ok": False, "error": "message must be a string"}
    msg = message.strip()
    if not msg:
        return {"ok": False, "error": "commit message required"}
    if _CTRL_ONLY_RE.match(message):
        return {"ok": False, "error": "commit message cannot be only control chars"}
    if len(msg) > COMMIT_MSG_MAX:
        return {"ok": False, "error": f"commit message too long (>{COMMIT_MSG_MAX})"}

    rc, _out, err = _git_run(["add", "-A"], path)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or f"git add rc={rc}")[:500]}

    rc, out, err = _git_run(["commit", "-m", msg], path)
    if rc != 0:
        combined = (out + "\n" + err).lower()
        if "nothing to commit" in combined:
            return {"ok": False, "error": "nothing to commit"}
        return {"ok": False, "error": (err.strip() or out.strip() or f"git commit rc={rc}")[:500]}

    rc2, sha_out, _ = _git_run(["rev-parse", "HEAD"], path)
    sha = sha_out.strip() if rc2 == 0 else ""

    files_changed = 0
    # `--root` makes diff-tree emit the file list for the initial commit too.
    rc3, diff_out, _ = _git_run(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha or "HEAD"], path,
    )
    if rc3 == 0:
        files_changed = sum(1 for line in diff_out.splitlines() if line.strip())

    return {"ok": True, "sha": sha, "files_changed": files_changed}


def push_current(path: Path, *, set_upstream: bool = True) -> dict[str, Any]:
    """``git push origin <current-branch>``. Adds ``--set-upstream`` when needed."""
    if not path.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    if not _is_repo(path):
        return {"ok": False, "error": "not a git repo"}

    branch = _current_branch(path)
    if not branch:
        return {"ok": False, "error": "detached HEAD; cannot push"}
    branch = _validate_branch(branch)

    # Detect upstream presence (rc=0 if @{u} resolves to anything).
    rc, _out, _err = _git_run(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], path,
    )
    has_upstream = rc == 0

    args = ["push"]
    if set_upstream and not has_upstream:
        args.append("--set-upstream")
    args += ["origin", branch]

    rc, out, err = _git_run(args, path, timeout_s=PUSH_TIMEOUT_S)
    if rc != 0:
        return {
            "ok": False,
            "error": (err.strip() or out.strip() or f"git push rc={rc}")[:500],
        }
    summary = (err.strip() or out.strip())[:500]
    return {"ok": True, "pushed_to": f"origin/{branch}", "branch": branch, "summary": summary}


def git_fetch(path: Path, *, prune: bool = True) -> dict[str, Any]:
    """``git fetch [--prune] --all``. Surfaces git's stderr summary.

    Bound to PUSH_TIMEOUT_S since fetch can stall on auth / network just
    like push.
    """
    if not path.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    if not _is_repo(path):
        return {"ok": False, "error": "not a git repo"}
    args = ["fetch", "--all"]
    if prune:
        args.append("--prune")
    rc, out, err = _git_run(args, path, timeout_s=PUSH_TIMEOUT_S)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or out.strip() or f"git fetch rc={rc}")[:500]}
    return {"ok": True, "summary": (err.strip() or out.strip())[:500]}


def status_porcelain(path: Path) -> dict[str, Any]:
    """Parse ``git status --porcelain=v1 -z`` into a structured list.

    Returns ``{ok, files: [{path, status_x, status_y}, ...]}``. For
    rename/copy entries we record both ``path`` (new) and ``orig_path``
    (old) since porcelain v1 -z emits them as two NUL-separated chunks.
    """
    if not path.is_dir():
        return {"ok": False, "error": f"not a directory: {path}", "files": []}
    if not _is_repo(path):
        return {"ok": False, "error": "not a git repo", "files": []}

    rc, out, err = _git_run(["status", "--porcelain=v1", "-z"], path)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or f"rc={rc}")[:500], "files": []}

    files: list[dict[str, Any]] = []
    if not out:
        return {"ok": True, "files": files}

    chunks = out.split("\x00")
    i = 0
    while i < len(chunks):
        entry = chunks[i]
        i += 1
        if not entry:
            continue
        # entry layout: "XY <path>" — XY is 2 chars, then a space, then path.
        if len(entry) < 3:
            continue
        x = entry[0]
        y = entry[1]
        rest = entry[3:] if len(entry) > 3 else ""
        item: dict[str, Any] = {
            "path": rest,
            "status_x": x,
            "status_y": y,
        }
        # Rename / copy → next chunk is the original path.
        if x in ("R", "C") or y in ("R", "C"):
            if i < len(chunks):
                item["orig_path"] = chunks[i]
                i += 1
        files.append(item)

    return {"ok": True, "files": files}


def _validate_pr_title(title: str) -> str:
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    title = title.strip()
    if not title:
        raise ValueError("title required")
    if len(title) > PR_TITLE_MAX:
        raise ValueError(f"title too long (>{PR_TITLE_MAX})")
    if "\n" in title or "\r" in title:
        raise ValueError("title cannot contain newlines")
    return title


def _validate_pr_base(base: str) -> str:
    if not isinstance(base, str):
        raise ValueError("base must be a string")
    base = base.strip()
    if not base:
        raise ValueError("base required")
    if len(base) > PR_BASE_MAX:
        raise ValueError(f"base too long (>{PR_BASE_MAX})")
    if not re.match(r"^[A-Za-z0-9._/-]{1,200}$", base):
        raise ValueError(f"invalid base ref: {base!r}")
    return base


def _resolve_gh_bin() -> str:
    """Locate ``gh`` once per call. Falls back to /usr/bin/gh."""
    return shutil.which("gh") or "/usr/bin/gh"


def open_pr(path: Path, *, title: str, body: str, base: str) -> dict[str, Any]:
    """``gh pr create`` for the current branch into ``base``.

    Body passed via stdin (``--body-file -``). Title + base validated
    locally before shelling out. Returns ``{ok, number, url}`` or
    ``{ok: False, error}``.
    """
    if not path.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    if not _is_repo(path):
        return {"ok": False, "error": "not a git repo"}

    try:
        title_v = _validate_pr_title(title)
        base_v = _validate_pr_base(base)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not isinstance(body, str):
        return {"ok": False, "error": "body must be a string"}

    branch = _current_branch(path)
    if not branch:
        return {"ok": False, "error": "detached HEAD; cannot open PR"}
    try:
        branch_v = _validate_branch(branch)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    gh_bin = _resolve_gh_bin()
    env = _git_env()
    env["GH_PAGER"] = ""
    env["NO_COLOR"] = "1"
    env["GH_PROMPT_DISABLED"] = "true"

    cmd = [
        gh_bin, "pr", "create",
        "--title", title_v,
        "--body-file", "-",
        "--head", branch_v,
        "--base", base_v,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(path),
            env=env,
            input=body,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": f"gh not found: {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"gh pr create timed out after {DEFAULT_TIMEOUT_S}s"}

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or f"gh pr create rc={proc.returncode}"
        return {"ok": False, "error": msg[:500]}

    url = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("http"):
            url = line
            break
    number: int | None = None
    if url:
        m = re.search(r"/pull/(\d+)", url)
        if m:
            try:
                number = int(m.group(1))
            except ValueError:
                number = None
    return {"ok": True, "number": number, "url": url}


def list_recent_commits(path: Path, limit: int = 5) -> dict[str, Any]:
    """Recent commits for the Overview tab. Returns ``{ok, commits}``.

    Each commit: ``{sha, author, ts, subject}``. ``ts`` is unix seconds.
    """
    if not path.is_dir():
        return {"ok": False, "error": f"not a directory: {path}", "commits": []}
    if not _is_repo(path):
        return {"ok": False, "error": "not a git repo", "commits": []}
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit must be an integer", "commits": []}
    n = max(1, min(n, 100))

    rc, out, err = _git_run(
        ["log", f"-n{n}", "--pretty=format:%H%x09%an%x09%at%x09%s"],
        path,
    )
    if rc != 0:
        return {"ok": False, "error": (err.strip() or f"rc={rc}")[:500], "commits": []}

    commits: list[dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        sha, author, ts_raw, subject = parts
        try:
            ts = int(ts_raw)
        except ValueError:
            ts = 0
        commits.append({
            "sha": sha,
            "author": author,
            "ts": ts,
            "subject": subject,
        })
    return {"ok": True, "commits": commits}
