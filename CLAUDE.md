# orbit — context for AI agents

Personal hub for the user's Hetzner server `your-server`, served at root `/` of `https://your-dashboard.example/` (Tailscale-only, no public access).

## Project goals

Mobile-responsive entry point to everything on the box, auto-discovered from filesystem (PARA + nginx) with a thin override layer for labels/icons/ordering, plus a live chat panel to drive Claude Code on the server.

## Stack

- **Backend:** FastAPI, uvicorn, PyYAML — Python 3.12, uv-managed
- **Frontend:** CDN React 18 + Babel-standalone (no build step), JSX modules in `static/`. `marked` + `DOMPurify` via CDN for markdown rendering.
- **Deploy:** systemd service rendered from `deploy/orbit.service.template` by `scripts/install.sh`, port 8766, behind nginx at `/`. `BASE_PATH=""` for root deployment. Zero-downtime blue/green is in `deploy/advanced/`.

## File structure

```
src/orbit/
├── __main__.py              # argparse → uvicorn.run
├── app.py                   # FastAPI; routes wired inline in create_app()
├── discovery.py             # PARA scan: ~/Areas, ~/Projects, ~/Resources
├── nginx_parser.py          # parse /etc/nginx/conf.d/apps/*.conf
├── config.py                # load override.yaml, merge with discovery
├── system_status.py         # live metrics from /proc + tailscale + systemctl
├── share.py                 # ~/Sync browser (list/upload/zip/thumb)
├── logs.py                  # whitelisted journalctl + nginx tails
├── orchestrator.py          # /api/orchestrator/* routes, claude -p spawn
├── orchestrator_runner.py   # ClaudeRunner subprocess + NDJSON→SSE bridge
├── orchestrator_artifacts.py # per-agent .artifacts/ store + manifest + safe serving
├── orchestrator_events.py   # SessionEventHub: persistent per-session SSE (artifact push)
├── bundled_skills.py        # seed repo skills/* into ~/.orchestrator/skills-registry/ on boot
├── orchestrator_jsonl.py    # native session JSONL reader (+ historical-envelope shim)
├── orchestrator_meta.py     # sidecar archive flags / title overrides
├── orchestrator_prompts.py  # system prompt management
├── orchestrator_uploads.py  # per-session upload staging
├── orchestrator_terminal_shortcuts.py # ~/.orchestrator/terminal_shortcuts.json full-layout store + DEFAULT_LAYOUT seed (mobile soft-keyboard manager)
├── tasks.py                 # /api/tasks/* CRUD over GH Projects v2
├── tasks_config.py          # `tasks:` block of override.yaml → TasksConfig
├── tasks_github.py          # async gh CLI + GraphQL client (schema/items caches)
├── tasks_storage.py         # ~/.orchestrator/tasks.json sidecar (reminders only)
├── tasks_reminders.py       # compute_fire_time + scan_and_fire loop
├── tasks_reminders_seed.py  # registers the 60s tick into cron_store
├── templates/
│   └── index.html           # tiny React bootstrap; preloads window.HUB_INITIAL_DATA + window.HUB_BASE_PATH
└── static/
    ├── tokens.css
    ├── components.jsx
    ├── sections.jsx
    ├── details.jsx
    ├── app.jsx
    ├── file-preview.jsx
    ├── logs-view.jsx
    ├── orchestrator.jsx
    ├── orchestrator-shortcuts.jsx # window.HubShortcuts: layout store + applyLayout/decodeEscapes/helpers for the terminal shortcut manager
    └── tasks.jsx            # Tasks tab: Kanban + List, CRUD, reminder editor
```

Discovery sources unchanged: `~/Areas/`, `~/Projects/`, `~/Resources/`, `/etc/nginx/conf.d/apps/*.conf`. Sync at `~/Sync/`.

## Subpath / root deployment

Default deploy: `BASE_PATH=""` → app at root. Subpath-ready via `request.scope.root_path`, `url_for` everywhere on the backend, and `window.HUB_BASE_PATH` on the frontend — could move under e.g. `/hub` without code changes.

## Conventions

- `url_for` for ALL backend-rendered links (the bootstrap template still uses it for static asset URLs)
- `config/override.yaml` is **gitignored** — examples in `config/override.yaml.example`
- No per-request DB; in-memory caches with TTL (discovery cache: 30s)
- React JSX modules publish their components/hooks to `window` so sibling JSX files can pick them up (no bundler, no imports)

## Orchestrator panel (live)

- Chat with Claude Code running on this server, scoped to `~/`. Default surface is the in-browser ttyd **terminal** (tmux-backed interactive `claude`); the chat transcript is a plain-markdown mirror.
- Backend spawns `claude` (interactive, tmux) or `claude -p` (programmatic) per turn; native sessions own conversational context (`~/.claude/projects/-home-<user>/<uuid>.jsonl`)
- SSE streams the assistant response token-stream into the React panel
- Storage is minimal: the JSONL is the single source of truth; sidecar at `~/.orchestrator/sessions_meta.json` only tracks archive flags / title overrides — no SQLite
- Routes mounted by `orchestrator.register_routes(app)` from `app.create_app()`; `orchestrator_prompts.ensure_prompts()` runs once at registration

### Detached sessions (survive `systemctl restart`)

The dashboard is a systemd **system** service (`KillMode=control-group`), so a tmux server forked from it inherits the service cgroup and a restart SIGKILLs every `claude` REPL. The `tmux_detached_sessions` flag (default **True**, in `orchestrator_settings`) fixes this end-to-end — four conspirators had to change together:

1. **Cgroup escape** — `SubprocessTmuxRunner.spawn` wraps the tmux server launch in `systemd-run --user --scope -- …` (`orchestrator_tmux._user_scope_prefix`), so the server lands in `/user.slice/…` (a sibling of the service). Needs the per-user bus: `XDG_RUNTIME_DIR=/run/user/1000` (set in the unit) + **linger** (`loginctl enable-linger <user>`). Falls back to a legacy in-cgroup spawn if `systemd-run`/the bus is unavailable (session still *starts*, just won't survive). tmux's native systemd integration additionally auto-scopes each pane once the bus is reachable.
2. **No teardown on shutdown** — the lifespan calls `pool.shutdown(kill_sessions=False)` (`app.py`) so a graceful stop drops in-memory pool state but leaves the REPLs running.
3. **Re-attach** — `TmuxPool.acquire` adopts an already-live `hd-<id>` session instead of `new-session` (which errors "duplicate session").
4. **Orphan sweep spares survivors** — `recover_orphans` preserves `hd-*` sessions (still sweeps non-`hd-` junk like the `main` session `~/.tmux.conf` auto-creates).

The unit also sets `PrivateTmp=true`, so the default tmux socket (`/tmp/tmux-1000/hd-orch`) lives in a per-start ephemeral `/tmp` that's destroyed on restart — the survivor would become invisible. `ensure_tmux_socket_dir()` (called at lifespan start, before any tmux call) pins `TMUX_TMPDIR` to `~/.orchestrator/tmux` (shared host fs via `ReadWritePaths`, persistent, pre-created so tmux doesn't fall back to `/tmp`), so spawn + every `_tmux` client + ttyd resolve the same restart-stable socket.

Rollback L1: flip the flag off (Settings → Terminal or `settings.json`) → pre-feature behavior. Known trade-off: survivors that are never re-opened linger until manually killed (the idle evictor only reaps *tracked* slots).

## Replies & artifacts (envelope removed)

The `{"blocks":[…]}` JSON response envelope was **removed** (the ttyd terminal made it unusable — Claude writes prose, not JSON). Replies are now **plain GitHub-flavored Markdown**; both runners finalize the assistant turn as an `assistant_message` markdown block. Choices use the native **AskUserQuestion** tool. Old transcripts stay readable via a self-contained shim in `orchestrator_jsonl._try_parse_envelope` (recognises legacy envelope JSONL, extracts markdown/code/image). System prompts are `v14`; per-agent baselines `v2` (`agent_prompts._seed_or_migrate` backs up + rewrites stale copies, never touches `identity.md`/`custom.md`).

Rich media is a first-class **artifact** instead of an inline block:

- Claude runs the `artifact` CLI skill → writes `<id>.<ext>` + `<id>.json` manifest into the agent's `<cwd>/.artifacts/` (committable to the project repo; **global** agent → `~/.orchestrator/artifacts/global/`). Types: image/audio/video/youtube/chart/map/html/file (chart/map/youtube specs inline in the manifest). The skill **ships in the repo** at `skills/artifacts/` and is installed into the registry on boot by `bundled_skills.seed_bundled_skills()` (like `generate-image`/`notify`), so the CLI lives at `~/.orchestrator/skills-registry/artifacts/scripts/artifacts_cli.py` — no manual rsync.
- `orchestrator_artifacts.py` is the dashboard-side reader/mutator + dir resolver (keyed on lib_id, traversal-guarded). The `/file` route serves with a **safe-mime allowlist + nosniff** so an agent-written manifest can't pick an executable Content-Type on the app origin.
- `orchestrator_events.py` `SessionEventHub` is a process-lifetime per-session SSE channel **independent of the turn runner** (which reaps ~60s after a turn) — `artifact create` pushes a toast, `artifact open <id>` pushes a modal, even between turns. The CLI POSTs `/api/orchestrator/artifacts/notify` (token-gated via `~/.orchestrator/artifact_token`); the browser holds a persistent `GET /sessions/{id}/events` EventSource.
- Frontend: `static/orchestrator-artifacts.jsx` renders the gallery (per-session in the chat panel, per-agent in the Agent panel) + viewer modal, **reusing** the widget renderers in `orchestrator-widgets.jsx`. The runner injects `HD_SESSION_ID`/`HD_LIB_ID`/`HD_NOTIFY_URL`/token-file so the CLI auto-discovers its session.

## Settings page (tabbed)

The Settings view is split from one flat 1885-line file into focused modules (loaded in this order, all before `orchestrator-blocks.jsx`):

- `settings-primitives.jsx` — **the contract**. Publishes `window.useSettings` (per-device localStorage hook — external consumers like `orchestrator-unread`/`-conversation` read it off `window`), `useServerSettings` (GET/PATCH `/api/orchestrator/settings`, optimistic-with-revert, **module-level cache** so the Serwer↔Terminal tab switch doesn't refetch/flash), and the `window.HubSettings` UI atoms: `SettingCard`/`SettingRow`/`ToggleRow`/`FieldRow`/`SettingGroup`/`Toggle`(44px tap)/`Segmented`(wraps)/`SettingSelect`/`NumberField`/`ScopeChip`/`AdvancedDisclosure`/`StatusBanner`.
- `settings-notifications.jsx` / `-voice.jsx` / `-server.jsx` / `-terminal.jsx` — per-tab bodies (`window.Settings{Notifications,Voice,Server,Terminal}`). `-shortcuts-editor.jsx` is the shortcut layout editor, opened as a drawer from the Terminal tab.
- `settings-view.jsx` — the shell: 5 tabs (**Powiadomienia / Głos / Czat / Serwer / Terminal**), owns the single `useSettings` instance and passes `{settings,updateSettings}` to device-scoped tabs; server tabs call `useServerSettings()` themselves. Tabs are **URL-synced** (`/settings/<tab>` via `useRouter().replace` — `parseRoute` keeps `section:'settings'`, so no router/app.jsx change) → deep-linkable + restored on PWA reopen.

Conventions: every group carries a **`ScopeChip`** — `device` (this device / localStorage), `server` (whole server / `~/.orchestrator/settings.json`), or `secrets` (`~/.env`). Niche/rollback knobs (the 5 `*_runner_mode` levers, `pool_prewarm_on_start`, `terminal_instant_attach`, `ttyd_port_min/max`) live behind an `AdvancedDisclosure`; **no settings were deleted** — the "legacy" `programmatic` runner modes are deliberate billing-rollback levers. RWD: the shell root carries `className="settings-root"`; tokens.css bumps form controls to 16px **inside `.settings-root` only** on `≤960px` (kills iOS auto-zoom without touching the rest of the app).

Two backend touches support the redesign: (1) **`GET`/`PUT /api/notify/mutes`** (in `notify_routes.py`) read/write `NOTIFY_DISABLE_TOPICS` **without captcha** (topic names, not a credential) and update `os.environ` live; (2) `secrets_routes._put_env`/`_delete_env` now **sync `os.environ` for the `global` scope** so an inline-written `~/.env` secret (e.g. the Telegram token entered in Settings) takes effect without a process restart.

## Terminal shortcut manager (mobile soft-keyboard)

The mobile soft-keyboard toolbar (`_MobileSoftKeyboard` in `static/orchestrator-terminal-preview.jsx`) sends keys to the live xterm.js terminal **client-side** (synthetic KeyboardEvents into xterm, raw PTY bytes for tmux chords, paste, or special actions) — there is **no managed `~/.tmux.conf`**, so "applying" a change is just the React toolbar re-reading config; no session restart.

A **full views+buttons management panel** in Settings (`SettingsShortcutsEditor` in `settings-shortcuts-editor.jsx`, opened as a drawer from the Terminal tab's `settings-terminal.jsx`) lets the user define the whole layout: add/remove/edit/hide/reorder **views**, and per view add/remove/edit/hide/reorder/**pin** buttons (row actions collapse into a kebab menu on narrow screens). Adding a button picks a **kind** (`_ButtonEditModal`): `send-key` (capture or build), `send-raw` (a string with escapes `\n` / `\x1b` ESC / `\x00` tmux C-Space prefix — live hex preview), `paste-text` (+optional Enter), `slash-command`, or `special` (microphone / upload / clipboard-paste / session-switcher). A 6th kind `modifier` (sticky Ctrl/Alt/Cmd) is schema-only — seeded, not in the Add picker. **Structural anchors** — the view cycler + the red Esc + Tab — are always rendered, never in the layout, so they can't be deleted/bricked. Built-in **⇤ Ctrl+A** / **⇥ Ctrl+E** line-nav buttons ship in the Nawigacja seed.

- **Single source of truth = backend `DEFAULT_LAYOUT`** (Python, in `orchestrator_terminal_shortcuts.py`). `GET` always returns a complete renderable layout (seeded if no file). The frontend keeps the literal `_NAV_KEYS`/`_SPECIAL_KEYS`/`_ACTION_KEYS` arrays ONLY as the flag-off render fallback (a drift test cross-checks them against the seed). Do NOT add a cross-module frontend seed — `orchestrator-shortcuts.jsx` loads before `orchestrator-terminal-preview.jsx`.
- **Flag + rollback (opt-in):** `terminal_shortcuts_enabled` in `orchestrator_settings.py` (default **False**). OFF → the toolbar renders the static default arrays (via the same unified `renderButton`) and the manager hides (level-1 rollback). Level 2: drop the manager UI + the `applyLayout` branch in the toolbar. Level 3: delete `orchestrator_terminal_shortcuts.py`, the routes, the flag, `static/orchestrator-shortcuts.jsx`, and `~/.orchestrator/terminal_shortcuts.json`.
- **Storage (schema v1):** `{version:1, layout:{views:[{id,label,icon,hidden?,buttons:[{id,kind,label,hint?,icon?,pinned?,hidden?,payload}]}]}}` at `~/.orchestrator/terminal_shortcuts.json` (server-side → follows the user across devices). Array order == render order; pinned render first. `GET`/`PUT` (full replace) + `POST .../reset` (reseed); the GET also returns the flag as `enabled`. The store size-gates before `json.load`, re-sanitizes on read/write (caps, per-kind validation, unknown kinds dropped, `send-raw` validated by decoding — control bytes allowed, decoded length capped, malformed escapes drop the button), and `get_layout()` deep-copies. The old sparse-override file is discarded + reseeded.
- **Wiring:** `static/orchestrator-shortcuts.jsx` publishes `window.HubShortcuts` (cached fetch/save store, `useConfig()` hook, `applyLayout`, `decodeEscapes`, `kindLabel`/`labelForButton`/`labelForDescriptor` + key helpers) — loaded before both consumers. The toolbar's `renderButton`/`dispatchButton` switch on `kind` (reusing `_sendKeyToIframe`/`_sendRawToTerminal`/`_pasteToTerminal`); the flag-off path maps the legacy arrays via `_legacyToButton` into the SAME renderer. Saves dispatch `hub:terminal-shortcuts-change` so an open toolbar updates instantly.

## Tasks tab (live)

GitHub-Issues backlog backed by a single Projects v2 board (config under `tasks:` in `config/override.yaml` — see `.example`). Disabled by default; flip `tasks.enabled: true` to mount routes + start the reminder loop.

- **Single source of truth:** GH. Items cached 30 s in memory, schema cached 5 min, both bust on writes. The sidecar at `~/.orchestrator/tasks.json` stores **reminder configs + delivery state only** — never duplicates issue data.
- **Auth:** reuses `library_github._gh_env()` / `_gh_auth_check()` (the existing `gh` CLI keyring). `project` scope is required (`gh auth refresh -s project` if missing).
- **PARA tagging:** issues live in per-area / per-project repos. Area / project encoded as labels (`area:<slug>`, `proj:<slug>`, `global`). The PATCH endpoint **refuses cross-repo moves** with HTTP 422 — close + recreate is the supported flow.
- **Reminders:** `tasks_reminders.scan_and_fire()` runs every 60 s via `cron_store` (job id `tasks-reminders`, command `python -m orbit tasks-reminders-tick`). Each reminder slot is stamped on first fire; restart-after-outage replays within `grace_window_hours` (default 6) and silently no-ops beyond that. Delivery goes through `notify.py` topic `tasks` (Telegram, "Open task" inline-keyboard button).
- **Skill parity:** a project-agnostic Claude skill lives at `~/.claude/skills/gh-tasks/` (CLI + importable `gh_tasks.py` + bootstrap script). The dashboard and the skill don't share code — only the GH project as the source of truth — so each can ship independently. Install on the Hetzner box with a manual rsync of the skill directory.
