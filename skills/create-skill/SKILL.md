---
name: create-skill
description: This skill should be used when the user asks to create a new skill — Polish "stwórz skill", "nowy skill", "dodaj skill"; English "create skill", "scaffold skill", "new skill". Generates SKILL.md frontmatter + body and writes to ~/.orchestrator/skills-registry/<name>/, then triggers a dashboard rescan webhook so the new skill appears in /skills.
metadata: {"clawdbot":{"emoji":"✨","requires":{"bins":["curl"],"env":[]}}}
---

# create-skill

## What this is

A meta-skill that scaffolds a brand-new claude-cli skill into the orchestrator's
registry at `~/.orchestrator/skills-registry/<name>/`, then pings the dashboard
so the new entry shows up under `/skills` immediately.

## How to invoke

Follow these steps, in order. Skip nothing — each step has a reason.

### 1. Resolve the target name

- Required format: `kebab-case`, only `a-z0-9-`, length ≤ 64 chars.
- If the user didn't give a name explicitly, ask once. Don't invent one
  silently — the name is the URL-segment users will see in `/skills/<name>`.
- Reject names that already exist:

  ```bash
  if [ -d ~/.orchestrator/skills-registry/<name> ]; then
    echo "skill <name> already exists — choose another name or uninstall first"
    exit 1
  fi
  ```

### 2. Resolve description + when-to-use triggers

- One-paragraph description. Lead with **"This skill should be used when…"**
  so the model's skill-selector picks it up reliably.
- Include trigger keywords in **both Polish and English** if the user
  operates in PL/EN. Examples: `"stwórz/zrób X"` and `"create/make X"`.
- Pick an emoji for `metadata.clawdbot.emoji` that matches the domain.

### 3. Decide if helper scripts are needed

- If the skill needs to shell out (call an API, transform a file, etc.),
  write the script under `~/.orchestrator/skills-registry/<name>/scripts/`
  with `chmod +x` set. Reference it from the SKILL.md body using
  `{baseDir}/scripts/<script>.sh` so it works regardless of install
  location.
- Pure-instruction skills (no scripts) are fine too — most marketplace
  skills are exactly that.

### 4. Write SKILL.md

Use a `Bash` heredoc so newlines don't get mangled:

```bash
mkdir -p ~/.orchestrator/skills-registry/<name>
cat > ~/.orchestrator/skills-registry/<name>/SKILL.md <<'EOF'
---
name: <name>
description: This skill should be used when ... <PL triggers> ... <EN triggers>.
metadata: {"clawdbot":{"emoji":"<emoji>","requires":{"bins":[],"env":[]}}}
---

# <name>

## What this is
<one-line scope>

## How to invoke
1. ...
2. ...

## Examples
...

## Output
...
EOF
```

### 5. Write register.json

The dashboard reads this to learn the skill's source, install time, and
display metadata. Keep it tiny:

```bash
cat > ~/.orchestrator/skills-registry/<name>/register.json <<EOF
{"name":"<name>","source":"custom","installed_at":$(date +%s),"icon":"<emoji>","description":"<one-line desc>"}
EOF
```

### 6. Trigger the dashboard rescan

The dashboard listens on `127.0.0.1:8766` on the box (the user's
reverse proxy adds TLS externally). The webhook is fire-and-forget:

```bash
curl -fsS -X POST http://127.0.0.1:8766/api/skills/rescan || true
```

If `curl` fails (dashboard down), the new skill is still on disk and will
be picked up on the next `GET /api/skills` poll.

## Constraints

- **Name format:** `^[a-z0-9][a-z0-9-]{0,63}$`. No uppercase, no underscores,
  no slashes, no leading hyphen.
- **No overwrites:** check `ls ~/.orchestrator/skills-registry/<name>/`
  before writing. If it exists, abort and tell the user.
- **No secrets in SKILL.md:** secrets go in env vars referenced by
  `metadata.clawdbot.requires.env`, never hardcoded.
- **Idempotent writes:** the heredocs above use `>` (truncate). That's
  fine on a fresh dir; for re-runs the existence check in step 1 stops
  you before you get here.

## Output

After step 6 succeeds, emit a `markdown` envelope confirming the skill
was created, then a `choice` envelope offering next steps. Example:

```json
{"type":"markdown","content":"✨ Skill **<name>** created at `~/.orchestrator/skills-registry/<name>/` and dashboard notified."}
{"type":"choice","prompt":"Co dalej?","options":[
  {"id":"view","label":"Otwórz w /skills"},
  {"id":"enable_global","label":"Włącz na Global"},
  {"id":"done","label":"Gotowe"}
]}
```

## Examples

### Example 1 — image-resizer

User: *"stwórz skill image-resizer który zmniejsza obrazki"*

Resulting `~/.orchestrator/skills-registry/image-resizer/SKILL.md`:

```markdown
---
name: image-resizer
description: This skill should be used when the user asks to resize/shrink an image — Polish "zmniejsz obrazek", "przeskaluj zdjęcie"; English "resize image", "shrink picture". Calls ImageMagick `convert` with the requested dimensions and returns the new file path.
metadata: {"clawdbot":{"emoji":"🖼️","requires":{"bins":["convert"],"env":[]}}}
---

# image-resizer

## What this is
Resize an image file already attached to the session via ImageMagick.

## How to invoke
1. Resolve target dimensions from the user prompt (e.g. "do 800px szerokości").
2. Run `{baseDir}/scripts/resize.sh <input> <width>x<height>`.
3. Emit the resulting path in a `markdown` envelope.
```

### Example 2 — slack-notifier

User: *"create a skill slack-notifier that posts to #ops"*

Resulting `~/.orchestrator/skills-registry/slack-notifier/SKILL.md`:

```markdown
---
name: slack-notifier
description: This skill should be used when the user asks to send a Slack message — Polish "wyślij na slacka", "powiadom slack"; English "send slack", "post to slack", "notify ops". Posts to the configured Slack webhook.
metadata: {"clawdbot":{"emoji":"💬","requires":{"bins":["curl"],"env":["SLACK_WEBHOOK_URL"]}}}
---

# slack-notifier

## What this is
Post a one-line message to the user's configured Slack incoming webhook.

## How to invoke
1. Compose the message text from the user's request.
2. `curl -fsS -X POST -H 'Content-Type: application/json' --data "{\"text\":\"<msg>\"}" "$SLACK_WEBHOOK_URL"`
3. Confirm with a short `markdown` envelope.
```
