# Orchestrator HTTP contract (verified live)

Base: `http://localhost:8766/api/orchestrator`. The MCP server wraps these; this
is the ground truth if you need to call them directly or extend the server.

## Sessions
- `GET  /sessions?cwd=<abs|__global__>&include_corpus=false` → `{sessions:[{id,title,cwd,lib_id,msg_count,unread_count,updated_at,last_model,archived,pinned,...}]}`
- `POST /sessions` `{title?,cwd?,lib_id?,model?,extra_system_prompt?}` → `{ok,id,cwd,lib_id,model}`. `cwd` must already exist under `$HOME` (else 400). `model ∈ {opus,sonnet,haiku}`.
- `PATCH /sessions/{id}` `{title?,archived?,pinned?,persistent?}` · `PATCH /sessions/{id}/model {model}` · `DELETE /sessions/{id}`
- `POST /sessions/{id}/start` → `{ok,started:true,spawned:bool}` (warms tmux `hd-<id>`) · `POST .../stop` (teardown, keep transcript) · `.../close` (alias) · `.../cancel` (SSE-only; does NOT stop claude)
- `GET  /sessions/{id}/status` → `{in_flight,started_at_ms,last_seq}` · `GET .../state` → `{todos[],plan}`

## Messaging
- `POST /sessions/{id}/messages` `{text, reply_to_turn_idx?, interactive_mode?}`
  - ok → `{ok:true, turn_idx:<ms-epoch req id — ignore>, expected_turn_idx:<cursor>, turn_started_ts, runner_mode:"interactive"|"programmatic"}`
  - busy → **HTTP 200** `{ok:false, status:"busy", error:"turn already in flight; cancel first"}`
  - `interactive_mode:true` → TmuxClaudeRunner (pane, can be driven by keystrokes). Omitted → global default (programmatic `claude -p`).
  - `expected_turn_idx` = current max transcript turn_idx, or **-1** for a fresh session. Pass it to `/wait?since_turn=`.
- `GET  /sessions/{id}/wait?since_turn=<int>&timeout=<≤60,def 25>` → `{ok,status:"done"|"error"|"timeout",since_turn,latest_turn_idx,new_messages:[…],cost_usd,error}`. Re-poll on timeout (same `since_turn`). 429 if >32 concurrent waiters.
- `GET  /sessions/{id}/messages?after_turn=<int>&limit=<0..500>` → `{ok,total,truncated,messages:[{role,ts,turn_idx,blocks:[…]}]}`

### Block kinds (orchestrator_jsonl)
`text` (assistant prose AND plain user text — **this is the reply**), `thinking`,
`tool_use` `{tool_use_id,name,input}`, `tool_result` `{tool_use_id,output,is_error}`,
`reply_to` `{turn_idx}`, `attachments` `{paths}`, `compacted_from`. Legacy-only:
`markdown`,`code`,`choice`,`error`. **Extract assistant text = join `text` blocks on `role=="assistant"` messages.**

`AskUserQuestion` is a normal `tool_use` block:
`{kind:"tool_use",name:"AskUserQuestion",input:{questions:[{question,header,multiSelect,options:[{label,description}]}]}}`.
The chosen answer returns as the matching `tool_result`. **There is no HTTP endpoint
to supply that answer** — it is typed into the pane (see the tmux control plane).

## Search / introspection
- `GET /sessions/search?q=&limit=<1..100>&cwd=` → `{ok,mode:"hybrid"|"lexical",results:[{session_id,score,title,updated_at,snippet,matched_terms}]}`
- `GET /capabilities` → feature flags + limits · `GET /turns/running` → `{running:[{session_id,started_at,runner}]}` (dashboard-dispatched turns only) · `GET /pool` → tmux slots
- `GET /sessions/{id}/events` (SSE, persistent): `turn_started|turn_done|turn_error|artifact_*|speak`. Build "notify on reply" on `turn_done`/`turn_error`.
- `GET /sessions/{id}/pane` (SSE) → `event: pane_snapshot, data:{text}` — a socket-independent HTTP pane read (alternative to `tmux capture-pane`).

## tmux control plane (the interactive layer)
- Socket: **`-L hd-orch`** (dedicated; not the default socket). Session name: **`hd-<session_id>`**, target `hd-<id>:0.0`.
- Read: `tmux -L hd-orch capture-pane -p -t hd-<id>` (or the HTTP `/pane`).
- Answer a menu: `tmux -L hd-orch send-keys -t hd-<id> Down` … `Enter` (≈0.35 s between keys).
- Interrupt: `send-keys Escape`. **Never** create your own named sessions on `hd-orch` (recover_orphans kills untracked ones on dashboard restart).

## Spawn facts (orchestrator_tmux.py)
`tmux -L hd-orch new-session -d -s hd-<id> -x 200 -y 50 -c <cwd> -e ANTHROPIC_API_KEY= -e TERM=xterm-256color … env PATH=… claude --session-id <id> --permission-mode auto --add-dir ~/.claude [--add-dir <cwd>] [--append-system-prompt-file …] [--model …]`.
No `--mcp-config` flag → MCP servers come from `$CLAUDE_CONFIG_DIR/.claude.json`
(= `~/.claude/.claude.json`, since the service sets `CLAUDE_CONFIG_DIR=~/.claude`).
The MCP is registered there under **`projects["~"].mcpServers`**
(per-cwd) so **only the Global agent** loads it — matching the skill's scope.
Top-level `mcpServers` would apply to every cwd/agent (not what we want yet).
