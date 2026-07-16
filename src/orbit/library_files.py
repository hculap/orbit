"""Library file browser — tree/file accessors with whitelisted writes.

Path safety: every operation is rooted at the area/project directory. Symlinks
are reported as ``type:"link"`` but never traversed (we don't list their
contents). The resolved path is asserted to live under the item's own root.

Writes are restricted to a small basename allowlist (INDEX.md, README.md,
AGENTS.md, CLAUDE.md, .gitignore) and use optimistic concurrency via SHA-256.
"""
from __future__ import annotations
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from .library import _safe_area_path, _safe_project_path, read_sidecar, write_sidecar

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".next", ".turbo"}

MAX_TEXT_BYTES = 1 * 1024 * 1024  # 1 MB cap on read_file / write_file
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB cap on file_for_download
NULL_SNIFF_BYTES = 4 * 1024  # first 4 KB sniffed for null bytes

WRITABLE_BASENAMES = frozenset({
    "INDEX.md", "README.md", "AGENTS.md", "CLAUDE.md", ".gitignore",
})

# Per-area / per-project agent profile defaults. Stored under sidecar key
# ``agent`` in ``.library.json``. Each field is optional:
#   - ``model`` — None means "inherit global default"; else one of
#     ``orchestrator_meta.ALLOWED_MODELS`` ({"opus", "sonnet", "haiku"}).
#   - ``icon`` — None or single emoji / grapheme cluster shown on /agents
#     cards + chat header. Mirrored to ``~/.orchestrator/agents/<kind>/<lib_id>/icon.txt``
#     by ``agent_prompts``; the sidecar carries it as a fast-path read.
#   - ``identity_generated_at`` — None or epoch float; set by the identity
#     generator after a successful run so the UI can hide / show "Generate
#     identity" CTA.
#   - ``skills_allowlist`` — DEPRECATED. Lazy-migrated to
#     ``~/.orchestrator/agents/<kind>/<lib_id>/skills_allowlist.json`` by
#     ``skills_per_agent.migrate_sidecar_skills_allowlist`` (called at app
#     boot). Read paths still tolerate this field for backwards compat; new
#     writes should target the per-agent allowlist file directly. The sidecar
#     value is set to ``null`` after migration so a re-run is a no-op.
#
# Legacy field ``system_prompt`` (free-form text up to 8 KB) is no longer in
# the default shape but is still tolerated on read: ``read_agent`` migrates
# any non-empty value to ``custom.md`` on the central per-agent store and
# clears the sidecar field. See ``_migrate_legacy_system_prompt``.
AGENT_DEFAULT: dict = {
    "model": None,
    "icon": None,
    "identity_generated_at": None,
    "skills_allowlist": None,
}

# Kept for the migration path + legacy write_agent calls that still pass a
# ``system_prompt`` field — those are persisted to the central ``custom.md``
# instead of the sidecar.
AGENT_PROMPT_MAX_BYTES = 8 * 1024  # 8 KB clamp on agent.system_prompt / custom.md
ICON_MAX_LEN = 32  # mirrors agent_prompts._ICON_MAX_LEN; ZWJ-tolerant

# Map ``read_agent``'s ``Literal["area", "project"]`` parameter to the plural
# bucket name used by ``agent_prompts`` / library on-disk paths. Resources is
# accepted by the prompt module but currently has no read_agent path here.
_KIND_TO_BUCKET: dict[str, str] = {"area": "areas", "project": "projects"}


def _normalize_agent(raw: object) -> dict:
    """Coerce arbitrary sidecar value into the canonical agent shape.

    Tolerates missing or malformed fields by falling back to ``AGENT_DEFAULT``;
    returns a fresh dict (no aliasing of inputs) so callers can mutate freely.
    Legacy ``system_prompt`` is preserved verbatim on the returned dict so the
    caller (``read_agent``) can detect + migrate it; new shapes never carry it.
    """
    if not isinstance(raw, dict):
        return dict(AGENT_DEFAULT)
    model = raw.get("model")
    if not (isinstance(model, str) and model.strip()):
        model = None
    else:
        # Lazy import to avoid an import cycle at module load. orchestrator_meta
        # imports nothing from library_files, so this is safe at call time.
        from . import orchestrator_meta as meta_mod
        cand = model.strip().lower()
        model = cand if cand in meta_mod.ALLOWED_MODELS else None
    icon = raw.get("icon")
    if not (isinstance(icon, str) and icon.strip()):
        icon = None
    else:
        cleaned = icon.strip()
        icon = cleaned if len(cleaned) <= ICON_MAX_LEN else None
    identity_at = raw.get("identity_generated_at")
    if isinstance(identity_at, bool) or not isinstance(identity_at, (int, float)):
        identity_at = None
    else:
        identity_at = float(identity_at)
    skills_allowlist = raw.get("skills_allowlist")
    if isinstance(skills_allowlist, list):
        skills_allowlist = [str(s) for s in skills_allowlist if isinstance(s, str)]
    else:
        skills_allowlist = None
    legacy_prompt = raw.get("system_prompt")
    if not (isinstance(legacy_prompt, str) and legacy_prompt.strip()):
        legacy_prompt = None
    return {
        "model": model,
        "icon": icon,
        "identity_generated_at": identity_at,
        "skills_allowlist": skills_allowlist,
        # Surfaced ONLY for the migration path in ``read_agent``. Stripped
        # from the public response by ``_strip_legacy`` before return.
        "_legacy_system_prompt": legacy_prompt,
    }


def _strip_legacy(state: dict) -> dict:
    """Drop the internal ``_legacy_system_prompt`` key before exposing the agent shape."""
    out = dict(state)
    out.pop("_legacy_system_prompt", None)
    return out


def _migrate_legacy_system_prompt(
    kind: Literal["area", "project"], name: str, legacy_text: str
) -> None:
    """Write legacy sidecar ``system_prompt`` to the central ``custom.md``.

    Called from ``read_agent`` on the first read after the per-agent prompt
    stack landed. Idempotent by construction: after the migration the sidecar
    no longer carries ``system_prompt`` (the next read sees no legacy field
    and skips this branch entirely).

    Errors are swallowed with a warning — we'd rather lose a copy of the old
    prompt than crash every read for an item with a corrupted sidecar.
    """
    bucket = _KIND_TO_BUCKET.get(kind)
    if bucket is None:
        return
    try:
        from . import agent_prompts as _ap
        custom_path = _ap.agent_custom_path(bucket, name)
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        # Don't clobber a custom.md the user might have already authored on
        # disk via the new path. Migration is best-effort — if the file is
        # already present, the new flow takes precedence.
        if not custom_path.exists():
            custom_path.write_text(legacy_text, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"[library_files] legacy system_prompt migration failed for {bucket}/{name}: {exc}")


def read_agent(kind: Literal["area", "project"], name: str) -> dict:
    """Return the agent profile for an item; defaults if sidecar absent.

    Migration: when the sidecar still carries a legacy non-empty
    ``system_prompt``, write it to ``~/.orchestrator/agents/<bucket>/<name>/custom.md``,
    drop the field from the sidecar via ``write_sidecar``, and continue with
    the migrated shape. Idempotent — next read sees no legacy field.
    """
    root = _item_root(kind, name)
    sidecar = read_sidecar(root)
    state = _normalize_agent(sidecar.get("agent"))

    legacy = state.get("_legacy_system_prompt")
    if isinstance(legacy, str) and legacy.strip():
        _migrate_legacy_system_prompt(kind, name, legacy)
        # Persist sidecar without the legacy field. ``_strip_legacy`` removes
        # the internal marker; the persisted shape becomes the new schema.
        try:
            write_sidecar(root, {"agent": _strip_legacy(state)})
        except Exception as exc:  # noqa: BLE001 — never break read on write fail
            print(f"[library_files] legacy migration sidecar write failed: {exc}")

    return _strip_legacy(state)


def write_agent(kind: Literal["area", "project"], name: str, patch: dict) -> dict:
    """Merge ``patch`` into the agent block of the sidecar; return new state.

    Validation is strict: every supplied field is checked before any disk
    write. Unknown keys raise ``ValueError`` so typos surface immediately.
    Pass ``None`` for a field to clear it (e.g. ``{"model": None}`` removes
    the per-agent override).

    Legacy ``system_prompt`` patches are routed to the central ``custom.md``
    instead of the sidecar — same migration target as ``read_agent``. New
    callers should target ``custom.md`` directly via ``agent_prompts``.
    """
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    allowed_keys = {"model", "icon", "identity_generated_at", "skills_allowlist", "system_prompt"}
    extra = set(patch.keys()) - allowed_keys
    if extra:
        raise ValueError(f"unknown agent fields: {sorted(extra)}")

    root = _item_root(kind, name)
    current = _strip_legacy(_normalize_agent(read_sidecar(root).get("agent")))
    new_state = dict(current)

    if "model" in patch:
        raw = patch["model"]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            new_state["model"] = None
        elif isinstance(raw, str):
            from . import orchestrator_meta as meta_mod
            cand = raw.strip().lower()
            if cand not in meta_mod.ALLOWED_MODELS:
                allowed = ", ".join(sorted(meta_mod.ALLOWED_MODELS))
                raise ValueError(f"model must be null or one of: {allowed}")
            new_state["model"] = cand
        else:
            raise ValueError("model must be a string or null")

    if "icon" in patch:
        raw = patch["icon"]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            new_state["icon"] = None
        elif isinstance(raw, str):
            cleaned = raw.strip()
            if len(cleaned) > ICON_MAX_LEN:
                raise ValueError(f"icon too long (>{ICON_MAX_LEN} chars)")
            new_state["icon"] = cleaned
        else:
            raise ValueError("icon must be a string or null")

    if "identity_generated_at" in patch:
        raw = patch["identity_generated_at"]
        if raw is None:
            new_state["identity_generated_at"] = None
        elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("identity_generated_at must be a number or null")
        else:
            new_state["identity_generated_at"] = float(raw)

    if "skills_allowlist" in patch:
        raw = patch["skills_allowlist"]
        if raw is None:
            new_state["skills_allowlist"] = None
        elif isinstance(raw, list):
            if not all(isinstance(s, str) for s in raw):
                raise ValueError("skills_allowlist entries must be strings")
            new_state["skills_allowlist"] = list(raw)
        else:
            raise ValueError("skills_allowlist must be a list of strings or null")

    # Legacy ``system_prompt`` still accepted by the route layer; we route it
    # to ``custom.md`` so callers don't have to know about the new path.
    if "system_prompt" in patch:
        raw = patch["system_prompt"]
        bucket = _KIND_TO_BUCKET.get(kind)
        if bucket is None:
            raise ValueError(f"unsupported kind for system_prompt migration: {kind}")
        from . import agent_prompts as _ap
        custom_path = _ap.agent_custom_path(bucket, name)
        if raw is None or (isinstance(raw, str) and not raw):
            try:
                custom_path.unlink()
            except FileNotFoundError:
                pass
        elif isinstance(raw, str):
            if len(raw.encode("utf-8")) > AGENT_PROMPT_MAX_BYTES:
                raise ValueError(
                    f"system_prompt too large (>{AGENT_PROMPT_MAX_BYTES} bytes)"
                )
            custom_path.parent.mkdir(parents=True, exist_ok=True)
            custom_path.write_text(raw, encoding="utf-8")
        else:
            raise ValueError("system_prompt must be a string or null")

    write_sidecar(root, {"agent": new_state})
    return new_state


def _item_root(kind: Literal["area", "project"], name: str) -> Path:
    if kind == "area":
        root = _safe_area_path(name)
    elif kind == "project":
        root = _safe_project_path(name)
    else:
        raise ValueError("kind must be 'area' or 'project'")
    if not root.is_dir():
        raise FileNotFoundError(f"{kind} not found: {name}")
    return root


def _safe_rel(root: Path, rel: str) -> Path:
    """Resolve ``rel`` against ``root``; refuse traversal."""
    rel = (rel or "").strip().lstrip("/")
    if rel == "":
        return root
    if ".." in rel.replace("\\", "/").split("/"):
        raise ValueError("rel cannot contain '..'")
    p = (root / rel).resolve()
    root_resolved = root.resolve()
    if p != root_resolved and root_resolved not in p.parents:
        raise ValueError("path escapes item root")
    return p


def _entry_type(entry: Path) -> str:
    if entry.is_symlink():
        return "link"
    if entry.is_dir():
        return "dir"
    if entry.is_file():
        return "file"
    return "other"


def list_tree(
    kind: Literal["area", "project"],
    name: str,
    rel: str = "",
    max_entries: int = 500,
) -> dict:
    """One-level listing. Excludes vcs/build dirs. Symlinks reported flat."""
    root = _item_root(kind, name)
    target = _safe_rel(root, rel)
    if not target.exists():
        raise FileNotFoundError(f"path not found: {rel}")
    if target.is_file():
        raise ValueError("rel points at a file; use /file endpoint")
    if not target.is_dir() and not target.is_symlink():
        raise ValueError("rel is not a directory")

    items: list[dict] = []
    count = 0
    try:
        entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        raise FileNotFoundError(f"cannot list {rel}: {e}") from e

    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and entry.name in EXCLUDE_DIRS:
            continue
        if count >= max_entries:
            break
        try:
            stat = entry.stat() if not entry.is_symlink() else entry.lstat()
        except OSError:
            continue
        item = {
            "name": entry.name,
            "type": _entry_type(entry),
            "size": stat.st_size if entry.is_file() and not entry.is_symlink() else None,
            "mtime": stat.st_mtime,
        }
        if entry.is_symlink():
            try:
                import os
                item["link_target"] = os.readlink(entry)
            except OSError:
                item["link_target"] = None
        items.append(item)
        count += 1

    return {
        "kind": kind,
        "name": name,
        "rel": rel.strip("/"),
        "items": items,
        "truncated": count >= max_entries,
    }


def _looks_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:NULL_SNIFF_BYTES]


def _sha256_of(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def read_file(
    kind: Literal["area", "project"],
    name: str,
    rel: str,
) -> dict:
    """Read a UTF-8 text file under the item. Refuses binary + >1MB."""
    if not rel:
        raise ValueError("rel required")
    root = _item_root(kind, name)
    target = _safe_rel(root, rel)
    if not target.exists():
        raise FileNotFoundError(f"file not found: {rel}")
    if target.is_dir():
        raise ValueError("rel points at a directory; use /tree endpoint")
    if target.is_symlink():
        raise ValueError("symlinks are not readable via this endpoint")
    if not target.is_file():
        raise ValueError("not a regular file")

    stat = target.stat()
    if stat.st_size > MAX_TEXT_BYTES:
        raise ValueError(f"file too large (>{MAX_TEXT_BYTES} bytes)")

    blob = target.read_bytes()
    if _looks_binary(blob):
        raise ValueError("binary file; refuse to read as text")

    try:
        content = blob.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"file is not valid UTF-8: {e}") from e

    return {
        "content": content,
        "sha256": _sha256_of(blob),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


def _read_optional(item_path: Path, fname: str) -> dict:
    """Return ``{exists, content, sha256, mtime}`` shape; ``content=None`` if absent."""
    target = item_path / fname
    if not target.is_file():
        return {"exists": False, "content": None, "sha256": None, "mtime": None}
    try:
        stat = target.stat()
        if stat.st_size > MAX_TEXT_BYTES:
            return {"exists": True, "content": None, "sha256": None, "mtime": stat.st_mtime,
                    "error": "too large"}
        blob = target.read_bytes()
        if _looks_binary(blob):
            return {"exists": True, "content": None, "sha256": None, "mtime": stat.st_mtime,
                    "error": "binary"}
        return {
            "exists": True,
            "content": blob.decode("utf-8", errors="replace"),
            "sha256": _sha256_of(blob),
            "mtime": stat.st_mtime,
        }
    except OSError as e:
        return {"exists": False, "content": None, "sha256": None, "mtime": None, "error": str(e)}


def write_file(
    kind: Literal["area", "project"],
    name: str,
    rel: str,
    content: str,
    expected_sha256: str | None = None,
) -> dict:
    """Write a UTF-8 text file under the item dir.

    Optimistic concurrency: callers pass the SHA they read; if the on-disk
    hash differs we raise ``FileExistsError`` (HTTP 409) so the caller can
    reload and reconcile manually.

    Hardening:
    - ``rel`` resolved via ``_safe_rel`` (no ``..``, no escapes).
    - ``Path(rel).name`` must be in ``WRITABLE_BASENAMES``.
    - ``content`` must be a string and ≤ 1 MB encoded.
    - Atomic write via ``tempfile.mkstemp`` + ``os.replace`` in the same dir.
    """
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if not rel:
        raise ValueError("rel required")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValueError(f"content too large (>{MAX_TEXT_BYTES} bytes)")

    root = _item_root(kind, name)
    target = _safe_rel(root, rel)
    if target == root:
        raise ValueError("rel cannot be empty / root")

    basename = Path(rel).name
    if basename not in WRITABLE_BASENAMES:
        raise PermissionError(
            f"basename not writable: {basename!r} (allowed: {sorted(WRITABLE_BASENAMES)})"
        )

    if target.exists():
        if target.is_dir():
            raise ValueError("target is a directory")
        if target.is_symlink():
            raise ValueError("refusing to write through a symlink")
        if expected_sha256:
            current = target.read_bytes()
            current_sha = _sha256_of(current)
            if current_sha != expected_sha256:
                raise FileExistsError(
                    f"sha256 mismatch (expected {expected_sha256[:12]}…, "
                    f"current {current_sha[:12]}…) — reload and retry"
                )
        # If file exists and no expected_sha256 supplied, allow blind overwrite.
    else:
        # Make sure parent exists and lives under the item root.
        parent = target.parent
        if not parent.is_dir():
            raise FileNotFoundError(f"parent dir missing: {parent}")

    parent = target.parent
    fd, tmp = tempfile.mkstemp(prefix=".write.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    stat = target.stat()
    return {
        "ok": True,
        "rel": rel.strip("/"),
        "sha256": _sha256_of(encoded),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def delete_file(kind: Literal["area", "project"], name: str, rel: str) -> dict:
    """Remove a regular file under the item dir.

    Refuses:
      - traversal (``rel`` must resolve under item dir)
      - any path containing a ``.git`` segment (no nuking ``.git/config`` etc.)
      - symlinks (don't follow + delete other people's stuff)
      - directories (only files; UI doesn't expose dir delete)

    Returns ``{ok: True, rel}``.
    """
    if not rel:
        raise ValueError("rel required")
    root = _item_root(kind, name)
    target = _safe_rel(root, rel)
    if target == root:
        raise ValueError("rel cannot be empty / root")

    rel_parts = target.relative_to(root).parts
    if any(seg == ".git" for seg in rel_parts):
        raise PermissionError(".git/ is read-only")

    # _safe_rel follows symlinks via .resolve() — check lstat on the literal
    # path for the symlink refusal so we never silently delete the link target.
    raw_target = root / rel.strip().lstrip("/")
    if raw_target.is_symlink():
        raise PermissionError("refusing to delete symlink")

    if not target.exists():
        raise FileNotFoundError(rel)
    if target.is_dir():
        raise ValueError("rel is a directory; only files supported")
    if not target.is_file():
        raise ValueError("not a regular file")
    target.unlink()
    return {"ok": True, "rel": rel.strip("/")}


def file_for_download(
    kind: Literal["area", "project"],
    name: str,
    rel: str,
) -> tuple[Path, str]:
    """Resolve a file path for streaming as an attachment download.

    Performs the same safety checks as :func:`read_file` (no ``.git/``,
    no traversal, no symlinks) plus a 50 MB size cap. Returns
    ``(absolute_path, basename)``; the caller is expected to wrap the
    path in :class:`fastapi.responses.FileResponse`.
    """
    if not rel:
        raise ValueError("rel required")
    root = _item_root(kind, name)
    target = _safe_rel(root, rel)
    if target == root:
        raise ValueError("rel cannot be empty / root")
    rel_parts = target.relative_to(root).parts
    if any(seg == ".git" for seg in rel_parts):
        raise PermissionError(".git/ is read-only")

    raw_target = root / rel.strip().lstrip("/")
    if raw_target.is_symlink():
        raise PermissionError("refusing to follow symlink")

    if not target.exists():
        raise FileNotFoundError(f"file not found: {rel}")
    if target.is_dir():
        raise ValueError("rel points at a directory; cannot download")
    if not target.is_file():
        raise ValueError("not a regular file")
    stat = target.stat()
    if stat.st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"file too large (>{MAX_DOWNLOAD_BYTES} bytes)")
    return target, target.name


def read_main_files(kind: Literal["area", "project"], name: str) -> dict:
    """Bundle the canonical 'context' files in one round trip.

    Areas: INDEX.md + AGENTS.md + CLAUDE.md + .gitignore
    Projects: README.md + AGENTS.md + CLAUDE.md + .gitignore
    """
    root = _item_root(kind, name)
    main_name = "INDEX.md" if kind == "area" else "README.md"
    return {
        "kind": kind,
        "name": name,
        "main_filename": main_name,
        "files": {
            main_name: _read_optional(root, main_name),
            "AGENTS.md": _read_optional(root, "AGENTS.md"),
            "CLAUDE.md": _read_optional(root, "CLAUDE.md"),
            ".gitignore": _read_optional(root, ".gitignore"),
        },
    }
