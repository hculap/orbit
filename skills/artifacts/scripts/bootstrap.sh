#!/usr/bin/env bash
# bootstrap.sh — idempotent setup for the `artifacts` skill.
#
# The artifacts skill is mostly self-contained (stdlib Python, no external
# services required to create artifacts). This bootstrap:
#   1. makes the CLI executable,
#   2. writes <skill_dir>/config.json if missing (default dashboard URL),
#   3. verifies the Python entrypoint imports + runs,
#   4. runs an offline smoke test (no dashboard needed) and cleans up.
#
# Usage:
#   ./bootstrap.sh                       # defaults to http://localhost:8766
#   DASHBOARD_URL=http://host:port ./bootstrap.sh
#   DASHBOARD_URL=... ./bootstrap.sh --no-smoke   # skip the smoke test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_PATH="$SKILL_DIR/config.json"
CLI="$SCRIPT_DIR/artifacts_cli.py"

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
  if python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import artifacts_lib"; then
    echo "  ✓ artifacts_lib imports cleanly"
  else
    echo "  ! artifacts_lib failed to import" >&2
    exit 1
  fi
}

smoke_test() {
  # Exercise the real invariant: with HD_LIB_ID set, the artifact must land in
  # the lib_id's PARA dir REGARDLESS of the process cwd (the bug this fixes was
  # the CLI keying off cwd, so running it from a scratchpad hid the artifact).
  local tmp para
  tmp="$(mktemp -d)"                              # unrelated cwd (outside $HOME)
  para="$HOME/Projects/.bootstrap-smoke-$$"       # synthetic PARA project dir
  mkdir -p "$para"
  local spec="$tmp/spec.json"
  cat > "$spec" <<'EOF'
{"chart_type":"bar","data":{"labels":["a"],"datasets":[{"data":[1]}]}}
EOF
  (
    cd "$tmp"                                      # run from the WRONG dir
    HD_LIB_ID="projects/.bootstrap-smoke-$$" HD_SESSION_ID="bootstrap-sid" \
      python3 "$CLI" create --type chart --title "bootstrap smoke" --spec-file "$spec"
  )
  local ok=1
  if ! ls "$para"/.artifacts/*.json >/dev/null 2>&1; then
    echo "  ! smoke test: no manifest in the PARA dir ($para/.artifacts)" >&2
    ok=0
  elif [[ -d "$tmp/.artifacts" ]]; then
    echo "  ! smoke test: artifact leaked into the cwd ($tmp/.artifacts) — cwd bug!" >&2
    ok=0
  else
    echo "  ✓ smoke test: manifest landed in the PARA dir, not the cwd"
  fi
  rm -rf "$tmp" "$para"
  [[ "$ok" -eq 1 ]] || exit 1
}

main() {
  require_python
  echo "Bootstrapping artifacts skill in $SKILL_DIR"
  ensure_executable
  ensure_config
  verify_import
  if [[ "$RUN_SMOKE" -eq 1 ]]; then
    echo "Running offline smoke test:"
    smoke_test
  fi
  echo "Done. Try:"
  echo "  python3 $CLI create --type chart --title 'Demo' --spec-file spec.json --open"
}

main "$@"
