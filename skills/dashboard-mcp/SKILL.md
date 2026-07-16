---
name: dashboard-mcp
description: Use this skill to drive THIS orbit system with typed MCP tools (mcp__dashboard__*) instead of ad-hoc curl — search/list/find/create/start/stop the other PARA agent sessions, send a message and wait for the reply, read transcripts, drive interactive children (detect + answer their AskUserQuestion / permission prompts by injecting keystrokes into the tmux pane), plus PARA discovery and Telegram notify. This is how the Global agent delegates work to child agents and orchestrates a real multi-agent loop "in the user's name". Triggers (EN) — "delegate to an agent", "spawn a session", "orchestrate agents", "ask the X agent", "drive another session", "answer the question in session Y", "list/search my sessions", "notify me when". Triggers (PL) — "deleguj do agenta", "odpal sesję", "zapytaj agenta X", "steruj inną sesją", "odpowiedz na pytanie w sesji", "orkiestracja agentów". Backed by a zero-dependency stdio MCP server bundled in the repo (skills/dashboard-mcp/) and registered for the Global agent on boot.
metadata: {"clawdbot":{"emoji":"🔌","requires":{"bins":["python3","tmux"],"env":[]}}}
---

# dashboard-mcp  (issue #95)

## What this is

A **zero-dependency stdio MCP server** (`mcp__dashboard__*`) that gives the
orchestrator agent typed tools to drive THIS dashboard — the *other* PARA agent
sessions, PARA discovery, and notify — instead of hand-writing curl. Each
dashboard session runs as the tmux session `hd-<session_id>` on socket
`hd-orch`, so beyond the HTTP request/response loop the MCP can *see the live
pane* and *type into it* — which is what makes answering **AskUserQuestion**
menus and permission prompts on a child's behalf possible.

It is a thin adapter over the dashboard's localhost HTTP API (port 8766) — the
single source of truth — and ships in the repo at `skills/dashboard-mcp/`, so it
auto-deploys (git pull + restart) via `bundled_skills.seed_bundled_skills()` and
is registered for the agent on boot by `bundled_mcp.ensure_dashboard_mcp()`.

> **Scope: the Global agent only (for now).** The MCP is registered under
> `projects["~"].mcpServers.dashboard` in the config the spawned
> Global agent reads (`$CLAUDE_CONFIG_DIR/.claude.json`), and the skill is in the
> Global agent's allowlist — NOT globally enabled. Other agents see neither yet.
> To widen later: add the cwd to `bundled_mcp.AGENT_CWDS` + the skill to that
> agent's allowlist.
>
> The MCP loads at session start; a session already running before registration
> won't have the tools until restarted. A new Global chat gets them immediately.

## Tool surface (mcp__dashboard__*)

Discovery / read: `capabilities`, `list_agents`, `para_overview`, `list_sessions(cwd?)`,
`search_sessions(q,cwd?)`, `get_messages(id,after_turn?,limit?)`, `session_status`,
`session_state`, `running_turns`.

Lifecycle (incl. destructive): `create_session(cwd,…)`, `start_session(id)`,
`stop_session(id)`, `cancel_turn(id)`, `delete_session(id)` ⚠, `set_session_model`.

Messaging: `send_message(id,text,…)` → `{expected_turn_idx}`; `wait_for_reply(id,since_turn)`;
**`send_and_wait(id,text)`** — send AND block for the reply text (the delegate-and-collect primitive).

Interactive control plane (tmux): `find_tmux(id?)`, `capture_pane(id)` (+ heuristic
`pending_question`), `send_keys(id,text?,keys?)`, **`answer_question(id,mode,…)`** —
answer a menu (`option_index` | `text` | `keys`).

Notify: `notify(text,topic?,url?)` — Telegram push to the phone.

## The canonical delegation loop

```
para_overview / search_sessions   → pick the agent (its cwd)
create_session(cwd) → start_session → child online as hd-<id>
send_and_wait(id, "do X, report Y") → reply text
   ├─ if the child asks a question:
   │     capture_pane(id) → read options → answer_question(id, mode="option_index", option_index=K)
   │     → wait_for_reply(id, since_turn=…)
   └─ collect result, send next, or stop_session(id)
```

See `references/recipes.md` for fan-out, interrupt, recursive sub-orchestration
(a child driving a grandchild), and the **keep-each-turn's-shell-calls-short**
gotcha; `references/api-contract.md` for exact HTTP shapes.

## Verified

`scripts/selftest.py` — full create→delegate→detect→answer(non-default)→collect→verify
loop (deterministic). `scripts/recursion_demo.py` — Global → manager child →
worker grandchild → answer flows back. Both run live against the box; they
create + clean up throwaway sessions.

## Safety

- `send_keys`/`answer_question` refuse this server's **own** session (matched via
  `HD_SESSION_ID`) unless `force:true` — never type into your own pane.
- `delete_session` is destructive (transcript + sidecar); prefer `stop_session`.
- `turn_idx`/`since_turn` are cursors you re-read, never stable keys.
- Single-user Tailscale box, localhost only — don't expose the server beyond localhost.
