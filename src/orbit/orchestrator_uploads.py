"""Orchestrator upload sink — per-session staging dir for files attached to prompts.

Layout under ``~/.orchestrator/uploads/<session-id>/``: one dir per UUID-v4
session, each holding the uploaded files (collision-suffixed) plus a
``.pending.json`` queue of absolute paths the runner pops on the next
prompt. Streams write in 64 KB chunks with a 100 MB per-file ceiling; the
session dir is created lazily on first save. ``safe_upload_path`` resolves
single files for the GET ``/uploads/{sid}/{name}`` static-serve route and
defeats traversal even after filename sanitization.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

UPLOADS_ROOT = Path.home() / ".orchestrator" / "uploads"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB per file

SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_PENDING_NAME = ".pending.json"
_CHUNK_SIZE = 64 * 1024

_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg"}
_TEXT_EXT = {".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".toml",
             ".ini", ".conf", ".csv", ".tsv", ".xml", ".html", ".py", ".js", ".ts",
             ".sh", ".rb", ".go", ".rs", ".env"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}


def _warn(msg: str) -> None:
    print(f"[orchestrator_uploads] {msg}", file=sys.stderr)


def _entry_kind(ext: str) -> str:
    if ext in _IMG_EXT: return "image"
    if ext in _TEXT_EXT: return "text"
    if ext == ".pdf": return "pdf"
    if ext in _VIDEO_EXT: return "video"
    return "binary"


def safe_session_dir(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        raise ValueError("invalid session_id")
    return UPLOADS_ROOT / session_id


def safe_upload_path(session_id: str, filename: str) -> Path:
    """Resolve a single uploaded file path, refusing traversal in either segment."""
    session_dir = safe_session_dir(session_id)
    name = (filename or "").split("/")[-1].split("\\")[-1]
    if not name or name.startswith(".") or ".." in name:
        raise ValueError("invalid filename")
    target = (session_dir / name).resolve()
    if not target.is_relative_to(session_dir.resolve()):
        raise ValueError("path escapes session dir")
    return target


def _next_collision_target(session_dir: Path, name: str) -> Path:
    target = session_dir / name
    stem, ext, i = target.stem, target.suffix, 1
    while target.exists():
        target = session_dir / f"{stem} ({i}){ext}"
        i += 1
    return target


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
    except Exception:
        try: tmp.close()
        except Exception: pass
        if tmp_path.exists():
            try: tmp_path.unlink()
            except OSError: pass
        raise


def _read_pending(session_dir: Path) -> list[str]:
    pending_path = session_dir / _PENDING_NAME
    if not pending_path.exists():
        return []
    try:
        with pending_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        _warn(f"corrupt or unreadable {pending_path}: {e}; treating as empty")
        return []
    if not isinstance(payload, list):
        _warn(f"{pending_path} is not a list; treating as empty")
        return []
    return [p for p in payload if isinstance(p, str)]


async def save_uploads(
    session_id: str,
    files: list[UploadFile],
    *,
    queue_pending: bool = True,
) -> list[dict]:
    """Stream each upload to disk under the session dir; append paths to .pending.json.

    If any file in the batch overshoots `MAX_UPLOAD_BYTES`, ALL files written
    so far in the same call are unlinked before re-raising — otherwise earlier
    files would be orphaned (on disk, but never recorded in `.pending.json`
    because the commit happens after the loop).

    ``queue_pending`` (default True): append the saved paths to the
    ``.pending.json`` queue the chat `claude -p` runner pops on the next turn.
    The interactive terminal sets it False — it pastes the absolute path
    straight into the tmux session, so the chat runner must NOT also inject
    the same files as a trailing ``<attached>`` block on the user's next
    chat message.
    """
    session_dir = safe_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    written_paths: list[Path] = []
    try:
        for f in files:
            name = (f.filename or "upload").split("/")[-1].split("\\")[-1]
            if not name or name.startswith("."):
                continue
            target = _next_collision_target(session_dir, name)
            written = 0
            with target.open("wb") as out:
                while True:
                    chunk = await f.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        out.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(413, detail=f"file '{name}' exceeds {MAX_UPLOAD_BYTES} bytes")
                    out.write(chunk)
            written_paths.append(target)
            saved.append({
                "name": target.name, "size": written,
                "server_path": str(target),
                "rel_path": str(target.relative_to(UPLOADS_ROOT)),
            })
    except BaseException:
        # Roll back partial writes so we don't leave orphans the user can't
        # see and pop_pending will never inject.
        for p in written_paths:
            try: p.unlink(missing_ok=True)
            except OSError: pass
        raise

    if saved and queue_pending:
        existing = _read_pending(session_dir)
        new_pending = [*existing, *(item["server_path"] for item in saved)]
        try:
            _atomic_write_json(session_dir / _PENDING_NAME, new_pending)
        except OSError as e:
            _warn(f"failed to update pending list for {session_id}: {e}")

    return saved


def list_uploads(session_id: str) -> list[dict]:
    """Return saved files (skipping dotfiles) with name/size/mtime/kind."""
    session_dir = safe_session_dir(session_id)
    if not session_dir.is_dir():
        return []
    items: list[dict] = []
    try:
        scanner = os.scandir(session_dir)
    except OSError as e:
        _warn(f"scandir failed for {session_dir}: {e}")
        return []
    with scanner as it:
        for entry in it:
            if entry.name.startswith(".") or not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            items.append({
                "name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime,
                "server_path": entry.path,
                "kind": _entry_kind(Path(entry.name).suffix.lower()),
            })
    items.sort(key=lambda x: x["name"].lower())
    return items


def pop_pending(session_id: str) -> list[str]:
    """Return and clear the queue of server paths waiting for the next prompt.

    Only returns the paths if the queue was successfully cleared; otherwise
    returns []. This prevents duplicate `<attached>` injection on the next
    call when the atomic write fails — the paths stay queued and get popped
    once the underlying error clears.
    """
    session_dir = safe_session_dir(session_id)
    if not session_dir.is_dir():
        return []
    pending = _read_pending(session_dir)
    if not pending:
        return []
    try:
        _atomic_write_json(session_dir / _PENDING_NAME, [])
    except OSError as e:
        _warn(f"failed to clear pending list for {session_id}: {e}; keeping queued")
        return []
    return pending


def delete_session_uploads(session_id: str) -> None:
    """Remove the entire session upload directory; no-op if missing."""
    session_dir = safe_session_dir(session_id)
    shutil.rmtree(session_dir, ignore_errors=True)
