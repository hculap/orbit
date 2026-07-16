"""Unit tests for orbit.system_watchdog."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orbit import system_watchdog as sw


# ── disk thresholds ──────────────────────────────────────────────


def _fake_disk_usage(percent: float):
    total = 1_000_000_000
    used = int(total * percent / 100)
    free = total - used

    class _U:
        def __init__(self):
            self.total = total
            self.used = used
            self.free = free
    return _U()


def test_disk_below_warning_no_event(monkeypatch):
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(84.9))
    state = sw._empty_state()
    events = sw.check_disk(state)
    assert events == []
    assert state["disk_root_severity"] == "ok"


def test_disk_at_warning_threshold_fires_warning(monkeypatch):
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(85.0))
    state = sw._empty_state()
    events = sw.check_disk(state)
    assert len(events) == 1
    assert events[0]["severity"] == "warning"
    assert events[0]["type"] == "disk"
    assert state["disk_root_severity"] == "warning"


def test_disk_at_critical_threshold_fires_critical(monkeypatch):
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(95.0))
    state = sw._empty_state()
    events = sw.check_disk(state)
    assert len(events) == 1
    assert events[0]["severity"] == "critical"
    assert state["disk_root_severity"] == "critical"


def test_disk_held_state_does_not_re_fire(monkeypatch):
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(90.0))
    state = sw._empty_state()
    first = sw.check_disk(state)
    second = sw.check_disk(state)
    assert len(first) == 1
    assert second == []


def test_disk_warning_to_critical_re_fires(monkeypatch):
    state = sw._empty_state()
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(90.0))
    sw.check_disk(state)
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(96.0))
    events = sw.check_disk(state)
    assert len(events) == 1
    assert events[0]["severity"] == "critical"


# ── service transitions ─────────────────────────────────────────


def _patch_systemctl(monkeypatch, mapping: dict[str, str]):
    def _is_active(unit: str) -> str:
        return mapping.get(unit, "unknown")
    monkeypatch.setattr(sw, "_systemctl_active", _is_active)


def test_service_active_to_failed_fires_critical(monkeypatch):
    state = sw._empty_state()
    _patch_systemctl(monkeypatch, {"nginx": "active"})
    sw.check_services(state, units=("nginx",))
    _patch_systemctl(monkeypatch, {"nginx": "failed"})
    events = sw.check_services(state, units=("nginx",))
    assert len(events) == 1
    assert events[0]["severity"] == "critical"
    assert events[0]["context"]["unit"] == "nginx"


def test_service_failed_held_does_not_re_fire(monkeypatch):
    state = sw._empty_state()
    _patch_systemctl(monkeypatch, {"nginx": "active"})
    sw.check_services(state, units=("nginx",))
    _patch_systemctl(monkeypatch, {"nginx": "failed"})
    first = sw.check_services(state, units=("nginx",))
    second = sw.check_services(state, units=("nginx",))
    assert len(first) == 1
    assert second == []


def test_service_recovery_emits_info(monkeypatch):
    state = sw._empty_state()
    _patch_systemctl(monkeypatch, {"nginx": "active"})
    sw.check_services(state, units=("nginx",))
    _patch_systemctl(monkeypatch, {"nginx": "failed"})
    sw.check_services(state, units=("nginx",))
    _patch_systemctl(monkeypatch, {"nginx": "active"})
    events = sw.check_services(state, units=("nginx",))
    assert len(events) == 1
    assert events[0]["severity"] == "info"


def test_service_unknown_is_ignored(monkeypatch):
    state = sw._empty_state()
    _patch_systemctl(monkeypatch, {"nginx": "unknown"})
    events = sw.check_services(state, units=("nginx",))
    assert events == []


# ── state file round-trip ────────────────────────────────────────


def test_save_then_load_roundtrips(tmp_path: Path):
    path = tmp_path / "state.json"
    state = sw._empty_state()
    state["disk_root_severity"] = "warning"
    state["services"] = {"nginx": "active"}
    sw.save_state(state, path)
    loaded = sw.load_state(path)
    assert loaded["disk_root_severity"] == "warning"
    assert loaded["services"] == {"nginx": "active"}


def test_load_missing_file_returns_fresh_state(tmp_path: Path):
    state = sw.load_state(tmp_path / "missing.json")
    assert state["disk_root_severity"] == "ok"
    assert state["services"] == {}
    assert state["last_uptime_s"] is None


def test_load_malformed_file_returns_fresh_state(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    state = sw.load_state(path)
    assert state["disk_root_severity"] == "ok"


def test_save_state_is_mode_0600(tmp_path: Path):
    path = tmp_path / "state.json"
    sw.save_state(sw._empty_state(), path)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600


def test_save_state_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "state.json"
    sw.save_state(sw._empty_state(), path)
    assert path.is_file()


# ── uptime reset ─────────────────────────────────────────────────


def test_uptime_reset_below_tolerance_fires_critical(monkeypatch):
    state = sw._empty_state()
    state["last_uptime_s"] = 100_000.0
    monkeypatch.setattr(sw, "_read_uptime_s", lambda: 30.0)
    events = sw.check_uptime(state)
    assert len(events) == 1
    assert events[0]["severity"] == "critical"
    assert events[0]["type"] == "uptime"


def test_uptime_growing_no_event(monkeypatch):
    state = sw._empty_state()
    state["last_uptime_s"] = 1_000.0
    monkeypatch.setattr(sw, "_read_uptime_s", lambda: 1_500.0)
    assert sw.check_uptime(state) == []
    assert state["last_uptime_s"] == 1_500.0


def test_uptime_first_run_no_event(monkeypatch):
    state = sw._empty_state()
    monkeypatch.setattr(sw, "_read_uptime_s", lambda: 12_345.0)
    assert sw.check_uptime(state) == []
    assert state["last_uptime_s"] == 12_345.0


def test_uptime_within_tolerance_no_event(monkeypatch):
    state = sw._empty_state()
    state["last_uptime_s"] = 1_000.0
    monkeypatch.setattr(sw, "_read_uptime_s",
                        lambda: 1_000.0 - sw.UPTIME_RESET_TOLERANCE_S + 1)
    assert sw.check_uptime(state) == []


# ── dry run / check_all ──────────────────────────────────────────


def test_check_all_persists_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(50.0))
    monkeypatch.setattr(sw, "_memory_percent", lambda: 50.0)
    monkeypatch.setattr(sw, "_systemctl_active", lambda u: "unknown")
    monkeypatch.setattr(sw, "_tailnet_peers", lambda: None)
    monkeypatch.setattr(sw, "_read_uptime_s", lambda: 1234.0)
    state_path = tmp_path / "state.json"

    events = sw.check_all(state_path=state_path)

    assert events == []
    assert state_path.is_file()
    data = json.loads(state_path.read_text())
    assert data["last_uptime_s"] == 1234.0
    assert data["last_check_iso"] is not None


def test_check_all_dry_run_via_persist_false(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda p: _fake_disk_usage(96.0))
    monkeypatch.setattr(sw, "_memory_percent", lambda: 50.0)
    monkeypatch.setattr(sw, "_systemctl_active", lambda u: "unknown")
    monkeypatch.setattr(sw, "_tailnet_peers", lambda: None)
    monkeypatch.setattr(sw, "_read_uptime_s", lambda: 1234.0)
    state_path = tmp_path / "state.json"

    events = sw.check_all(state_path=state_path, persist=False)

    assert any(e["type"] == "disk" for e in events)
    assert not state_path.exists()


def test_cli_dry_run_does_not_call_notify(monkeypatch, capsys):
    """The __main__.system-check --dry-run path must skip notify().

    We patch system_watchdog.check_all to return one event and ensure
    notify.notify is never awaited.
    """
    from orbit import __main__ as cli
    from orbit import notify as notify_mod

    fake_event = {"severity": "warning", "type": "disk",
                  "message": "disk at 92%", "context": {}}
    monkeypatch.setattr(cli, "system_watchdog", sw, raising=False)
    monkeypatch.setattr(sw, "check_all", lambda **kw: [fake_event])

    notify_called = {"count": 0}

    async def _fake_notify(**kw):
        notify_called["count"] += 1
        return {"ok": True}

    monkeypatch.setattr(notify_mod, "notify", _fake_notify)

    import argparse
    args = argparse.Namespace(dry_run=True, json=False)
    rc = cli._run_system_check(args)

    assert rc == 0
    assert notify_called["count"] == 0
    out = capsys.readouterr().out
    assert "disk" in out


def test_cli_non_dry_run_calls_notify_for_warnings(monkeypatch, capsys):
    from orbit import __main__ as cli
    from orbit import notify as notify_mod

    monkeypatch.setattr(sw, "check_all", lambda **kw: [
        {"severity": "info", "type": "tailnet", "message": "peer x", "context": {}},
        {"severity": "warning", "type": "disk", "message": "disk hot", "context": {}},
        {"severity": "critical", "type": "service", "message": "nginx down", "context": {}},
    ])

    calls: list[dict] = []

    async def _fake_notify(**kw):
        calls.append(kw)
        return {"ok": True}

    monkeypatch.setattr(notify_mod, "notify", _fake_notify)

    import argparse
    args = argparse.Namespace(dry_run=False, json=False)
    rc = cli._run_system_check(args)

    assert rc == 0
    assert len(calls) == 2  # info filtered out
    assert {c["title"] for c in calls} == {"system: disk", "system: service"}
    severities = sorted(c["priority"] for c in calls)
    assert severities == [4, 5]
