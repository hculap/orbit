"""GitHub PR + Issue ops for the Areas/Projects library.

All ops shell out to ``gh`` CLI (already authenticated as the operator's GitHub
account with ``gist, read:org, repo`` scopes — verified at startup with ``gh auth status``).
We deliberately don't use httpx/Octokit equivalents — ``gh`` handles auth,
pagination, rate-limit retries, error formatting; one CLI call per op,
JSON output parsed via ``json.loads``.

``gh`` working dir is set to the repo path so it auto-resolves the
owner/repo from origin's remote URL — but we ALSO cache ``(owner, repo)``
to the sidecar so list operations don't re-shell ``git remote`` every call.

Errors: when ``gh`` fails (auth, network, 404, rate-limit), we return
``{"ok": False, "error": "<message>"}`` and the route handler maps to
HTTP 424 Failed Dependency (so the frontend can render a clean inline
error without making it look like a server bug).
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from . import library as library_mod

# `gh` is at /opt/homebrew/bin/gh on local dev (macOS) and /usr/bin/gh on
# the hetzner host. Resolve at import time — falls back to /usr/bin/gh.
GH_BIN = shutil.which("gh") or "/usr/bin/gh"
DEFAULT_TIMEOUT_S = 20.0
AUTH_TIMEOUT_S = 5.0

# Module-level auth cache populated lazily on first `gh_auth_check()`.
_auth_state: dict[str, Any] = {"checked": False, "ok": False, "user": None}


# ── auth + env ──────────────────────────────────────────────────


def _gh_env() -> dict[str, str]:
    """Subprocess env for ``gh``: drop pager, force C locale, no prompts."""
    e = dict(os.environ)
    e["GH_PAGER"] = ""
    e["PAGER"] = ""
    e["NO_COLOR"] = "1"
    e["LC_ALL"] = "C"
    e["GH_PROMPT_DISABLED"] = "true"
    return e


def gh_auth_check() -> dict:
    """Run ``gh auth status --hostname github.com`` once; cache the result.

    Returns ``{ok, user, scopes, error}``. Cached for the process lifetime
    so this is a one-time hit at startup; the rest of the call sites just
    consume ``_auth_state``.
    """
    if _auth_state["checked"]:
        return {
            "ok": _auth_state["ok"],
            "user": _auth_state.get("user"),
            "scopes": _auth_state.get("scopes"),
            "error": _auth_state.get("error"),
        }
    try:
        proc = subprocess.run(
            [GH_BIN, "auth", "status", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=AUTH_TIMEOUT_S,
            env=_gh_env(),
            check=False,
        )
    except FileNotFoundError as e:
        out = {"ok": False, "user": None, "scopes": [], "error": f"gh not found: {e}"}
        _auth_state.update({"checked": True, **out})
        return out
    except subprocess.TimeoutExpired:
        out = {"ok": False, "user": None, "scopes": [],
               "error": f"gh auth status timed out after {AUTH_TIMEOUT_S}s"}
        _auth_state.update({"checked": True, **out})
        return out

    # `gh auth status` writes its human-readable output on stderr regardless
    # of success — parse both streams to find user + scopes.
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    user: str | None = None
    scopes: list[str] = []
    m = re.search(r"account\s+(\S+)", blob, re.IGNORECASE)
    if m:
        user = m.group(1).rstrip(")").strip()
    else:
        m = re.search(r"Logged in to github\.com as\s+(\S+)", blob, re.IGNORECASE)
        if m:
            user = m.group(1).strip()
    m = re.search(r"Token scopes?:\s*([^\n]+)", blob, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        scopes = [s.strip().strip("'\"") for s in raw.split(",") if s.strip()]

    ok = proc.returncode == 0
    out = {
        "ok": ok,
        "user": user,
        "scopes": scopes,
        "error": None if ok else (proc.stderr.strip() or "gh auth failed"),
    }
    _auth_state.update({"checked": True, **out})
    return out


# ── subprocess wrapper ──────────────────────────────────────────


async def _gh_run(
    args: list[str],
    cwd: Path | None = None,
    stdin_input: bytes | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, bytes, bytes]:
    """``asyncio.create_subprocess_exec`` wrapper. Returns ``(rc, stdout, stderr)``.

    Always uses ``_gh_env()`` and the resolved ``GH_BIN``. ``cwd`` should
    typically be the repo path so ``gh`` can resolve owner/repo from
    origin. ``stdin_input`` is for ``--body-file -`` style ops.
    """
    cmd = [GH_BIN, *args]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            env=_gh_env(),
            stdin=asyncio.subprocess.PIPE if stdin_input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin_input), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return 124, b"", f"timeout after {timeout_s}s".encode("utf-8")
    except FileNotFoundError as e:
        return 127, b"", f"gh not found: {e}".encode("utf-8")
    return proc.returncode or 0, out or b"", err or b""


# ── owner/repo resolution ──────────────────────────────────────


_GH_URL_RE = re.compile(
    r"^(?:"
    r"https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?|"
    r"git@github\.com:([\w.-]+)/([\w.-]+?)(?:\.git)?|"
    r"ssh://git@github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?"
    r")$"
)


def _parse_owner_repo_from_url(url: str) -> tuple[str, str] | None:
    """Parse owner+repo from origin URL.

    Supported forms:
      - ``https://github.com/<owner>/<repo>(.git)?(/)?``
      - ``git@github.com:<owner>/<repo>(.git)?``
      - ``ssh://git@github.com/<owner>/<repo>(.git)?``
    Returns ``(owner, repo)`` or ``None`` for non-github / unparseable.
    """
    if not isinstance(url, str):
        return None
    m = _GH_URL_RE.match(url.strip())
    if not m:
        return None
    groups = [g for g in m.groups() if g is not None]
    if len(groups) < 2:
        return None
    owner, repo = groups[0], groups[1]
    return owner, repo


async def gh_resolve_repo(path: Path) -> tuple[str, str]:
    """Resolve ``(owner, repo)`` for the repo at ``path``.

    Fast path: if the sidecar has owner/repo/origin_url AND a stored
    ``git_config_mtime`` that matches the current ``.git/config`` mtime,
    trust the cache — origin URLs change rarely and the file mtime gives
    us a cheap freshness signal.

    Slow path: shell out to ``git remote get-url origin``, parse, write
    back to the sidecar (including the new mtime).

    Raises ``ValueError`` if the directory is not a github repo.
    """
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")

    sidecar = library_mod.read_sidecar(path)
    cached = sidecar.get("github") if isinstance(sidecar, dict) else None
    cached_owner = cached.get("owner") if isinstance(cached, dict) else None
    cached_repo = cached.get("repo") if isinstance(cached, dict) else None
    cached_origin = cached.get("origin_url") if isinstance(cached, dict) else None
    cached_mtime = cached.get("git_config_mtime") if isinstance(cached, dict) else None

    # Fast path: stat .git/config (one syscall) and trust the sidecar when
    # its mtime hasn't moved since the cached read. Saves a git subprocess
    # on every PR/Issue tab open — origin URLs change so rarely that this
    # cache is effectively permanent until the user runs `git remote set-url`.
    git_config_path = path / ".git" / "config"
    current_mtime: float | None = None
    try:
        current_mtime = git_config_path.stat().st_mtime
    except OSError:
        current_mtime = None
    if (
        cached_owner
        and cached_repo
        and isinstance(cached_mtime, (int, float))
        and current_mtime is not None
        and float(cached_mtime) == float(current_mtime)
    ):
        return cached_owner, cached_repo

    # Slow path: read the live origin URL.
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/git", "remote", "get-url", "origin",
        cwd=str(path),
        env=_gh_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        out_b, err_b = b"", b"timeout"
    origin_url = (out_b or b"").decode("utf-8", errors="replace").strip()

    if origin_url:
        parsed = _parse_owner_repo_from_url(origin_url)
        if parsed is None:
            raise ValueError(f"origin is not a github repo: {origin_url}")
        owner, repo = parsed
        # Write back if changed (or missing) — also record current mtime so
        # the next call can take the fast path.
        if (
            cached_owner != owner
            or cached_repo != repo
            or cached_origin != origin_url
            or cached_mtime != current_mtime
        ):
            try:
                library_mod.write_sidecar(path, {
                    "github": {
                        "owner": owner,
                        "repo": repo,
                        "origin_url": origin_url,
                        "git_config_mtime": current_mtime,
                    },
                })
            except Exception:
                # Best-effort cache; don't fail the call if sidecar write fails.
                pass
        return owner, repo

    # No live origin — fall back to cache if present.
    if cached_owner and cached_repo:
        return cached_owner, cached_repo
    raise ValueError(
        f"unable to resolve github owner/repo (no origin remote): "
        f"{err_b.decode('utf-8', 'replace').strip()[:200]}"
    )


# ── PRs ─────────────────────────────────────────────────────────


_PR_LIST_FIELDS = "number,title,state,headRefName,author,updatedAt,url,isDraft"
_PR_VIEW_FIELDS = (
    "number,title,body,state,headRefName,baseRefName,author,url,"
    "updatedAt,isDraft,mergeable"
)


async def list_prs(
    path: Path,
    state: Literal["open", "closed", "all"] = "open",
    limit: int = 50,
) -> dict:
    """List PRs for the repo at ``path``.

    Returns ``{ok: True, items: [...]}`` or ``{ok: False, error}``.
    """
    if state not in ("open", "closed", "all"):
        return _err(f"invalid state: {state}")
    limit = max(1, min(int(limit), 200))
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, out, err = await _gh_run(
        ["pr", "list", "--state", state, "--limit", str(limit),
         "--json", _PR_LIST_FIELDS],
        cwd=path,
    )
    if rc != 0:
        return _err(err, fallback=f"gh pr list rc={rc}")
    try:
        items = json.loads(out.decode("utf-8") or "[]")
    except json.JSONDecodeError as e:
        return _err(f"could not parse gh output: {e}")
    return {"ok": True, "items": items}


async def get_pr(path: Path, number: int) -> dict:
    """Fetch a single PR by number. Returns ``{ok, data}`` or ``{ok:False, error}``."""
    if not isinstance(number, int) or number <= 0:
        return _err("number must be a positive int")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, out, err = await _gh_run(
        ["pr", "view", str(number), "--json", _PR_VIEW_FIELDS],
        cwd=path,
    )
    if rc != 0:
        return _err(err, fallback=f"gh pr view rc={rc}")
    try:
        data = json.loads(out.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        return _err(f"could not parse gh output: {e}")
    return {"ok": True, "data": data}


async def update_pr_body(path: Path, number: int, body: str) -> dict:
    """Edit a PR body. Body passed via stdin (``--body-file -``)."""
    if not isinstance(number, int) or number <= 0:
        return _err("number must be a positive int")
    if not isinstance(body, str):
        return _err("body must be a string")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, _out, err = await _gh_run(
        ["pr", "edit", str(number), "--body-file", "-"],
        cwd=path,
        stdin_input=body.encode("utf-8"),
    )
    if rc != 0:
        return _err(err, fallback=f"gh pr edit rc={rc}")
    return {"ok": True, "number": number}


# ── Issues ──────────────────────────────────────────────────────


_ISSUE_LIST_FIELDS = "number,title,state,author,updatedAt,url,labels,comments"
_ISSUE_VIEW_FIELDS = (
    "number,title,body,state,author,url,updatedAt,labels,comments,closedAt"
)


async def list_issues(
    path: Path,
    state: Literal["open", "closed", "all"] = "open",
    limit: int = 50,
) -> dict:
    """List issues for the repo at ``path``."""
    if state not in ("open", "closed", "all"):
        return _err(f"invalid state: {state}")
    limit = max(1, min(int(limit), 200))
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, out, err = await _gh_run(
        ["issue", "list", "--state", state, "--limit", str(limit),
         "--json", _ISSUE_LIST_FIELDS],
        cwd=path,
    )
    if rc != 0:
        return _err(err, fallback=f"gh issue list rc={rc}")
    try:
        items = json.loads(out.decode("utf-8") or "[]")
    except json.JSONDecodeError as e:
        return _err(f"could not parse gh output: {e}")
    return {"ok": True, "items": items}


async def get_issue(path: Path, number: int) -> dict:
    """Fetch a single issue by number."""
    if not isinstance(number, int) or number <= 0:
        return _err("number must be a positive int")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, out, err = await _gh_run(
        ["issue", "view", str(number), "--json", _ISSUE_VIEW_FIELDS],
        cwd=path,
    )
    if rc != 0:
        return _err(err, fallback=f"gh issue view rc={rc}")
    try:
        data = json.loads(out.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        return _err(f"could not parse gh output: {e}")
    return {"ok": True, "data": data}


async def create_issue(
    path: Path,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
) -> dict:
    """Create an issue. Body passed via stdin to handle newlines/quotes safely.

    Returns ``{ok, number, url}`` on success.
    """
    if not isinstance(title, str) or not title.strip():
        return _err("title required")
    if not isinstance(body, str):
        return _err("body must be a string")
    if labels is not None and not isinstance(labels, list):
        return _err("labels must be a list of strings")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    args = ["issue", "create", "--title", title.strip(), "--body-file", "-"]
    for label in (labels or []):
        if not isinstance(label, str) or not label.strip():
            return _err("each label must be a non-empty string")
        args += ["--label", label.strip()]

    rc, out, err = await _gh_run(
        args, cwd=path, stdin_input=body.encode("utf-8"),
    )
    if rc != 0:
        return _err(err, fallback=f"gh issue create rc={rc}")
    # Output is just the URL on the first non-empty line.
    url = ""
    for line in out.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("http"):
            url = line
            break
    number: int | None = None
    if url:
        m = re.search(r"/issues/(\d+)", url)
        if m:
            try:
                number = int(m.group(1))
            except ValueError:
                number = None
    return {"ok": True, "number": number, "url": url}


async def update_issue(
    path: Path,
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    state: Literal["open", "closed", None] = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict:
    """Edit an issue. State changes use ``gh issue close|reopen``."""
    if not isinstance(number, int) or number <= 0:
        return _err("number must be a positive int")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    # Edits (title/body/labels). Run only if any of those provided.
    has_edit = (
        (isinstance(title, str) and title.strip())
        or isinstance(body, str)
        or (isinstance(add_labels, list) and add_labels)
        or (isinstance(remove_labels, list) and remove_labels)
    )
    if has_edit:
        args = ["issue", "edit", str(number)]
        if isinstance(title, str) and title.strip():
            args += ["--title", title.strip()]
        stdin_bytes: bytes | None = None
        if isinstance(body, str):
            args += ["--body-file", "-"]
            stdin_bytes = body.encode("utf-8")
        for label in (add_labels or []):
            if not isinstance(label, str) or not label.strip():
                return _err("each add_label must be a non-empty string")
            args += ["--add-label", label.strip()]
        for label in (remove_labels or []):
            if not isinstance(label, str) or not label.strip():
                return _err("each remove_label must be a non-empty string")
            args += ["--remove-label", label.strip()]
        rc, _out, err = await _gh_run(args, cwd=path, stdin_input=stdin_bytes)
        if rc != 0:
            return _err(err, fallback=f"gh issue edit rc={rc}")

    # State changes are separate sub-commands.
    if state in ("open", "closed"):
        sub = "reopen" if state == "open" else "close"
        rc, _out, err = await _gh_run(["issue", sub, str(number)], cwd=path)
        if rc != 0:
            return _err(err, fallback=f"gh issue {sub} rc={rc}")
    elif state is not None:
        return _err(f"invalid state: {state}")

    return {"ok": True, "number": number}


async def get_issue_node_id(path: Path, number: int) -> dict:
    """Resolve an issue's GraphQL node id.

    Needed to add the issue to a Projects v2 board (the board mutation keys on
    the node id, not the repo+number). Returns ``{ok, id}`` or ``{ok:False,error}``.
    """
    if not isinstance(number, int) or number <= 0:
        return _err("number must be a positive int")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, out, err = await _gh_run(["issue", "view", str(number), "--json", "id"], cwd=path)
    if rc != 0:
        return _err(err, fallback=f"gh issue view rc={rc}")
    try:
        data = json.loads(out.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        return _err(f"could not parse gh output: {e}")
    node_id = data.get("id")
    if not node_id:
        return _err("issue has no node id")
    return {"ok": True, "id": node_id}


async def add_comment(path: Path, number: int, body: str) -> dict:
    """Add a comment to an issue. Body via stdin to handle newlines/quotes safely."""
    if not isinstance(number, int) or number <= 0:
        return _err("number must be a positive int")
    if not isinstance(body, str) or not body.strip():
        return _err("comment body required")
    try:
        await gh_resolve_repo(path)
    except ValueError as e:
        return _err(str(e))

    rc, out, err = await _gh_run(
        ["issue", "comment", str(number), "--body-file", "-"],
        cwd=path, stdin_input=body.encode("utf-8"),
    )
    if rc != 0:
        return _err(err, fallback=f"gh issue comment rc={rc}")
    lines = out.decode("utf-8", errors="replace").strip().splitlines()
    return {"ok": True, "number": number, "url": (lines[-1].strip() if lines else "")}


# ── helpers ─────────────────────────────────────────────────────


def _ok(data: Any) -> dict:
    """Compose a success envelope around arbitrary data."""
    if isinstance(data, dict):
        return {"ok": True, **data}
    return {"ok": True, "data": data}


def _err(stderr: bytes | str, fallback: str = "gh failed") -> dict:
    """Compose an error envelope. Truncates to 500 chars."""
    if isinstance(stderr, bytes):
        msg = stderr.decode("utf-8", errors="replace")
    else:
        msg = stderr or ""
    msg = (msg.strip() or fallback)[:500]
    return {"ok": False, "error": msg}
