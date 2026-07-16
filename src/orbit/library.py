"""Library — CRUD core for Areas/Projects with FastAPI route registration.

Filesystem of record (matches discovery.py conventions):
- Area  exists = ``~/Areas/<name>/``
- Project exists = ``~/Projects/<name>/`` or ``~/Projects/<group>/<name>/``
- Area↔Project link = symlink ``~/Areas/<area>/projects/<project>`` → project abs path
- Soft-delete = move to ``_archive/<unix-ts>__<name>/``
- Sidecar ``.library.json`` = optional ``{icon, color, created_iso, github, tags}``

All public ops raise:
- ``ValueError`` for bad input            (HTTP 400)
- ``FileExistsError`` for conflicts       (HTTP 409)
- ``FileNotFoundError`` for missing items (HTTP 404)
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile

from .discovery import AREAS, HOME, PROJECTS

AREAS_ARCHIVE = AREAS / "_archive"
PROJECTS_ARCHIVE = PROJECTS / "_archive"

SIDECAR_NAME = ".library.json"

# Names go into URLs (``/projects/<name>``) and onto the filesystem. Allowing
# spaces produced URLs like ``/projects/FIFA%20WC26%20Family%20Game`` and
# directory names that need quoting in every shell call — confirmed 2026-05-17
# when "FIFA WC26 Family Game" hit the dashboard's own paths.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._-]{0,63}$")


def _rel_to_home(p: Path) -> str:
    """Best-effort path-relative-to-HOME, tolerant of symlink-resolved targets.

    macOS resolves ``/var`` → ``/private/var`` which can break a naive
    ``Path.relative_to`` when ``p`` is resolved but ``HOME`` is not. We
    compare resolved roots before falling back to the raw absolute path.
    """
    try:
        return str(p.relative_to(HOME))
    except ValueError:
        try:
            return str(p.resolve().relative_to(HOME.resolve()))
        except ValueError:
            return str(p)


# ── path safety + validation ─────────────────────────────────────


def _validate_name(name: str) -> str:
    """Reject path traversal, leading dot/underscore, separators."""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = name.strip()
    if not name:
        raise ValueError("name required")
    if "/" in name or "\\" in name:
        raise ValueError("name cannot contain path separators")
    if name.startswith(".") or name.startswith("_"):
        raise ValueError("name cannot start with '.' or '_'")
    if name in ("..", "."):
        raise ValueError("invalid name")
    if not _NAME_RE.match(name):
        raise ValueError("name has invalid characters")
    return name


def _safe_area_path(name: str) -> Path:
    """Absolute path under ~/Areas/. Existence not asserted here."""
    name = _validate_name(name)
    p = (AREAS / name).resolve()
    root = AREAS.resolve()
    if p != root and root not in p.parents:
        raise ValueError("path escapes Areas root")
    return p


def _safe_project_path(rel: str) -> Path:
    """Absolute path under ~/Projects/. Accepts ``name`` or ``group/name``."""
    if not isinstance(rel, str):
        raise ValueError("project must be a string")
    rel = rel.strip().strip("/")
    if not rel:
        raise ValueError("project required")
    parts = rel.split("/")
    if len(parts) > 2:
        raise ValueError("project may be at most 'group/name'")
    for part in parts:
        _validate_name(part)
    p = (PROJECTS / Path(*parts)).resolve()
    root = PROJECTS.resolve()
    if p != root and root not in p.parents:
        raise ValueError("path escapes Projects root")
    return p


# ── sidecar ──────────────────────────────────────────────────────


def read_sidecar(item_path: Path) -> dict:
    """Read ``.library.json``; return ``{}`` if absent or malformed."""
    sidecar = item_path / SIDECAR_NAME
    if not sidecar.is_file():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_sidecar(item_path: Path, patch: dict) -> dict:
    """Merge ``patch`` into existing sidecar; atomic write; return merged dict."""
    if not isinstance(patch, dict):
        raise ValueError("sidecar patch must be an object")
    if not item_path.is_dir():
        raise FileNotFoundError(f"item not found: {item_path}")
    current = read_sidecar(item_path)
    merged = {**current, **patch}
    sidecar = item_path / SIDECAR_NAME
    fd, tmp = tempfile.mkstemp(prefix=".library.", suffix=".tmp", dir=str(item_path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, sidecar)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return merged


# ── create ───────────────────────────────────────────────────────


def create_area(name: str, description: str = "") -> dict:
    """mkdir + INDEX.md + projects/ resources/ notes/."""
    target = _safe_area_path(name)
    if target.exists():
        raise FileExistsError(f"area exists: {target.name}")
    AREAS.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    if description:
        (target / "INDEX.md").write_text(f"# {target.name}\n\n{description}\n", encoding="utf-8")
    (target / "projects").mkdir()
    (target / "resources").mkdir()
    (target / "notes").mkdir()
    write_sidecar(target, {"created_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True, "name": target.name, "rel_path": _rel_to_home(target)}


_DEFAULT_README_BODY = (
    "Scaffolded by the orbit. Replace this paragraph with a "
    "short summary of what this project does, then commit so the agent "
    "identity generator has real context to work from."
)


def _render_readme(name: str, description: str) -> str:
    """Generic README body, customized when the user supplied a description.

    Always emits a heading; uses the user's description when present and a
    placeholder otherwise so the initial commit isn't an empty file.
    """
    body = description.strip() if description else _DEFAULT_README_BODY
    return f"# {name}\n\n{body}\n"


def create_project(name: str, description: str = "", group: str | None = None) -> dict:
    """mkdir + README.md + .gitignore."""
    rel = f"{group}/{name}" if group else name
    target = _safe_project_path(rel)
    if target.exists():
        raise FileExistsError(f"project exists: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    (target / "README.md").write_text(_render_readme(target.name, description), encoding="utf-8")
    (target / ".gitignore").write_text(
        ".DS_Store\n*.swp\nnode_modules/\n.venv/\n__pycache__/\n", encoding="utf-8"
    )
    write_sidecar(target, {"created_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True, "name": target.name, "rel_path": _rel_to_home(target)}


# ── rename + relink ──────────────────────────────────────────────


def _relink_after_rename(old_abs: Path, new_abs: Path) -> int:
    """Walk ~/Areas/*/projects/* and update symlinks pointing at old_abs."""
    if not AREAS.is_dir():
        return 0
    updated = 0
    for area in AREAS.iterdir():
        if not area.is_dir() or area.name.startswith(("_", ".")):
            continue
        proj_dir = area / "projects"
        if not proj_dir.is_dir():
            continue
        for link in proj_dir.iterdir():
            if not link.is_symlink():
                continue
            try:
                target = Path(os.readlink(link))
                if not target.is_absolute():
                    target = (proj_dir / target).resolve()
                else:
                    target = target.resolve()
            except OSError:
                continue
            if target == old_abs.resolve():
                link.unlink()
                # Use the new project basename for the link name
                new_link = proj_dir / new_abs.name
                if not new_link.exists():
                    os.symlink(str(new_abs), str(new_link))
                    updated += 1
    return updated


def rename_item(kind: Literal["area", "project"], old: str, new: str) -> dict:
    """Path.rename + update incoming symlinks if kind=='project'."""
    if kind == "area":
        src = _safe_area_path(old)
        dst = _safe_area_path(new)
    elif kind == "project":
        src = _safe_project_path(old)
        # New name keeps the same group as the old (rename != move)
        old_parts = old.strip("/").split("/")
        if len(old_parts) == 2:
            new_rel = f"{old_parts[0]}/{_validate_name(new)}"
        else:
            new_rel = _validate_name(new)
        dst = _safe_project_path(new_rel)
    else:
        raise ValueError("kind must be 'area' or 'project'")

    if not src.exists() or src.is_symlink():
        raise FileNotFoundError(f"{kind} not found: {old}")
    if dst.exists():
        raise FileExistsError(f"target exists: {new}")

    src_resolved = src.resolve()
    src.rename(dst)
    if kind == "project":
        _relink_after_rename(src_resolved, dst.resolve())
    return {"ok": True, "name": dst.name}


# ── archive + restore ────────────────────────────────────────────


def _drop_dangling_symlinks(archived_path: Path) -> int:
    """Remove symlinks under ~/Areas/*/projects/* that point into archived_path."""
    if not AREAS.is_dir():
        return 0
    archived_resolved = archived_path
    dropped = 0
    for area in AREAS.iterdir():
        if not area.is_dir() or area.name.startswith(("_", ".")):
            continue
        proj_dir = area / "projects"
        if not proj_dir.is_dir():
            continue
        for link in proj_dir.iterdir():
            if not link.is_symlink():
                continue
            try:
                target = Path(os.readlink(link))
                if not target.is_absolute():
                    target = (proj_dir / target)
            except OSError:
                continue
            try:
                target_norm = target.resolve(strict=False)
            except Exception:
                target_norm = target
            if target_norm == archived_resolved or archived_resolved in target_norm.parents:
                link.unlink()
                dropped += 1
    return dropped


def archive_item(kind: Literal["area", "project"], name: str) -> dict:
    """Soft-delete via move to ``_archive/<ts>__<name>/``."""
    if kind == "area":
        src = _safe_area_path(name)
        archive_root = AREAS_ARCHIVE
    elif kind == "project":
        src = _safe_project_path(name)
        archive_root = PROJECTS_ARCHIVE
    else:
        raise ValueError("kind must be 'area' or 'project'")
    if not src.exists() or src.is_symlink():
        raise FileNotFoundError(f"{kind} not found: {name}")

    archive_root.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    archived_name = f"{ts}__{src.name}"
    dst = archive_root / archived_name
    src_resolved = src.resolve()
    src.rename(dst)

    if kind == "project":
        _drop_dangling_symlinks(src_resolved)
    return {"ok": True, "archived_to": _rel_to_home(dst)}


def _strip_ts_prefix(archived_name: str) -> str:
    m = re.match(r"^(\d+)__(.+)$", archived_name)
    if not m:
        raise ValueError(f"unrecognized archive entry: {archived_name}")
    return m.group(2)


def restore_item(kind: Literal["area", "project"], archived_name: str) -> dict:
    """Move archive entry back to ``~/Areas/`` or ``~/Projects/`` root."""
    archived_name = _validate_name(archived_name) if "__" not in archived_name else archived_name
    # archived names look like "1700000000__name"; need to allow digits + __ + name
    if not re.match(r"^[0-9]+__[A-Za-z0-9._-][A-Za-z0-9._ -]{0,63}$", archived_name):
        raise ValueError("invalid archived_name")
    if kind == "area":
        src = (AREAS_ARCHIVE / archived_name).resolve()
        archive_root = AREAS_ARCHIVE.resolve()
        target_root = AREAS
    elif kind == "project":
        src = (PROJECTS_ARCHIVE / archived_name).resolve()
        archive_root = PROJECTS_ARCHIVE.resolve()
        target_root = PROJECTS
    else:
        raise ValueError("kind must be 'area' or 'project'")
    if archive_root not in src.parents:
        raise ValueError("archive path escapes archive root")
    if not src.exists():
        raise FileNotFoundError(f"archive entry not found: {archived_name}")

    base = _strip_ts_prefix(src.name)
    dst = target_root / base
    if dst.exists():
        raise FileExistsError(f"destination exists: {base}")
    src.rename(dst)
    return {"ok": True, "name": base, "rel_path": _rel_to_home(dst)}


# ── link/unlink ──────────────────────────────────────────────────


def _ensure_gitignore_excludes(root: Path, *entries: str) -> None:
    """Append ``entries`` to ``root/.gitignore`` if ``root`` is a git repo
    and any entry is missing. Symlink folders (``projects/``, ``resources/``)
    are workspace-scoped views and shouldn't be tracked by the parent repo.
    """
    if not (root / ".git").is_dir():
        return
    gi = root / ".gitignore"
    existing: set[str] = set()
    body = ""
    if gi.is_file():
        try:
            body = gi.read_text(errors="replace")
        except Exception:
            body = ""
        existing = {line.strip() for line in body.splitlines() if line.strip()}
    missing = [e for e in entries if e not in existing]
    if not missing:
        return
    sep = "" if not body or body.endswith("\n") else "\n"
    block = "\n# library symlink folders (managed by dashboard)\n" + "\n".join(missing) + "\n"
    try:
        gi.write_text(body + sep + block)
    except Exception:
        pass


def link_project_to_area(area: str, project: str) -> dict:
    """``os.symlink(project_abs, area/projects/<basename>)``. Refuses dangling."""
    area_path = _safe_area_path(area)
    project_path = _safe_project_path(project)
    if not area_path.is_dir():
        raise FileNotFoundError(f"area not found: {area}")
    if not project_path.is_dir():
        raise FileNotFoundError(f"project not found: {project}")
    proj_dir = area_path / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    link = proj_dir / project_path.name
    if link.exists() or link.is_symlink():
        raise FileExistsError(f"link exists: {area}/{project_path.name}")
    os.symlink(str(project_path.resolve()), str(link))
    _ensure_gitignore_excludes(area_path, "projects/", "resources/")
    return {"ok": True, "area": area, "project": project_path.name}


def unlink_project_from_area(area: str, project: str) -> dict:
    """Only remove the entry if it's a symlink."""
    area_path = _safe_area_path(area)
    if not area_path.is_dir():
        raise FileNotFoundError(f"area not found: {area}")
    # `project` may be 'group/name' — only basename matters for the link
    basename = _validate_name(project.strip().strip("/").split("/")[-1])
    link = area_path / "projects" / basename
    if not link.is_symlink():
        raise FileNotFoundError(f"link not found: {area}/{basename}")
    link.unlink()
    return {"ok": True, "area": area, "project": basename}


# ── INDEX.md / README.md updates ────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^(---\n.*?\n---\n)", re.DOTALL)


def update_index_md(kind: Literal["area", "project"], name: str, description: str) -> dict:
    """Rewrite first paragraph in INDEX.md (areas) or README.md (projects).

    Preserves YAML frontmatter and the first heading.
    """
    if kind == "area":
        item = _safe_area_path(name)
        fname = "INDEX.md"
    elif kind == "project":
        item = _safe_project_path(name)
        fname = "README.md"
    else:
        raise ValueError("kind must be 'area' or 'project'")
    if not item.is_dir():
        raise FileNotFoundError(f"{kind} not found: {name}")

    md = item / fname
    description = description or ""
    if not md.is_file():
        md.write_text(f"# {item.name}\n\n{description}\n", encoding="utf-8")
        return {"ok": True, "rewrote": fname, "created": True}

    text = md.read_text(encoding="utf-8")
    fm = ""
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        fm = fm_match.group(1)
        body = text[len(fm):]
    else:
        body = text

    lines = body.splitlines()
    heading = ""
    rest_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            heading = line
            rest_idx = i + 1
            break

    # Skip the existing first paragraph; keep everything from the next blank line + 1
    j = rest_idx
    # skip leading blanks
    while j < len(lines) and not lines[j].strip():
        j += 1
    # skip the paragraph
    while j < len(lines) and lines[j].strip():
        j += 1
    tail = "\n".join(lines[j:]).lstrip("\n")

    new_body_parts = []
    if heading:
        new_body_parts.append(heading)
    else:
        new_body_parts.append(f"# {item.name}")
    new_body_parts.append("")
    new_body_parts.append(description)
    new_body = "\n".join(new_body_parts) + "\n"
    if tail:
        new_body += "\n" + tail
        if not new_body.endswith("\n"):
            new_body += "\n"

    md.write_text(fm + new_body, encoding="utf-8")
    return {"ok": True, "rewrote": fname, "created": False}


# ── FastAPI route registration ───────────────────────────────────


def _invalidate_cache(app: FastAPI) -> None:
    """Drop discovery cache so next /api/data re-runs discover_all.

    IMPORTANT: set the 'data' key to None — DO NOT pop it. app.py's
    _get_data() reads ``_cache["data"]`` directly (not ``.get``), so popping
    the key raises KeyError on the next request. The same dict is exposed
    as both ``app._cache`` and the module-level ``app_module._cache``.
    """
    cache = getattr(app, "_cache", None)
    if isinstance(cache, dict):
        cache["data"] = None
        cache["ts"] = 0.0


def _http_for(exc: Exception) -> HTTPException:
    """Map domain errors to HTTP."""
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(409, detail=str(exc))
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return HTTPException(404, detail=str(exc))
    return HTTPException(500, detail=str(exc))


async def _generate_identity_for_new_item(
    kind: Literal["areas", "projects"],
    name: str,
    group: str | None,
    item_path: Path,
    generator,
    logger,
) -> None:
    """Synchronously generate identity + icon for a freshly-created item.

    Best-effort: any exception is caught and logged. Sidecar `agent` block
    is updated either way — successful generation sets
    ``identity_generated_at`` to a wall-clock timestamp + persists the
    chosen icon; failure leaves ``identity_generated_at = None`` so the UI
    can show a "Generate" CTA on the agent panel.

    The 10-30s blocking is accepted by the plan (UI shows a spinner with
    "Tworzę agenta…").
    """
    single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
    # `lib_id` mirrors discovery's lib_id convention: equals `name` for areas
    # and top-level projects; `<group>/<name>` for nested projects. This
    # matches the `{name:path}` URL segment used by the prompts routes.
    if single_kind == "project" and group:
        lib_id = f"{group}/{name}"
    else:
        lib_id = name

    icon = ""
    ok = False
    error: str | None = None
    try:
        result = await generator.generate_identity(single_kind, lib_id, item_path)
        if isinstance(result, dict):
            ok = bool(result.get("ok"))
            icon = result.get("icon") or ""
            error = result.get("error")
        else:
            error = "generator returned non-dict"
    except Exception as e:  # never propagate — create succeeded already
        error = str(e)
        logger.warning(
            "identity generation failed for %s/%s: %s", kind, lib_id, e,
        )

    # Patch the sidecar agent block. Read-modify-write through write_sidecar
    # so we don't clobber other agent fields (model, skills_allowlist, etc).
    try:
        from . import library_files as _files_mod
        current = _files_mod._normalize_agent(read_sidecar(item_path).get("agent"))
        agent_patch = dict(current)
        agent_patch["identity_generated_at"] = time.time() if ok else None
        agent_patch["icon"] = icon if ok and icon else current.get("icon")
        write_sidecar(item_path, {"agent": agent_patch})
    except Exception as e:
        logger.warning(
            "failed to update agent sidecar after identity gen for %s/%s: %s",
            kind, lib_id, e,
        )

    if not ok and error:
        logger.warning(
            "identity generation reported error for %s/%s: %s",
            kind, lib_id, error,
        )


_GH_REPO_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _gh_repo_name_from(project_name: str) -> str:
    """Slugify a project dir name into a GitHub-legal repo name.

    GitHub forbids spaces and most punctuation in repo names. Collapse any
    illegal run into a single ``-`` and strip leading/trailing dashes. The
    project's filesystem name is unchanged — only the remote slug differs.
    Examples:
      "FIFA WC26 Family Game" → "FIFA-WC26-Family-Game"
      "my project!!"          → "my-project"
    """
    slug = _GH_REPO_NAME_RE.sub("-", project_name).strip("-")
    return slug or "project"


async def _create_github_repo_for_project(target: Path, visibility: str) -> dict:
    """Create a GitHub repo for ``target``, set as remote, push initial commit.

    Returns ``{ok, url?, error?}``. Never raises — every failure is captured
    and returned so the caller can keep the local project on disk and surface
    the message to the UI.

    Auth is the host-level ``gh`` CLI (same one used by tasks_github). The
    user must have run ``gh auth login`` on the box at some point.
    """
    from . import library_github as _gh_mod

    visibility_flag = "--public" if visibility == "public" else "--private"
    repo_name = _gh_repo_name_from(target.name)
    auth = _gh_mod.gh_auth_check()
    if not auth.get("ok"):
        return {"ok": False, "error": f"gh not authenticated: {auth.get('error') or 'unknown'}"}

    rc, out, err = await _gh_mod._gh_run(
        ["repo", "create", repo_name, visibility_flag, "--source", str(target), "--push"],
    )
    if rc != 0:
        msg = (err.decode("utf-8", errors="replace") or out.decode("utf-8", errors="replace")
               or f"gh repo create rc={rc}").strip()
        return {"ok": False, "error": msg[:500]}

    # `gh repo create --push` prints the new repo URL on stdout. Capture it
    # so the UI can link to the freshly-created repo.
    url = (out.decode("utf-8", errors="replace").strip().splitlines() or [""])[-1].strip()
    return {"ok": True, "url": url, "repo_name": repo_name, "visibility": visibility}


async def create_github_repo_for_item(target: Path, visibility: str) -> dict:
    """Create a GitHub repo for an EXISTING project/area dir + set it as origin.

    Ensures the dir is a git repo with at least one commit, then reuses
    :func:`_create_github_repo_for_project` (``gh repo create --source --push``).
    Returns ``{ok, url?, repo_name?, error?}`` — never raises. Used by the
    one-click "Create repo" CTA on the Issues tab (gated on ``issues.create_repo``).
    """
    from . import library_git as git_mod
    from . import library_github as gh_mod

    if not target.is_dir():
        return {"ok": False, "error": f"not a directory: {target}"}
    # Refuse if a GitHub origin already exists — avoids creating a duplicate.
    try:
        await gh_mod.gh_resolve_repo(target)
        return {"ok": False, "error": "this item already has a GitHub remote"}
    except ValueError:
        pass  # no parseable github origin yet — proceed

    try:
        git_mod.git_init(target)
    except Exception as e:  # noqa: BLE001 — surface, don't crash
        return {"ok": False, "error": f"git init failed: {e}"}
    # Initial commit. A no-op ("nothing to commit") when the repo already has
    # history — `--source --push` then just pushes the existing commits.
    commit_info = git_mod.commit_all(target, "Initial commit")
    # Guard the orphaned-repo case: if the commit failed AND the repo still has
    # no commit at all (empty dir, or git user.email unset), `gh repo create
    # --source --push` would create an EMPTY remote and then fail to push,
    # leaving a stray repo on GitHub. Refuse before touching GitHub — mirrors
    # the project-create flow, which skips GH create on a failed initial commit.
    if not commit_info.get("ok") and not git_mod.list_recent_commits(target, 1).get("commits"):
        return {
            "ok": False,
            "error": f"nothing to commit — add at least one file first "
                     f"({commit_info.get('error', 'initial commit failed')})",
        }
    return await _create_github_repo_for_project(target, visibility)


def register_routes(app: FastAPI) -> None:
    """Mount /api/library/* routes on the given FastAPI app."""
    from . import agent_identity_generator
    from . import agent_prompt_routes
    from . import library_files as files_mod
    from . import library_git_routes as git_routes
    from . import library_github as library_gh
    from . import library_github_routes as gh_routes
    from . import library_uploads as uploads_mod
    from . import cron_routes
    from . import settings_prompt_routes
    from . import skills_routes
    try:
        from . import secrets_routes
    except Exception as e:  # pragma: no cover — defensive soft-import
        secrets_routes = None  # type: ignore[assignment]
        import logging as _ext_logging
        _ext_logging.getLogger(__name__).warning(
            "secrets_routes import failed — /api/secrets/* disabled: %s", e,
        )
    try:
        from . import notify_routes
    except Exception as e:  # pragma: no cover — defensive soft-import
        notify_routes = None  # type: ignore[assignment]
        import logging as _ext_logging
        _ext_logging.getLogger(__name__).warning(
            "notify_routes import failed — /api/notify disabled: %s", e,
        )

    # One-shot gh auth probe (cached for process lifetime). Logged
    # best-effort; if not authenticated, /github/* endpoints still register
    # and will gracefully return 424 with the gh error message.
    import logging
    _logger = logging.getLogger(__name__)
    try:
        _ga = library_gh.gh_auth_check()
        if _ga.get("ok"):
            _logger.info("gh auth ok: user=%s scopes=%s", _ga.get("user"), _ga.get("scopes"))
        else:
            _logger.warning("gh auth NOT ok: %s", _ga.get("error"))
    except Exception as e:  # never block startup on this probe
        _logger.warning("gh auth probe failed: %s", e)

    # expose cache handle on app so siblings can invalidate
    if not hasattr(app, "_cache"):
        # If app.py hasn't attached its module-level `_cache` (it's module
        # private) we still want a hook for tests. The real cache lives on
        # the module; this is just a safe shim.
        app._cache = {"data": None, "ts": 0.0}  # type: ignore[attr-defined]

    def _drop_cache() -> None:
        # Always invalidate the app's cache plus the app module's cache
        from . import app as app_module
        if hasattr(app_module, "_cache") and isinstance(app_module._cache, dict):
            app_module._cache["data"] = None
            app_module._cache["ts"] = 0.0
        _invalidate_cache(app)

    # ── git ops + file mutations (Round 2) ──────────────────
    # Registered BEFORE the catch-all `DELETE /{kind}/{name:path}` archive
    # route below — FastAPI uses first-match, so the more specific
    # `DELETE /{kind}/{name:path}/file` must register first to avoid being
    # shadowed by the archive route.
    git_routes.register_git_and_file_routes(app, _drop_cache)

    # ── github (PRs + Issues) ───────────────────────────────
    # Registered BEFORE the catch-all `PATCH /{kind}/{name:path}` below for
    # the same first-match reason — `PATCH /{kind}/{name:path}/github/prs/{n}`
    # would otherwise be shadowed and silently no-op.
    gh_routes.register_github_routes(app)

    # ── create ──────────────────────────────────────────────

    from . import library_git as git_mod

    async def _create_handler(
        kind: Literal["areas", "projects"],
        name: str,
        description: str,
        mode: str,
        group: str | None,
        zip_file: UploadFile | None,
        git_url: str | None,
        github: bool = False,
        github_visibility: str = "private",
    ) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        if mode not in ("manual", "zip", "git_url"):
            raise HTTPException(400, detail="mode must be 'manual', 'zip' or 'git_url'")

        # ── git_url mode: clone first, then layer sidecar ───────
        if mode == "git_url":
            if not git_url:
                raise HTTPException(400, detail="git_url required for git_url mode")
            try:
                git_mod.validate_clone_url(git_url)
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e
            if not name:
                raise HTTPException(400, detail="name required for git_url mode")
            try:
                _validate_name(name)
            except ValueError as e:
                raise HTTPException(400, detail=str(e)) from e
            if kind == "areas":
                dest = _safe_area_path(name)
            else:
                dest = _safe_project_path(f"{group}/{name}" if group else name)
            if dest.exists():
                raise HTTPException(409, detail=f"{kind}/{name} already exists")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                clone_result = await git_mod.git_clone_async(git_url, dest)
            except ValueError as e:
                raise HTTPException(422, detail=f"clone failed: {e}") from e
            except FileExistsError as e:
                raise HTTPException(409, detail=str(e)) from e
            except FileNotFoundError as e:
                raise HTTPException(404, detail=str(e)) from e

            # Sidecar with optional GitHub owner/repo metadata
            try:
                gh_meta = git_mod.parse_github_owner_repo(git_url) or {}
                write_sidecar(dest, {
                    "created_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "github": gh_meta,
                    "source": "git_url",
                })
            except Exception:
                # Sidecar is best-effort; don't fail the clone if it errors.
                pass

            # Generate identity synchronously (10-30s blocking is acceptable
            # per plan section 6). Failure here doesn't fail the create.
            await _generate_identity_for_new_item(
                kind, name, group, dest, agent_identity_generator, _logger,
            )

            _drop_cache()
            return {
                "ok": True,
                "name": dest.name,
                "rel_path": _rel_to_home(dest),
                "branch": clone_result.get("branch"),
                "remote_url": clone_result.get("remote_url"),
            }

        # ── manual / zip modes ──────────────────────────────────
        try:
            if kind == "areas":
                result = create_area(name, description or "")
                target = _safe_area_path(name)
            else:
                result = create_project(name, description or "", group=group or None)
                target = _safe_project_path(f"{group}/{name}" if group else name)
        except Exception as e:
            raise _http_for(e) from e

        if mode == "zip":
            # Roll back the freshly-created skeleton if the upload is missing
            # or invalid — otherwise a partial empty item is left on disk and
            # the user hits 409 on retry, plus discovery surfaces a phantom.
            def _rollback() -> None:
                try:
                    if target.is_dir():
                        import shutil
                        shutil.rmtree(target, ignore_errors=True)
                except Exception:
                    pass
            if zip_file is None:
                _rollback()
                raise HTTPException(400, detail="zip mode requires a 'file' upload")
            data = await zip_file.read()
            if not data[:2] == b"PK":
                _rollback()
                raise HTTPException(400, detail="upload is not a zip file")
            try:
                uploads_mod.extract_starter_zip(data, target)
            except ValueError as e:
                _rollback()
                raise HTTPException(400, detail=str(e)) from e

        # Auto-init git for projects so discover_projects picks them up —
        # it requires a `.git/`, `pyproject.toml`, or `package.json` marker
        # at the project root, otherwise manually-created skeletons silently
        # vanish from the dashboard. Best-effort: a missing/broken git
        # binary must not fail the create.
        if kind == "projects":
            try:
                git_mod.git_init(target)
            except Exception:
                pass
            # Initial commit so the project has a real history root before
            # the user opens the agent / git panels. `commit_all` returns
            # ``{ok, error}``; both outcomes are silenced here because a
            # failure (missing user.email config, etc.) shouldn't fail the
            # whole create — discover_projects already picked it up.
            commit_info = git_mod.commit_all(target, "init project")
            result["initial_commit"] = commit_info

            # Optional GitHub repo creation. Triggered only when the form
            # explicitly opts in. Local create already succeeded; surface
            # any GH failure in the response so the UI can show a toast,
            # but never roll back the on-disk project — the user can
            # `gh repo create` manually from the project page later.
            if github and commit_info.get("ok"):
                result["github"] = await _create_github_repo_for_project(
                    target, github_visibility,
                )
            elif github and not commit_info.get("ok"):
                # Initial commit failed → push would also fail. Surface
                # the reason so the user knows why their checkbox didn't
                # produce a repo.
                result["github"] = {
                    "ok": False,
                    "error": f"skipped: initial commit failed ({commit_info.get('error', 'unknown')})",
                }

        # Manual / zip create succeeded. Generate identity synchronously
        # — best-effort, surfaced via sidecar timestamp regardless of outcome.
        await _generate_identity_for_new_item(
            kind, name, group, target, agent_identity_generator, _logger,
        )

        _drop_cache()
        return result

    @app.post("/api/library/areas")
    async def api_create_area(
        name: str = Form(...),
        description: str = Form(""),
        mode: str = Form("manual"),
        group: str | None = Form(None),
        git_url: str | None = Form(None),
        file: UploadFile | None = File(None),
    ) -> dict:
        return await _create_handler("areas", name, description, mode, group, file, git_url)

    @app.post("/api/library/projects")
    async def api_create_project(
        name: str = Form(...),
        description: str = Form(""),
        mode: str = Form("manual"),
        group: str | None = Form(None),
        git_url: str | None = Form(None),
        file: UploadFile | None = File(None),
        github: bool = Form(False),
        github_visibility: str = Form("private"),
    ) -> dict:
        return await _create_handler(
            "projects", name, description, mode, group, file, git_url,
            github=github, github_visibility=github_visibility,
        )

    # ── agent profile (read/write) ─────────────────────────
    # Registered BEFORE the catch-all PATCH /{kind}/{name:path} below — FastAPI
    # uses first-match order, so the more specific `.../agent` route must come
    # first or the catch-all swallows it (silently treating "agent" as a name).

    @app.get("/api/library/{kind}/{name:path}/agent")
    async def api_get_agent(kind: str, name: str) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            agent = files_mod.read_agent(single_kind, name)
        except Exception as e:
            raise _http_for(e) from e
        return {"ok": True, "agent": agent}

    @app.patch("/api/library/{kind}/{name:path}/agent")
    async def api_patch_agent(
        kind: str,
        name: str,
        payload: dict = Body(default={}),
    ) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        if not isinstance(payload, dict):
            raise HTTPException(400, detail="payload must be an object")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            agent = files_mod.write_agent(single_kind, name, payload)
        except Exception as e:
            raise _http_for(e) from e
        _drop_cache()
        return {"ok": True, "agent": agent}

    # ── per-agent prompt stack + global settings prompts ────
    # Both blocks register routes whose `{name:path}` URLs include extra
    # literal segments after `name` (e.g. `.../agent/prompts`). They MUST
    # come before the catch-all `PATCH /{kind}/{name:path}` below — same
    # first-match reason as the `/agent` routes above.
    agent_prompt_routes.register(app)
    settings_prompt_routes.register(app)
    # Skills register: GETs/POSTs/PATCH/DELETE under /api/skills/* + the
    # per-agent skills allowlist at /api/library/<kind>/<lib_id>/agent/skills
    # (also gated by the catch-all PATCH below — must register first).
    skills_routes.register(app)
    # Scheduler (cron jobs registry): /api/cron/* — must register before
    # the catch-all PATCH below for the same first-match reason.
    cron_routes.register(app)
    # Env & secrets manager (/api/secrets/*) — same first-match reason; the
    # routes are top-level (not under /api/library/...) so this is mostly
    # defensive ordering.
    if secrets_routes is not None:
        secrets_routes.register(app)
    if notify_routes is not None:
        notify_routes.register(app)

    # ── patch (rename / description / sidecar) ──────────────

    @app.patch("/api/library/{kind}/{name:path}")
    async def api_patch_item(kind: str, name: str, payload: dict = Body(default={})) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        out: dict = {"ok": True, "name": name}
        try:
            new_name = payload.get("new_name")
            description = payload.get("description")
            sidecar_patch = payload.get("sidecar_patch")
            single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"

            if new_name:
                renamed = rename_item(single_kind, name, new_name)
                out["name"] = renamed["name"]
                # subsequent ops act on the new name
                name = renamed["name"]

            if description is not None:
                update_index_md(single_kind, name, str(description))

            if isinstance(sidecar_patch, dict):
                target = _safe_area_path(name) if single_kind == "area" else _safe_project_path(name)
                write_sidecar(target, sidecar_patch)
        except Exception as e:
            raise _http_for(e) from e

        _drop_cache()
        return out

    # ── delete (archive) ────────────────────────────────────

    @app.delete("/api/library/{kind}/{name:path}")
    async def api_delete_item(kind: str, name: str) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            result = archive_item(single_kind, name)
        except Exception as e:
            raise _http_for(e) from e
        _drop_cache()
        return result

    # ── restore ────────────────────────────────────────────

    @app.get("/api/library/archive")
    async def api_list_archive() -> dict:
        """List soft-deleted areas + projects under each kind's _archive/ dir.

        Each entry: ``{archived_name, original_name, archived_at, kind}``.
        ``archived_name`` is the on-disk dir name (`<ts>__<orig>`); use it
        to restore via ``POST /api/library/{kind}s/{archived_name}/restore``.
        """
        out: dict[str, list[dict]] = {"areas": [], "projects": []}
        for label, root in (("areas", AREAS_ARCHIVE), ("projects", PROJECTS_ARCHIVE)):
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir(), reverse=True):
                if not entry.is_dir():
                    continue
                m = re.match(r"^(\d+)__(.+)$", entry.name)
                if not m:
                    continue
                ts = int(m.group(1))
                out[label].append({
                    "archived_name": entry.name,
                    "original_name": m.group(2),
                    "archived_at": ts,
                    "kind": label,
                })
        return {"ok": True, **out}

    @app.post("/api/library/{kind}/{name}/restore")
    async def api_restore_item(kind: str, name: str, payload: dict = Body(default={})) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        archived_name = payload.get("archived_name") or name
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            result = restore_item(single_kind, archived_name)
        except Exception as e:
            raise _http_for(e) from e
        _drop_cache()
        return result

    # ── links ───────────────────────────────────────────────

    @app.post("/api/library/links")
    async def api_link(payload: dict = Body(...)) -> dict:
        try:
            result = link_project_to_area(payload.get("area", ""), payload.get("project", ""))
        except Exception as e:
            raise _http_for(e) from e
        _drop_cache()
        return result

    @app.delete("/api/library/links")
    async def api_unlink(payload: dict = Body(...)) -> dict:
        try:
            result = unlink_project_from_area(payload.get("area", ""), payload.get("project", ""))
        except Exception as e:
            raise _http_for(e) from e
        _drop_cache()
        return result

    # ── tree / file / main ──────────────────────────────────

    @app.get("/api/library/{kind}/{name:path}/tree")
    async def api_tree(kind: str, name: str, rel: str = "") -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            return files_mod.list_tree(single_kind, name, rel)
        except Exception as e:
            raise _http_for(e) from e

    @app.get("/api/library/{kind}/{name:path}/file")
    async def api_file(kind: str, name: str, rel: str) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            return files_mod.read_file(single_kind, name, rel)
        except Exception as e:
            raise _http_for(e) from e

    @app.get("/api/library/{kind}/{name:path}/main")
    async def api_main(kind: str, name: str) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        try:
            return files_mod.read_main_files(single_kind, name)
        except Exception as e:
            raise _http_for(e) from e

    # ── write file (whitelist) ──────────────────────────────

    @app.put("/api/library/{kind}/{name:path}/file")
    async def api_write_file(kind: str, name: str, payload: dict = Body(...)) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        single_kind: Literal["area", "project"] = "area" if kind == "areas" else "project"
        rel = (payload.get("rel") or "").strip()
        content = payload.get("content")
        expected = payload.get("expected_sha256") or None
        if not isinstance(content, str):
            raise HTTPException(400, detail="content must be a string")
        try:
            result = files_mod.write_file(
                single_kind, name, rel, content, expected_sha256=expected,
            )
        except PermissionError as e:
            raise HTTPException(403, detail=str(e)) from e
        except FileExistsError as e:
            raise HTTPException(409, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        _drop_cache()
        return result

    # ── git status / checkout ───────────────────────────────

    @app.get("/api/library/{kind}/{name:path}/git")
    async def api_git_status(kind: str, name: str) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        try:
            item_path = (
                _safe_area_path(name) if kind == "areas" else _safe_project_path(name)
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        if not item_path.is_dir():
            raise HTTPException(404, detail=f"{kind}/{name} not found")
        # git_status chains ~5 blocking git subprocesses; off-load so opening a
        # project's git tab never freezes the loop.
        return await asyncio.to_thread(git_mod.git_status, item_path)

    @app.post("/api/library/{kind}/{name:path}/git/checkout")
    async def api_git_checkout(kind: str, name: str, payload: dict = Body(...)) -> dict:
        if kind not in ("areas", "projects"):
            raise HTTPException(400, detail="kind must be 'areas' or 'projects'")
        branch = (payload.get("branch") or "").strip()
        create = bool(payload.get("create", False))
        force = bool(payload.get("force", False))
        if not branch:
            raise HTTPException(400, detail="branch required")
        try:
            item_path = (
                _safe_area_path(name) if kind == "areas" else _safe_project_path(name)
            )
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e
        if not item_path.is_dir():
            raise HTTPException(404, detail=f"{kind}/{name} not found")
        try:
            return await asyncio.to_thread(
                git_mod.git_checkout, item_path, branch, create=create, force=force
            )
        except FileExistsError as e:
            raise HTTPException(409, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(400, detail=str(e)) from e

    # github routes already registered above (before catch-all PATCH).
