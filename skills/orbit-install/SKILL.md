---
name: orbit-install
description: Provision Orbit (a self-hosted personal-server dashboard) for the user, end-to-end, acting as an interactive wizard/configurator. Handles a brand-new Hetzner Cloud VPS, an existing VPS over SSH, or the local machine; Tailscale-only (recommended) or public-with-auth exposure; and installs only the tiers the user wants (core dashboard, optional orchestrator/tasks/notifications). Use when the user pastes the "set up Orbit for me" prompt from the Orbit README, or otherwise asks an agent to install/stand up/deploy Orbit for them. The user should NOT run install steps by hand — you do it, asking before anything that costs money, opens a network port, weakens security, or writes a secret.
metadata: {"clawdbot":{"emoji":"🛰️"}}
---

# orbit-install — stand up Orbit for the user

You are an **install wizard**. The user pointed you at this file so that **you**,
not they, provision [Orbit](https://github.com/hculap/orbit) — a self-hosted,
mobile-first personal-server dashboard (FastAPI + CDN React, Python 3.12,
uv-managed). Drive the whole thing: ask the config questions, provision, install
dependencies, render config, stand up the service, and verify it's live.

You may have **no prior context** about Orbit — everything you need is here and
in the repo you'll clone. Read this whole file before acting.

## Golden rules

1. **Confirm before consequential actions.** ALWAYS ask the user (AskUserQuestion,
   or just stop and ask) before you:
   - create a paid cloud server (money),
   - open a public network port / firewall rule,
   - weaken systemd hardening (operator-parity mode),
   - write a secret/token, or delete/overwrite anything.
2. **Security first.** Orbit has **no in-app authentication** — the network is
   the security boundary. Default to **Tailscale-only**. Never expose the
   orchestrator terminal to the public internet without auth.
3. **Show your work.** Run a command, report the result. Don't narrate ahead.
4. **Idempotent.** `scripts/install.sh` and `scripts/update.sh` are re-runnable;
   prefer them over hand-rolled steps.

## Step 0 — Interview the user

Ask (batch these — use AskUserQuestion):

1. **Where should Orbit run?**
   - **A new Hetzner Cloud VPS** (you'll create it — needs a Hetzner API token)
   - **An existing VPS** (you'll `ssh` in — needs host + user)
   - **This machine** (local Linux with systemd)
2. **How will the user reach it?**
   - **Tailscale-only** (recommended; needs a Tailscale auth key)
   - **Public with TLS + basic-auth** (needs a domain pointed at the box)
   - **Localhost only** (dev)
3. **Which tiers?** Core dashboard is always installed. Optionally:
   - **Orchestrator** (Claude Code terminal) — needs `claude` CLI + an
     interactive login you (the agent) CANNOT do for them; you'll install it and
     pause for them to run `claude` once.
   - **Tasks** (GitHub Projects board) — needs `gh` auth with `project` scope.
   - **Notifications** (Telegram) — needs a bot token + chat id.

Record the answers; they drive the steps below. If the user is vague, default to:
new-or-existing VPS, **Tailscale-only**, **core tier only**.

## Step 1 — Provision the box

### New Hetzner VPS
Ask for the **Hetzner Cloud API token** (Project → Security → API Tokens,
Read&Write) if not already in the environment. **Confirm the spend** (a CX22 in
fsn1 is ~€4/mo, billed hourly) before creating.

```bash
# hcloud CLI (install if missing)
command -v hcloud || { curl -fsSL https://github.com/hetznercloud/cli/releases/latest/download/hcloud-linux-amd64.tar.gz | tar xz -C /tmp && sudo mv /tmp/hcloud /usr/local/bin/; }
export HCLOUD_TOKEN=<the token>          # do NOT print the token

# an SSH key hcloud will inject (reuse the user's, or make a throwaway)
ssh-keygen -t ed25519 -f ~/.ssh/orbit_deploy -N '' -C orbit-deploy
hcloud ssh-key create --name orbit-deploy --public-key-from-file ~/.ssh/orbit_deploy.pub

hcloud server create --name orbit --type cx22 --image ubuntu-24.04 \
  --location fsn1 --ssh-key orbit-deploy
IP=$(hcloud server ip orbit); echo "server at $IP"
```

SSH in as `root@$IP` (with `-i ~/.ssh/orbit_deploy`). Create a dedicated sudo
user to run Orbit (don't run it as root):

```bash
ssh -i ~/.ssh/orbit_deploy -o StrictHostKeyChecking=accept-new root@$IP '
  id orbit >/dev/null 2>&1 || adduser --disabled-password --gecos "" orbit
  usermod -aG sudo orbit
  echo "orbit ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/orbit
  install -d -o orbit -g orbit /home/orbit/.ssh
  cp /root/.ssh/authorized_keys /home/orbit/.ssh/ && chown orbit:orbit /home/orbit/.ssh/authorized_keys
'
```

From here on, work as `orbit@$IP`.

### Existing VPS
`ssh` in with the host/user the user gave. Make sure they have sudo. Continue.

### Local machine
Work directly. Ensure it's Linux with systemd (`systemctl --version`). Continue.

## Step 2 — Install system dependencies

On the target box (via ssh or locally). **Required** for the core tier:

```bash
sudo apt-get update
sudo apt-get install -y nginx git curl tmux python3 gettext-base
# uv (Astral) — runtime + dependency manager
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
# make uv available to the systemd unit
sudo ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv
```

Install **only if the user opted into that tier**:
- Orchestrator terminal: `sudo apt-get install -y ttyd` (or build ≥1.7.0), and
  the `claude` CLI: `curl -fsSL https://claude.ai/install.sh | sh` (auth is
  interactive — see Step 6).
- Tasks: `sudo apt-get install -y gh` then `gh auth login` + `gh auth refresh -s project`.
- Notifications: nothing to install — just a Telegram bot token (Step 5).

## Step 3 — Clone Orbit

```bash
git clone https://github.com/hculap/orbit ~/orbit
cd ~/orbit
uv sync --frozen
```

## Step 4 — Configure

```bash
cp config/override.yaml.example config/override.yaml
```
Edit `config/override.yaml` per the user's answers (area/project labels+icons,
`external_apps`, and — if they enabled tasks — the `tasks:` block). Ask the user
for any values you don't know; keep the file minimal if they don't care.

Write secrets to `~/.env` (the unit loads it via `EnvironmentFile=-`). **Confirm
before writing any token.** Only add what applies:
```bash
cat >> ~/.env <<'EOF'
# DASHBOARD_PUBLIC_URL=https://<how the user reaches Orbit>   # enables notification deep-links
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
EOF
chmod 600 ~/.env
```

## Step 5 — Install the service

Run the installer. It auto-detects user/home/uid/uv/repo, renders the systemd
unit, enables linger, syncs deps, starts the service, and health-checks it:

```bash
# Tailscale-only or localhost: no nginx server_name needed
scripts/install.sh
# Public with a domain: also install the nginx vhost
# scripts/install.sh --nginx --server-name orbit.example.com
```

Safe hardening is the default (`NoNewPrivileges=true`, `ProtectSystem=strict`).
Only if the user explicitly wants the orchestrator agent to have full
operator/sudo parity, edit `/etc/systemd/system/orbit.service` per the commented
opt-in block and `sudo systemctl daemon-reload && sudo systemctl restart orbit`
— **ask first; it removes real security boundaries.**

## Step 6 — Wire up access

### Tailscale-only (recommended)
```bash
command -v tailscale || curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname orbit            # prints a login URL — or use:
# sudo tailscale up --authkey <tskey-...> --hostname orbit

# Orbit listens ONLY on 127.0.0.1 (safe). Bridge it onto the tailnet over HTTPS
# so your other devices can reach it — without opening any public port:
sudo tailscale serve --bg 127.0.0.1:8766
```
Ask the user for a Tailscale **auth key** (admin console → Settings → Keys) to
join non-interactively, or hand them the printed login URL. `tailscale serve`
then exposes Orbit at `https://orbit.<tailnet>.ts.net` (TLS via Tailscale,
**tailnet-only — no public port**). Verify with `tailscale serve status`. Set
`DASHBOARD_PUBLIC_URL=https://orbit.<tailnet>.ts.net` in `~/.env` and restart
Orbit (`sudo systemctl restart orbit`) so notification deep-links work.

### Public with TLS + auth
Only if the user chose this and pointed a domain at the box. Open the firewall
**after confirming**, get a cert, and add basic-auth:
```bash
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw --force enable
sudo apt-get install -y certbot python3-certbot-nginx apache2-utils
sudo certbot --nginx -d orbit.example.com
sudo htpasswd -c /etc/nginx/orbit.htpasswd <username>   # prompts for a password
```
Then enable the TLS+auth server block in `/etc/nginx/conf.d/orbit.conf` (see the
commented block in the template) and `sudo nginx -t && sudo systemctl reload nginx`.

## Step 7 — Optional tiers finish-up

- **Orchestrator**: after installing `claude`, it needs a one-time interactive
  login. **Pause and tell the user** to run `claude` in a terminal on the box
  (`ssh` in) and complete the OAuth login, then enable the terminal in Orbit's
  Settings. You cannot do this for them.
- **Tasks**: confirm `gh auth status` shows the `project` scope, set the
  `tasks:` block in `override.yaml`, restart Orbit.
- **Notifications**: with the Telegram token/chat-id in `~/.env`, restart Orbit
  and use Settings → Notifications → "Test push" to verify.

## Step 8 — Verify & hand off

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/system   # expect 200
systemctl status orbit --no-pager
```
Tell the user the exact URL to open, which tiers are live, and what's left for
them (e.g. finish `claude` login, or approve the Tailscale device). If you
created a Hetzner server, remind them of the hourly cost and how to destroy it
(`hcloud server delete orbit`).

## Updating later

To upgrade an existing install (pull latest, re-sync, restart, auto-rollback on
failure):
```bash
cd ~/orbit && scripts/update.sh
```
The user can also just ask you to "update Orbit" and you run that.

## Troubleshooting

- Service won't come up: `journalctl -u orbit -n 80 --no-pager`.
- `uv: not found` from systemd: ensure `/usr/local/bin/uv` symlink exists.
- `systemctl --user`/orchestrator sessions don't survive restart: confirm
  `loginctl show-user <user> | grep Linger=yes`.
- nginx 502: confirm Orbit is listening on `127.0.0.1:8766` and the
  `connection_upgrade` map exists.
