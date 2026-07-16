---
name: gh-issues
description: Manage the CURRENT project/area's GitHub Issues as a persistent, cross-session todo list — list/create/edit/comment/close/reopen/search, and promote an issue to a global Task. The repo is auto-detected from the working directory, so run it from inside a project/area. Use when working inside a specific repo and the user wants to track that project's own todos/issues (PL+EN triggers — "issue", "issues", "todo projektu", "zrób issue", "dodaj issue", "lista issues", "zamknij issue", "co mamy do zrobienia w tym projekcie", "open an issue", "track this", "project todo", "make a task from this issue", "zrób z tego task"). This is the per-repo counterpart to the global gh-tasks board — issues are the lightweight working list; promote to a Task only when the user asks or clicks.
metadata: {"clawdbot":{"emoji":"🗒️"}}
---

# gh-issues

## What this is

A per-repo GitHub **Issues** manager: a persistent todo list scoped to ONE
project/area, surviving across orchestrator sessions. Unlike `gh-tasks` (which
drives a single global Projects v2 board, user-facing, with detailed status +
reminders), `gh-issues`:

- operates on **whatever repo the cwd belongs to** (auto-discovered), so it's
  the tool to reach for while working inside a project;
- uses **plain `gh issue`** porcelain — only the `repo` auth scope;
- is the **working list** the agent reads/writes freely. An issue becomes a
  **Task** (added to the global board) only on `make-task`, when the user asks
  or clicks the dashboard "Make task" button.

The same issues show up in the dashboard's **Issues tab** on the project/area
detail page (they're the same GitHub issues), so what the agent writes here, the
user sees there — and vice versa.

## When to use

- The user, while working in a project, wants to jot down / track / review that
  project's todos ("add an issue to…", "what's left in this repo?", "close #4").
- You (the agent) want to record follow-ups for a project so they persist past
  this session instead of living in chat.
- The user wants to elevate a project issue into the global Tasks board
  (`make-task`).

Do NOT use for the global personal backlog / reminders — that's `gh-tasks`.

## Usage

Run the CLI (it auto-detects the repo from cwd):

```
python3 ~/.orchestrator/skills-registry/gh-issues/scripts/gh_issues.py <command> [options]
```

Commands:

```
list      [--state open|closed|all] [--label L] [--limit N] [--json]
get       <number> [--comments] [--json]
create    --title "..." [--body "..."] [--label L]... [--assignee A]...
edit      <number> [--title ...] [--body ...] [--add-label L]... [--remove-label L]...
comment   <number> --body "..."
close     <number> [--reason completed|not_planned]
reopen    <number>
search    --query "..." [--state ...] [--limit N] [--json]
make-task <number> [--status "Todo"]
```

Global `--repo owner/name` overrides cwd discovery (also `$GH_ISSUES_REPO` or a
`./.gh-issues.json` with `{"repo": "owner/name"}`).

### Examples

```
# what's open in this project?
gh_issues.py list

# jot a todo
gh_issues.py create --title "Wire up the export button" --label todo

# record a follow-up on an existing issue
gh_issues.py comment 7 --body "Blocked on the API rename — see #5"

# done
gh_issues.py close 7

# the user wants this tracked on the real backlog
gh_issues.py make-task 7 --status Todo
```

## Notes

- `make-task` POSTs to the orbit (`/api/library/<kind>/<name>/github/
  issues/<n>/make-task`), which owns the Projects v2 board config and the
  `project` scope. It only works from inside `~/Projects/<...>` or `~/Areas/<...>`
  (the dashboard's library roots); elsewhere, use the dashboard button. It is
  gated on the dashboard's `issues.make_task` flag + a configured Tasks board —
  a clean error is printed if either is missing.
- All other commands are pure `gh issue` calls and work for any GitHub repo the
  cwd belongs to, online with `gh` authenticated (`repo` scope).
- `make-task` is idempotent: promoting an already-on-board issue is a no-op that
  returns the same board item.
