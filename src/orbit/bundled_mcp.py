"""Boot-time registration of the bundled `dashboard` MCP server (issue #95).

`bundled_skills.seed_bundled_skills()` ships `skills/dashboard-mcp/` into the
registry on boot; this module then *wires* its stdio MCP server so the spawned
`claude` actually loads `mcp__dashboard__*` tools, and pins the skill to the
**Global agent only** (per the user's "global agent only for now").

It does two idempotent, best-effort things — call `ensure_dashboard_mcp()` from
`orchestrator.register_routes()` beside `ensure_prompts()`:

1. **Register the MCP per-agent.** Adds `projects["<cwd>"].mcpServers.dashboard`
   to the claude config the agent reads — `$CLAUDE_CONFIG_DIR/.claude.json`
   (= /home/user/.claude/.claude.json under the systemd unit) and ~/.claude.json
   for the user's ssh shell. **Per-cwd scope, NOT top-level mcpServers** — so only
   sessions opened at an AGENT_CWDS dir (the Global agent's /home/user) load it.
   This deliberately differs from #95's `claude mcp add --scope user` (every
   session on the box): the user wants Global-agent-only.

2. **Pin the skill to the Global agent.** `seed_bundled_skills` global-enables
   every bundled skill (all agents); this removes `dashboard-mcp` from
   `.global-enabled.json` and adds it to the Global agent's allowlist
   (`agents/global/global/skills_allowlist.json`). Runs AFTER seeding because
   `register_routes` is wired after `seed_bundled_skills` in `create_app`.

Deliberate deviations from the #95 plan (both the user's call): zero-dependency
stdlib server (no FastMCP / `mcp[cli]` dep), and Global-agent scope (not user).

To widen to another agent later: add its cwd to AGENT_CWDS and the skill to that
agent's allowlist.
"""
from __future__ import annotations

import json
import os
import sys
import time
import shutil
from pathlib import Path

from . import skills_registry as registry_mod
from . import skills_per_agent as per_agent_mod

HOME = Path.home()
MCP_NAME = "dashboard"
SKILL_NAME = "dashboard-mcp"

# Agents (by cwd) that load the MCP — must match who has the skill. Global only.
AGENT_CWDS: list[str] = [str(HOME)]   # /home/user == the Global agent
GLOBAL_AGENT = ("global", "global")   # (kind, lib_id) for the Global agent

# Server lives in the registry (seeded from skills/dashboard-mcp/ on boot), so it
# survives repo moves and matches how the artifacts CLI is referenced.
SERVER_PATH = registry_mod.skill_dir(SKILL_NAME) / "scripts" / "dashboard_mcp.py"


def _python() -> str:
    """A stable interpreter for the stdio server (stdlib-only, any python3)."""
    for c in ("/usr/bin/python3", "/usr/local/bin/python3", sys.executable):
        if c and Path(c).exists():
            return c
    return "python3"


def _mcp_entry() -> dict:
    return {"type": "stdio", "command": _python(), "args": [str(SERVER_PATH)], "env": {}}


def _config_paths() -> list[Path]:
    """The claude config(s) to register in: what the spawned agent reads
    ($CLAUDE_CONFIG_DIR/.claude.json) + the user's ssh ~/.claude.json."""
    paths: list[Path] = []
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        paths.append(Path(cfg_dir) / ".claude.json")
    paths.append(HOME / ".claude.json")
    # de-dup preserving order
    seen, out = set(), []
    for p in paths:
        rp = str(p.resolve()) if p.exists() else str(p)
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    os.replace(tmp, path)


def _register_in_config(path: Path) -> bool:
    """Idempotently add projects[cwd].mcpServers.dashboard. Returns True if changed."""
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[bundled_mcp] skip {path}: unreadable ({exc})", file=sys.stderr)
        return False
    if not isinstance(data, dict):
        return False
    want = _mcp_entry()
    projects = data.setdefault("projects", {})
    changed = False
    for cwd in AGENT_CWDS:
        proj = projects.setdefault(cwd, {})
        servers = proj.setdefault("mcpServers", {})
        if servers.get(MCP_NAME) != want:
            servers[MCP_NAME] = want
            changed = True
    if changed:
        # one-time insurance backup before the first mutation of a big user file
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + f".bak-dashmcp-{int(time.time())}"))
        except OSError:
            pass
        _atomic_write(path, data)
        print(f"[bundled_mcp] registered mcp '{MCP_NAME}' in {path} for {AGENT_CWDS}")
    return changed


def _pin_skill_to_global() -> None:
    """Move the skill from global-enabled (all agents) to the Global agent allowlist."""
    try:
        ge = registry_mod.read_global_enabled()
        if SKILL_NAME in ge:
            registry_mod.write_global_enabled(ge - {SKILL_NAME})
            print(f"[bundled_mcp] removed {SKILL_NAME} from global-enabled (pin to Global agent)")
    except Exception as exc:  # noqa: BLE001
        print(f"[bundled_mcp] global-enabled adjust failed: {exc}", file=sys.stderr)
    try:
        kind, lib = GLOBAL_AGENT
        allow = per_agent_mod.read_allowlist(kind, lib)
        if SKILL_NAME not in allow:
            per_agent_mod.write_allowlist(kind, lib, allow | {SKILL_NAME})
            print(f"[bundled_mcp] enabled {SKILL_NAME} for the Global agent")
    except Exception as exc:  # noqa: BLE001
        print(f"[bundled_mcp] global allowlist adjust failed: {exc}", file=sys.stderr)


def ensure_dashboard_mcp() -> None:
    """Wire the dashboard MCP for the Global agent. Idempotent + best-effort —
    never blocks boot. Safe to call on every startup."""
    if not SERVER_PATH.is_file():
        # Skill not seeded yet (e.g. registry not bootstrapped) — nothing to wire.
        print(f"[bundled_mcp] server not found at {SERVER_PATH}; skipping MCP registration",
              file=sys.stderr)
        return
    for cfg in _config_paths():
        try:
            _register_in_config(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[bundled_mcp] register in {cfg} failed: {exc}", file=sys.stderr)
    _pin_skill_to_global()
