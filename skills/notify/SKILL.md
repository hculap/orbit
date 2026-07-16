---
name: notify
description: Use this skill when the user wants a push notification to their phone for an async result, long-running task completion, or attention-worthy error (PL+EN triggers — "notyfikacja", "powiadom mnie", "ping mnie", "daj znać kiedy", "notify me", "send notification", "alert me", "ping me when", "let me know when"). Posts to POST /api/notify which forwards to a Telegram bot. NOT for every reply — only when the user explicitly opted in or the task is genuinely async.
metadata: {"clawdbot":{"emoji":"🔔"}}
---

# notify

## What this is

A meta-skill that publishes a mobile push notification through the
dashboard's Telegram bot. POSTs to `http://127.0.0.1:8766/api/notify`
and returns once Telegram acknowledges; the user's phone (subscribed
to the bot via `t.me/<bot_username>`) lights up within a couple of
seconds.

Telegram features the helper exposes:
- **Tappable button** under the message that opens any URL (default
  per-topic, e.g. cron → "Open scheduler").
- **Multi-button row** (up to 5) for richer follow-up actions.
- **Image / audio / document / video attachment** via `attach`.
- **HTML formatting** in the message body (auto-escaped, but title
  renders as bold automatically).
- **Silent push** (priority 1) and **urgent** prefix emoji (priority
  4–5).

## When to use

Fire a notification when:

- A long-running task you kicked off has finished and the result is
  ready (the user explicitly asked you to ping them, or the task took
  more than a couple of minutes and the chat tab is likely closed).
- An async result the user was waiting for has arrived.
- An error needs the user's attention NOW and the chat won't catch
  their eye on its own (e.g. they're away from the laptop).

Do NOT fire a notification for:

- Every chat reply. The chat panel already streams live; pinging the
  phone for every turn is spam.
- Trivial confirmations ("ok, done", "got it").
- Reasoning / thinking-aloud turns where there's no concrete result.
- Multiple times per turn. Cap: at most ONE notify per turn.

If unsure, ask once: *"Czy chcesz, żebym wysłał Ci powiadomienie kiedy
skończę?"* / *"Want me to ping your phone when this is done?"*

## How to invoke

Compose the JSON body and POST. The endpoint is local; no auth needed
(Tailscale is the boundary).

```bash
curl -fsS -X POST http://127.0.0.1:8766/api/notify \
  -H 'Content-Type: application/json' \
  -d '{
    "topic": "agent",
    "title": "Garden brief ready",
    "message": "3 sensors flagged · 2 PRs awaiting review.",
    "priority": 3,
    "click": "https://<your-dashboard>/chat/<session_id>"
  }'
```

The response is `{"ok": true, "topic": "agent", "message_id": <int>}`
on success or `{"ok": false, "topic": "<...>", "reason": "<...>"}` on
any failure. On non-2xx, surface the error in your reply and stop —
don't silently retry.

## Field reference

- **`topic`** (string, required) — logical channel for the alert.
  Default: `"agent"`. Other valid topics on this box: `cron`, `chat`,
  `system`. The topic is shown as a hashtag in the message and
  mapped to a per-topic emoji + button label (cron → ⏰ "Open
  scheduler", agent → 🤖 "Open chat", chat → 💬, system → 🛠️).

- **`title`** (string, optional) — short headline shown **bold** at
  the top of the push. Aim for ≤ 60 chars. Include the task name.

- **`message`** (string, required) — body text. ≤ 1500 chars. Long
  output (logs, error tails) is better attached as a file — see
  `attach` below.

- **`priority`** (int 1–5, optional, default 3):
  - `1` (min) — info dump, **silent push** (no sound / vibration).
  - `3` (default) — normal task done.
  - `4` — needs attention soonish (`⚠️` prefix).
  - `5` (max) — urgent / on fire (`🚨` prefix). Use sparingly.

- **`tags`** (list of strings, optional) — extra hashtags for filtering
  in Telegram (e.g. `["failed", "garden"]` → `#failed #garden`).

- **`click`** (URL, optional) — primary inline-keyboard button under
  the message. **Always set this when you have a session id** so the
  user's tap lands them back in context:
  `https://<your-dashboard>/chat/<session_id>`. Per-topic default
  button labels are picked automatically.

- **`actions`** (list, optional) — extra buttons in the same row.
  Each entry: `{"action": "view", "label": "<short>", "url": "<...>"}`.
  Max 5 buttons total (including `click`). Useful for "View logs",
  "Retry", "Dismiss".

- **`attach`** (string, optional) — file path under `$HOME` / `/tmp`,
  or `https://` URL. Type is auto-detected from extension:
  - `.png .jpg .jpeg .gif .webp` → photo (10 MB cap)
  - `.mp3 .m4a .ogg .opus .wav .flac` → audio
  - `.mp4 .mov .webm` → video
  - everything else → document (50 MB cap)
  When `attach` is set, `message` becomes the caption. Inline-keyboard
  buttons still work.

- **`code`** (string, optional) — raw text rendered as a Telegram
  monospace block (`<pre>`). Use whenever the content has columns,
  alignment, or shell-like formatting that you want to survive (e.g.
  `df -h` output, JSON dump, log tail, stack trace). Auto-escaped, so
  pass raw text — no need to worry about HTML chars. Truncated at
  3500 chars (Telegram's text-message cap is 4096 with the rest of
  the body). For larger blobs use `attach` instead — they ship as a
  file the user can scrub through.

## Constraints

- **Cap: 1 notify per turn.** If you already fired one for this turn
  and need to update the user, edit your reply instead — don't queue
  another push.
- **Don't notify yourself.** If your turn is just "I'm thinking…"
  filler, skip the notification. Wait until you have a concrete
  message.
- **Keep `topic` to `"agent"`** unless the user redirected you.
  Cross-posting to `cron` / `system` confuses the topic semantics.
- **Always set `click` to the current session URL** when you have
  it — a tap with no click just opens Telegram, which is unhelpful.
- **Don't include secrets in `title` / `message`.** Telegram has
  the message contents server-side; treat it as third-party trust.
- **`attach` paths must live under `$HOME` or `/tmp`.** Other paths
  (e.g. `/etc/...`) are rejected by the helper for safety.

## Output contract

After a successful publish, emit a single short `markdown` envelope
confirming what you sent. Do NOT include the full JSON body in the
chat — the user already gets the push.

```json
{"type":"markdown","content":"🔔 Sent ping: \"<title>\""}
```

If the POST fails (non-2xx) or the response carries `ok: false`,
emit a single `markdown` envelope with the error detail and stop.

## Examples

### Example 1 — long task done

User: *"Zrób mi briefing ogrodu i powiadom mnie kiedy skończysz."*

After completing the brief, POST:

```json
{
  "topic": "agent",
  "title": "Garden brief ready",
  "message": "3 sensors flagged · 2 PRs awaiting review · soil moisture down 8%.",
  "priority": 3,
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

### Example 2 — agent stuck on a question

```json
{
  "topic": "agent",
  "title": "Need your call",
  "message": "Refactor blocked: rename SecretsManager → CredentialsManager?",
  "priority": 4,
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

### Example 3 — urgent failure with multiple actions

```json
{
  "topic": "agent",
  "title": "Migration FAILED",
  "message": "DB migration aborted at step 4/8. Rollback ran.",
  "priority": 5,
  "click": "https://<your-dashboard>/chat/<session_id>",
  "actions": [
    {"action": "view", "label": "Logs", "url": "https://<your-dashboard>/logs?session=<session_id>"},
    {"action": "view", "label": "Retry", "url": "https://<your-dashboard>/chat/<session_id>?prompt=retry"}
  ]
}
```

### Example 4 — generated image (e.g. via generate-image skill)

After saving an image at `~/.orchestrator/uploads/<session_id>/chart.png`:

```json
{
  "topic": "agent",
  "title": "Chart ready",
  "message": "Last 7 days · garden moisture vs. soil temp.",
  "priority": 3,
  "attach": "~/.orchestrator/uploads/<session_id>/chart.png",
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

### Example 5 — TTS audio (e.g. via generate-audio skill)

```json
{
  "topic": "agent",
  "title": "Audio gotowy",
  "message": "Voice memo for the briefing — 1m 12s.",
  "priority": 3,
  "attach": "~/.orchestrator/tts-cache/<sha256>.mp3",
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

### Example 6 — long error log as file

When the body would exceed ~500 chars, write it to a temp file and
attach instead — keeps the push readable:

```json
{
  "topic": "agent",
  "title": "Build failed — see log",
  "message": "127 errors, 42 warnings. Full log attached.",
  "priority": 4,
  "attach": "/tmp/build-failure-<timestamp>.txt",
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

### Example 7 — diff / shell output with preserved formatting

Use `code` when the content's value depends on alignment (columns,
indentation, monospace tools). Without `code`, Telegram renders it as
a paragraph and columns collapse:

```json
{
  "topic": "agent",
  "title": "Disk check",
  "message": "All within thresholds.",
  "priority": 3,
  "code": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       150G   23G  122G  16% /",
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

Same idea for log tails, JSON dumps, git diffs:

```json
{
  "topic": "agent",
  "title": "Build summary",
  "message": "Compile clean. 3 warnings.",
  "code": "src/foo.py:12: W: unused import\nsrc/bar.py:88: W: line too long\nsrc/baz.py:3: W: missing docstring",
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```

### Example 8 — silent FYI

```json
{
  "topic": "agent",
  "title": "Index reflow done",
  "message": "Background reindex completed in 14s. No action needed.",
  "priority": 1,
  "click": "https://<your-dashboard>/chat/<session_id>"
}
```
