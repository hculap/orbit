#!/usr/bin/env bash
# bootstrap.sh — idempotent setup for the `geo` skill.
#
# The geo skill is a thin HTTP client (stdlib Python only); the dashboard owns
# every moving part. This bootstrap:
#   1. makes the CLI executable,
#   2. writes <skill_dir>/config.json if missing (default dashboard URL),
#   3. verifies the Python entrypoint imports + runs,
#   4. runs an OFFLINE smoke test — the one invariant that actually matters
#      here: with no dashboard reachable, `geo status` and `geo where` must
#      print a typed answer and exit non-zero WITHOUT a traceback. An agent
#      hanging or dumping a stacktrace into the chat is the failure mode this
#      whole skill exists to avoid.
#
# Usage:
#   ./bootstrap.sh                       # defaults to http://localhost:8766
#   DASHBOARD_URL=http://host:port ./bootstrap.sh
#   DASHBOARD_URL=... ./bootstrap.sh --no-smoke   # skip the smoke test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_PATH="$SKILL_DIR/config.json"
CLI="$SCRIPT_DIR/geo_cli.py"

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
  if python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import geo_lib"; then
    echo "  ✓ geo_lib imports cleanly"
  else
    echo "  ! geo_lib failed to import" >&2
    exit 1
  fi
}

# Run one CLI verb against a dead port; assert typed output, non-zero exit, no
# traceback. Args: <verb> <expected substring> [extra CLI args…]
assert_offline() {
  local verb="$1" expect="$2"; shift 2
  local out rc=0
  out="$(HD_NOTIFY_URL="http://127.0.0.1:1" HD_SESSION_ID="00000000-0000-4000-8000-000000000000" \
        python3 "$CLI" "$verb" "$@" 2>&1)" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    echo "  ! smoke test: '$verb' exited 0 with no dashboard — must be non-zero" >&2
    return 1
  fi
  if grep -q "Traceback" <<<"$out"; then
    echo "  ! smoke test: '$verb' printed a traceback:" >&2
    echo "$out" >&2
    return 1
  fi
  if ! grep -q "$expect" <<<"$out"; then
    echo "  ! smoke test: '$verb' output missing '$expect':" >&2
    echo "$out" >&2
    return 1
  fi
  echo "  ✓ smoke test: '$verb' → typed answer, exit $rc, no traceback"
}

smoke_test() {
  assert_offline status "dashboard_unreachable"
  assert_offline where "dashboard_unreachable" --timeout 5
}

main() {
  require_python
  echo "Bootstrapping geo skill in $SKILL_DIR"
  ensure_executable
  ensure_config
  verify_import
  if [[ "$RUN_SMOKE" -eq 1 ]]; then
    echo "Running offline smoke test:"
    smoke_test
  fi
  echo "Done. Try:"
  echo "  python3 $CLI status"
  echo "  python3 $CLI where --reason 'najbliższa apteka'"
}

main "$@"
