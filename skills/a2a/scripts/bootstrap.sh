#!/usr/bin/env bash
# bootstrap.sh — idempotent setup for the `a2a` (agent-to-agent) skill.
#
# The a2a skill is mostly self-contained (stdlib Python). Sending a message
# needs the dashboard online, but reading/draining a maildir does not. This
# bootstrap:
#   1. makes the CLI executable,
#   2. writes <skill_dir>/config.json if missing (default dashboard URL),
#   3. verifies the Python entrypoint imports,
#   4. runs an offline smoke test (no dashboard needed): writes a fake envelope
#      into a throwaway maildir and confirms the CLI drains it, then cleans up.
#
# Usage:
#   ./bootstrap.sh                       # defaults to http://localhost:8766
#   DASHBOARD_URL=http://host:port ./bootstrap.sh
#   DASHBOARD_URL=... ./bootstrap.sh --no-smoke   # skip the smoke test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_PATH="$SKILL_DIR/config.json"
CLI="$SCRIPT_DIR/a2a_cli.py"

DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8766}"
RUN_SMOKE=1
for arg in "$@"; do
  case "$arg" in
    --no-smoke) RUN_SMOKE=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

require_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH." >&2
    exit 1
  fi
}

ensure_executable() {
  chmod +x "$CLI"
  echo "  ✓ made $CLI executable"
}

ensure_config() {
  if [[ -f "$CONFIG_PATH" ]]; then
    echo "  ✓ config exists ($CONFIG_PATH)"
    return
  fi
  cat > "$CONFIG_PATH" <<EOF
{"dashboard_url": "$DASHBOARD_URL"}
EOF
  echo "  ✓ wrote config ($CONFIG_PATH → $DASHBOARD_URL)"
}

verify_import() {
  if python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import a2a_lib"; then
    echo "  ✓ a2a_lib imports cleanly"
  else
    echo "  ! a2a_lib failed to import" >&2
    exit 1
  fi
}

# Offline smoke test: point A2A_ROOT at a temp dir, write a fake inbox envelope,
# confirm `inbox --drain` reads it and moves it to cur/. No dashboard required.
smoke_test() {
  local tmp
  tmp="$(mktemp -d)"
  if python3 - "$SCRIPT_DIR" "$tmp" <<'PY'
import json, sys
from pathlib import Path
script_dir, tmp = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
import a2a_lib as lib
lib.A2A_ROOT = Path(tmp) / "a2a"          # redirect: never touch the real maildir
import os
os.environ["HD_LIB_ID"] = "projects/bootstrap-smoke"
md = lib.maildir_for(lib.resolve_lib_id())
mid = lib.new_id()
env = {"id": mid, "from": "global", "to": "projects/bootstrap-smoke",
       "type": "message", "ts": "2026-01-01T00:00:00+00:00",
       "payload": {"text": "bootstrap smoke", "meta": {}}}
(md / "inbox" / f"{mid}.json").write_text(json.dumps(env))
drained = lib.drain_inbox()
assert len(drained) == 1, drained
assert drained[0]["payload"]["text"] == "bootstrap smoke"
assert not list((md / "inbox").glob("*.json"))   # moved out of inbox
assert list((md / "cur").glob("*.json"))          # landed in cur
print("ok")
PY
  then
    echo "  ✓ smoke test drained a fake inbox envelope"
  else
    echo "  ! smoke test failed" >&2
    rm -rf "$tmp"
    exit 1
  fi
  rm -rf "$tmp"
}

main() {
  require_python
  echo "Bootstrapping a2a skill in $SKILL_DIR"
  ensure_executable
  ensure_config
  verify_import
  if [[ "$RUN_SMOKE" -eq 1 ]]; then
    echo "Running offline smoke test:"
    smoke_test
  fi
  echo "Done. Try:"
  echo "  python3 $CLI list"
  echo "  python3 $CLI whois global   # one agent's identity + dir + sessions"
  echo "  python3 $CLI send --to global 'hello from another agent'"
}

main "$@"
