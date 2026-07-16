"""Filesystem discovery for PARA structure + nginx app paths."""
from __future__ import annotations
import os
import socket
from collections import defaultdict
from pathlib import Path
from .nginx_parser import discover_apps

HOME = Path(os.environ.get("HOME", str(Path.home())))
PROJECTS = HOME / "Projects"
AREAS = HOME / "Areas"
RESOURCES = HOME / "Resources"


def _read_first_paragraph(p: Path, max_chars: int = 240) -> str:
    if not p.is_file():
        return ""
    try:
        text = p.read_text(errors="replace")
    except Exception:
        return ""
    out = []
    in_frontmatter = False
    for line in text.splitlines():
        s = line.strip()
        if s == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if s.startswith("#") or not s:
            if out:
                break
            continue
        out.append(s)
        if sum(len(x) for x in out) > max_chars:
            break
    return " ".join(out)[:max_chars]


def _stat_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _project_to_areas() -> dict[str, list[str]]:
    """Build inverse index: project_name → [area_names...] that link it.

    Skips dangling symlinks — ``Path.exists()`` follows the link and returns
    ``False`` for broken targets, which is exactly what we want here.
    """
    result: dict[str, list[str]] = defaultdict(list)
    if not AREAS.is_dir():
        return result
    for area in sorted(AREAS.iterdir()):
        if not area.is_dir() or area.name.startswith(("_", ".")):
            continue
        proj_dir = area / "projects"
        if not proj_dir.is_dir():
            continue
        for link in proj_dir.iterdir():
            if link.is_symlink() and link.exists():
                # Use the symlink NAME (which equals project name)
                result[link.name].append(area.name)
    return result


def _area_project_names(area: Path) -> list[str]:
    """Names of projects symlinked inside Areas/<area>/projects/.

    Skips dangling symlinks (``link.exists()`` returns False for broken links).
    """
    proj_dir = area / "projects"
    if not proj_dir.is_dir():
        return []
    return sorted(x.name for x in proj_dir.iterdir() if x.is_symlink() and x.exists())


def discover_areas() -> list[dict]:
    if not AREAS.is_dir():
        return []
    out = []
    for d in sorted(AREAS.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        index_md = d / "INDEX.md"
        projects_dir = d / "projects"
        resources_dir = d / "resources"
        notes_dir = d / "notes"
        out.append({
            "name": d.name,
            # Path under AREAS/ — used by frontend as the {name:path} URL
            # segment for library API calls. For areas this equals `name`
            # (areas are always single-level), but kept as a separate field
            # so the frontend code can be uniform between areas + projects.
            "lib_id": d.name,
            "label": d.name,
            "icon": "📂",
            "description": _read_first_paragraph(index_md),
            "linked_projects": _area_project_names(d),
            # Excludes dangling links — same filter as `_area_project_names`.
            "links_projects": sum(1 for x in projects_dir.iterdir() if x.is_symlink() and x.exists()) if projects_dir.is_dir() else 0,
            "links_resources": sum(1 for x in resources_dir.iterdir() if x.is_symlink()) if resources_dir.is_dir() else 0,
            "notes": sum(1 for x in notes_dir.iterdir() if x.is_file() and x.suffix == ".md") if notes_dir.is_dir() else 0,
            "mtime": _stat_mtime(d),
            # Areas can be git repos too (when cloned via mode=git_url). The
            # frontend uses these flags to decide whether to show the
            # Branches / PRs / Issues tabs without first round-tripping /git.
            "is_repo": (d / ".git").is_dir(),
            "has_github_remote": _has_github_remote(d),
        })
    return out


def discover_projects(exposed_paths: dict[str, dict]) -> list[dict]:
    """Walk ~/Projects/ 2 levels deep. Match against nginx-exposed paths."""
    if not PROJECTS.is_dir():
        return []
    backlinks = _project_to_areas()
    out = []
    for top in sorted(PROJECTS.iterdir()):
        if not top.is_dir() or top.name.startswith("."):
            continue
        is_proj = (
            (top / ".git").exists()
            or (top / "pyproject.toml").exists()
            or (top / "package.json").exists()
            # Dashboard-created sidecar: POST /api/library/projects writes
            # `.library.json` into a fresh directory before the user has had
            # a chance to `git init` or drop in a manifest. Without this,
            # the project the user just created via the UI doesn't appear
            # in the projects list — instead it's silently treated as a
            # "group" and recursed into.
            or (top / ".library.json").exists()
        )
        if is_proj:
            out.append(_project_info(top, exposed_paths, backlinks))
        else:
            for sub in sorted(top.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    out.append(_project_info(sub, exposed_paths, backlinks))
    return out


def _project_info(p: Path, exposed_paths: dict[str, dict], backlinks: dict[str, list[str]]) -> dict:
    name_lower = p.name.lower()
    exposed = None
    for path_key, app in exposed_paths.items():
        cfg_name = (app.get("name") or "").lower()
        if cfg_name == name_lower or path_key.strip("/").endswith(name_lower):
            exposed = app
            break
    return {
        "name": p.name,
        # Path under PROJECTS/ — used by the frontend as the {name:path} URL
        # segment for library API calls. For top-level projects equals
        # `name`; for nested projects (e.g. ~/Projects/my-app/sub-project)
        # equals `<group>/<name>`. Without this, the frontend was passing
        # the basename and the backend's `_safe_project_path` couldn't find
        # the directory ('projects/sub-project not found').
        "lib_id": str(p.relative_to(PROJECTS)),
        "label": p.name,
        "icon": "📦",
        "rel_path": str(p.relative_to(HOME)),
        "mtime": _stat_mtime(p),
        "exposed": exposed,
        "linked_areas": backlinks.get(p.name, []),
        # Cheap git/github hint at list time so the frontend can decide which
        # tabs to render in detail without fetching /git first. `is_repo`
        # checks for a `.git` dir; `has_github_remote` is a synchronous file
        # read of `.git/config` (no subprocess) looking for github.com.
        "is_repo": (p / ".git").is_dir(),
        "has_github_remote": _has_github_remote(p),
    }


def _has_github_remote(p: Path) -> bool:
    """Cheap sync check: does .git/config reference github.com?

    Avoids spawning `git remote get-url origin` for every list entry. Good
    enough for the 'show PRs/Issues tab?' hint — full resolution still
    happens at /git endpoint time.
    """
    cfg = p / ".git" / "config"
    if not cfg.is_file():
        return False
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "github.com" in text


def _count_files(d: Path) -> int:
    """Recursive count of non-dotfile regular files. Capped at 9999."""
    n = 0
    try:
        for p in d.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                n += 1
                if n >= 9999:
                    return 9999
    except Exception:
        pass
    return n


def discover_resources() -> list[dict]:
    if not RESOURCES.is_dir():
        return []
    out = []
    for d in sorted(RESOURCES.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        readme = d / "README.md"
        out.append({
            "name": d.name,
            "label": d.name,
            "icon": "📚",
            "description": _read_first_paragraph(readme),
            "files": _count_files(d),
            "mtime": _stat_mtime(d),
        })
    return out


def discover_system() -> dict:
    """Light system info — hostname, uptime."""
    info = {"hostname": socket.gethostname()}
    try:
        with open("/proc/uptime") as f:
            info["uptime_s"] = float(f.read().split()[0])
    except Exception:
        info["uptime_s"] = 0.0
    return info


def discover_host() -> dict:
    """Host metadata — name, IP placeholders. Domain/region come from override.yaml."""
    return {
        "name": socket.gethostname(),
        "domain": "",
        "region": "",
        "ip": "",
    }


def discover_all() -> dict:
    apps_list = discover_apps()
    apps_by_path = {a["path"]: a for a in apps_list}
    return {
        "areas": discover_areas(),
        "projects": discover_projects(apps_by_path),
        "resources": discover_resources(),
        "apps": apps_list,
        "system": discover_system(),
        "host": discover_host(),
    }
