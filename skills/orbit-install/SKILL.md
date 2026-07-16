---
name: orbit-install
description: Provision Orbit (a self-hosted personal-server dashboard) for the user, end-to-end, acting as an interactive wizard/configurator. Works for ANY setup — the local machine, an existing server over SSH, or a freshly provisioned cloud VM (Hetzner, AWS, DigitalOcean, GCP, bare-metal, a Pi …); exposed however the user wants (a mesh VPN like Tailscale, another VPN, or public behind TLS + auth). You do NOT know the user's environment in advance — discover it and ASK. Use when the user pastes the "set up Orbit for me" prompt from the Orbit README, or otherwise asks an agent to install/stand up/deploy Orbit for them. The user should NOT run steps by hand — you do it, asking before anything that costs money, opens a network port, weakens security, or writes a secret.
metadata: {"clawdbot":{"emoji":"🛰️"}}
---

# orbit-install — stand up Orbit for the user

You are an **install wizard**. The user pointed you here so that **you**, not
they, provision [Orbit](https://github.com/hculap/orbit) — a self-hosted,
mobile-first personal-server dashboard (FastAPI + CDN React, Python 3.12,
uv-managed). It runs on **any Linux host with systemd**.

**You do not know the user's environment.** Don't assume a cloud, an OS shell, a
VPN, or where their credentials live. The steps below are **high-level goals with
example commands** — treat the examples as *one way* for the common case and
**adapt to what the user actually has**. When in doubt, **ask**.

## Golden rules

1. **Confirm before consequential actions** (AskUserQuestion, or stop and ask): creating a paid server (money), opening a public port / firewall rule, weakening systemd hardening, writing a secret, or deleting/overwriting anything.
2. **Security first.** Orbit has **no in-app authentication** — the network is the boundary. Never expose it — *especially* the orchestrator terminal — to the public internet without auth. Prefer a private network (VPN/mesh) or an authenticating reverse proxy.
3. **Discover, don't assume.** Detect the OS/distro, the init system, the package manager, and what's already installed before acting.
4. **Idempotent.** Prefer the repo's `scripts/install.sh` / `scripts/update.sh` over hand-rolled steps.

## Phase 1 — Interview & discover

Ask the user (batch with AskUserQuestion), then adapt everything below to the answers:

1. **Where should Orbit run?**
   - **This machine** (you're already on the target), or
   - **An existing server** you'll reach over **SSH** (they give you host + user — could be *anywhere*: a VPS, a home server, a Pi, an EC2 box they already have), or
   - **Provision a new cloud VM** — and if so, **on which provider?** Hetzner, AWS, DigitalOcean, GCP, Vultr, … Use whatever account/CLI *they* have. Only create a paid resource after confirming the spend.
2. **How should it be reached?**
   - **A mesh VPN** (Tailscale, or Nebula/ZeroTier) — recommended, no public port,
   - **Another private path** (WireGuard, an SSH tunnel, LAN-only), or
   - **Public** behind **TLS + authentication** (a domain + reverse-proxy auth).
3. **Which tiers?** Core dashboard is always installed. Optionally: **orchestrator** (Claude Code terminal — needs the `claude` CLI + a one-time interactive login you cannot do for them), **tasks** (GitHub `gh` with `project` scope), **notifications** (Telegram bot token).
4. **Credentials.** For each secret a chosen path needs (cloud API token, VPN auth key, Telegram token, …), **ask the user for it**. If they say "it's already on the machine / in my environment", **discover it** — check env vars and any secrets file *they point you to* (don't assume a specific shell or path like `~/.zshrc`/`~/.zsh_secrets`). Never print secret values.

If the user is vague, propose a sensible default (existing/new VPS, a mesh VPN, core tier) and confirm.

## Phase 2 — Get a target Linux host (systemd)

Goal: a Linux box with systemd where you have a shell and sudo.

- **This machine / existing server**: confirm `systemctl --version` works and you have sudo. For a remote box, `ssh` in (`-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15`).
- **Provision a new VM**: use the user's provider. *Example (Hetzner):* install/[use `hcloud`], create an `ubuntu-24.04` VM, note its IP. The **same shape** applies to AWS (`aws ec2 run-instances`), DigitalOcean (`doctl compute droplet create`), GCP (`gcloud compute instances create`), etc. — pick the one the user uses, confirm the size/region and the cost, and inject an SSH key you control.
- **Recommended**: run Orbit as a **dedicated non-root sudo user** (e.g. `orbit`), not root — create it and copy the SSH key over before continuing.

## Phase 3 — Install runtime dependencies

On the target, using its package manager (`apt`, `dnf`, `pacman`, …). Required for core:

- `git`, `curl`, `tmux`, a `python3` (≥3.12), `gettext` (provides `envsubst`), and — if you'll reverse-proxy — `nginx`.
- **uv** (Astral): `curl -LsSf https://astral.sh/uv/install.sh | sh`, then make it available to systemd (e.g. symlink into `/usr/local/bin`).

Install tier extras only if selected: `ttyd` (orchestrator terminal), the `claude` CLI (orchestrator), `gh` (tasks). Use non-interactive flags (e.g. `DEBIAN_FRONTEND=noninteractive ... -y`) so a headless run doesn't hang.

## Phase 4 — Clone & configure

```bash
git clone https://github.com/hculap/orbit ~/orbit && cd ~/orbit && uv sync --frozen
cp config/override.yaml.example config/override.yaml   # edit to taste
```
Write any secrets the user provided to `~/.env` (the service loads it via
`EnvironmentFile=-`; this is shell-agnostic). **Confirm before writing tokens.**
Only include what applies — e.g. `DASHBOARD_PUBLIC_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. `chmod 600 ~/.env`.

## Phase 5 — Install the service

`scripts/install.sh` auto-detects the user/home/uid/group/uv/repo, renders the
systemd unit from `deploy/orbit.service.template`, enables linger, syncs deps,
starts the service, and health-checks it. Safe hardening is the default.

```bash
scripts/install.sh                                   # private-network / LAN
scripts/install.sh --nginx --server-name orbit.example.com   # if you'll reverse-proxy
```
Operator-parity (giving the orchestrator agent sudo / `/etc` access) is an
**opt-in** documented in the unit template — enable only if the user asks; it
removes real security boundaries.

## Phase 6 — Expose it safely (per the chosen access mode)

Orbit listens on `127.0.0.1:8766`. Bridge it according to Phase-1's answer:

- **Mesh VPN (e.g. Tailscale):** join the box to the user's network, then bridge loopback onto it *without a public port* — e.g. `tailscale up` (using an auth key the user gave you, or hand them the login URL) + `tailscale serve --bg 127.0.0.1:8766`, giving `https://<box>.<tailnet>.ts.net`. Nebula/ZeroTier/WireGuard: expose it only on the private interface.
- **Public + TLS + auth:** only after confirming, open the firewall, get a cert (e.g. certbot), and require auth (e.g. nginx basic-auth or an auth proxy). The nginx template has a commented TLS+auth block.
- **LAN / localhost only:** nothing more to do; reachable on the local network / the box itself.

Then set `DASHBOARD_PUBLIC_URL` in `~/.env` to whatever URL the user will actually use, and restart Orbit so notification deep-links work.

## Phase 7 — Optional tiers finish-up

- **Orchestrator:** after installing `claude`, it needs a one-time interactive OAuth login. **Pause and tell the user** to run `claude` once on the box; you can't do it for them.
- **Tasks:** confirm `gh auth status` shows `project` scope; set the `tasks:` block in `override.yaml`; restart.
- **Notifications:** with the Telegram token/chat-id in `~/.env`, restart and verify via Settings → Notifications → "Test push".

## Phase 8 — Verify & hand off

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/api/system   # expect 200
systemctl is-active orbit
```
Also confirm the chosen access path actually works (e.g. the VPN URL resolves).
Tell the user the exact URL to open, which tiers are live, and anything left for
them (finish `claude` login, approve a VPN device, …). If you created a paid VM,
remind them of the ongoing cost and how to destroy it.

## Updating

```bash
cd ~/orbit && scripts/update.sh   # git pull + uv sync + restart + auto-rollback on failure
```
The user can also just ask you to "update Orbit" and you run that.

## Troubleshooting

- Service won't start: `journalctl -u orbit -n 80 --no-pager`.
- `uv: not found` from systemd: ensure it's on a path the unit sees (e.g. `/usr/local/bin/uv`).
- Orchestrator sessions don't survive restart: `loginctl show-user <user> | grep Linger=yes`.
- nginx 502: Orbit must be listening on `127.0.0.1:8766` and the `connection_upgrade` map present.
