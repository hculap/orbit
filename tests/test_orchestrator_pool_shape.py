"""Contract test for tmux_pool_snapshot() — the /api/orchestrator/pool payload.

The desktop session switcher renders each warm slot from these fields, so this
pins the shape the UI depends on: the base slot keys PLUS the enriched
agent/title/lib_id. The enrichment lives behind a broad try/except (diagnostics
must never break the page), so we also assert a failing enrichment degrades to
base slots instead of silently dropping them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orbit import orchestrator as orch


class _FakePool:
    def __init__(self, slots, *, live_ids=None, live_raises=False):
        self._snap = {"active": len(slots), "pool_size": 4, "idle_ttl_s": 600.0, "slots": slots}
        # None → every slot is considered live (no reconciliation drops).
        self._live = live_ids
        self._live_raises = live_raises

    def snapshot(self):
        return self._snap

    async def live_session_ids(self):
        if self._live_raises:
            raise RuntimeError("tmux list-sessions blew up")
        if self._live is None:
            return {s["session_id"] for s in self._snap["slots"]}
        return set(self._live)


def _base_slot(sid: str, cwd: str = "/home/testuser/Projects/my-project"):
    return {
        "session_id": sid,
        "cwd": cwd,
        "uptime_s": 12.0,
        "idle_s": 3.0,
        "cooling": False,
        "evict_in_s": None,
        "persistent": True,
    }


# UI-required keys per slot (what session-switcher.jsx reads).
_UI_KEYS = ("session_id", "agent", "title", "lib_id", "cwd", "idle_s", "persistent", "cooling", "evict_in_s")


def test_pool_snapshot_no_pool_returns_empty(monkeypatch):
    """No slot ever acquired → empty pool without spawning one just to poll."""
    monkeypatch.setattr(orch, "_tmux_pool", None)
    snap = orch.tmux_pool_snapshot()
    assert snap["active"] == 0
    assert snap["slots"] == []
    assert isinstance(snap["pool_size"], int)
    assert isinstance(snap["idle_ttl_s"], float)


def test_pool_snapshot_enriches_slots_with_ui_fields(monkeypatch):
    sid = "abc-123"
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([_base_slot(sid)]))
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {sid: {"title": "My Title", "lib_id": "projects/my-project"}})
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [{"id": sid, "first_user_preview": "preview text"}])

    snap = orch.tmux_pool_snapshot()
    assert snap["active"] == 1
    s = snap["slots"][0]
    for key in _UI_KEYS:
        assert key in s, f"missing UI field: {key}"
    assert s["title"] == "My Title"            # manual sidecar title wins
    assert s["agent"] == "My Project"        # humanized from lib_id
    assert s["lib_id"] == "projects/my-project"
    assert s["persistent"] is True


def test_pool_snapshot_title_falls_back_to_first_user_preview(monkeypatch):
    sid = "def-456"
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([_base_slot(sid)]))
    # No manual title — only a lib_id; title should fall back to the JSONL preview.
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {sid: {"lib_id": "areas/Work"}})
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [{"id": sid, "first_user_preview": "raport Q2"}])

    s = orch.tmux_pool_snapshot()["slots"][0]
    assert s["title"] == "raport Q2"
    assert s["agent"] == "Work"


def test_pool_snapshot_agent_from_cwd_when_no_lib_id(monkeypatch):
    home = Path.home()
    sid = "ghi-789"
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([_base_slot(sid, cwd=str(home / "Projects" / "my-project"))]))
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {})  # no sidecar at all
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [])

    s = orch.tmux_pool_snapshot()["slots"][0]
    assert s["agent"] == "My Project"            # humanized from the derived lib_id
    assert s["lib_id"] == "projects/my-project"  # now DERIVED from cwd (no sidecar)


def test_pool_snapshot_cwdless_session_stays_global(monkeypatch):
    """A session whose cwd is outside Areas/Projects keeps an empty lib_id →
    Global marker for the UI."""
    sid = "glob-1"
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([_base_slot(sid, cwd="/tmp/scratch")]))
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {})
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [])
    s = orch.tmux_pool_snapshot()["slots"][0]
    assert s["lib_id"] == ""


def test_pool_snapshot_enrich_failure_keeps_base_slots(monkeypatch):
    """If enrichment raises, slots must still come back with their base keys
    (the broad except must not drop the whole slot list)."""
    sid = "jkl-000"
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([_base_slot(sid)]))

    def _boom():
        raise RuntimeError("meta blew up")

    monkeypatch.setattr(orch.meta_mod, "all_meta", _boom)
    snap = orch.tmux_pool_snapshot()
    assert snap["active"] == 1
    assert snap["slots"][0]["session_id"] == sid
    assert snap["slots"][0]["persistent"] is True


# ── tmux_pool_snapshot_live: reconcile the snapshot against live tmux ────────


def _stub_enrich(monkeypatch):
    """Make the (sync) snapshot enrichment a no-op so tests focus on the live
    reconciliation, not title/agent derivation."""
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {})
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [])


def test_pool_snapshot_live_drops_dead_slots(monkeypatch):
    """A slot whose hd-<id> tmux died out-of-band is dropped from the snapshot
    (so it can't show as a phantom agent tab / session dot)."""
    _stub_enrich(monkeypatch)
    slots = [_base_slot("alive-1"), _base_slot("dead-2"), _base_slot("alive-3")]
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool(slots, live_ids={"alive-1", "alive-3"}))

    snap = asyncio.run(orch.tmux_pool_snapshot_live())
    assert snap["active"] == 2
    assert [s["session_id"] for s in snap["slots"]] == ["alive-1", "alive-3"]


def test_pool_snapshot_live_keeps_all_when_all_live(monkeypatch):
    _stub_enrich(monkeypatch)
    slots = [_base_slot("a"), _base_slot("b")]
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool(slots))  # live_ids=None → all live

    snap = asyncio.run(orch.tmux_pool_snapshot_live())
    assert snap["active"] == 2
    assert {s["session_id"] for s in snap["slots"]} == {"a", "b"}


def test_pool_snapshot_live_probe_error_passes_through_unfiltered(monkeypatch):
    """A failing tmux probe must NOT hide live slots — degrade to the raw
    snapshot rather than blanking the UI on a transient hiccup."""
    _stub_enrich(monkeypatch)
    slots = [_base_slot("a"), _base_slot("b")]
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool(slots, live_raises=True))

    snap = asyncio.run(orch.tmux_pool_snapshot_live())
    assert snap["active"] == 2
    assert {s["session_id"] for s in snap["slots"]} == {"a", "b"}


def test_pool_snapshot_live_empty_live_set_folds_every_slot(monkeypatch):
    """A genuinely empty live set (no tmux sessions at all) correctly drops
    every tracked slot — distinct from the probe-error passthrough above."""
    _stub_enrich(monkeypatch)
    slots = [_base_slot("a"), _base_slot("b")]
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool(slots, live_ids=set()))

    snap = asyncio.run(orch.tmux_pool_snapshot_live())
    assert snap["active"] == 0
    assert snap["slots"] == []


def test_pool_snapshot_live_no_pool_returns_empty(monkeypatch):
    monkeypatch.setattr(orch, "_tmux_pool", None)
    snap = asyncio.run(orch.tmux_pool_snapshot_live())
    assert snap["active"] == 0
    assert snap["slots"] == []


# ── lib_id-from-cwd fallback (legacy sessions with no sidecar lib_id) ─────────


def test_lib_id_from_cwd_maps_areas_projects():
    home = Path.home()
    assert orch._lib_id_from_cwd(str(home / "Areas" / "Health")) == "areas/Health"
    assert orch._lib_id_from_cwd(str(home / "Areas" / "Health" / "sub")) == "areas/Health"
    assert orch._lib_id_from_cwd(str(home / "Projects" / "my-project")) == "projects/my-project"
    assert orch._lib_id_from_cwd(str(home / "Projects" / "Group" / "Name")) == "projects/Group/Name"
    assert orch._lib_id_from_cwd(str(home)) is None
    assert orch._lib_id_from_cwd("/tmp/x") is None
    assert orch._lib_id_from_cwd(None) is None


def test_lib_id_from_cwd_area_symlinked_project_stays_in_area(tmp_path, monkeypatch):
    """A PARA area's symlinked project (~/Areas/Work/projects/OffBall →
    ~/Projects/OffBall) must group under the AREA, not the standalone project —
    i.e. symlinks are NOT followed."""
    monkeypatch.setattr(orch, "HOME", tmp_path)
    (tmp_path / "Projects" / "OffBall").mkdir(parents=True)
    area_projects = tmp_path / "Areas" / "Work" / "projects"
    area_projects.mkdir(parents=True)
    (area_projects / "OffBall").symlink_to(tmp_path / "Projects" / "OffBall")
    assert orch._lib_id_from_cwd(str(area_projects / "OffBall")) == "areas/Work"
    # a genuine ~/Projects cwd still maps to the project
    assert orch._lib_id_from_cwd(str(tmp_path / "Projects" / "OffBall")) == "projects/OffBall"


def test_pool_snapshot_derives_lib_id_and_updated_at_from_cwd(monkeypatch):
    """A legacy slot whose sidecar has no lib_id but whose cwd is under an area
    must still resolve to that agent (correct tab/icon), and carry updated_at."""
    home = Path.home()
    sid = "legacy-1"
    slot = _base_slot(sid, cwd=str(home / "Areas" / "Health"))
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([slot]))
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {sid: {"title": "Z"}})  # NO lib_id
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [{"id": sid, "updated_at": 1234.5}])

    s = orch.tmux_pool_snapshot()["slots"][0]
    assert s["lib_id"] == "areas/Health"
    assert s["agent"] == "Health"
    assert s["updated_at"] == 1234.5


def test_pool_snapshot_keeps_explicit_lib_id_over_cwd(monkeypatch):
    home = Path.home()
    sid = "x"
    slot = _base_slot(sid, cwd=str(home / "Areas" / "Health"))
    monkeypatch.setattr(orch, "_tmux_pool", _FakePool([slot]))
    monkeypatch.setattr(orch.meta_mod, "all_meta", lambda: {sid: {"lib_id": "areas/Custom"}})
    monkeypatch.setattr(orch.jsonl_mod, "list_sessions", lambda: [{"id": sid}])
    s = orch.tmux_pool_snapshot()["slots"][0]
    assert s["lib_id"] == "areas/Custom"  # explicit sidecar wins
    assert s["updated_at"] == 0.0          # missing summary updated_at → 0.0
