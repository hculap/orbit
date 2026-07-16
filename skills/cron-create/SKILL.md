---
name: cron-create
description: Use this skill when the user wants to schedule a recurring or one-shot task (PL+EN triggers — "stwórz cron", "zaplanuj zadanie", "schedule a job", "co minut", "codziennie o", "every weekday", "at 9am", "remind me", "co poniedziałek", "uruchamiaj co dzień"). Creates a job in the dashboard's scheduler registry via POST /api/cron/jobs and confirms with a /scheduler link.
metadata: {"clawdbot":{"emoji":"⏰"}}
---

# cron-create

## What this is

A meta-skill that creates scheduled jobs in the dashboard's scheduler registry.
Posts to `http://127.0.0.1:8766/api/cron/jobs` and reports back with a
`/scheduler/<id>` link.

## How to invoke

Follow these steps in order. Skip nothing — each step has a reason.

### 1. Resolve the target name

- Required format: `kebab-case`, only `a-z0-9-`, length ≤ 64 chars.
- Derive from the user's request (e.g. "morning briefing" → `morning-briefing`).
- If ambiguous, ask once. Don't invent silently — the name becomes the
  URL segment users see in `/scheduler/<id>`.

### 2. Resolve the trigger

Pick the trigger type that best matches user intent:

- **One-shot** ("za 5 min", "tomorrow at 9am", "in 2 hours") →
  `{"type":"date","spec":"<ISO Z>","tz":"Europe/Warsaw"}`. Compute the
  ISO timestamp via Bash:
  ```bash
  date -u -d '+5 minutes' '+%Y-%m-%dT%H:%M:%SZ'
  ```
- **Daily at fixed time** ("codziennie o 9", "every day at 9am") →
  `{"type":"cron","spec":"0 9 * * *","tz":"Europe/Warsaw"}`
- **Weekly on a specific day** ("co poniedziałek o 14:30",
  "every Monday at 2:30pm") →
  `{"type":"cron","spec":"30 14 * * 1","tz":"Europe/Warsaw"}`
  (dow: 0=Sun, 1=Mon, …, 6=Sat)
- **Interval** ("co X minut", "every X hours") →
  `{"type":"interval","spec":"<X>m","tz":"Europe/Warsaw"}` (units: `s|m|h|d`)
- **Other recurring patterns** → cron expression (5-field
  `min hour dom mon dow`); refer to crontab.guru if unsure.

If the user's request is ambiguous (e.g. "every weekday morning" →
which hour?), ask one clarifying question instead of guessing.

### 3. Resolve end condition (optional)

- "5 razy" / "5 times" → `end_condition.max_runs: 5`
- "do 31 grudnia" / "until Dec 31" → `end_condition.until: "2026-12-31T23:59:59+01:00"`
- Otherwise omit `end_condition` (runs forever).

### 4. Resolve action mode

- Default: `mode: "llm"` — agent describes the work in natural language.
  - `action.prompt`: required string.
  - `action.agent`: optional `<kind>/<lib_id>` (e.g. `projects/my-project`).
    Use the current agent if known, else `null` (Global).
- Switch to `mode: "shell"` when user wants a deterministic command
  (`"jako shell command"`, `"run df -h"`, `"exec script.sh"`).
  - `action.command`: required string, ≤4096 chars.

### 5. Resolve destination

- Default: `mode: "rolling"` — single long-lived session per job (so we
  don't flood the session list).
  - `destination.agent`: same as action.agent (or `null` for Global).
- `mode: "fresh"` — only when user asks "new session each fire".
- `mode: "existing"` — user picks a session SID at create time.
  - `destination.session_id`: required.
- `mode: "none"` — fire-and-forget; only run logs preserved. Use for
  pure-shell jobs that don't need a chat trail.

### 6. POST to the dashboard

Construct the payload and POST:

```bash
curl -fsS -X POST http://127.0.0.1:8766/api/cron/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "<name>",
    "enabled": true,
    "trigger": {"type":"<type>","spec":"<spec>","tz":"Europe/Warsaw"},
    "action": {"mode":"<mode>","prompt":"<...>","agent":null},
    "destination": {"mode":"rolling","agent":null},
    "concurrency": "skip",
    "created_by": "agent"
  }'
```

The response contains the canonical job (with derived `id` and computed
`next_run_at`). Capture `id` and `next_run_at` for the confirmation
envelope.

### 7. Confirm to the user

Emit a single `markdown` envelope summarising the new job, then a
`choice` envelope with three options. See **Output contract** below.

## Constraints

- **Timezone:** always `Europe/Warsaw` unless the user explicitly gives
  another IANA tz (e.g. `America/New_York`).
- **No past triggers:** for `type:"date"`, refuse if the spec is in the
  past or within 10 seconds of now. Ask the user to confirm a time
  ahead.
- **Recurring default:** `destination.mode = "rolling"` so a single
  thread accumulates the conversation. Use `"fresh"` only on explicit
  request ("new session each fire").
- **Shell safety:** the command runs as the dashboard user with full
  permissions. Do **not** auto-block, but if the command looks
  destructive (`rm -rf /`, `:(){ :|:& };:`, `dd of=/dev/sda`, etc.)
  warn the user once before posting and ask for confirmation.
- **Ambiguity:** never fabricate a cron expression from a vague
  request. Ask one targeted clarifying question instead.

## Output contract

After the POST succeeds, emit exactly two envelopes:

```json
{"type":"markdown","content":"⏰ Created **<name>** — runs <human-trigger> (next: <relative>). Open </scheduler/<id>>."}
{"type":"choice","prompt":"Co dalej?","options":[
  {"id":"open","label":"Otwórz /scheduler/<id>"},
  {"id":"trigger","label":"Trigger now"},
  {"id":"done","label":"Done"}
]}
```

If the POST fails (non-2xx), emit a single `markdown` envelope with the
error detail and stop. Do not silently retry.

## Examples

### Example 1 — shell, interval, fire-and-forget

User: *"stwórz cron co 10 minut wysyłający `df -h` jako shell"*

Payload:
```json
{
  "name": "df-watch",
  "enabled": true,
  "trigger": {"type":"interval","spec":"10m","tz":"Europe/Warsaw"},
  "action": {"mode":"shell","command":"df -h"},
  "destination": {"mode":"none"},
  "concurrency": "skip",
  "created_by": "agent"
}
```

### Example 2 — LLM, daily, rolling session

User: *"codziennie o 7 rano podsumuj mój ogród"*

Payload:
```json
{
  "name": "garden-morning-brief",
  "enabled": true,
  "trigger": {"type":"cron","spec":"0 7 * * *","tz":"Europe/Warsaw"},
  "action": {
    "mode":"llm",
    "prompt":"Daily garden brief — sensor readings, recent decisions, anything urgent.",
    "agent":"projects/garden"
  },
  "destination": {"mode":"rolling","agent":"projects/garden"},
  "concurrency": "skip",
  "created_by": "agent"
}
```

## Other operations

The full scheduler API surface, for managing jobs after creation. All
endpoints are local to the box (`http://127.0.0.1:8766`) and accept /
return JSON.

### List jobs

```bash
curl -fsS http://127.0.0.1:8766/api/cron/jobs
```

Optional `?agent=<kind>/<lib_id>` (or `?agent=global`) filters to jobs whose
`action.agent` OR `destination.agent` matches — handy when scoped to one
project / area. The response is the array of enriched job dicts (every
field including `next_run_at` and `recent_runs`).

### Get one job (with last 50 runs)

```bash
curl -fsS http://127.0.0.1:8766/api/cron/jobs/<id>
```

`runs` in the response is `[{run_id, status, started_at, finished_at,
duration_ms, exit_code, output_preview, error}]` — newest first.

### Trigger now (manual fire)

```bash
curl -fsS -X POST http://127.0.0.1:8766/api/cron/jobs/<id>/run
```

Use to verify a freshly-created job before its first scheduled fire. Runs
synchronously and returns the run record. Concurrency rules from the job
still apply (a `skip` job with an in-flight run will refuse).

### Pause / resume

```bash
curl -fsS -X POST http://127.0.0.1:8766/api/cron/jobs/<id>/pause
curl -fsS -X POST http://127.0.0.1:8766/api/cron/jobs/<id>/resume
```

Pause flips `enabled: false` (the card shows `paused` and the job's
APScheduler entry is removed). Resume re-arms it.

### Patch (edit)

```bash
curl -fsS -X PATCH http://127.0.0.1:8766/api/cron/jobs/<id> \
  -H 'Content-Type: application/json' \
  -d '{"trigger":{"type":"cron","spec":"0 8 * * *","tz":"Europe/Warsaw"}}'
```

Send only the fields that change. The same validators run as POST, so
trigger / action / destination shape requirements still apply.

### Delete

```bash
curl -fsS -X DELETE http://127.0.0.1:8766/api/cron/jobs/<id>
```

Removes the job from the registry, cancels its APScheduler entry, and
preserves the run history in `runs.jsonl`. There is no soft-delete — be
sure before you call it.

### When to use each operation

- User asks "show me the jobs" → **list jobs**, then summarise (name,
  trigger, last status, next fire). Don't dump raw JSON.
- User asks "did X run last night?" → **get one job**, scan `runs` for
  the relevant date, report status + `output_preview`.
- User wants to test their just-created job → **trigger now**, then read
  the returned run for status + output.
- User says "stop running X for now" → **pause** (preserves the job,
  resumable). Only **delete** when they explicitly say "remove" / "skasuj".
- User wants "every 8am instead of 7am" / "different agent" / "different
  prompt" → **patch** with just the changed sub-tree.
