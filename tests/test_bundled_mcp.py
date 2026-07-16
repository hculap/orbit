"""Tests for bundled_mcp.ensure_dashboard_mcp — the boot seeder that wires the
`dashboard` MCP for the Global agent (issue #95).

Asserts: per-agent (projects[cwd].mcpServers) registration — NOT top-level — in
the claude config, idempotency, and that the skill is pinned to the Global agent
(removed from global-enabled, added to the global allowlist).
"""
from __future__ import annotations

import json

import pytest

from orbit import bundled_mcp as b


@pytest.fixture
def wired(tmp_path, monkeypatch):
    # a claude config the seeder will edit
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"projects": {"/home/testuser/Projects/anmar": {"mcpServers": {"x": {}}}}}))
    monkeypatch.setattr(b, "_config_paths", lambda: [cfg])

    # server path guard must pass
    server = tmp_path / "dashboard_mcp.py"
    server.write_text("# stub")
    monkeypatch.setattr(b, "SERVER_PATH", server)

    # in-memory skill enablement state
    state = {"global_enabled": {"dashboard-mcp", "artifacts"}, "allowlist": set()}
    monkeypatch.setattr(b.registry_mod, "read_global_enabled", lambda: set(state["global_enabled"]))
    monkeypatch.setattr(b.registry_mod, "write_global_enabled", lambda s: state.update(global_enabled=set(s)))
    monkeypatch.setattr(b.per_agent_mod, "read_allowlist", lambda k, l: set(state["allowlist"]))
    monkeypatch.setattr(b.per_agent_mod, "write_allowlist", lambda k, l, s: state.update(allowlist=set(s)))
    return cfg, state


def test_registers_per_agent_not_toplevel(wired):
    cfg, _ = wired
    b.ensure_dashboard_mcp()
    data = json.loads(cfg.read_text())
    # registered under projects[$HOME] (the Global agent cwd), NOT top-level.
    # Use b.HOME (= Path.home()) so this is portable: /home/testuser on the box,
    # /home/runner in CI — the seeder writes under AGENT_CWDS == [str(HOME)].
    assert "mcpServers" not in data or "dashboard" not in data.get("mcpServers", {})
    entry = data["projects"][str(b.HOME)]["mcpServers"]["dashboard"]
    assert entry["type"] == "stdio"
    assert entry["args"] == [str(b.SERVER_PATH)]
    # existing per-project servers untouched
    assert data["projects"]["/home/testuser/Projects/anmar"]["mcpServers"]["x"] == {}


def test_pins_skill_to_global_agent(wired):
    _, state = wired
    b.ensure_dashboard_mcp()
    assert "dashboard-mcp" not in state["global_enabled"]   # off all-agents
    assert "artifacts" in state["global_enabled"]            # others untouched
    assert "dashboard-mcp" in state["allowlist"]             # on the Global agent


def test_idempotent(wired):
    cfg, _ = wired
    b.ensure_dashboard_mcp()
    first = cfg.read_text()
    b.ensure_dashboard_mcp()
    b.ensure_dashboard_mcp()
    # second/third runs make no further change to the config
    assert cfg.read_text() == first
    # exactly one server entry, no duplication
    data = json.loads(cfg.read_text())
    assert list(data["projects"][str(b.HOME)]["mcpServers"]) == ["dashboard"]


def test_skips_when_server_absent(wired, monkeypatch):
    cfg, _ = wired
    monkeypatch.setattr(b, "SERVER_PATH", b.SERVER_PATH.with_name("nope.py"))
    before = cfg.read_text()
    b.ensure_dashboard_mcp()  # must be a no-op, never crash
    assert cfg.read_text() == before
