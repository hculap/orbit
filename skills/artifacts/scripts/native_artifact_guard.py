#!/usr/bin/env python3
"""PreToolUse guard for the NATIVE ``Artifact`` tool (claude.ai publishing).

Two different things are called "artifact" in an orchestrator session:

* the **panel artifact** — ``artifacts_cli.py``, lands in the orbit
  gallery, lives on this box;
* the **claude.ai artifact** — the built-in ``Artifact`` tool, publishes a page
  to Anthropic's servers, and is completely invisible in the dashboard.

Agents drift towards the built-in tool simply because it is a tool and the
other one is a CLI, so "zrób artefakt" silently ends up somewhere the user is
not looking. Prompt wording alone did not hold.

This guard makes the mix-up impossible without making claude.ai artifacts
unavailable: a publish through the native tool is BLOCKED until someone opens a
short opt-in window, and the refusal text tells the agent to ask the user which
kind they meant. Read-only actions (listing, reading comments) always pass.

Wiring (``~/.claude/settings.json``)::

    "PreToolUse": [{"matcher": "Artifact", "hooks": [{"type": "command",
      "command": "python3 ~/.orchestrator/skills-registry/artifacts/scripts/native_artifact_guard.py hook"}]}]

Usage::

    native_artifact_guard.py hook            # stdin = PreToolUse JSON
    native_artifact_guard.py allow [--minutes N] [--note "…"]
    native_artifact_guard.py status
    native_artifact_guard.py revoke
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# One shared window per box (single user), not per session: the agent that has
# to open it runs in a Bash tool call where the session id is not reliably in
# the environment, so keying on it would just break the escape hatch.
OPTIN_PATH = Path.home() / ".orchestrator" / "native-artifact-optin.json"
DEFAULT_MINUTES = 30
MAX_MINUTES = 240

TOOL_NAME = "Artifact"
# Actions that only READ from claude.ai — no publishing, nothing the user could
# mistake for a panel artifact. Never gated.
READ_ONLY_ACTIONS = frozenset({"list", "comments", "list_assets", "read_asset"})

CLI_HINT = "python3 ~/.orchestrator/skills-registry/artifacts/scripts/artifacts_cli.py"
GUARD_HINT = "python3 ~/.orchestrator/skills-registry/artifacts/scripts/native_artifact_guard.py allow"

DENY_REASON = f"""BLOCKED — the native `Artifact` tool publishes to claude.ai, which is NOT visible in the orbit the user is looking at.

On this box the default deliverable is a PANEL artifact:
  {CLI_HINT} create --type <doc|file|image|chart|map|html|audio|video|youtube> --title "…" [--open] <content>
Use --type doc for a document to read/copy, --type file for bytes to download.

If — and only if — the user really asked for a claude.ai artifact (a shareable link, a page outside this server):
  1. confirm it with AskUserQuestion ("artefakt w panelu" vs "artefakt claude.ai") unless they already said so explicitly;
  2. run `{GUARD_HINT}` to open a {DEFAULT_MINUTES}-minute window;
  3. retry the Artifact call.

Do not open the window on your own initiative — the whole point is that the user gets to choose which artifact they get."""


def _read_optin() -> dict | None:
    """The stored opt-in record, or None when absent / expired / unreadable."""
    try:
        data = json.loads(OPTIN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        expires_at = float(data.get("expires_at", 0))
    except (TypeError, ValueError):
        return None
    return data if expires_at > time.time() else None


def _tool_action(payload: dict) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        action = tool_input.get("action")
        if isinstance(action, str) and action.strip():
            return action.strip()
    return "publish"


def cmd_hook() -> int:
    """PreToolUse entrypoint. 0 = let the call through, 2 = block with a reason.

    Any malformed input is treated as "not our business" and allowed — a guard
    that crashes the harness on a payload shape change is worse than one that
    occasionally misses.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != TOOL_NAME:
        return 0
    if _tool_action(payload) in READ_ONLY_ACTIONS:
        return 0
    if _read_optin() is not None:
        return 0
    print(DENY_REASON, file=sys.stderr)
    # Newer harnesses read a structured decision off stdout; older ones act on
    # exit code 2 + stderr. Emitting both keeps the guard version-proof.
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": DENY_REASON,
    }}))
    return 2


def cmd_allow(minutes: int, note: str | None) -> int:
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    now = time.time()
    record = {
        "granted_at": now,
        "expires_at": now + minutes * 60,
        "minutes": minutes,
        "note": note or "",
        "granted_by_session": os.environ.get("HD_SESSION_ID", ""),
    }
    OPTIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPTIN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OPTIN_PATH)
    print(f"claude.ai artifacts allowed for {minutes} min "
          f"(until {time.strftime('%H:%M', time.localtime(record['expires_at']))})")
    return 0


def cmd_status() -> int:
    rec = _read_optin()
    if rec is None:
        print("blocked · native Artifact needs an explicit opt-in window")
        return 0
    left = int((rec["expires_at"] - time.time()) / 60)
    print(f"allowed · {left} min left" + (f" · {rec['note']}" if rec.get("note") else ""))
    return 0


def cmd_revoke() -> int:
    try:
        OPTIN_PATH.unlink()
        print("revoked")
    except FileNotFoundError:
        print("nothing to revoke")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="native-artifact-guard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="PreToolUse entrypoint (reads JSON on stdin)")
    p_allow = sub.add_parser("allow", help="Open an opt-in window for claude.ai artifacts")
    p_allow.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    p_allow.add_argument("--note", default=None, help="Why the user asked for it")
    sub.add_parser("status", help="Is the window open?")
    sub.add_parser("revoke", help="Close the window now")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return cmd_hook()
    if args.command == "allow":
        return cmd_allow(args.minutes, args.note)
    if args.command == "status":
        return cmd_status()
    return cmd_revoke()


if __name__ == "__main__":
    raise SystemExit(main())
