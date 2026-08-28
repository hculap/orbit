"""``artifacts`` CLI — the ``doc`` type + the original-filename manifest field.

``doc`` is the "I only want to READ this" artifact: the agent hands over
markdown/plain text (raw, a path, or stdin) and the panel renders it with a
copy button instead of forcing a download. It is a written payload like
``html``, not a copied one like ``file``.

The lib ships under ``skills/`` (not the package), so it is loaded by path —
same pattern as ``test_dashboard_mcp.py``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "skills/artifacts/scripts/artifacts_lib.py"


def _load():
    spec = importlib.util.spec_from_file_location("artifacts_lib_under_test", LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lib(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setenv("HD_LIB_ID", "")
    monkeypatch.setenv("HD_SESSION_ID", "sess-test")
    monkeypatch.setattr(mod, "_GLOBAL_ARTIFACTS_ROOT", tmp_path / "artifacts")
    # Never touch the real dashboard from a test run.
    monkeypatch.setattr(mod, "notify", lambda *a, **k: False)
    return mod


def test_doc_is_a_known_type(lib):
    assert "doc" in lib.ARTIFACT_TYPES


def test_create_doc_from_raw_text(lib, tmp_path):
    res = lib.create("doc", "Raport Q1", file="# Raport\n\nTreść raportu.")
    m = res["manifest"]
    assert m["type"] == "doc"
    assert m["src"].endswith(".md")
    assert m["mime"] == "text/markdown"
    payload = Path(res["path"]).parent / m["src"]
    assert payload.read_text(encoding="utf-8").startswith("# Raport")
    assert m["size"] == len(payload.read_bytes())


def test_create_doc_from_a_file_keeps_name_and_extension(lib, tmp_path):
    src = tmp_path / "notatki ze spotkania.txt"
    src.write_text("punkt 1\npunkt 2\n", encoding="utf-8")
    m = lib.create("doc", "Notatki", file=str(src))["manifest"]
    assert m["src"].endswith(".txt")
    assert m["mime"] == "text/plain"
    # The download name survives the copy into the opaque art-<id>.<ext> store.
    assert m["extra"]["filename"] == "notatki ze spotkania.txt"


def test_create_doc_rejects_oversized_payload(lib):
    with pytest.raises(lib.ArtifactError):
        lib.create("doc", "Za duży", file="x" * (lib.DOC_MAX_BYTES + 1))


def test_create_doc_requires_content(lib):
    with pytest.raises(lib.ArtifactError):
        lib.create("doc", "Pusty")


def test_file_artifact_records_original_filename(lib, tmp_path):
    src = tmp_path / "eksport danych.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    m = lib.create("file", "Eksport", file=str(src))["manifest"]
    assert m["extra"]["filename"] == "eksport danych.csv"
    assert m["mime"] == "text/csv"


def test_image_artifact_records_original_filename(lib, tmp_path):
    src = tmp_path / "wykres.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    m = lib.create("image", "Wykres", file=str(src))["manifest"]
    assert m["extra"]["filename"] == "wykres.png"
