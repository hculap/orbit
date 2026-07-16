"""``GET /api/orchestrator/artifacts/{id}/view`` — full-screen html render.

The route serves an ``html`` artifact as real ``text/html`` (so a new tab
renders it) but pins it into an opaque origin with a ``Content-Security-Policy:
sandbox`` header that deliberately OMITS ``allow-same-origin`` — the escape
hatch that would give the agent-written doc access to the dashboard's cookies /
localStorage. These tests lock that contract down.
"""
from __future__ import annotations

import json

import pytest

from orbit import orchestrator as orch


def _write_artifact(dirp, artifact_id, *, type="html", body="<h1>hi</h1>", ext="html"):
    dirp.mkdir(parents=True, exist_ok=True)
    payload = dirp / f"{artifact_id}.{ext}"
    payload.write_text(body, encoding="utf-8")
    manifest = {
        "id": artifact_id,
        "type": type,
        "title": "demo",
        "src": payload.name,
        "created_at": "2026-07-07T00:00:00Z",
    }
    (dirp / f"{artifact_id}.json").write_text(json.dumps(manifest), encoding="utf-8")


HTML_ID = "art-20260707T000000-abc123"
FILE_ID = "art-20260707T000000-def456"


@pytest.fixture
def artdir(tmp_path, monkeypatch):
    d = tmp_path / ".artifacts"
    monkeypatch.setattr(orch, "_resolve_artifacts_dir", lambda *a, **k: d)
    return d


def test_view_serves_html_sandboxed(client, artdir):
    _write_artifact(artdir, HTML_ID, body="<h1>full screen</h1>")
    r = client.get(f"/api/orchestrator/artifacts/{HTML_ID}/view?lib_id=__global__")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "full screen" in r.text
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox")
    assert "allow-scripts" in csp
    # The whole point: no same-origin escape → opaque origin, no cookie access.
    assert "allow-same-origin" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_view_rejects_non_html_artifact(client, artdir):
    _write_artifact(artdir, FILE_ID, type="file", body="data", ext="bin")
    r = client.get(f"/api/orchestrator/artifacts/{FILE_ID}/view?lib_id=__global__")
    assert r.status_code == 404


def test_view_missing_artifact_404(client, artdir):
    r = client.get(f"/api/orchestrator/artifacts/{HTML_ID}/view?lib_id=__global__")
    assert r.status_code == 404
