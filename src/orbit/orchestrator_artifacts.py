"""Artifact storage — per-agent ``.artifacts/`` dirs + JSON manifests.

An *artifact* is a rich, persistent deliverable Claude produces from its tmux
shell (chart / map / youtube / audio / video / image / interactive html /
downloadable file). The ``artifact`` CLI skill writes them; this module is the
dashboard-side reader/mutator + path resolver.

Layout (decision: artifacts are committable to the project's GitHub repo):
  - per-agent session under a PARA item → ``<cwd>/.artifacts/<id>.<ext>`` plus
    ``<id>.json`` manifest.
  - the **global** agent (no lib_id, cwd == HOME or absent) → never pollute
    ``$HOME``; fall back to ``~/.orchestrator/artifacts/global/``.

Per-session view = filter manifests by ``session_id``. Per-agent gallery = all
manifests in the agent's dir (keyed by lib_id). Manifest shape is owned jointly
with ``~/.claude/skills/artifacts/`` (the on-disk contract is the only coupling).

Reuses the ``_atomic_write_json`` + ``.resolve()``/``is_relative_to`` traversal
idioms from :mod:`orchestrator_uploads` (copied locally, like
:mod:`orchestrator_meta` does).
"""
from __future__ import annotations
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import orchestrator_meta as meta_mod
from .discovery import AREAS, HOME, PROJECTS

# Dir where per-agent artifacts live (committable into the project repo).
ARTIFACTS_DIRNAME = ".artifacts"
# Global agent has no project dir — keep its artifacts out of $HOME.
GLOBAL_ARTIFACTS_ROOT = HOME / ".orchestrator" / "artifacts" / "global"

# Matches the id the CLI mints: art-<utc compact>-<6 hex>. The compact UTC
# stamp is digits + 'T' (+ defensive 'Z'); reject anything else BEFORE any
# filesystem touch (defeats ``../`` / absolute-path traversal in id args).
ARTIFACT_ID_RE = re.compile(r"^art-[0-9TZ]+-[0-9a-f]{6}$")

ALLOWED_TYPES = frozenset(
    {"image", "audio", "video", "youtube", "chart", "map", "html", "file"}
)
# Pure-spec types store their payload INLINE in manifest["extra"] (src=None);
# the rest reference a sibling payload file via manifest["src"].
SPEC_TYPES = frozenset({"chart", "map", "youtube"})


def _warn(msg: str) -> None:
    print(f"[orchestrator_artifacts] {msg}", file=sys.stderr)


# ── id + manifest io ──────────────────────────────────────────────


def new_id() -> str:
    """Mint a fresh artifact id matching the CLI format."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"art-{stamp}-{os.urandom(3).hex()}"


def _validate_id(artifact_id: str) -> str:
    if not ARTIFACT_ID_RE.fullmatch(artifact_id or ""):
        raise ValueError("invalid artifact id")
    return artifact_id


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, ensure_ascii=False)
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


# ── CLI bridge: auth token + per-session env ──────────────────────

# The `artifact` CLI authenticates mutating + notify routes with this shared
# token (the box is single-user + Tailscale-gated, so a localhost-readable
# token is sufficient). GET file/list routes stay open so browser <img src>
# works without header plumbing.
ARTIFACT_TOKEN_PATH = HOME / ".orchestrator" / "artifact_token"
# Dashboard bind the CLI POSTs notifications to (matches the reminders skill).
NOTIFY_URL = "http://localhost:8766"


def ensure_token() -> str:
    """Create the shared CLI auth token (0600) if missing; return it."""
    try:
        if ARTIFACT_TOKEN_PATH.is_file():
            existing = ARTIFACT_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    ARTIFACT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(str(ARTIFACT_TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, tok.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(ARTIFACT_TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


def read_token() -> str | None:
    try:
        tok = ARTIFACT_TOKEN_PATH.read_text(encoding="utf-8").strip()
        return tok or None
    except OSError:
        return None


def session_env(session_id: str, lib_id: str | None) -> dict[str, str]:
    """Per-session env the runner injects into the tmux/subprocess environment.

    ``HD_*`` lets the ``artifact`` CLI auto-discover itself (it keys
    "global vs per-agent" on HD_LIB_ID and uses its own ``os.getcwd()`` for the
    ``.artifacts/`` location). ``ORCHESTRATOR_SESSION_ID`` / ``ORCHESTRATOR_UPLOADS_DIR``
    are the legacy vars the media skills (generate-image / generate-audio) read
    — the tmux runner historically set NEITHER, so interactive sessions couldn't
    generate images; mirror the programmatic runner so both paths work. (Those
    skills also fall back to the ``hd-<uuid>`` tmux session name, so they work
    even in a warm slot spawned before this env was added.)
    """
    uploads_dir = HOME / ".orchestrator" / "uploads" / (session_id or "")
    return {
        "HD_SESSION_ID": session_id or "",
        "HD_LIB_ID": lib_id or "",
        "HD_NOTIFY_URL": NOTIFY_URL,
        "HD_ARTIFACT_TOKEN_FILE": str(ARTIFACT_TOKEN_PATH),
        "ORCHESTRATOR_SESSION_ID": session_id or "",
        "ORCHESTRATOR_UPLOADS_DIR": str(uploads_dir),
    }


# ── dir resolution ────────────────────────────────────────────────


def _is_global(lib_id: str | None) -> bool:
    """True for the global agent — artifacts go to the central dir, not a repo.

    Mirrors the ``artifact`` CLI contract EXACTLY: a session is "per-agent"
    (writes into ``<cwd>/.artifacts/``) iff it carries a lib_id; everything
    else (legacy sessions with a cwd but no lib_id, and the literal global
    agent) is global. Keying on lib_id — not ``cwd != HOME`` — is what keeps
    the dashboard reading the same dir the CLI writes to.
    """
    return not (lib_id and str(lib_id).strip())


def _lib_id_to_cwd(lib_id: str | None) -> Path | None:
    """Map a ``projects/<x>`` / ``areas/<x>`` lib_id to its PARA dir, or None."""
    if not lib_id or "/" not in lib_id:
        return None
    kind, _, name = lib_id.partition("/")
    name = name.strip().strip("/")
    if not name or ".." in name:
        return None
    root = {"projects": PROJECTS, "areas": AREAS}.get(kind)
    if root is None:
        return None
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_dir():
        return None
    return candidate


def artifacts_dir(
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    lib_id: str | None = None,
) -> Path:
    """Resolve the artifacts dir for a session or an agent.

    When ``session_id`` is given, the sidecar's ``cwd``/``lib_id`` win unless an
    explicit override is passed. Global → ``~/.orchestrator/artifacts/global/``;
    otherwise ``<cwd>/.artifacts`` (cwd from the sidecar, falling back to the
    PARA dir derived from lib_id, then to the global root).
    """
    eff_cwd, eff_lib = cwd, lib_id
    if session_id:
        meta = meta_mod.get_meta(session_id)
        if eff_cwd is None:
            eff_cwd = meta.get("cwd")
        if eff_lib is None:
            eff_lib = meta.get("lib_id")

    if _is_global(eff_lib):
        return GLOBAL_ARTIFACTS_ROOT

    anchor: Path | None = None
    if eff_cwd and str(eff_cwd).strip():
        try:
            candidate = Path(eff_cwd).expanduser().resolve()
            if candidate.is_dir() and candidate.is_relative_to(HOME.resolve()):
                anchor = candidate
        except (OSError, RuntimeError):
            anchor = None
    if anchor is None:
        anchor = _lib_id_to_cwd(eff_lib)
    if anchor is None:
        # cwd deleted / lib_id unresolvable → don't crash, keep it out of $HOME.
        return GLOBAL_ARTIFACTS_ROOT
    return anchor / ARTIFACTS_DIRNAME


# ── reads ─────────────────────────────────────────────────────────


def _coerce_manifest(raw: object, artifact_id: str) -> dict | None:
    """Validate a loaded manifest dict; return a normalized copy or None."""
    if not isinstance(raw, dict):
        return None
    rid = raw.get("id")
    if not isinstance(rid, str) or rid != artifact_id or not ARTIFACT_ID_RE.fullmatch(rid):
        return None
    if raw.get("type") not in ALLOWED_TYPES:
        return None
    return raw


def get(dirp: Path, artifact_id: str) -> dict | None:
    """Read + validate one manifest by id (traversal-guarded)."""
    _validate_id(artifact_id)
    path = dirp / f"{artifact_id}.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return _coerce_manifest(raw, artifact_id)


def list_dir(dirp: Path) -> list[dict]:
    """All valid manifests in ``dirp``, newest-first by created_at."""
    if not dirp.is_dir():
        return []
    items: list[dict] = []
    try:
        scanner = os.scandir(dirp)
    except OSError as e:
        _warn(f"scandir failed for {dirp}: {e}")
        return []
    with scanner as it:
        for entry in it:
            if not entry.name.endswith(".json") or not entry.is_file():
                continue
            aid = entry.name[:-5]
            if not ARTIFACT_ID_RE.fullmatch(aid):
                continue
            m = get(dirp, aid)
            if m is not None:
                items.append(m)
    items.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    return items


def list_for_session(session_id: str) -> list[dict]:
    """Per-session view: manifests in the agent's dir tagged with this sid."""
    dirp = artifacts_dir(session_id=session_id)
    return [m for m in list_dir(dirp) if m.get("session_id") == session_id]


def list_for_agent(*, cwd: str | None = None, lib_id: str | None = None) -> list[dict]:
    """Per-agent gallery: every manifest under the agent's dir (keyed by lib_id)."""
    return list_dir(artifacts_dir(cwd=cwd, lib_id=lib_id))


def file_path(dirp: Path, manifest: dict) -> Path | None:
    """Resolve a manifest's payload file, refusing traversal. None for spec types."""
    src = manifest.get("src")
    if not isinstance(src, str) or not src:
        return None
    name = src.split("/")[-1].split("\\")[-1]
    if not name or name.startswith(".") or ".." in name:
        raise ValueError("invalid artifact src")
    target = (dirp / name).resolve()
    if not target.is_relative_to(dirp.resolve()):
        raise ValueError("artifact path escapes dir")
    return target if target.is_file() else None


# ── mutations (gallery actions) ───────────────────────────────────


def duplicate(dirp: Path, artifact_id: str) -> dict:
    """Copy an artifact (payload + manifest) under a fresh id; return the copy."""
    src_manifest = get(dirp, artifact_id)
    if src_manifest is None:
        raise FileNotFoundError(artifact_id)
    nid = new_id()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new_manifest = {**src_manifest, "id": nid, "created_at": now, "updated_at": now}
    title = src_manifest.get("title") or ""
    new_manifest["title"] = f"{title} (copy)".strip()
    src_file = file_path(dirp, src_manifest)
    if src_file is not None:
        ext = src_file.suffix
        dst_file = dirp / f"{nid}{ext}"
        shutil.copy2(src_file, dst_file)
        new_manifest["src"] = dst_file.name
    _atomic_write_json(dirp / f"{nid}.json", new_manifest)
    return new_manifest


def edit(dirp: Path, artifact_id: str, *, title: str | None = None,
         type: str | None = None) -> dict:
    """Patch manifest fields (None = unchanged); bump updated_at. Payload untouched."""
    manifest = get(dirp, artifact_id)
    if manifest is None:
        raise FileNotFoundError(artifact_id)
    updated = dict(manifest)
    if title is not None:
        updated["title"] = title
    if type is not None:
        if type not in ALLOWED_TYPES:
            raise ValueError("invalid artifact type")
        updated["type"] = type
    updated["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_json(dirp / f"{artifact_id}.json", updated)
    return updated


def delete(dirp: Path, artifact_id: str) -> bool:
    """Remove manifest + payload. Idempotent; returns True if a manifest existed."""
    manifest = get(dirp, artifact_id)
    if manifest is None:
        return False
    try:
        src_file = file_path(dirp, manifest)
    except ValueError:
        src_file = None
    if src_file is not None:
        try: src_file.unlink(missing_ok=True)
        except OSError as e: _warn(f"failed to unlink {src_file}: {e}")
    try:
        (dirp / f"{artifact_id}.json").unlink(missing_ok=True)
    except OSError as e:
        _warn(f"failed to unlink manifest {artifact_id}: {e}")
        return False
    return True
