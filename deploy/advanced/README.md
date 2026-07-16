# Advanced deploy — zero-downtime blue-green + push-to-deploy

Most people should use the **simple** single-service install (`scripts/install.sh`,
`deploy/orbit.service.template`) and update with `scripts/update.sh`. That has a
few seconds of downtime on restart — fine for a personal box.

This directory is for power users who want **zero-downtime** deploys and
**push-to-deploy** via their own self-hosted GitHub Actions runner. Everything
here uses `YOUR_USER` / `YOUR_UID` / `your-host` placeholders — adapt them.

## How it works

Two instances behind an nginx upstream with retry:

- **blue** (`orbit.service`, port `8766`) — canonical; owns the cron scheduler.
- **green** (`orbit-standby.service`, port `8866`, `HD_STANDBY=1`, no scheduler) —
  a transient failover target spun up only during a deploy.

`deploy.sh` brings green up on the new code, restarts blue onto the new code
(nginx transparently retries requests on green while blue restarts, so no client
sees a 502), then retires green. It runs from a **clean RUN checkout** that it
`git reset --hard`s — keep your dev checkout elsewhere so the two never fight.
It auto-rolls-back to the previous commit if the new code won't come up healthy.

## Files

| File | Purpose |
|------|---------|
| `orbit.service` | blue (canonical) unit |
| `orbit-standby.service` | green (standby) unit — `HD_STANDBY=1` |
| `nginx-orbit-upstream.conf` | nginx `upstream` with blue primary + green backup + retry |
| `deploy.sh` | the blue-green flip (fetch, sync, green-up, blue-restart, health, rollback) |
| `orbit-deploy.service` + `orbit-deploy.timer` | a 2-min poll fallback that runs `deploy.sh` when the runner is offline |
| `deploy.yml.example` | the GitHub Actions workflow (test gate → self-hosted deploy job) |

## Setup sketch

1. Install a [self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners) on the box; give it a label and set `<RUNNER_LABEL>` in `deploy.yml.example`.
2. Set up two checkouts: a **RUN** checkout `deploy.sh` hard-resets, and your **dev** checkout.
3. Render/adapt the units (`User=`, paths, uid) and install them to `/etc/systemd/system/`.
4. Install `nginx-orbit-upstream.conf` + a server-level `location` proxying to the `orbit` upstream.
5. Enable linger, passwordless sudo for the `systemctl` restarts, and a writable `/run/lock`.
6. Copy `deploy.yml.example` to `.github/workflows/deploy.yml` in your fork.
