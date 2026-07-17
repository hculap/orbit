<p align="center">
  <img src="src/orbit/static/orbit-mark.png" alt="Orbit" width="96" height="96">
</p>

# Orbit

**A self-hostable, mobile-first dashboard for your personal server — install it by pasting one prompt to an AI agent.**

Orbit is the front door to a Linux box you own. It auto-discovers a [PARA](https://fortelabs.com/blog/para/) filesystem (`~/Areas`, `~/Projects`, `~/Resources`) plus your nginx-served apps and turns them into a fast, phone-friendly hub — with live system status, a log viewer, a `~/Sync` file browser, a cron scheduler, GitHub-backed tasks, Telegram notifications, and an optional Claude Code "orchestrator" terminal you can drive from your pocket. FastAPI + CDN React, **no build step**. Runs on any VPS or locally.

## Features

- **Auto-discovery** — scans `~/Areas`, `~/Projects`, `~/Resources` and `/etc/nginx/conf.d/apps/*.conf`; a thin `override.yaml` layer tunes labels, icons, and ordering.
- **Mobile-first** — one responsive page, PWA-installable, built for the phone.
- **Live system status** — CPU / RAM / disk from `/proc`, Tailscale peers, `systemctl` service health.
- **Log viewer** — whitelisted `journalctl` units and nginx tails.
- **File browser** — list, upload, zip, and thumbnail anything under `~/Sync`.
- **Cron scheduler** — schedule recurring jobs from the UI.
- **Tasks** — a GitHub Issues + Projects v2 backlog with reminders (opt-in).
- **Notifications** — Telegram alerts with inline actions.
- **Orchestrator terminal** *(optional)* — chat with or drive Claude Code running on the box, backed by a real tmux/ttyd terminal.
- **No build step** — CDN React 18 + Babel-standalone; edit a `.jsx` file and reload.

## Quick start — let an agent install it for you

The fastest way to run Orbit is to **not install it yourself**. Paste the prompt below into **[Claude Code](https://www.anthropic.com/claude-code)** or **[Codex](https://openai.com/codex/)** and let the agent do it. It fetches Orbit's install skill from GitHub and runs it as an **interactive wizard** — it asks you the config questions, installs only what's needed, and stands the service up for you.

```text
Set up Orbit for me. Read and follow the install guide at
https://raw.githubusercontent.com/hculap/orbit/main/skills/orbit-install/SKILL.md
```

The guide is a self-contained wizard — the agent interviews you (where to run it, how to reach it, which features), installs what's needed, and asks before anything that costs money, opens a port, or writes a secret.

What it does:

- **Works with Claude Code or Codex.** Either agent reads the skill straight from GitHub and follows it step by step.
- **Acts as a wizard / configurator.** It interviews you first — where to run it, how to reach it, which features — instead of assuming your setup.
- **Deploys wherever you point it.** The machine you're on, an existing server over SSH, or a freshly provisioned cloud VM — Hetzner, AWS, DigitalOcean, GCP, a Pi, whatever you use.
- **Your access model, your call.** A mesh VPN (e.g. Tailscale), another private path, or public behind TLS + auth.
- **Asks before spending or exposing.** Nothing that costs money, opens a network port, or writes a secret happens without your OK.

## Manual install

Prefer to do it by hand? Orbit is a normal Python app.

```bash
git clone https://github.com/hculap/orbit
cd orbit
uv sync
cp config/override.yaml.example config/override.yaml   # then edit to taste
uv run python -m orbit --host 127.0.0.1 --port 8766
```

For a real deployment, put it behind **nginx** and run it as a **systemd** service — the units and helper live in [`deploy/`](deploy/) and [`scripts/install.sh`](scripts/install.sh).

> [!WARNING]
> **Orbit has no in-app authentication — the network is the security boundary.** Serve it **Tailscale-only** (recommended) or behind an authenticating reverse proxy. **Never** expose it — and *especially* the orchestrator terminal — to the public internet without auth. Bind to `127.0.0.1` and let your proxy / VPN handle access.

## Updating

Orbit ships **tagged releases** — track those, not bleeding-edge `main`.
[`scripts/update.sh`](scripts/update.sh) does the whole update in one step: fetch,
`uv sync`, restart, health-check, and **auto-rollback** if the new version won't
come up healthy.

```bash
cd ~/orbit
scripts/update.sh --ref v0.1.0          # update to a specific release
# …or to the newest release tag:
git fetch --tags origin && scripts/update.sh --ref "$(git tag -l 'v*' | sort -V | tail -1)"
```

Or just tell your agent **"update Orbit"** and it runs this for you. Watch the
[Releases](https://github.com/hculap/orbit/releases) page (or ⭐/Watch the repo)
to know when a new version lands. Tracking `main` (`scripts/update.sh` with no
`--ref`) also works, but gives you every in-progress commit.

## Configuration

Two places hold config:

- **`config/override.yaml`** (gitignored — start from [`config/override.yaml.example`](config/override.yaml.example)) — overlays discovery with custom **labels / icons / ordering**, declares **`external_apps`** (links to services nginx doesn't proxy, e.g. Tailscale-only apps), and holds the **`tasks:`** block wiring the GitHub Projects v2 board.
- **`~/.env`** — secrets and environment: the **Telegram** bot token, **TTS** provider keys, `DASHBOARD_PUBLIC_URL` (your `https://your-domain/`, used to build outbound links), and similar.

## Security

- **Single-tenant.** Orbit assumes one trusted operator — you. There is **no in-app login**; the network is the boundary. Keep it Tailscale-only or behind an authenticating proxy.
- **Agents run with operator-level shell access.** The optional orchestrator drives Claude Code on the host with the same reach you have — it can read files, run commands, and change the system.
- **Run it only on a box you trust and control**, and never expose the dashboard (or the orchestrator) publicly without authentication.

## License

[MIT](LICENSE) © 2026 Szymon Paluch
