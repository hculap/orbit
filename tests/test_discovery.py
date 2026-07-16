"""Smoke tests — assume run on Hetzner with PARA structure in place."""
from __future__ import annotations
import os
from pathlib import Path

import pytest

from orbit import discovery
from orbit.discovery import discover_all, discover_projects
from orbit.nginx_parser import discover_apps


def test_discover_all_returns_expected_keys():
    data = discover_all()
    assert set(data.keys()) == {"areas", "projects", "resources", "apps", "system", "host"}
    # PARA collections are lists; system/host are metadata dicts.
    for k in ("areas", "projects", "resources", "apps"):
        assert isinstance(data[k], list)
    for k in ("system", "host"):
        assert isinstance(data[k], dict)


@pytest.mark.skipif(not Path("/etc/nginx/conf.d/apps").is_dir(), reason="nginx conf dir missing")
def test_apps_have_path_and_port():
    apps = discover_apps()
    for app in apps:
        assert app["path"].startswith("/")
        assert isinstance(app["port"], int)
        assert 1 <= app["port"] <= 65535


def test_discover_projects_recognizes_dashboard_sidecar(tmp_path, monkeypatch):
    """Project freshly created via POST /api/library/projects has only
    ``.library.json``. It must still appear in discovery — otherwise the
    dashboard's own "New Project" form creates invisible projects.

    Regression for 2026-05-17: user created "FIFA WC26 Family Game" via UI;
    discovery treated the empty dir as a "group" (no marker file), recursed
    into it, found nothing, emitted no entry.
    """
    monkeypatch.setattr(discovery, "PROJECTS", tmp_path)
    monkeypatch.setattr(discovery, "HOME", tmp_path.parent)

    fresh = tmp_path / "FIFA WC26 Family Game"
    fresh.mkdir()
    (fresh / ".library.json").write_text('{"created_iso":"2026-05-17T17:43:29Z"}')
    (fresh / ".gitignore").write_text("")

    results = discover_projects({})
    names = [p["name"] for p in results]
    assert "FIFA WC26 Family Game" in names


def test_discover_projects_populates_linked_areas(tmp_path, monkeypatch):
    """``projects[].linked_areas`` is derived from ``~/Areas/<area>/projects/``
    symlinks — the contract the dashboard's area-grouped Projects view (#98)
    depends on. ``_project_to_areas()`` reads the module-level ``AREAS``
    constant, so the test MUST patch it too, or it resolves the real ~/Areas.
    """
    home = tmp_path
    projects_dir = home / "Projects"
    areas_dir = home / "Areas"
    projects_dir.mkdir()
    areas_dir.mkdir()

    foo = projects_dir / "foo"
    foo.mkdir()
    (foo / ".library.json").write_text('{"created_iso":"2026-06-15T00:00:00Z"}')

    dom_links = areas_dir / "Dom" / "projects"
    dom_links.mkdir(parents=True)
    (dom_links / "foo").symlink_to(foo)

    monkeypatch.setattr(discovery, "PROJECTS", projects_dir)
    monkeypatch.setattr(discovery, "AREAS", areas_dir)
    monkeypatch.setattr(discovery, "HOME", home)

    res = {p["name"]: p for p in discover_projects({})}
    assert res["foo"]["linked_areas"] == ["Dom"]
