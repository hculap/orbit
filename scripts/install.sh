#!/usr/bin/env bash
# Orbit installer — render + install the systemd service on THIS machine.
#
# Deterministic, idempotent, re-runnable. It does the mechanical on-box parts;
# the interactive wizard (skills/orbit-install/SKILL.md) drives provisioning,
# config, Tailscale, and secrets around it.
#
# Usage:
#   scripts/install.sh [--nginx] [--server-name <name>] [--no-start]
#
# Everything auto-detects; override via env: ORBIT_USER, ORBIT_HOME, ORBIT_UID,
# ORBIT_REPO, ORBIT_UV, ORBIT_SERVER_NAME.
set -euo pipefail

log()  { printf '\033[1;36m[orbit]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[orbit] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[orbit] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

INSTALL_NGINX=0
NO_START=0
while [ $# -gt 0 ]; do
  case "$1" in
    --nginx)        INSTALL_NGINX=1 ;;
    --server-name)
      shift
      [ $# -gt 0 ] || die "--server-name needs a value"
      case "$1" in -*) die "--server-name needs a value" ;; esac
      ORBIT_SERVER_NAME="$1" ;;
    --no-start)     NO_START=1 ;;
    -h|--help)      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
  shift
done

# ── detect the target box ────────────────────────────────────────────────
ORBIT_USER="${ORBIT_USER:-$(id -un)}"
ORBIT_UID="${ORBIT_UID:-$(id -u "$ORBIT_USER")}"
ORBIT_GROUP="${ORBIT_GROUP:-$(id -gn "$ORBIT_USER")}"
ORBIT_HOME="${ORBIT_HOME:-$(getent passwd "$ORBIT_USER" | cut -d: -f6)}"
ORBIT_REPO="${ORBIT_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
ORBIT_UV="${ORBIT_UV:-$(command -v uv || true)}"
ORBIT_SERVER_NAME="${ORBIT_SERVER_NAME:-$(hostname)}"

[ -n "$ORBIT_HOME" ] || die "could not resolve home for user '$ORBIT_USER'"
[ -n "$ORBIT_UV" ]   || die "uv not found on PATH — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v git >/dev/null   || die "git not found — install it first"
command -v curl >/dev/null   || die "curl not found — install it (apt install curl)"
command -v systemctl >/dev/null || die "systemd not found — this installer targets Linux/systemd hosts"
[ -f "$ORBIT_REPO/pyproject.toml" ] || die "no pyproject.toml in ORBIT_REPO=$ORBIT_REPO"

log "user=$ORBIT_USER group=$ORBIT_GROUP uid=$ORBIT_UID home=$ORBIT_HOME"
log "repo=$ORBIT_REPO uv=$ORBIT_UV"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || die "not root and no sudo — re-run as root or install sudo"
  SUDO="sudo"
fi

# ── deps ─────────────────────────────────────────────────────────────────
log "syncing Python deps (uv sync --frozen)…"
( cd "$ORBIT_REPO" && "$ORBIT_UV" sync --frozen )

# ── config scaffold ──────────────────────────────────────────────────────
if [ ! -f "$ORBIT_REPO/config/override.yaml" ]; then
  log "creating config/override.yaml from the example (edit it to taste later)"
  cp "$ORBIT_REPO/config/override.yaml.example" "$ORBIT_REPO/config/override.yaml"
fi
mkdir -p "$ORBIT_HOME/.orchestrator/tmux"

# ── render + install the unit ────────────────────────────────────────────
render() { # $1 = template, $2 = dest (sudo-written)
  local tmp; tmp="$(mktemp)"
  ORBIT_USER="$ORBIT_USER" ORBIT_GROUP="$ORBIT_GROUP" ORBIT_UID="$ORBIT_UID" ORBIT_HOME="$ORBIT_HOME" \
  ORBIT_REPO="$ORBIT_REPO" ORBIT_UV="$ORBIT_UV" ORBIT_SERVER_NAME="$ORBIT_SERVER_NAME" \
    envsubst '${ORBIT_USER} ${ORBIT_GROUP} ${ORBIT_UID} ${ORBIT_HOME} ${ORBIT_REPO} ${ORBIT_UV} ${ORBIT_SERVER_NAME}' \
    < "$1" > "$tmp"
  $SUDO install -m 0644 "$tmp" "$2"
  rm -f "$tmp"
}

command -v envsubst >/dev/null || die "envsubst not found — install 'gettext' (apt install gettext-base)"

log "installing /etc/systemd/system/orbit.service"
render "$ORBIT_REPO/deploy/orbit.service.template" /etc/systemd/system/orbit.service

# Orchestrator session survival across restarts needs a lingering user manager.
log "enabling linger for $ORBIT_USER (orchestrator session survival)"
$SUDO loginctl enable-linger "$ORBIT_USER" || warn "enable-linger failed — orchestrator sessions won't survive restarts"

$SUDO systemctl daemon-reload

# ── nginx (optional) ─────────────────────────────────────────────────────
if [ "$INSTALL_NGINX" -eq 1 ]; then
  command -v nginx >/dev/null || die "--nginx given but nginx not installed"
  # Create the websocket upgrade map FIRST (orbit.conf references
  # $connection_upgrade, so it must be defined before nginx parses the vhost).
  # Gate on our own map file — grepping for the token would match the vhost we
  # are about to write and skip creating the map, breaking `nginx -t`.
  if [ ! -f /etc/nginx/conf.d/orbit-upgrade-map.conf ] \
     && ! $SUDO grep -rqsE 'map[[:space:]]+\$http_upgrade[[:space:]]+\$connection_upgrade' /etc/nginx/nginx.conf /etc/nginx/conf.d/ 2>/dev/null; then
    printf 'map $http_upgrade $connection_upgrade { default upgrade; "" close; }\n' \
      | $SUDO tee /etc/nginx/conf.d/orbit-upgrade-map.conf >/dev/null
  fi
  log "installing /etc/nginx/conf.d/orbit.conf (server_name=$ORBIT_SERVER_NAME)"
  render "$ORBIT_REPO/deploy/nginx-orbit.conf.template" /etc/nginx/conf.d/orbit.conf
  $SUDO nginx -t || die "nginx config test failed — see output above"
  $SUDO systemctl reload nginx
fi

# ── start + health check ─────────────────────────────────────────────────
if [ "$NO_START" -eq 1 ]; then
  log "unit installed; --no-start given, not starting."
  exit 0
fi

log "starting orbit.service…"
$SUDO systemctl enable --now orbit.service

log "waiting for health (127.0.0.1:8766/api/system)…"
ok=0
for _ in $(seq 1 30); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8766/api/system || true)" = "200" ]; then
    ok=1; break
  fi
  sleep 1
done

if [ "$ok" -eq 1 ]; then
  log "✅ Orbit is up on http://127.0.0.1:8766"
  log "next: edit $ORBIT_REPO/config/override.yaml, add secrets to $ORBIT_HOME/.env,"
  log "and reach it via Tailscale (recommended) or an authenticated nginx vhost."
else
  warn "service did not become healthy in 30s — check: journalctl -u orbit -n 50 --no-pager"
  exit 1
fi
