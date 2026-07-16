"""Tests for orchestrator_env — subscription-billing env hygiene (Phase 0).

Every non-tmux `claude` spawn must build its child env through
``scrubbed_env`` so a stray ANTHROPIC_API_KEY (or AUTH_TOKEN) can never leak
into the child and force it onto the post-2026-06-15 programmatic credit pool
/ raw API billing.
"""
from __future__ import annotations


def test_scrubbed_env_strips_billing_keys(monkeypatch):
    from orbit import orchestrator_env as env_mod
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = env_mod.scrubbed_env({"CLAUDE_CONFIG_DIR": "/home/x/.claude"})
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"               # other env preserved
    assert env["CLAUDE_CONFIG_DIR"] == "/home/x/.claude"


def test_scrubbed_env_extra_cannot_reintroduce_key(monkeypatch):
    """A user-managed .env layered in via `extra` must not re-add a key."""
    from orbit import orchestrator_env as env_mod
    env = env_mod.scrubbed_env(
        {"ANTHROPIC_API_KEY": "sneak", "SAFE": "ok"},
        base={"ANTHROPIC_API_KEY": "from-base", "KEEP": "yes"},
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert env["SAFE"] == "ok"
    assert env["KEEP"] == "yes"


def test_scrubbed_env_no_key_present_is_noop(monkeypatch):
    from orbit import orchestrator_env as env_mod
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    env = env_mod.scrubbed_env()
    assert all(k not in env for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))


def test_scope_env_values_reads_and_scrubs(tmp_path):
    """<cwd>/.env secrets are returned scrubbed of billing keys; the interactive
    one-shot path layers these into the spawn so it matches the legacy -p path."""
    from orbit import orchestrator_env as env_mod
    (tmp_path / ".env").write_text("API_TOKEN=s3cret\nANTHROPIC_API_KEY=leak\n")
    vals = env_mod.scope_env_values(tmp_path)
    assert vals.get("API_TOKEN") == "s3cret"
    assert "ANTHROPIC_API_KEY" not in vals


def test_scope_env_values_noop_cases(tmp_path):
    from orbit import orchestrator_env as env_mod
    assert env_mod.scope_env_values(None) == {}
    assert env_mod.scope_env_values(tmp_path) == {}  # no .env present


def test_warn_and_log_helpers_never_raise(monkeypatch, capsys):
    from orbit import orchestrator_env as env_mod
    env_mod._warned_once = False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    env_mod.warn_if_api_key_present()
    env_mod.warn_if_api_key_present()  # one-shot — second is a no-op
    env_mod.log_billing_path("unit-test", interactive=True)
    env_mod.log_billing_path("unit-test", interactive=False)
    err = capsys.readouterr().err
    assert "WARNING" in err and err.count("WARNING") == 1
    assert "interactive(subscription)" in err and "programmatic" in err
