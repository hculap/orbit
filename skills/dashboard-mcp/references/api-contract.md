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
- `GET /sessions/{id}/events` (SSE, persistent): `turn_started|turn_done|turn_error|artifact_*|speak|geo_request|geo_cancelled`. Build "notify on reply" on `turn_done`/`turn_error`.
- `GET /sessions/{id}/pane` (SSE) → `event: pane_snapshot, data:{text}` — a socket-independent HTTP pane read (alternative to `tmux capture-pane`).

## Geolocation (`/api/orchestrator/geo/*`)

Auth is **asymmetric**: agent→server routes need `X-Geo-Token:` = the contents of
`~/.orchestrator/artifact_token` (`HD_ARTIFACT_TOKEN_FILE`), compared with
`hmac.compare_digest`, failing closed → **403** `invalid geo token`. Browser→server
routes (`answer`, `pending`, `forget`) are un-gated; the 128-bit `request_id`,
which only ever travels over that session's own SSE, is the capability. Every
agent-facing route also re-reads the `geolocation_enabled` flag **per request**
(no restart to toggle) → **403** `geolocation disabled — Ustawienia → Serwer`.

| Method | Path | Token | Body / Query → Response |
|---|---|:-:|---|
| POST | `/geo/request` | ✅ | `{session_id, reason?, accuracy?:fine\|coarse, max_age_s?=300, ttl_s?=180}` → `cached` \| `no_browser` \| `rate_limited` \| `pending{request_id,expires_at_ms,targets_n}`. **429** at `GEO_MAX_PENDING=16` |
| GET | `/geo/requests/{rid}/wait?timeout=45` | ✅ | → a terminal payload, or `{"ok":true,"status":"timeout","terminal":false}`. **429** at `GEO_WAIT_MAX_CONCURRENT=8` |
| DELETE | `/geo/requests/{rid}` | ✅ | → `{ok,cancelled,status}` — idempotent |
| GET | `/geo/status?session_id=` | ✅ | → `{ok,enabled,geocode_enabled,browser_attached,browsers_n,last_fix_age_s,pending_n,wait_inflight}` (side-effect free preflight) |
| GET | `/geo/last` | ✅ | the cached fix, READ-ONLY (never registers an ask, never prompts) → a `cached` payload, or `{ok:true,cached:false,fix:null}` |
| POST | `/geo/reverse` | ✅ | `{lat,lon}` → `{ok,status:"ok",cached,address:{display_name,road,house_number,city,postcode,country,raw}}` |
| POST | `/geo/nearby` | ✅ | `{lat,lon,query,radius_m?=1500,limit?=8}` → `{ok,status:"ok",query,radius_m,source:"overpass"\|"nominatim",tag,cached,places:[{name,kind,lat,lon,distance_m,tags}]}` |
| POST | `/geo/requests/{rid}/answer` | ❌ | the browser's `{status,reason?,coords?,ts_ms?}` → `{ok,accepted,status}`. 404 unknown rid; with the flag off `{ok:true,accepted:false,status:"disabled"}` (a body, so a stale tab stops instead of retrying) |
| GET | `/geo/requests/pending?session_id=` | ❌ | the SSE-reconnect guarantee → `{ok,requests:[…]}`; an EMPTY list with the flag off |
| POST | `/geo/forget` | ❌ | → `{ok,forgotten}`; also clears the geocode caches |

**`terminal` is the discriminator, not `status`.** `/wait` uses the wire name
`timeout` for BOTH the poll window elapsing (`ok:true, terminal:false,
reason:"poll_window_elapsed"` — re-poll the same rid) and the browser's own
20 s geolocation timeout (`ok:false, terminal:true`). **Re-poll iff
`terminal is false`;** anything else is final.

Timeout ladder (do not let these drift): browser 20 s < `/wait` poll 45 s
(server clamp 60) < request TTL 180 s == the MCP's `overall_timeout`. A resolved
request is retained 120 s (`GEO_RESULT_GRACE_S`) so an answer that lands *before*
`/wait` is issued — the common case — is not lost.

Every payload carries `{ok,status,terminal,reason,retryable,message,hint}` from
the server's single `STATUSES` table (18 rows: `fixed cached pending denied
device_opt_out dismissed insecure_context no_api unavailable timeout expired
aborted no_browser not_found rate_limited disabled geocode_disabled
geocode_failed`). `message`/`hint` are Polish, written for the user.

Geocoding sits behind a **second** flag (`geolocation_geocode_enabled`); off
returns **HTTP 200** `{"ok":false,"status":"geocode_disabled"}` — a body, not a
403 — so a caller degrades to raw lat/lon instead of erroring. Fixes live in
process memory only (nothing on disk), but the coordinates do reach public OSM
servers when geocoding is on.

SSE (on the existing `GET /sessions/{id}/events`): `geo_request`
`{request_id,reason,accuracy,max_age_s,ttl_s,expires_at_ms,agent:{lib_id,title}}`
and `geo_cancelled` `{request_id, reason:"answered_elsewhere"|"expired"|"aborted"}`.

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
