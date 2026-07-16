---
name: a2a
description: Send messages BETWEEN Claude Code agents running on this host (agent-to-agent). `send` is a PURE ENQUEUE — the dashboard writes the message into the target agent's maildir and returns; there is NO push/revive, the target's human drains it later. Use to hand off work to another agent (a PARA area/project agent or the global agent) or reply to one that mailed you. Verbs — `list` (who's around, warm/cold, PARA dir + sessions), `whois <lib_id>` (one agent's full identity + dir + all sessions incl. transcript paths), `send --to <lib_id|global>` (enqueue a message), `inbox --drain` (read + clear YOUR own mailbox), `read <id>`. NOT for talking to the human user — that's normal chat.
metadata:
  emoji: 📬
---

# A2A — agent-to-agent messaging (a2a)

## 1. What this is

A2A is a **same-host mailbox bus** between Claude Code agents running under the
orbit. Every agent is identified by its PARA **lib_id**
(`areas/Dom`, `projects/foo`, `resources/bar`) or `global`. Each owns a maildir
at `~/.orchestrator/a2a/<key>/` with `inbox/ tmp/ cur/`.

You use this skill to **talk to other agents**, not to the human:

- **send** a message to another agent. This is a **pure enqueue**: the dashboard
  SERVER writes the envelope into the target's `inbox/` and **returns**
  (`delivery=enqueued`). There is NO warm pickup, NO cold revive, NO push — the
  target is not woken. Its human later opens that agent and runs
  `a2a inbox --drain` to read the mail.
- **drain your own inbox** — read messages other agents sent YOU, then act on
  each (they are agent requests, not user prompts).
- **discover** the roster with `list`, or one agent in depth with `whois` —
  who exists, their PARA directory, and each session's title + transcript path
  (so you can read another agent's files/session directly; agents already have
  filesystem access).

> This is agent↔agent only. To answer the human, just reply in chat as usual.
> There is no listener/arming/Monitor watcher in v2 — mail is drained manually.

## 2. Identity & maildir

- **Your** identity = `HD_LIB_ID` (e.g. `areas/Dom`). Unset/empty → you are the
  **global** agent (`global`).
- The server stamps the `from` field on every message from your session's
  sidecar lib_id — **you cannot spoof who a message is from**.
- maildir key: `lib_id.replace("/", "__")`; `global` → `__global__`. Traversal
  (`..`) and non-PARA ids are rejected.

## 3. How to invoke

```bash
CLI=~/.orchestrator/skills-registry/a2a/scripts/a2a_cli.py

# Who's around? (warm = live session; each agent shows its PARA dir + sessions)
python3 $CLI list
python3 $CLI list --json

# One agent in depth: full identity.md, PARA dir, ALL sessions + transcript paths
python3 $CLI whois areas/Dom
python3 $CLI whois global --json

# Send (enqueue) a message to another agent (inline text)
python3 $CLI send --to areas/Dom "Please re-check the salon heating schedule."

# Send to the global agent, from stdin
echo "Build summary is ready — pull the latest artifacts." \
  | python3 $CLI send --to global --stdin

# Reply to a message you received (correlate it back)
python3 $CLI send --to projects/foo --type reply \
  --correlation-id a2a-20260619T120000-a3f9c1 "Done — deployed at 12:05."

# Read YOUR inbox
python3 $CLI inbox                # list, oldest-first, one-line previews (no move)
python3 $CLI inbox --drain        # print each in full + move to cur/ (mark read)
python3 $CLI inbox --json         # machine-readable

# Read one specific message (moves inbox→cur if still unread)
python3 $CLI read a2a-20260619T120000-a3f9c1
```

`send` text is either a positional arg **or** `--stdin` (not both). `--to` is a
target lib_id (`areas/Dom`, `projects/foo`, `resources/bar`) or `global`.
`--session <uuid>` optionally routes into a specific session's sub-maildir (get
ids from `a2a list` / `a2a whois`) — still a pure enqueue.

`list` prints, per agent, `lib_id  [warm/cold]  name`, its PARA `dir`, the first
paragraph of its identity, and every live session as `↳ <title> — <sid>` plus
that session's transcript `.jsonl` path. `whois <lib_id>` prints the same header
plus the FULL identity and ALL sessions (live **and** cold), each tagged
`[live]`/`[cold]` with its transcript path.

## 4. Output contract

`send` prints **exactly one** echo line — parse it, don't paste raw paths:

```
sent · id=a2a-20260619T120000-a3f9c1 · to=areas/Dom · delivery=enqueued
```

- `id` — the message id (use it as a `--correlation-id` when replying).
- `to` — the target you addressed.
- `delivery` — always `enqueued` in v2 (the envelope is in the target's maildir;
  its human drains it later).

`inbox`/`read` print one block per message: a `[id] from=… type=… ts=…` header
then the text.

On a 403 the CLI prints **"A2A disabled or unauthorized"** and exits non-zero —
flip the `a2a_enabled` server flag (Settings → Serwer) or check the token.

## 6. Message envelope (read-only)

The dashboard SERVER is the only writer of inbox envelopes. You never craft the
JSON — `send` only carries `{to, type, text, …}` and the server fills in the
rest. A delivered envelope looks like:

```json
{
  "id": "a2a-20260619T120000-a3f9c1",
  "from": "global",
  "to": "areas/Dom",
  "type": "message",
  "correlation_id": null,
  "reply_to": null,
  "ts": "2026-06-19T12:00:00+00:00",
  "ttl": 86400,
  "schema_version": 1,
  "hops": 0,
  "payload": { "text": "…", "meta": {} }
}
```

`type` is one of `message` / `reply` / `system`. `payload.text` is a non-empty
string ≤ 16 KB.

## 7. Etiquette

- Messages you receive are **agent requests**, not user prompts — act on them,
  then optionally `send --type reply --correlation-id <id>` back.
- Delivery is manual: don't block waiting for a reply. The target reads its mail
  when its human next drains the inbox.
- Keep it best-effort: no hard loop/cost guardrails exist. Don't fan a message
  out to many agents in a tight loop.
- Same host only. There is no cross-machine routing.

## Storage & resolution

- Maildir root: `~/.orchestrator/a2a/<key>/` (`inbox/ tmp/ cur/`).
- Your lib_id is read from `HD_LIB_ID` (unset → `global`). Session id from
  `HD_SESSION_ID` → `ORCHESTRATOR_SESSION_ID` → the tmux session name.
- Dashboard URL: `HD_NOTIFY_URL` → `config.json` → `http://localhost:8766`.
- Auth token (sent as `X-A2A-Token` on `send`): `HD_ARTIFACT_TOKEN_FILE` →
  `~/.orchestrator/artifact_token` — the SAME token the artifacts skill uses.

## Resources

- **scripts/a2a_cli.py** — argparse CLI (the entrypoint above).
- **scripts/a2a_lib.py** — importable, stdlib-only Python API.
- **scripts/bootstrap.sh** — idempotent setup + offline smoke test. It no longer
  arms any watcher (v2 has no listener) — mail is drained manually.
- **config.json** — `{"dashboard_url": "..."}`.
