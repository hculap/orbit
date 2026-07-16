#!/usr/bin/env bash
# Zero-downtime blue-green deploy for orbit.
#
# nginx fronts an upstream `hd_backend` = { blue :8766 (primary), green :8866
# (backup) } with `proxy_next_upstream` retry (see
# deploy/nginx-orbit-upstream.conf + the app's location). This
# script never touches nginx — it just sequences the two services:
#   1. bring up the GREEN holder (:8866, HD_STANDBY=1 → no cron) on new code,
#      so there is a live failover target
#   2. restart canonical BLUE (:8766, scheduler owner) onto new code — while
#      blue is down, nginx transparently RETRIES every request on green, so no
#      client ever sees a 502 (no queueing latency either)
#   3. once blue is healthy and nginx has re-routed to it, retire green
# The cron scheduler lives EXCLUSIVELY on blue (green is HD_STANDBY) → the two
# instances never double-fire jobs. Detached tmux sessions are external/shared
# and survive untouched. Auto-rolls-back to the previous commit if new code
# won't come up healthy.  No-op when origin/main == HEAD.
set -euo pipefail

# Single-deploy mutex. Both the self-hosted Actions runner (primary, near-
# instant after a green CI) AND the 2-min fallback timer invoke this script;
# flock -n makes the second caller exit cleanly instead of stacking a blue
# restart on an in-flight deploy. (The origin==HEAD no-op below means the
# skipped caller loses nothing — the lock holder deploys the same commit.)
exec 9>/run/lock/hd-deploy.lock
flock -n 9 || { echo "[deploy] another deploy holds the lock — skipping"; exit 0; }

# Clean RUN checkout — NEVER hand-edited (dev work lives in ~/Projects), so a
# hard reset is safe + deterministic (no merge conflicts / dirty-tree cases).
REPO=/home/YOUR_USER/srv/orbit
BLUE=8766
GREEN=8866
FAIL_TIMEOUT=3   # must match `fail_timeout` in the nginx upstream
cd "$REPO"

git fetch --quiet origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "[deploy] up to date ($(git rev-parse --short HEAD)) — nothing to do"
  exit 0
fi
PREV=$LOCAL

health() {  # $1 = port → echoes seconds-to-healthy, returns 1 on timeout
  local p=$1 i
  for i in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$p/api/system" || true)" = "200" ] \
      && { echo "$i"; return 0; }
    sleep 1
  done
  return 1
}

echo "[deploy] ${PREV:0:9} -> $(git rev-parse --short origin/main)"
git reset --hard origin/main >/dev/null
# Deterministic deps: re-sync (cheap no-op unless uv.lock changed) BEFORE
# green-up so a cold sync can't trip the health() poll mid-deploy.
/usr/local/bin/uv sync --frozen

# 1. green (new code) up as the failover target
echo "[deploy] starting green holder :$GREEN on new code…"
sudo systemctl restart orbit-standby.service
if ! t=$(health "$GREEN"); then
  echo "[deploy] FAIL: green :$GREEN never healthy (bad commit?) — blue untouched" >&2
  sudo systemctl stop orbit-standby.service 2>/dev/null || true
  git reset --hard "$PREV" >/dev/null
  exit 1
fi
echo "[deploy] green healthy (${t}s) — nginx will fail over to it during blue's restart"

# 2. restart blue onto new code; nginx auto-retries on green while blue is down
echo "[deploy] restarting blue :$BLUE onto new code…"
sudo systemctl restart orbit.service
if ! t=$(health "$BLUE"); then
  echo "[deploy] FAIL: blue :$BLUE unhealthy after restart — rolling back to ${PREV:0:9}" >&2
  git reset --hard "$PREV" >/dev/null
  sudo systemctl restart orbit.service || true
  health "$BLUE" >/dev/null && echo "[deploy] rolled back; blue healthy on ${PREV:0:9}" >&2 \
                            || echo "[deploy] CRITICAL: blue unhealthy even after rollback" >&2
  sudo systemctl stop orbit-standby.service 2>/dev/null || true
  exit 1
fi
echo "[deploy] blue healthy (${t}s)"

# 3. let nginx re-route to the recovered blue (fail_timeout window), re-probe it,
#    THEN retire green so no request lands on a stopped backup.
sleep "$((FAIL_TIMEOUT + 2))"
curl -s -o /dev/null --max-time 5 "http://127.0.0.1:$BLUE/api/system" || true
sudo systemctl stop orbit-standby.service
echo "[deploy] DONE — zero-downtime, live on blue at $(git rev-parse --short HEAD)"
