"""Tests for the Global agent's user-editable custom prompt layer.

The Global meta-area's "Agent" tab edits ``orchestrator-custom.md`` — a layer
kept separate from the version-migrated ``orchestrator.md`` so user edits
survive prompt-version bumps. It's appended to a GLOBAL session's prompt stack
(after orchestrator.md) and never to a per-agent session's.
"""
from __future__ import annotations


def test_global_custom_read_write_roundtrip(tmp_path, monkeypatch):
    from orbit import agent_prompts as ap
    monkeypatch.setattr(ap, "AGENT_PROMPTS_DIR", tmp_path)

    assert ap.read_global_custom() == ""
    ap.write_global_custom("be terse and direct")
    assert ap.read_global_custom() == "be terse and direct"
    assert ap.global_custom_prompt_path().read_text(encoding="utf-8") == "be terse and direct"


def test_global_custom_blank_clears_file(tmp_path, monkeypatch):
    from orbit import agent_prompts as ap
    monkeypatch.setattr(ap, "AGENT_PROMPTS_DIR", tmp_path)
    ap.write_global_custom("something")
    assert ap.global_custom_prompt_path().is_file()
    ap.write_global_custom("   ")  # whitespace-only → clear
    assert not ap.global_custom_prompt_path().exists()
    assert ap.read_global_custom() == ""


def test_global_custom_appended_only_for_global_session(tmp_path, monkeypatch):
    from orbit import agent_prompts as ap
    monkeypatch.setattr(ap, "AGENT_PROMPTS_DIR", tmp_path)
    ap.write_global_custom("global persona")
    gcustom = ap.global_custom_prompt_path()

    # Global session (cwd None) → custom layer present.
    global_paths = ap.prompts_for_session(None, None)
    assert gcustom in global_paths, f"global custom missing from {global_paths!r}"

    # Per-agent session (real cwd + lib_id) → NOT present.
    agent_paths = ap.prompts_for_session("/home/x/Projects/foo", "projects/foo")
    assert gcustom not in agent_paths


def test_global_custom_empty_not_appended(tmp_path, monkeypatch):
    from orbit import agent_prompts as ap
    monkeypatch.setattr(ap, "AGENT_PROMPTS_DIR", tmp_path)
    # No custom written → not in the stack even for a global session.
    assert ap.global_custom_prompt_path() not in ap.prompts_for_session(None, None)
