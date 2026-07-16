#!/usr/bin/env bash
# Orbit updater — pull the latest code, re-sync deps, restart, health-check,
# and auto-roll-back to the previous commit if the new one won't come up.
#
# Usage:  scripts/update.sh [--ref <branch-or-tag>]   (default: origin's default branch)
#
# Safe to run from cron/systemd-timer: no-ops when already up to date.
set -euo pipefail

log()  { printf '\033[1;36m[orbit-update]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[orbit-update] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[orbit-update] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

REF=""
[ "${1:-}" = "--ref" ] && { REF="${2:?--ref needs a value}"; }

ORBIT_REPO="${ORBIT_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
ORBIT_UV="${ORBIT_UV:-$(command -v uv || true)}"
[ -n "$ORBIT_UV" ] || die "uv not found on PATH"
cd "$ORBIT_REPO"

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$ORBIT_REPO is not a git checkout"
BRANCH="${REF:-$(git symbolic-ref --quiet --short HEAD || echo main)}"

log "fetching origin…"
git fetch --quiet origin "$BRANCH"
PREV="$(git rev-parse HEAD)"
TARGET="$(git rev-parse "origin/$BRANCH")"

if [ "$PREV" = "$TARGET" ]; then
  log "already up to date ($(git rev-parse --short HEAD)) — nothing to do"
  exit 0
fi

health() {
  for _ in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8766/api/system || true)" = "200" ] && return 0
    sleep 1
  done
  return 1
}

rollback() { # $1 = reason — restore PREV, resync, restart, health-check, then bail
  warn "$1 — rolling back to ${PREV:0:9}"
  git reset --hard "$PREV"
  "$ORBIT_UV" sync --frozen || die "CRITICAL: rollback sync failed — inspect the box manually"
  $SUDO systemctl restart orbit.service
  if health; then
    warn "rolled back; healthy on ${PREV:0:9}"
  else
    die "CRITICAL: unhealthy even after rollback — check journalctl -u orbit -n 80 --no-pager"
  fi
  exit 1
}

log "updating ${PREV:0:9} → $(git rev-parse --short "$TARGET")"
git reset --hard "$TARGET"
# A failed sync on the new commit is itself a "won't come up" case — roll back.
"$ORBIT_UV" sync --frozen || rollback "dependency sync failed on new commit"

log "restarting orbit.service…"
$SUDO systemctl restart orbit.service
health || rollback "new commit unhealthy"
log "✅ updated and healthy on $(git rev-parse --short HEAD)"
