"""``GET /api/orchestrator/artifacts/{id}/preview`` — read-in-place, no download.

``/file`` deliberately forces ``application/octet-stream`` + ``attachment`` for
everything outside the image/audio/video mime allowlist, so a PDF or a CSV can
only be *downloaded*, never read in the panel. This route is the safe inline
counterpart: text-family payloads come back as ``text/plain`` (never
``text/html`` — that would be same-origin XSS), PDFs come back as
``application/pdf`` pinned into an opaque sandbox origin, and anything else is
refused with 415 so the UI can fall back to the download row.
"""
from __future__ import annotations

import json

import pytest

from orbit import orchestrator as orch

TXT_ID = "art-20260818T000000-aaa111"
DOC_ID = "art-20260818T000000-bbb222"
PDF_ID = "art-20260818T000000-ccc333"
BIN_ID = "art-20260818T000000-ddd444"
CHART_ID = "art-20260818T000000-eee555"
HTML_ID = "art-20260818T000000-fff666"


def _write_artifact(dirp, artifact_id, *, type="file", body=b"data", ext="bin",
                    mime=None, extra=None):
    dirp.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": artifact_id,
        "type": type,
        "title": "demo",
        "src": None,
        "mime": mime,
        "created_at": "2026-08-18T00:00:00Z",
        "extra": extra or {},
    }
    if body is not None:
        payload = dirp / f"{artifact_id}.{ext}"
        payload.write_bytes(body)
        manifest["src"] = payload.name
        manifest["size"] = len(body)
    (dirp / f"{artifact_id}.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def artdir(tmp_path, monkeypatch):
    d = tmp_path / ".artifacts"
    monkeypatch.setattr(orch, "_resolve_artifacts_dir", lambda *a, **k: d)
    return d


def test_preview_serves_text_inline(client, artdir):
    _write_artifact(artdir, TXT_ID, body="kolumna,wartość\na,1\n".encode("utf-8"),
                    ext="csv", mime="text/csv")
    r = client.get(f"/api/orchestrator/artifacts/{TXT_ID}/preview?lib_id=__global__")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["x-artifact-truncated"] == "0"
    assert "wartość" in r.text


def test_preview_serves_doc_type(client, artdir):
    _write_artifact(artdir, DOC_ID, type="doc", body=b"# Raport\n\ntresc", ext="md",
                    mime="text/markdown")
    r = client.get(f"/api/orchestrator/artifacts/{DOC_ID}/preview?lib_id=__global__")
    assert r.status_code == 200
    assert r.text.startswith("# Raport")


def test_preview_truncates_at_limit(client, artdir):
    _write_artifact(artdir, TXT_ID, body=b"x" * 5000, ext="txt", mime="text/plain")
    r = client.get(
        f"/api/orchestrator/artifacts/{TXT_ID}/preview?lib_id=__global__&limit=1000"
    )
    assert r.status_code == 200
    assert len(r.text) == 1000
    assert r.headers["x-artifact-truncated"] == "1"
    assert r.headers["x-artifact-size"] == "5000"


def test_preview_html_downgrades_to_plain_text(client, artdir):
    """html source is readable/copyable, but must never execute same-origin."""
    _write_artifact(artdir, HTML_ID, type="html", body=b"<script>alert(1)</script>",
                    ext="html", mime="text/html")
    r = client.get(f"/api/orchestrator/artifacts/{HTML_ID}/preview?lib_id=__global__")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "<script>" in r.text


def test_preview_serves_pdf_sandboxed(client, artdir):
    _write_artifact(artdir, PDF_ID, body=b"%PDF-1.4 fake", ext="pdf",
                    mime="application/pdf")
    r = client.get(f"/api/orchestrator/artifacts/{PDF_ID}/preview?lib_id=__global__")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"].startswith("inline")
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox")
    assert "allow-same-origin" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_preview_refuses_binary(client, artdir):
    _write_artifact(artdir, BIN_ID, body=b"\x00\x01\x02", ext="bin",
                    mime="application/octet-stream")
    r = client.get(f"/api/orchestrator/artifacts/{BIN_ID}/preview?lib_id=__global__")
    assert r.status_code == 415


def test_preview_spec_type_404(client, artdir):
    _write_artifact(artdir, CHART_ID, type="chart", body=None)
    r = client.get(f"/api/orchestrator/artifacts/{CHART_ID}/preview?lib_id=__global__")
    assert r.status_code == 404


def test_preview_missing_artifact_404(client, artdir):
    r = client.get(f"/api/orchestrator/artifacts/{TXT_ID}/preview?lib_id=__global__")
    assert r.status_code == 404


def test_preview_unknown_mime_falls_back_to_extension(client, artdir):
    """Manifest mime is agent-written; a missing one must not kill the preview."""
    _write_artifact(artdir, TXT_ID, body=b"# tytul\n", ext="md", mime=None)
    r = client.get(f"/api/orchestrator/artifacts/{TXT_ID}/preview?lib_id=__global__")
    assert r.status_code == 200
    assert r.text.startswith("# tytul")


def test_file_route_uses_original_filename(client, artdir):
    """Downloads keep the agent's filename, not the opaque art-<id>.<ext>."""
    _write_artifact(artdir, TXT_ID, body=b"a,b\n", ext="csv", mime="text/csv",
                    extra={"filename": "raport q1.csv"})
    r = client.get(f"/api/orchestrator/artifacts/{TXT_ID}/file?lib_id=__global__")
    assert r.status_code == 200
    assert 'filename="raport q1.csv"' in r.headers["content-disposition"]


def test_file_route_sanitizes_filename(client, artdir):
    """A hostile manifest filename must not break out of the header."""
    _write_artifact(artdir, TXT_ID, body=b"a,b\n", ext="csv", mime="text/csv",
                    extra={"filename": '../../evil"\r\nX-Injected: 1'})
    r = client.get(f"/api/orchestrator/artifacts/{TXT_ID}/file?lib_id=__global__")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert "x-injected" not in {k.lower() for k in r.headers}
    assert ".." not in cd
