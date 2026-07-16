#!/usr/bin/env python3
"""
artifacts_lib.py — importable, stdlib-only API for the `artifact` CLI.

An "artifact" is a rich, persistent deliverable (chart / map / youtube embed /
image / audio / video / interactive HTML / downloadable file) saved next to the
project as a JSON manifest + optional payload. The orbit surfaces it
as a toast / modal + gallery.

This module is import-safe: importing it has NO side effects (no I/O, no env
reads at import time). All discovery happens lazily inside functions.

Config resolution for the dashboard notify URL (first match wins):
    1. $HD_NOTIFY_URL
    2. <skill_dir>/config.json  "dashboard_url"
    3. http://localhost:8766

Artifacts directory resolution (resolve_artifacts_dir) — keyed on HD_LIB_ID, NOT
the live cwd, so artifacts always land where the dashboard scans:
    - HD_LIB_ID set & resolvable → <PARA dir from lib_id>/.artifacts/
    - else (unresolvable / global agent) → ~/.orchestrator/artifacts/global/

Manifest shape is documented in SKILL.md / TOOL.md and enforced here.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent

ARTIFACT_TYPES = (
    "image", "audio", "video", "youtube", "chart", "map", "html", "file",
)
# Types whose spec is stored INLINE in manifest.extra (src is always null).
INLINE_TYPES = ("chart", "map", "youtube")
# Types backed by a copied / written payload file on disk.
FILE_TYPES = ("image", "audio", "video", "file")

CHART_TYPES = ("line", "bar", "pie", "doughnut", "scatter")

ID_RE = re.compile(r"^art-[0-9TZ]+-[0-9a-f]{6}$")
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

HTML_MAX_BYTES = 200_000
NOTIFY_TIMEOUT_S = 3

DEFAULT_DASHBOARD_URL = "http://localhost:8766"
NOTIFY_PATH = "/api/orchestrator/artifacts/notify"

# Default file extension per artifact type when none can be inferred.
_DEFAULT_EXT = {
    "image": "png",
    "audio": "mp3",
    "video": "mp4",
    "file": "bin",
    "html": "html",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ArtifactError(Exception):
    """Raised for any user-facing artifact failure (bad input, missing file…)."""


# ---------------------------------------------------------------------------
# Time / id helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with explicit offset."""
    return datetime.now(timezone.utc).isoformat()


def _utc_compact() -> str:
    """Current UTC time as a compact YYYYMMDDTHHMMSS stamp (no separators/zone)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def new_id() -> str:
    """Generate a fresh artifact id: art-<utc compact>-<6 lowercase hex>."""
    return f"art-{_utc_compact()}-{secrets.token_hex(3)}"


def validate_id(art_id: str) -> str:
    """Validate an artifact id against the strict regex before any FS op.

    Rejects path traversal (../), absolute paths and anything that is not the
    exact `art-<digits/T/Z>-<6 hex>` shape. Returns the id on success.
    """
    if not isinstance(art_id, str) or not ID_RE.match(art_id):
        raise ArtifactError(
            f"invalid artifact id {art_id!r}: must match {ID_RE.pattern}"
        )
    return art_id


# ---------------------------------------------------------------------------
# Config / env discovery
# ---------------------------------------------------------------------------


def _load_skill_config() -> dict[str, Any]:
    """Read <skill_dir>/config.json if present. Never raises — returns {}."""
    path = SKILL_DIR / "config.json"
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except (OSError, ValueError):
        pass
    return {}


def notify_url() -> str:
    """Dashboard base URL: HD_NOTIFY_URL → config.json → localhost:8766."""
    return (
        os.environ.get("HD_NOTIFY_URL")
        or _load_skill_config().get("dashboard_url")
        or DEFAULT_DASHBOARD_URL
    ).rstrip("/")


def _read_token() -> str | None:
    """Read the artifact auth token, if any.

    HD_ARTIFACT_TOKEN_FILE → ~/.orchestrator/artifact_token. Returns the file
    contents (stripped) or None when no token file exists / is readable.
    """
    candidates = [
        os.environ.get("HD_ARTIFACT_TOKEN_FILE"),
        str(Path.home() / ".orchestrator" / "artifact_token"),
    ]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand).expanduser()
        try:
            if path.is_file():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    return token
        except OSError:
            continue
    return None


def resolve_lib_id() -> str | None:
    """PARA library id for this agent. Empty / unset HD_LIB_ID → global (None)."""
    val = os.environ.get("HD_LIB_ID", "").strip()
    return val or None


def resolve_session_id() -> str:
    """Resolve the orchestrator session id.

    Order: HD_SESSION_ID → ORCHESTRATOR_SESSION_ID → tmux session name with the
    leading "hd-" stripped → "" (unknown). The tmux probe is best-effort and
    swallows any failure (no tmux, not in a session, binary missing).
    """
    for key in ("HD_SESSION_ID", "ORCHESTRATOR_SESSION_ID"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            name = result.stdout.strip()
            if name:
                return name[3:] if name.startswith("hd-") else name
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# PARA roots — mirror the dashboard's discovery.PROJECTS / discovery.AREAS so
# the CLI resolves to the SAME dir the dashboard scans.
_PARA_ROOTS = {
    "projects": Path.home() / "Projects",
    "areas": Path.home() / "Areas",
}
_GLOBAL_ARTIFACTS_ROOT = Path.home() / ".orchestrator" / "artifacts" / "global"


def _lib_id_to_para_dir(lib_id: str | None) -> Path | None:
    """Map a ``projects/<x>`` / ``areas/<x>`` lib_id to its PARA dir, or None.

    Mirrors the dashboard's ``orchestrator_artifacts._lib_id_to_cwd`` EXACTLY
    (traversal-guarded) so both sides agree on the location by construction.
    """
    if not lib_id or "/" not in lib_id:
        return None
    kind, _, name = lib_id.partition("/")
    name = name.strip().strip("/")
    if not name or ".." in name:
        return None
    root = _PARA_ROOTS.get(kind)
    if root is None:
        return None
    try:
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_dir():
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def _sessions_meta_path() -> Path:
    return Path.home() / ".orchestrator" / "sessions_meta.json"


def _recorded_cwd(session_id: str) -> Path | None:
    """The session's recorded cwd from the dashboard sidecar (read-only, best-effort).

    This is the FIRST source the dashboard's ``orchestrator_artifacts.artifacts_dir``
    trusts, so reading it here makes the CLI agree with the gallery by
    construction — even for non-PARA lib_ids (e.g. ``cron-run:<uuid>``). Never
    writes the sidecar (it's owned by the dashboard). Returns the resolved dir
    only when it's a real directory under ``$HOME``.
    """
    if not session_id:
        return None
    try:
        with open(_sessions_meta_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        sessions = data if isinstance(data, dict) else {}
    entry = sessions.get(session_id)
    if not isinstance(entry, dict):
        return None
    cwd = entry.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    try:
        candidate = Path(cwd).expanduser().resolve()
        if candidate.is_dir() and candidate.is_relative_to(Path.home().resolve()):
            return candidate
    except (OSError, RuntimeError):
        return None
    return None


def resolve_artifacts_dir() -> Path:
    """Resolve (and create) the directory artifacts live in.

    Resolves the SAME dir the dashboard scans (``orchestrator_artifacts.
    artifacts_dir``) — the session's *recorded* cwd, then the PARA dir derived
    from the lib_id. The process's *live* cwd is deliberately NOT used: keying on
    it was the bug (running the CLI from a scratchpad wrote ``<scratchpad>/
    .artifacts`` while the gallery scanned the agent's PARA dir), so artifacts
    vanished from the gallery.

    - Per-agent session (HD_LIB_ID set): ``<recorded cwd | PARA dir>/.artifacts/``
      — committable with the project, always visible in the gallery no matter
      where the agent has ``cd``'d to.
    - HD_LIB_ID unset (global agent), or nothing resolvable:
      ``~/.orchestrator/artifacts/global/``. A ``.artifacts/`` dir is NEVER
      created inside ``$HOME`` or a stray cwd.
    """
    lib_id = resolve_lib_id()
    if lib_id is None:
        target = _GLOBAL_ARTIFACTS_ROOT
    else:
        anchor = _recorded_cwd(resolve_session_id()) or _lib_id_to_para_dir(lib_id)
        target = (anchor / ".artifacts") if anchor is not None else _GLOBAL_ARTIFACTS_ROOT
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactError(f"could not create artifacts dir {target}: {exc}") from exc
    return target


# ---------------------------------------------------------------------------
# Manifest persistence (atomic)
# ---------------------------------------------------------------------------


def _manifest_path(art_dir: Path, art_id: str) -> Path:
    return art_dir / f"{validate_id(art_id)}.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically via a sibling tempfile + os.replace."""
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArtifactError(f"could not write {path}: {exc}") from exc


def write_manifest(art_dir: Path, manifest: dict[str, Any]) -> Path:
    """Serialise + atomically write a manifest to <art_dir>/<id>.json."""
    art_id = validate_id(manifest["id"])
    path = _manifest_path(art_dir, art_id)
    payload = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(path, payload)
    return path


def read_manifest(art_dir: Path, art_id: str) -> dict[str, Any]:
    """Read + parse a manifest. Raises ArtifactError if missing / malformed."""
    path = _manifest_path(art_dir, art_id)
    if not path.exists():
        raise ArtifactError(f"artifact {art_id} not found in {art_dir}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"could not read manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"manifest {path} is not a JSON object")
    return data


def list_manifests(art_dir: Path) -> list[dict[str, Any]]:
    """Return all valid manifests in the dir, sorted by created_at ascending."""
    out: list[dict[str, Any]] = []
    try:
        names = sorted(p.name for p in art_dir.glob("*.json"))
    except OSError:
        return out
    for name in names:
        art_id = name[:-len(".json")]
        if not ID_RE.match(art_id):
            continue
        try:
            out.append(read_manifest(art_dir, art_id))
        except ArtifactError:
            continue  # skip unreadable manifests rather than abort the listing
    out.sort(key=lambda m: m.get("created_at", ""))
    return out


# ---------------------------------------------------------------------------
# Spec parsing / validation
# ---------------------------------------------------------------------------


def _read_spec_text(
    inline: str | None,
    spec_file: str | None,
    use_stdin: bool,
) -> str:
    """Read raw spec text from exactly one of: --spec-file, positional, --stdin."""
    sources = [s for s in (spec_file, inline) if s is not None]
    if use_stdin and sources:
        raise ArtifactError("provide spec via --stdin OR a file/positional, not both")
    if use_stdin:
        return sys.stdin.read()
    if spec_file is not None:
        path = Path(spec_file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ArtifactError(f"could not read --spec-file {spec_file}: {exc}") from exc
    if inline is not None:
        # A positional spec arg may itself be a path to a file, or raw text.
        candidate = Path(inline).expanduser()
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            pass
        return inline
    raise ArtifactError("no spec provided (need --spec-file, a positional arg, or --stdin)")


def _parse_json_spec(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ArtifactError(f"spec is not valid JSON: {exc}") from exc


def validate_chart(spec: Any) -> dict[str, Any]:
    """Validate a Chart.js spec → normalised extra dict (src stays null)."""
    if not isinstance(spec, dict):
        raise ArtifactError("chart spec must be a JSON object")
    chart_type = spec.get("chart_type")
    if chart_type not in CHART_TYPES:
        raise ArtifactError(
            f"chart_type must be one of {CHART_TYPES} (got {chart_type!r})"
        )
    data = spec.get("data")
    if not isinstance(data, dict):
        raise ArtifactError("chart spec needs a 'data' object")
    datasets = data.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise ArtifactError("chart data.datasets must be a non-empty list")
    if len(datasets) > 10:
        raise ArtifactError(f"too many datasets ({len(datasets)} > 10)")
    labels = data.get("labels")
    if labels is not None and not isinstance(labels, list):
        raise ArtifactError("chart data.labels must be a list when present")
    options = spec.get("options", {})
    if options is not None and not isinstance(options, dict):
        raise ArtifactError("chart 'options' must be an object when present")
    return {
        "chart_type": chart_type,
        "data": data,
        "options": options or {},
    }


def validate_map(spec: Any) -> dict[str, Any]:
    """Validate a Leaflet map spec → normalised extra dict."""
    if not isinstance(spec, dict):
        raise ArtifactError("map spec must be a JSON object")
    center = spec.get("center")
    if (
        not isinstance(center, (list, tuple))
        or len(center) != 2
        or not all(isinstance(c, (int, float)) for c in center)
    ):
        raise ArtifactError("map 'center' must be [lat, lng] numbers")
    zoom = spec.get("zoom", 13)
    if not isinstance(zoom, int):
        raise ArtifactError("map 'zoom' must be an integer")
    markers = spec.get("markers", [])
    if not isinstance(markers, list):
        raise ArtifactError("map 'markers' must be a list")
    route = spec.get("route", [])
    if not isinstance(route, list):
        raise ArtifactError("map 'route' must be a list of [lat, lng] points")
    return {
        "center": [center[0], center[1]],
        "zoom": zoom,
        "markers": markers,
        "route": route,
    }


def _extract_youtube_id(raw: str) -> str:
    """Pull an 11-char video id out of a bare id or any youtube URL form."""
    raw = raw.strip()
    if YOUTUBE_ID_RE.match(raw):
        return raw
    patterns = (
        r"(?:youtube\.com/watch\?[^ ]*?\bv=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/(?:embed|shorts|v)/)([A-Za-z0-9_-]{11})",
    )
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    raise ArtifactError(f"could not extract an 11-char YouTube video id from {raw!r}")


def validate_youtube(spec: Any) -> dict[str, Any]:
    """Accept a {video_id,...} object, a bare 11-char id, or a youtube URL."""
    if isinstance(spec, dict):
        vid = spec.get("video_id")
        if not isinstance(vid, str):
            raise ArtifactError("youtube spec needs a string 'video_id'")
        vid = _extract_youtube_id(vid)
        start = spec.get("start")
        if start is not None and not isinstance(start, int):
            raise ArtifactError("youtube 'start' must be an integer (seconds) or null")
        return {"video_id": vid, "start": start}
    if isinstance(spec, str):
        return {"video_id": _extract_youtube_id(spec), "start": None}
    raise ArtifactError("youtube spec must be a JSON object, a bare id, or a URL")


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _ext_for_file(source: Path, mime: str | None, art_type: str) -> str:
    """Pick a file extension for a copied/written payload.

    Source suffix wins; then a guess from --mime; then a per-type default.
    """
    suffix = source.suffix.lstrip(".").lower()
    if suffix:
        return suffix
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed.lstrip(".").lower()
    return _DEFAULT_EXT.get(art_type, "bin")


def _copy_payload(
    art_dir: Path,
    art_id: str,
    source_path: str,
    mime: str | None,
    art_type: str,
) -> tuple[str, str | None, int]:
    """Copy a source file into <art_dir>/<id>.<ext>. Returns (src, mime, size)."""
    source = Path(source_path).expanduser()
    if not source.is_file():
        raise ArtifactError(f"source file not found: {source_path}")
    ext = _ext_for_file(source, mime, art_type)
    dest_name = f"{art_id}.{ext}"
    dest = art_dir / dest_name
    try:
        shutil.copy2(source, dest)  # COPY, never move
    except OSError as exc:
        raise ArtifactError(f"could not copy {source_path}: {exc}") from exc
    resolved_mime = mime or mimetypes.guess_type(str(source))[0]
    try:
        size = dest.stat().st_size
    except OSError:
        size = 0
    return dest_name, resolved_mime, size


def _write_html_payload(art_dir: Path, art_id: str, html_text: str) -> tuple[str, int]:
    """Write raw HTML to <art_dir>/<id>.html, enforcing the size cap."""
    encoded = html_text.encode("utf-8")
    if len(encoded) > HTML_MAX_BYTES:
        raise ArtifactError(
            f"html payload is {len(encoded)} bytes > {HTML_MAX_BYTES} max"
        )
    dest_name = f"{art_id}.html"
    _atomic_write_bytes(art_dir / dest_name, encoded)
    return dest_name, len(encoded)


def _payload_paths(art_dir: Path, manifest: dict[str, Any]) -> list[Path]:
    """All on-disk payload files referenced by a manifest (may be empty)."""
    src = manifest.get("src")
    return [art_dir / src] if src else []


# ---------------------------------------------------------------------------
# Notify (best-effort HTTP POST to the dashboard)
# ---------------------------------------------------------------------------


def notify(manifest: dict[str, Any], kind: str, session_id: str) -> bool:
    """POST an artifact event to the dashboard. Best-effort.

    Returns True on an HTTP 2xx, False on any connection error / timeout / non-2xx.
    Never raises — a missing dashboard must not break artifact creation.
    """
    body = {
        "session_id": session_id or None,
        "kind": kind,
        "artifact": manifest,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = _read_token()
    if token:
        headers["X-Artifact-Token"] = token

    req = urllib.request.Request(
        notify_url() + NOTIFY_PATH, data=data, method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def _base_manifest(art_id: str, art_type: str, title: str) -> dict[str, Any]:
    """Construct the common manifest skeleton (immutable per-call dict)."""
    now = _utc_now_iso()
    return {
        "id": art_id,
        "type": art_type,
        "title": title,
        "session_id": resolve_session_id() or None,
        "lib_id": resolve_lib_id(),
        "created_at": now,
        "updated_at": now,
        "src": None,
        "mime": None,
        "size": None,
        "extra": {},
    }


def create(
    art_type: str,
    title: str,
    *,
    file: str | None = None,
    spec_file: str | None = None,
    use_stdin: bool = False,
    mime: str | None = None,
    open_after: bool = False,
) -> dict[str, Any]:
    """Create an artifact: build the manifest + optional payload, then notify.

    Returns {"manifest": <dict>, "path": <str>, "pushed": <bool>}.
    """
    if art_type not in ARTIFACT_TYPES:
        raise ArtifactError(f"type must be one of {ARTIFACT_TYPES} (got {art_type!r})")
    if not title or not title.strip():
        raise ArtifactError("title is required and must be non-empty")

    art_dir = resolve_artifacts_dir()
    art_id = new_id()
    manifest = _base_manifest(art_id, art_type, title)

    if art_type in INLINE_TYPES:
        spec_text = _read_spec_text(file, spec_file, use_stdin)
        if art_type == "youtube":
            stripped = spec_text.strip()
            spec: Any = (
                _parse_json_spec(stripped)
                if stripped.startswith("{")
                else stripped
            )
            manifest["extra"] = validate_youtube(spec)
        elif art_type == "chart":
            manifest["extra"] = validate_chart(_parse_json_spec(spec_text))
        else:  # map
            manifest["extra"] = validate_map(_parse_json_spec(spec_text))
        # src stays null for inline spec types.

    elif art_type == "html":
        html_text = _read_spec_text(file, spec_file, use_stdin)
        dest_name, size = _write_html_payload(art_dir, art_id, html_text)
        manifest = {
            **manifest,
            "src": dest_name,
            "mime": "text/html",
            "size": size,
            "extra": {},
        }

    else:  # file-backed: image / audio / video / file
        if not file:
            raise ArtifactError(
                f"type {art_type!r} needs a source file (positional <file>)"
            )
        dest_name, resolved_mime, size = _copy_payload(
            art_dir, art_id, file, mime, art_type,
        )
        manifest = {
            **manifest,
            "src": dest_name,
            "mime": resolved_mime,
            "size": size,
            "extra": {},
        }

    write_manifest(art_dir, manifest)

    session_id = manifest.get("session_id") or ""
    pushed = notify(manifest, "created", session_id)
    if open_after:
        # An explicit open event pops the modal; ignore its own push result.
        notify(manifest, "open", session_id)

    return {"manifest": manifest, "path": str(_manifest_path(art_dir, art_id)), "pushed": pushed}


def open_artifact(art_id: str) -> dict[str, Any]:
    """Re-emit an 'open' event for an existing artifact (pops the modal)."""
    validate_id(art_id)
    art_dir = resolve_artifacts_dir()
    manifest = read_manifest(art_dir, art_id)
    session_id = manifest.get("session_id") or resolve_session_id() or ""
    pushed = notify(manifest, "open", session_id)
    return {"manifest": manifest, "pushed": pushed}


def list_artifacts(*, session_only: bool = False) -> list[dict[str, Any]]:
    """List manifests in the current dir. session_only filters by HD_SESSION_ID."""
    art_dir = resolve_artifacts_dir()
    manifests = list_manifests(art_dir)
    if session_only:
        sid = resolve_session_id()
        manifests = [m for m in manifests if (m.get("session_id") or "") == sid]
    return manifests


def duplicate(art_id: str) -> dict[str, Any]:
    """Duplicate an artifact: new id, copied payload, title + ' (copy)'."""
    validate_id(art_id)
    art_dir = resolve_artifacts_dir()
    src_manifest = read_manifest(art_dir, art_id)

    new_art_id = new_id()
    now = _utc_now_iso()
    dup_manifest: dict[str, Any] = {
        **src_manifest,
        "id": new_art_id,
        "title": f"{src_manifest.get('title', '')} (copy)",
        "created_at": now,
        "updated_at": now,
    }

    src_name = src_manifest.get("src")
    if src_name:
        old_payload = art_dir / src_name
        ext = Path(src_name).suffix.lstrip(".")
        new_name = f"{new_art_id}.{ext}" if ext else new_art_id
        if old_payload.is_file():
            try:
                shutil.copy2(old_payload, art_dir / new_name)
            except OSError as exc:
                raise ArtifactError(f"could not copy payload for dup: {exc}") from exc
            dup_manifest["src"] = new_name

    write_manifest(art_dir, dup_manifest)
    return {"manifest": dup_manifest}


def edit(
    art_id: str,
    *,
    title: str | None = None,
    art_type: str | None = None,
) -> dict[str, Any]:
    """Read-modify-write a manifest's title / type. Bumps updated_at."""
    validate_id(art_id)
    if art_type is not None and art_type not in ARTIFACT_TYPES:
        raise ArtifactError(f"type must be one of {ARTIFACT_TYPES} (got {art_type!r})")
    if title is None and art_type is None:
        raise ArtifactError("nothing to edit — pass --title and/or --type")

    art_dir = resolve_artifacts_dir()
    current = read_manifest(art_dir, art_id)
    updated: dict[str, Any] = {
        **current,
        "updated_at": _utc_now_iso(),
    }
    if title is not None:
        updated["title"] = title
    if art_type is not None:
        updated["type"] = art_type
    write_manifest(art_dir, updated)
    return {"manifest": updated}


def delete(art_id: str) -> dict[str, Any]:
    """Delete a manifest + its payload (missing_ok)."""
    validate_id(art_id)
    art_dir = resolve_artifacts_dir()
    manifest: dict[str, Any] = {}
    try:
        manifest = read_manifest(art_dir, art_id)
    except ArtifactError:
        pass  # manifest already gone — still try to clean a stray payload

    _manifest_path(art_dir, art_id).unlink(missing_ok=True)
    for payload in _payload_paths(art_dir, manifest):
        try:
            payload.unlink(missing_ok=True)
        except OSError:
            pass
    return {"id": art_id}
