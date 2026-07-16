"""Tests for orchestrator_settings — new tmux/pool flags + validation."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_settings(tmp_path: Path, monkeypatch):
    """Reload the settings module with a tmp settings.json path so each test
    starts from a clean slate (defaults only, no leftover state)."""
    from orbit import orchestrator_settings as mod
    monkeypatch.setattr(mod, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(mod, "_data", None)
    return mod


def _run(coro):
    return asyncio.run(coro)


# ── defaults ───────────────────────────────────────────────────────


def test_defaults_include_runner_mode(fresh_settings):
    """runner_mode default = 'interactive' — tmux REPL is the default chat path."""
    settings = fresh_settings.get_settings()
    assert settings["runner_mode"] == "interactive"


def test_defaults_include_pool_size(fresh_settings):
    """pool_size default = 4 (per plan; Hetzner has 12 GB free, ~1.3 GB/seat)."""
    settings = fresh_settings.get_settings()
    assert settings["pool_size"] == 4


def test_defaults_include_pool_idle_ttl_s(fresh_settings):
    """pool_idle_ttl_s default = 14400 (4h)."""
    settings = fresh_settings.get_settings()
    assert settings["pool_idle_ttl_s"] == 14400


def test_resolve_runner_mode_validates_and_defaults(fresh_settings):
    """Single dispatch helper for all 5 one-shot sites: returns a VALIDATED mode
    and fails closed onto subscription ('interactive'), never onto the -p pool."""
    assert fresh_settings.resolve_runner_mode("cron_runner_mode") == "interactive"
    _run(fresh_settings.set_settings({"cron_runner_mode": "programmatic"}))
    assert fresh_settings.resolve_runner_mode("cron_runner_mode") == "programmatic"


def test_resolve_runner_mode_unknown_flag_defaults_interactive(fresh_settings):
    # An unregistered flag (None default) must resolve to subscription, not "".
    assert fresh_settings.resolve_runner_mode("nonexistent_runner_mode") == "interactive"


def test_get_flag_returns_concrete_defaults(fresh_settings):
    assert fresh_settings.get_flag("runner_mode") == "interactive"
    # Subscription-only migration: cron + one-shots default to interactive
    # (subscription billing); `programmatic` (`claude -p`) is manual rollback.
    assert fresh_settings.get_flag("cron_runner_mode") == "interactive"
    assert fresh_settings.get_flag("titles_runner_mode") == "interactive"
    assert fresh_settings.get_flag("identity_runner_mode") == "interactive"
    assert fresh_settings.get_flag("skill_runner_mode") == "interactive"
    assert fresh_settings.get_flag("pool_size") == 4
    assert fresh_settings.get_flag("pool_idle_ttl_s") == 14400


def test_cron_runner_mode_independent_from_user_chat(fresh_settings):
    """cron LLM fires route independently of the user-chat dispatch — set
    them to DIFFERENT values and verify neither bleeds into the other
    (regardless of their respective defaults)."""
    _run(fresh_settings.set_settings({"cron_runner_mode": "programmatic", "runner_mode": "interactive"}))
    assert fresh_settings.get_flag("cron_runner_mode") == "programmatic"
    assert fresh_settings.get_flag("runner_mode") == "interactive"


def test_cron_runner_mode_validation_rejects_garbage(fresh_settings):
    out = _run(fresh_settings.set_settings({"cron_runner_mode": "nope"}))
    assert out["cron_runner_mode"] == "interactive"  # unchanged (default)
    out = _run(fresh_settings.set_settings({"cron_runner_mode": 42}))
    assert out["cron_runner_mode"] == "interactive"


def test_get_settings_includes_validation_bounds(fresh_settings):
    """UAT follow-up (PR #41 review #4): frontend should NOT hardcode the
    bounds — it would drift from backend validation. Expose them so the
    settings UI can read them on mount."""
    settings = fresh_settings.get_settings()
    assert "_bounds" in settings
    bounds = settings["_bounds"]
    assert bounds["runner_mode"] == ["programmatic", "interactive"]
    assert bounds["cron_runner_mode"] == ["programmatic", "interactive"]
    assert bounds["pool_size"] == [1, 32]
    assert bounds["pool_idle_ttl_s"] == [1, 86400]


def test_set_settings_silently_drops_bounds_metadata(fresh_settings):
    """`_bounds` is read-only metadata. If the frontend ever round-trips it,
    set_settings must NOT crash or persist it."""
    out = _run(fresh_settings.set_settings({"_bounds": {"pool_size": [99, 99]}}))
    # Real bounds unchanged.
    assert out["_bounds"]["pool_size"] == [1, 32]


# ── runner_mode enum validation ───────────────────────────────────


def test_set_runner_mode_interactive(fresh_settings):
    out = _run(fresh_settings.set_settings({"runner_mode": "interactive"}))
    assert out["runner_mode"] == "interactive"


def test_set_runner_mode_programmatic(fresh_settings):
    _run(fresh_settings.set_settings({"runner_mode": "interactive"}))
    out = _run(fresh_settings.set_settings({"runner_mode": "programmatic"}))
    assert out["runner_mode"] == "programmatic"


def test_set_runner_mode_invalid_string_rejected(fresh_settings):
    """Garbage string for enum must NOT poison the file — drop silently
    (matches existing set_settings contract: unknown values for known keys
    don't raise; just drop). Old value remains in effect."""
    out = _run(fresh_settings.set_settings({"runner_mode": "nope"}))
    assert out["runner_mode"] == "interactive"  # default unchanged


def test_set_runner_mode_non_string_rejected(fresh_settings):
    out = _run(fresh_settings.set_settings({"runner_mode": 42}))
    assert out["runner_mode"] == "interactive"


# ── pool_size int validation ──────────────────────────────────────


def test_set_pool_size_valid(fresh_settings):
    out = _run(fresh_settings.set_settings({"pool_size": 8}))
    assert out["pool_size"] == 8


def test_set_pool_size_zero_rejected(fresh_settings):
    """0 makes no sense (pool can't be empty)."""
    out = _run(fresh_settings.set_settings({"pool_size": 0}))
    assert out["pool_size"] == 4


def test_set_pool_size_negative_rejected(fresh_settings):
    out = _run(fresh_settings.set_settings({"pool_size": -1}))
    assert out["pool_size"] == 4


def test_set_pool_size_excessive_rejected(fresh_settings):
    """Cap at 32 — beyond that is almost certainly a typo / OOM risk."""
    out = _run(fresh_settings.set_settings({"pool_size": 1000}))
    assert out["pool_size"] == 4


def test_set_pool_size_non_int_rejected(fresh_settings):
    out = _run(fresh_settings.set_settings({"pool_size": "four"}))
    assert out["pool_size"] == 4


def test_set_pool_size_bool_rejected(fresh_settings):
    """In Python `True` is int=1 — explicitly reject bool to avoid surprises."""
    out = _run(fresh_settings.set_settings({"pool_size": True}))
    assert out["pool_size"] == 4


# ── pool_idle_ttl_s int validation ────────────────────────────────


def test_set_idle_ttl_valid(fresh_settings):
    out = _run(fresh_settings.set_settings({"pool_idle_ttl_s": 60}))
    assert out["pool_idle_ttl_s"] == 60


def test_set_idle_ttl_zero_rejected(fresh_settings):
    out = _run(fresh_settings.set_settings({"pool_idle_ttl_s": 0}))
    assert out["pool_idle_ttl_s"] == 14400


def test_set_idle_ttl_excessive_rejected(fresh_settings):
    """Cap at 1 day. Longer = pool slots leak."""
    out = _run(fresh_settings.set_settings({"pool_idle_ttl_s": 999999}))
    assert out["pool_idle_ttl_s"] == 14400


# ── persistence ───────────────────────────────────────────────────


def test_runner_mode_persisted_across_reload(fresh_settings, monkeypatch):
    _run(fresh_settings.set_settings({"runner_mode": "interactive"}))
    # Drop in-memory cache; force re-read from disk.
    monkeypatch.setattr(fresh_settings, "_data", None)
    assert fresh_settings.get_flag("runner_mode") == "interactive"


def test_partial_patch_keeps_other_flags(fresh_settings):
    _run(fresh_settings.set_settings({"runner_mode": "interactive", "pool_size": 6}))
    _run(fresh_settings.set_settings({"pool_size": 8}))  # patch only pool_size
    assert fresh_settings.get_flag("runner_mode") == "interactive"
    assert fresh_settings.get_flag("pool_size") == 8


# ── session_switcher_enabled (desktop ⌘+⇧ switcher) ───────────────


def test_session_switcher_default_on(fresh_settings):
    """ON by default so the ⌥+⇥ all-sessions switcher works out of the box."""
    assert fresh_settings.get_flag("session_switcher_enabled") is True
    assert fresh_settings.get_settings()["session_switcher_enabled"] is True


def test_session_switcher_round_trip(fresh_settings, monkeypatch):
    out = _run(fresh_settings.set_settings({"session_switcher_enabled": True}))
    assert out["session_switcher_enabled"] is True
    # Persists across an in-memory cache drop (re-read from disk).
    monkeypatch.setattr(fresh_settings, "_data", None)
    assert fresh_settings.get_flag("session_switcher_enabled") is True


def test_session_switcher_coerces_truthy_to_bool(fresh_settings):
    """Boolean flags coerce so a stray string from the frontend can't poison
    the file (mirrors the documented set_settings contract)."""
    out = _run(fresh_settings.set_settings({"session_switcher_enabled": "yes"}))
    assert out["session_switcher_enabled"] is True
    out = _run(fresh_settings.set_settings({"session_switcher_enabled": 0}))
    assert out["session_switcher_enabled"] is False
