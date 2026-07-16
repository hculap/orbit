"""Sync module — file browser + upload/download/preview rooted at ~/Sync/.

(Module name kept as `share` for code stability, but SYNC_ROOT = ~/Sync.
This is the same Syncthing folder shared between Mac and Hetzner — single
source of truth. Deletes rely on Syncthing's staggered versioning to keep
recoverable history under .stversions/, so we don't maintain our own .trash.)
"""
from __future__ import annotations
import os
import shutil
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
SYNC_ROOT = HOME / "Sync"
# Backwards-compat alias
SHARE_ROOT = SYNC_ROOT

MAX_UPLOAD_BYTES = 500 * 1024 * 1024   # 500 MB per file

# Syncthing internals + sync-cleaner internals — never show in UI
HIDDEN_NAMES = {".stfolder", ".stversions", ".stignore", ".cleaner", ".trash"}

PREVIEW_TEXT_EXT = {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml",
                    ".toml", ".ini", ".conf", ".csv", ".tsv", ".xml", ".html",
                    ".py", ".js", ".ts", ".sh", ".rb", ".go", ".rs", ".env"}
PREVIEW_IMG_EXT  = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg"}
PREVIEW_PDF_EXT  = {".pdf"}
PREVIEW_VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}

# Folder size cache
_size_cache: dict[Path, tuple[float, int]] = {}
SIZE_CACHE_TTL = 60.0


def _safe_path(rel: str) -> Path:
    """Resolve `rel` relative to SYNC_ROOT, refusing path traversal."""
    rel = (rel or "").strip().lstrip("/")
    if rel == "":
        return SYNC_ROOT
    p = (SYNC_ROOT / rel).resolve()
    root = SYNC_ROOT.resolve()
    if p != root and root not in p.parents:
        raise ValueError("path escapes Sync root")
    return p


def ensure_root() -> None:
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)


def _entry_kind(ext: str) -> str:
    if ext in PREVIEW_IMG_EXT: return "image"
    if ext in PREVIEW_TEXT_EXT: return "text"
    if ext in PREVIEW_PDF_EXT: return "pdf"
    if ext in PREVIEW_VIDEO_EXT: return "video"
    return "binary"


def _folder_size(p: Path) -> int:
    """Recursive byte total with TTL cache.

    Uses ``os.scandir`` instead of ``Path.rglob`` + ``Path.stat`` — same
    recursive walk, ~3–5× cheaper because scandir's ``DirEntry`` already
    carries the stat info (no extra syscall) and yields plain strings
    instead of Path objects.
    """
    now = time.time()
    cached = _size_cache.get(p)
    if cached and (now - cached[0]) < SIZE_CACHE_TTL:
        return cached[1]
    total = 0
    stack: list[str] = [str(p)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
    _size_cache[p] = (now, total)
    return total


def list_dir(rel: str, with_folder_sizes: bool = True) -> dict:
    """List one directory level. Folders first, alphabetic. Hides Syncthing/cleaner internals."""
    ensure_root()
    p = _safe_path(rel)
    if not p.is_dir():
        return {"path": rel, "ok": False, "error": "not a directory", "items": []}
    items = []
    for entry in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if entry.name in HIDDEN_NAMES:
            continue
        if entry.name.startswith(".") and entry.name not in (".keep",):
            continue
        try:
            stat = entry.stat()
        except Exception:
            continue
        if entry.is_dir():
            items.append({
                "name": entry.name, "type": "dir",
                "size": _folder_size(entry) if with_folder_sizes else None,
                "mtime": stat.st_mtime,
            })
        else:
            items.append({
                "name": entry.name, "type": "file",
                "size": stat.st_size, "mtime": stat.st_mtime,
                "ext": entry.suffix.lower(),
                "kind": _entry_kind(entry.suffix.lower()),
            })
    return {"path": rel.strip("/"), "ok": True, "items": items}


# ── CRUD operations ─────────────────────────────────────────


def mkdir(rel_dir: str, name: str) -> dict:
    """Create new folder under rel_dir."""
    ensure_root()
    parent = _safe_path(rel_dir)
    if not parent.is_dir():
        return {"ok": False, "error": "parent is not a directory"}
    name = (name or "").strip().split("/")[-1].split("\\")[-1]
    if not name or name.startswith(".") or name in ("..",):
        return {"ok": False, "error": "invalid folder name"}
    target = parent / name
    if target.exists():
        return {"ok": False, "error": "folder already exists"}
    target.mkdir()
    _size_cache.clear()
    return {"ok": True, "path": str(target.relative_to(SYNC_ROOT))}


def delete(rel: str) -> dict:
    """Delete file/dir. Recovery via Syncthing's .stversions/ (staggered versioning)."""
    ensure_root()
    src = _safe_path(rel)
    if src == SYNC_ROOT:
        return {"ok": False, "error": "refuse to delete root"}
    if src.name in HIDDEN_NAMES:
        return {"ok": False, "error": "cannot delete protected entry"}
    if not src.exists():
        return {"ok": False, "error": "not found"}
    try:
        if src.is_dir():
            shutil.rmtree(src)
        else:
            src.unlink()
    except Exception as e:
        return {"ok": False, "error": f"delete failed: {e}"}
    _size_cache.clear()
    return {"ok": True, "deleted": str(src.relative_to(SYNC_ROOT))}


def rename(rel: str, new_name: str) -> dict:
    """Rename file/folder in place."""
    ensure_root()
    src = _safe_path(rel)
    if not src.exists() or src == SYNC_ROOT:
        return {"ok": False, "error": "not allowed"}
    if src.name in HIDDEN_NAMES:
        return {"ok": False, "error": "cannot rename protected entry"}
    new_name = (new_name or "").strip().split("/")[-1].split("\\")[-1]
    if not new_name or new_name.startswith(".") or new_name in ("..",):
        return {"ok": False, "error": "invalid name"}
    dst = src.parent / new_name
    if dst.exists():
        return {"ok": False, "error": "target name exists"}
    src.rename(dst)
    _size_cache.clear()
    return {"ok": True, "path": str(dst.relative_to(SYNC_ROOT))}
