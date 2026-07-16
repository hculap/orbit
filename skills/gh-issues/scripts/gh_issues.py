#!/usr/bin/env python3
"""gh_issues.py — per-project/area GitHub Issues as a persistent todo.

A thin CLI + importable wrapper over the ``gh`` CLI. Unlike ``gh-tasks`` (which
drives a single fixed Projects v2 board) this skill is **cwd-discovered**: it
operates on whatever GitHub repo the current directory belongs to, so it is the
right tool for the orchestrator agent working inside a project/area to track
that project's own todo list.

Repo resolution (first match wins):
    1. ``--repo owner/name``            (explicit flag)
    2. ``$GH_ISSUES_REPO``             (owner/name)
    3. ``./.gh-issues.json``           (cwd, {"repo": "owner/name"})
    4. ``gh repo view --json nameWithOwner``  (the cwd's git remote)

Plain ``gh issue`` porcelain only — only the ``repo`` auth scope is needed.
The one exception is ``make-task``, which promotes an issue onto the global
Tasks board by POSTing to the orbit (which owns the board config).

Usage:
    python3 gh_issues.py <command> [options]

Commands:
    list      [--state open|closed|all] [--label L] [--limit N] [--json]
    get       <number> [--comments] [--json]
    create    --title "..." [--body "..."] [--label L]... [--assignee A]...
    edit      <number> [--title ...] [--body ...] [--add-label L]... [--remove-label L]...
    comment   <number> --body "..."
    close     <number> [--reason completed|not_planned]
    reopen    <number>
    search    --query "..." [--state ...] [--limit N] [--json]
    make-task <number> [--status "Todo"]   # promote this issue onto the Tasks board
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# gh CLI helpers
# ---------------------------------------------------------------------------


def _gh(*args: str) -> str:
    cmd = ["gh", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh command failed: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def _gh_json(*args: str) -> Any:
    raw = _gh(*args)
    return json.loads(raw) if raw else []


# ---------------------------------------------------------------------------
# Repo discovery (cwd-based, overridable) — the gh-issues differentiator
# ---------------------------------------------------------------------------

_REPO_OVERRIDE: str | None = None  # set by the global --repo flag
_REPO_CACHE: str | None = None


def _optional_config() -> dict[str, Any]:
    path = Path.cwd() / ".gh-issues.json"
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _repo() -> str:
    """Resolve owner/name for the current repo (process-cached)."""
    global _REPO_CACHE
    if _REPO_OVERRIDE:
        return _REPO_OVERRIDE
    if _REPO_CACHE:
        return _REPO_CACHE
    env = os.environ.get("GH_ISSUES_REPO")
    if env:
        _REPO_CACHE = env.strip()
        return _REPO_CACHE
    cfg_repo = _optional_config().get("repo")
    if isinstance(cfg_repo, str) and cfg_repo.strip():
        _REPO_CACHE = cfg_repo.strip()
        return _REPO_CACHE
    try:
        _REPO_CACHE = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    except RuntimeError as exc:
        print(
            "ERROR: could not determine the GitHub repo. Run this inside a repo's "
            "working tree, or pass --repo owner/name (or set $GH_ISSUES_REPO, or add "
            f"a .gh-issues.json). Underlying error:\n{exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not _REPO_CACHE:
        print("ERROR: cwd is not a GitHub repo (no nameWithOwner).", file=sys.stderr)
        sys.exit(1)
    return _REPO_CACHE


# ---------------------------------------------------------------------------
# Issue operations
# ---------------------------------------------------------------------------

_LIST_FIELDS = "number,title,state,labels,url,updatedAt,comments"
_GET_FIELDS = "number,title,body,state,labels,url,createdAt,closedAt,updatedAt,author,assignees,comments"


def list_issues(state: str = "open", label: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    args = ["issue", "list", "--repo", _repo(), "--state", state,
            "--limit", str(limit), "--json", _LIST_FIELDS]
    if label:
        args.extend(["--label", label])
    return _gh_json(*args)


def search_issues(query: str, state: str = "all", limit: int = 50) -> list[dict[str, Any]]:
    return _gh_json("issue", "list", "--repo", _repo(), "--search", query,
                    "--state", state, "--limit", str(limit), "--json", _LIST_FIELDS)


def get_issue(number: int, comments: bool = False) -> dict[str, Any]:
    fields = _GET_FIELDS if comments else _GET_FIELDS.replace(",comments", "")
    return _gh_json("issue", "view", str(number), "--repo", _repo(), "--json", fields)


def create_issue(title: str, body: str = "", labels: list[str] | None = None,
                 assignees: list[str] | None = None) -> dict[str, Any]:
    args = ["issue", "create", "--repo", _repo(), "--title", title, "--body", body or ""]
    for lab in labels or []:
        args.extend(["--label", lab])
    for who in assignees or []:
        args.extend(["--assignee", who])
    url = _gh(*args).split()[-1]  # gh prints a URL (sometimes after a status line)
    number = int(url.rstrip("/").split("/")[-1])
    return _gh_json("issue", "view", str(number), "--repo", _repo(), "--json", "number,title,url,state")


def edit_issue(number: int, title: str | None = None, body: str | None = None,
               add_labels: list[str] | None = None, remove_labels: list[str] | None = None) -> None:
    args = ["issue", "edit", str(number), "--repo", _repo()]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body", body])
    for lab in add_labels or []:
        args.extend(["--add-label", lab])
    for lab in remove_labels or []:
        args.extend(["--remove-label", lab])
    if len(args) == 5:  # only the base [issue, edit, N, --repo, REPO] → no edit flags
        raise RuntimeError("edit: nothing to change (pass --title/--body/--add-label/--remove-label)")
    _gh(*args)


def comment_issue(number: int, body: str) -> str:
    return _gh("issue", "comment", str(number), "--repo", _repo(), "--body", body)


def close_issue(number: int, reason: str = "completed") -> None:
    _gh("issue", "close", str(number), "--repo", _repo(), "--reason", reason)


def reopen_issue(number: int) -> None:
    _gh("issue", "reopen", str(number), "--repo", _repo())


# ---------------------------------------------------------------------------
# Promote issue → Task (delegates to the dashboard, which owns the board config)
# ---------------------------------------------------------------------------


def _dashboard_url() -> str:
    return (os.environ.get("TASKS_DASHBOARD_URL")
            or os.environ.get("ISSUES_DASHBOARD_URL")
            or "http://localhost:8766").rstrip("/")


def _git_toplevel() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError("not inside a git working tree (git rev-parse failed)")
    return Path(out.stdout.strip()).resolve()


def _library_kind_name() -> tuple[str, str]:
    """Map the repo's on-disk location to the dashboard's (kind, name) identifier."""
    top = _git_toplevel()
    home = Path.home()
    for base, kind in ((home / "Projects", "projects"), (home / "Areas", "areas")):
        try:
            rel = top.relative_to(base.resolve())
        except ValueError:
            continue
        return kind, str(rel)
    raise RuntimeError(
        "make-task only works inside ~/Projects/<...> or ~/Areas/<...> "
        f"(repo root is {top}). Use the dashboard 'Make task' button instead."
    )


def make_task(number: int, status: str | None = None) -> dict[str, Any]:
    kind, name = _library_kind_name()
    path = "/api/library/{}/{}/github/issues/{}/make-task".format(
        kind, urllib.parse.quote(name), number,
    )
    body: dict[str, Any] = {}
    if status:
        body["status"] = status
    return _http_post(path, body)


def _http_post(path: str, body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    url = _dashboard_url() + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            msg = payload.get("detail") or raw
        except ValueError:
            msg = raw
        raise RuntimeError(f"dashboard POST {path} failed (HTTP {e.code}): {msg}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"dashboard POST {path} failed: {e.reason}") from e
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def _fmt_labels(labels: Any) -> str:
    names = [l.get("name", "") for l in (labels or []) if isinstance(l, dict)]
    return " ".join(f"[{n}]" for n in names if n)


def _print_list(issues: list[dict[str, Any]]) -> None:
    if not issues:
        print("(no issues)")
        return
    for it in issues:
        num = it.get("number")
        state = (it.get("state") or "").lower()
        mark = "✓" if state == "closed" else "○"
        ncom = it.get("comments")
        ncom_s = f"  💬{ncom}" if isinstance(ncom, int) and ncom else ""
        labels = _fmt_labels(it.get("labels"))
        labels_s = f"  {labels}" if labels else ""
        print(f"{mark} #{num}  {it.get('title', '')}{labels_s}{ncom_s}")


def _print_issue(issue: dict[str, Any], comments: bool) -> None:
    print(f"#{issue.get('number')}  {issue.get('title', '')}  [{issue.get('state', '')}]")
    if issue.get("url"):
        print(issue["url"])
    labels = _fmt_labels(issue.get("labels"))
    if labels:
        print(labels)
    body = (issue.get("body") or "").strip()
    if body:
        print("\n" + body)
    if comments:
        for c in issue.get("comments") or []:
            author = (c.get("author") or {}).get("login", "?")
            print(f"\n— {author} ({c.get('createdAt', '')}):")
            print((c.get("body") or "").strip())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(obj: Any, as_json: bool, printer) -> None:
    if as_json:
        print(json.dumps(obj, indent=2))
    else:
        printer(obj)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gh_issues", description=__doc__)
    p.add_argument("--repo", help="owner/name override (default: cwd's git remote)")
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("list", help="list issues")
    pl.add_argument("--state", default="open", choices=["open", "closed", "all"])
    pl.add_argument("--label")
    pl.add_argument("--limit", type=int, default=50)
    pl.add_argument("--json", action="store_true")

    pg = sub.add_parser("get", help="show one issue")
    pg.add_argument("number", type=int)
    pg.add_argument("--comments", action="store_true")
    pg.add_argument("--json", action="store_true")

    pc = sub.add_parser("create", help="create an issue")
    pc.add_argument("--title", required=True)
    pc.add_argument("--body", default="")
    pc.add_argument("--label", action="append", default=[])
    pc.add_argument("--assignee", action="append", default=[])

    pe = sub.add_parser("edit", help="edit an issue")
    pe.add_argument("number", type=int)
    pe.add_argument("--title")
    pe.add_argument("--body")
    pe.add_argument("--add-label", action="append", default=[])
    pe.add_argument("--remove-label", action="append", default=[])

    pcm = sub.add_parser("comment", help="add a comment")
    pcm.add_argument("number", type=int)
    pcm.add_argument("--body", required=True)

    pcl = sub.add_parser("close", help="close an issue")
    pcl.add_argument("number", type=int)
    pcl.add_argument("--reason", default="completed", choices=["completed", "not_planned"])

    pr = sub.add_parser("reopen", help="reopen an issue")
    pr.add_argument("number", type=int)

    ps = sub.add_parser("search", help="search issues")
    ps.add_argument("--query", required=True)
    ps.add_argument("--state", default="all", choices=["open", "closed", "all"])
    ps.add_argument("--limit", type=int, default=50)
    ps.add_argument("--json", action="store_true")

    pm = sub.add_parser("make-task", help="promote an issue onto the global Tasks board")
    pm.add_argument("number", type=int)
    pm.add_argument("--status", help="initial board Status (e.g. Todo)")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    global _REPO_OVERRIDE
    if getattr(args, "repo", None):
        _REPO_OVERRIDE = args.repo
    try:
        if args.command == "list":
            _emit(list_issues(args.state, args.label, args.limit), args.json, _print_list)
        elif args.command == "search":
            _emit(search_issues(args.query, args.state, args.limit), args.json, _print_list)
        elif args.command == "get":
            issue = get_issue(args.number, args.comments)
            _emit(issue, args.json, lambda i: _print_issue(i, args.comments))
        elif args.command == "create":
            res = create_issue(args.title, args.body, args.label, args.assignee)
            print(f"Created #{res.get('number')}: {res.get('title')}\n{res.get('url', '')}")
        elif args.command == "edit":
            edit_issue(args.number, args.title, args.body, args.add_label, args.remove_label)
            print(f"Edited #{args.number}")
        elif args.command == "comment":
            url = comment_issue(args.number, args.body)
            print(f"Commented on #{args.number}\n{url}")
        elif args.command == "close":
            close_issue(args.number, args.reason)
            print(f"Closed #{args.number} ({args.reason})")
        elif args.command == "reopen":
            reopen_issue(args.number)
            print(f"Reopened #{args.number}")
        elif args.command == "make-task":
            res = make_task(args.number, args.status)
            print(f"Promoted #{args.number} to a Task (board item {res.get('item_id', '?')})")
        else:  # pragma: no cover — argparse enforces choices
            print(f"unknown command {args.command!r}", file=sys.stderr)
            return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
