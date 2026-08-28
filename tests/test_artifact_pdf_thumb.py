"""``GET /api/orchestrator/artifacts/{id}/thumb`` — PDF gallery tiles.

The gallery used to show a generic file icon for every non-image artifact, so a
PDF was indistinguishable from a zip. The thumb route now rasterises page 1 via
poppler's ``pdftoppm``; when poppler is missing or the file is not a real PDF it
must 404 (the tile falls back to the icon) rather than 500.
"""
from __future__ import annotations

import json
import shutil

import pytest

from orbit import orchestrator as orch

PDF_ID = "art-20260818T010000-a1b2c3"
BIN_ID = "art-20260818T010000-d4e5f6"


def _minimal_pdf() -> bytes:
    """A one-page PDF poppler can rasterise, small enough to inline here."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 44 >>\nstream\nBT /F1 24 Tf 20 100 Td (Hello) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref,
    )
    return bytes(out)


def _write(dirp, artifact_id, *, body, ext, mime, type="file"):
    dirp.mkdir(parents=True, exist_ok=True)
    (dirp / f"{artifact_id}.{ext}").write_bytes(body)
    (dirp / f"{artifact_id}.json").write_text(json.dumps({
        "id": artifact_id, "type": type, "title": "demo",
        "src": f"{artifact_id}.{ext}", "mime": mime, "size": len(body),
        "created_at": "2026-08-18T01:00:00Z", "extra": {},
    }), encoding="utf-8")


@pytest.fixture
def artdir(tmp_path, monkeypatch):
    d = tmp_path / ".artifacts"
    monkeypatch.setattr(orch, "_resolve_artifacts_dir", lambda *a, **k: d)
    return d


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_thumb_renders_pdf_first_page(client, artdir):
    _write(artdir, PDF_ID, body=_minimal_pdf(), ext="pdf", mime="application/pdf")
    r = client.get(f"/api/orchestrator/artifacts/{PDF_ID}/thumb?lib_id=__global__&size=128")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")  # JPEG SOI


def test_thumb_on_broken_pdf_404s_not_500s(client, artdir):
    _write(artdir, PDF_ID, body=b"%PDF-1.4 not really a pdf", ext="pdf",
           mime="application/pdf")
    r = client.get(f"/api/orchestrator/artifacts/{PDF_ID}/thumb?lib_id=__global__")
    assert r.status_code == 404


def test_thumb_still_404s_for_plain_binary(client, artdir):
    _write(artdir, BIN_ID, body=b"\x00\x01", ext="bin", mime="application/octet-stream")
    r = client.get(f"/api/orchestrator/artifacts/{BIN_ID}/thumb?lib_id=__global__")
    assert r.status_code == 404
