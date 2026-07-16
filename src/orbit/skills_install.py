"""Skill installation paths — github / shorthand / zip / custom + update detection.

All install entry points end with a registry directory at
``~/.orchestrator/skills-registry/<name>/`` containing at minimum SKILL.md +
register.json. ``register.json`` shape is documented in the plan; key fields:

    source         : "github" | "github-shorthand" | "marketplace" | "zip"
                     | "custom" | "local"
    installed_at   : float (epoch)
    last_updated   : float (epoch)
    version        : str | null  (from frontmatter or plugin.json)
    git_origin     : str | null  (canonical remote URL when source=github)
    git_ref        : str | null  (branch / tag tracked when source=github)
    git_sha        : str | null  (currently checked-out commit sha)
    zip_hash       : str | null  (sha256 of uploaded ZIP)
    icon           : str | null  (single emoji from frontmatter)
    description    : str         (from frontmatter or repo description)
    plugin_root    : str | null  (sub-path within source repo for plugins)

Network ops (git clone / fetch) shell out to ``git`` so we don't pull a heavy
dep just for these flows.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from . import orchestrator_env as env_mod
from . import skills_registry as registry_mod

GITHUB_URL_RE: re.Pattern[str] = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

SHORTHAND_RE: re.Pattern[str] = re.compile(
    r"^(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:@(?P<ref>[A-Za-z0-9._/-]+))?$",
)

GIT_TIMEOUT_S: int = 120
GIT_FETCH_TIMEOUT_S: int = 60


# ── helpers ────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: int = GIT_TIMEOUT_S) -> str:
    """Run ``git <args>`` and return stdout; raise ``RuntimeError`` on non-zero."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git command timed out: {' '.join(args)}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _resolve_git_sha(repo_dir: Path) -> str | None:
    """Return the ``HEAD`` SHA for ``repo_dir`` or ``None`` on failure."""
    try:
        out = _run_git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=10)
    except RuntimeError:
        return None
    sha = out.strip()
    return sha or None


def _frontmatter_for(skill_md: Path) -> dict[str, Any]:
    """Parse SKILL.md frontmatter; return ``{}`` on failure."""
    try:
        parsed = registry_mod.parse_skill_md(skill_md)
    except (FileNotFoundError, RuntimeError):
        return {}
    fm = parsed.get("frontmatter") or {}
    return fm if isinstance(fm, dict) else {}


def _extract_metadata(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Pull description / version / icon from frontmatter; tolerate any shape."""
    description = frontmatter.get("description")
    version = frontmatter.get("version")
    icon = frontmatter.get("icon") or frontmatter.get("emoji")
    return {
        "description": str(description).strip() if isinstance(description, str) else "",
        "version": str(version).strip() if isinstance(version, str) and str(version).strip() else None,
        "icon": str(icon).strip() if isinstance(icon, str) and str(icon).strip() else None,
    }


def _ensure_no_overwrite(name: str) -> Path:
    """Resolve target dir + raise ``ValueError`` if a skill with that name exists."""
    target = registry_mod.skill_dir(name)
    if target.exists():
        raise ValueError(
            f"skill already installed: {name!r} — uninstall it first or use a different name"
        )
    return target


def _name_from_frontmatter_or(fallback: str, frontmatter: dict[str, Any]) -> str:
    """Prefer ``frontmatter['name']`` when valid; else use ``fallback``.

    Both candidates are passed through ``registry_mod.safe_skill_name`` so an
    invalid frontmatter name surfaces as a ``ValueError`` to the caller rather
    than silently being replaced.
    """
    fm_name = frontmatter.get("name")
    if isinstance(fm_name, str) and fm_name.strip():
        return registry_mod.safe_skill_name(fm_name)
    return registry_mod.safe_skill_name(fallback)


def _persist_skill(
    name: str,
    source_path: Path,
    register_payload: dict[str, Any],
    *,
    move: bool = False,
) -> dict[str, Any]:
    """Move/copy ``source_path`` → registry/<name>/ then write register.json.

    ``move=True`` is used by the github flow (we own the temp clone), ``False``
    by zip / custom (caller may want the source preserved).
    """
    target = _ensure_no_overwrite(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(source_path), str(target))
    else:
        shutil.copytree(str(source_path), str(target), symlinks=True)

    skill_md = target / registry_mod.SKILL_MD_FILENAME
    if not skill_md.is_file():
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError(f"installed skill {name!r} has no SKILL.md after persist")

    now = _now()
    final_payload = {
        **register_payload,
        "installed_at": now,
        "last_updated": now,
    }
    registry_mod.write_register_json(name, final_payload)
    return final_payload


# ── github / shorthand ─────────────────────────────────────────────


def _parse_github_url(url: str) -> tuple[str, str]:
    """Split ``https://github.com/owner/repo`` → ``("owner", "repo")``.

    ``.git`` suffix and trailing slash are tolerated. Raises ``ValueError`` on
    anything that doesn't match the expected shape (including non-github hosts).
    """
    if not isinstance(url, str):
        raise ValueError("github url must be a string")
    match = GITHUB_URL_RE.match(url.strip())
    if match is None:
        raise ValueError(f"not a github URL: {url!r}")
    return match.group("owner"), match.group("repo")


def _clone_github(url: str, ref: str, dest: Path) -> str:
    """Shallow-clone ``url`` into ``dest``; checkout ``ref`` if not the default.

    Returns the resolved commit SHA. Caller owns ``dest`` and must remove it
    on failure.
    """
    _run_git(["clone", "--depth", "1", "--branch", ref, url, str(dest)])
    sha = _resolve_git_sha(dest)
    if sha is None:
        raise RuntimeError(f"could not resolve HEAD sha after clone of {url}@{ref}")
    return sha


def _detect_install_kind(repo_dir: Path) -> dict[str, Any]:
    """Inspect a cloned repo and return ``{kind, ...}`` discriminating the install flow.

    Returns:
      - ``{"kind": "single", "name_hint": str}`` — root-level SKILL.md
      - ``{"kind": "plugin", "plugin_root": ".", "name_hint": str}``
        — ``.claude-plugin/plugin.json`` at root + ``skills/`` folder
      - ``{"kind": "marketplace", "plugins": list[dict]}``
        — ``.claude-plugin/marketplace.json`` lists multiple plugins
      - ``{"kind": "skills-collection", "skills": list[dict]}``
        — top-level ``skills/`` folder, each subdir with SKILL.md
      - raises ``ValueError`` when no recognisable layout is present
    """
    marketplace = repo_dir / ".claude-plugin" / "marketplace.json"
    plugin = repo_dir / ".claude-plugin" / "plugin.json"
    root_skill = repo_dir / registry_mod.SKILL_MD_FILENAME
    skills_dir = repo_dir / "skills"

    if marketplace.is_file():
        return {"kind": "marketplace", "marketplace_path": marketplace}
    if plugin.is_file():
        return {"kind": "plugin", "plugin_path": plugin, "plugin_root": "."}
    if root_skill.is_file():
        return {"kind": "single", "skill_md": root_skill}
    if skills_dir.is_dir() and any(
        (sub / registry_mod.SKILL_MD_FILENAME).is_file()
        for sub in skills_dir.iterdir()
        if sub.is_dir()
    ):
        return {"kind": "skills-collection", "skills_dir": skills_dir}
    raise ValueError(
        "could not detect skill layout (no SKILL.md, plugin.json, marketplace.json, or skills/ dir)"
    )


def _install_single_skill_from_clone(
    *,
    repo_dir: Path,
    skill_md: Path,
    url: str,
    ref: str,
    git_sha: str,
    name_override: str | None,
) -> dict[str, Any]:
    """Persist a single-SKILL.md repo as one registry entry."""
    frontmatter = _frontmatter_for(skill_md)
    meta = _extract_metadata(frontmatter)
    fallback = (name_override or _name_from_url(url)).strip()
    name = _name_from_frontmatter_or(fallback, frontmatter)

    payload = registry_mod.default_register_dict(
        source="github",
        description=meta["description"],
        icon=meta["icon"],
        version=meta["version"],
        git_origin=url,
        git_ref=ref,
        git_sha=git_sha,
    )
    return {"name": name, **_persist_skill(name, repo_dir, payload, move=True)}


def _install_plugin_from_clone(
    *,
    repo_dir: Path,
    plugin_path: Path,
    url: str,
    ref: str,
    git_sha: str,
    name_override: str | None,
) -> list[dict[str, Any]]:
    """Persist every SKILL.md under ``plugin/skills/<name>/`` as separate entries."""
    skills_dir = repo_dir / "skills"
    if not skills_dir.is_dir():
        # Plugin without skills/ — treat the plugin itself as a single skill if
        # SKILL.md exists at root, else error.
        root_skill = repo_dir / registry_mod.SKILL_MD_FILENAME
        if root_skill.is_file():
            return [
                _install_single_skill_from_clone(
                    repo_dir=repo_dir,
                    skill_md=root_skill,
                    url=url,
                    ref=ref,
                    git_sha=git_sha,
                    name_override=name_override,
                )
            ]
        raise ValueError("plugin.json found but no skills/ directory and no root SKILL.md")

    return _install_skills_collection(
        skills_dir=skills_dir,
        url=url,
        ref=ref,
        git_sha=git_sha,
        plugin_root=str(plugin_path.parent.relative_to(repo_dir)),
    )


def _install_skills_collection(
    *,
    skills_dir: Path,
    url: str,
    ref: str,
    git_sha: str,
    plugin_root: str | None,
) -> list[dict[str, Any]]:
    """Iterate ``skills_dir/*/SKILL.md`` and persist each as its own entry."""
    inserted: list[dict[str, Any]] = []
    for sub in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        skill_md = sub / registry_mod.SKILL_MD_FILENAME
        if not skill_md.is_file():
            continue
        frontmatter = _frontmatter_for(skill_md)
        meta = _extract_metadata(frontmatter)
        try:
            name = _name_from_frontmatter_or(sub.name, frontmatter)
        except ValueError as exc:
            print(f"[skills_install] skipping {sub.name}: {exc}")
            continue
        try:
            payload = registry_mod.default_register_dict(
                source="github",
                description=meta["description"],
                icon=meta["icon"],
                version=meta["version"],
                git_origin=url,
                git_ref=ref,
                git_sha=git_sha,
                plugin_root=plugin_root,
            )
            persisted = _persist_skill(name, sub, payload, move=False)
        except ValueError as exc:
            print(f"[skills_install] skipping {name}: {exc}")
            continue
        inserted.append({"name": name, **persisted})
    if not inserted:
        raise ValueError("no installable skills found in skills/ collection")
    return inserted


def _install_marketplace_from_clone(
    *,
    repo_dir: Path,
    marketplace_path: Path,
    url: str,
    ref: str,
    git_sha: str,
) -> list[dict[str, Any]]:
    """Walk a marketplace.json plugin list; install each plugin's skills."""
    import json as _json

    try:
        manifest = _json.loads(marketplace_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"could not parse marketplace.json: {exc}") from exc

    plugins = manifest.get("plugins") if isinstance(manifest, dict) else None
    if not isinstance(plugins, list) or not plugins:
        raise ValueError("marketplace.json missing 'plugins' list")

    inserted: list[dict[str, Any]] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path") or entry.get("source") or entry.get("location")
        if not isinstance(rel, str) or not rel.strip():
            continue
        plugin_root_dir = (repo_dir / rel).resolve()
        try:
            plugin_root_dir.relative_to(repo_dir.resolve())
        except ValueError:
            print(f"[skills_install] marketplace entry escapes repo: {rel}")
            continue
        skills_dir = plugin_root_dir / "skills"
        if not skills_dir.is_dir():
            root_skill = plugin_root_dir / registry_mod.SKILL_MD_FILENAME
            if root_skill.is_file():
                frontmatter = _frontmatter_for(root_skill)
                meta = _extract_metadata(frontmatter)
                try:
                    name = _name_from_frontmatter_or(plugin_root_dir.name, frontmatter)
                    payload = registry_mod.default_register_dict(
                        source="marketplace",
                        description=meta["description"],
                        icon=meta["icon"],
                        version=meta["version"],
                        git_origin=url,
                        git_ref=ref,
                        git_sha=git_sha,
                        plugin_root=rel,
                    )
                    persisted = _persist_skill(name, plugin_root_dir, payload, move=False)
                    inserted.append({"name": name, **persisted})
                except ValueError as exc:
                    print(f"[skills_install] marketplace plugin skip {rel}: {exc}")
            continue
        try:
            inserted.extend(
                _install_skills_collection(
                    skills_dir=skills_dir,
                    url=url,
                    ref=ref,
                    git_sha=git_sha,
                    plugin_root=rel,
                )
            )
        except ValueError as exc:
            print(f"[skills_install] marketplace plugin skip {rel}: {exc}")
    if not inserted:
        raise ValueError("marketplace produced zero installable skills")
    return inserted


def _name_from_url(url: str) -> str:
    """Derive a fallback skill name from a github URL's repo segment."""
    _, repo = _parse_github_url(url)
    return repo.lower()


def install_from_github(
    url: str,
    ref: str = "main",
    *,
    name_override: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Clone ``url@ref`` into the registry; auto-detect plugin / marketplace layout.

    Returns the new ``register.json`` payload (with ``name`` injected) for
    single-skill installs, or a ``list[dict]`` for plugin / marketplace installs
    that produced multiple registry entries.
    """
    _parse_github_url(url)  # validate shape early
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty string")
    ref = ref.strip()

    with tempfile.TemporaryDirectory(prefix="hd-skill-") as tmp:
        tmp_path = Path(tmp)
        clone_dir = tmp_path / "src"
        try:
            git_sha = _clone_github(url, ref, clone_dir)
        except RuntimeError as exc:
            raise ValueError(f"git clone failed: {exc}") from exc

        layout = _detect_install_kind(clone_dir)
        kind = layout["kind"]

        if kind == "single":
            return _install_single_skill_from_clone(
                repo_dir=clone_dir,
                skill_md=layout["skill_md"],
                url=url,
                ref=ref,
                git_sha=git_sha,
                name_override=name_override,
            )
        if kind == "plugin":
            return _install_plugin_from_clone(
                repo_dir=clone_dir,
                plugin_path=layout["plugin_path"],
                url=url,
                ref=ref,
                git_sha=git_sha,
                name_override=name_override,
            )
        if kind == "marketplace":
            return _install_marketplace_from_clone(
                repo_dir=clone_dir,
                marketplace_path=layout["marketplace_path"],
                url=url,
                ref=ref,
                git_sha=git_sha,
            )
        if kind == "skills-collection":
            return _install_skills_collection(
                skills_dir=layout["skills_dir"],
                url=url,
                ref=ref,
                git_sha=git_sha,
                plugin_root=None,
            )
        raise ValueError(f"unknown layout kind: {kind!r}")


def install_from_shorthand(repo: str, ref: str = "main") -> dict[str, Any] | list[dict[str, Any]]:
    """Resolve ``user/name`` (or ``user/name@ref``) → GH URL → ``install_from_github``."""
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo shorthand required")
    match = SHORTHAND_RE.match(repo.strip())
    if match is None:
        raise ValueError(f"invalid shorthand: {repo!r} (expected 'user/repo' or 'user/repo@ref')")
    owner = match.group("owner")
    name = match.group("repo")
    parsed_ref = match.group("ref") or ref
    url = f"https://github.com/{owner}/{name}"
    return install_from_github(url, parsed_ref)


# ── zip ────────────────────────────────────────────────────────────


def install_from_zip(file_bytes: bytes, name_hint: str | None = None) -> dict[str, Any]:
    """Extract a zip into the registry; require SKILL.md anywhere reasonable."""
    if not isinstance(file_bytes, (bytes, bytearray)):
        raise ValueError("file_bytes must be bytes")
    if not file_bytes:
        raise ValueError("zip is empty")
    sha = hashlib.sha256(file_bytes).hexdigest()
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid zip file: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="hd-skill-zip-") as tmp:
        tmp_path = Path(tmp)
        try:
            zf.extractall(tmp_path)
        except (RuntimeError, OSError) as exc:
            raise ValueError(f"failed to extract zip: {exc}") from exc
        finally:
            zf.close()

        skill_md = _find_skill_md(tmp_path)
        if skill_md is None:
            raise ValueError("zip does not contain a SKILL.md anywhere")

        skill_root = skill_md.parent
        frontmatter = _frontmatter_for(skill_md)
        meta = _extract_metadata(frontmatter)

        fallback = name_hint or "uploaded-skill"
        name = _name_from_frontmatter_or(fallback, frontmatter)

        payload = registry_mod.default_register_dict(
            source="zip",
            description=meta["description"],
            icon=meta["icon"],
            version=meta["version"],
            zip_hash=f"sha256-{sha}",
        )
        persisted = _persist_skill(name, skill_root, payload, move=False)
        return {"name": name, **persisted}


def _find_skill_md(root: Path) -> Path | None:
    """Locate the shallowest SKILL.md under ``root`` (depth ≤ 4 for safety)."""
    candidates: list[Path] = []
    for path in root.rglob(registry_mod.SKILL_MD_FILENAME):
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:
            continue
        if depth <= 4:
            candidates.append(path)
    if not candidates:
        return None
    return min(candidates, key=lambda p: len(p.relative_to(root).parts))


# ── custom (create-skill webhook) ──────────────────────────────────


def install_from_custom(
    name: str,
    skill_md_content: str,
    icon: str | None = None,
) -> dict[str, Any]:
    """Write a hand-authored ``SKILL.md`` straight into the registry.

    Used by the ``create-skill`` meta-skill webhook. Frontmatter is parsed from
    the supplied content (so the user's ``description: ...`` propagates to
    register.json).
    """
    if not isinstance(skill_md_content, str) or not skill_md_content.strip():
        raise ValueError("skill_md_content must be non-empty string")
    safe = registry_mod.safe_skill_name(name)

    target = _ensure_no_overwrite(safe)
    target.mkdir(parents=True, exist_ok=False)
    skill_md = target / registry_mod.SKILL_MD_FILENAME
    skill_md.write_text(skill_md_content, encoding="utf-8")

    frontmatter = _frontmatter_for(skill_md)
    meta = _extract_metadata(frontmatter)

    final_icon = icon if isinstance(icon, str) and icon.strip() else meta["icon"]
    payload = registry_mod.default_register_dict(
        source="custom",
        description=meta["description"],
        icon=final_icon,
        version=meta["version"],
    )
    now = _now()
    final_payload = {**payload, "installed_at": now, "last_updated": now}
    registry_mod.write_register_json(safe, final_payload)
    return {"name": safe, **final_payload}


# ── update / check_update ──────────────────────────────────────────


def update_skill(name: str) -> dict[str, Any]:
    """Refresh ``<registry>/<name>``. ``source=github`` → git fetch+reset; else no-op."""
    safe = registry_mod.safe_skill_name(name)
    register = registry_mod.read_register_json(safe)
    source = register.get("source")
    target = registry_mod.skill_dir(safe)

    if not target.is_dir():
        raise FileNotFoundError(f"skill not in registry: {safe}")

    if source != "github":
        return {"ok": True, "message": "no upstream", "name": safe}

    git_dir = target / ".git"
    if not git_dir.is_dir():
        return {"ok": False, "message": "git repo missing — reinstall to refresh", "name": safe}

    ref = register.get("git_ref") or "main"
    try:
        _run_git(["fetch", "--depth", "1", "origin", ref], cwd=target, timeout=GIT_FETCH_TIMEOUT_S)
        _run_git(["reset", "--hard", f"origin/{ref}"], cwd=target)
    except RuntimeError as exc:
        return {"ok": False, "message": str(exc), "name": safe}

    new_sha = _resolve_git_sha(target)
    skill_md = target / registry_mod.SKILL_MD_FILENAME
    frontmatter = _frontmatter_for(skill_md)
    meta = _extract_metadata(frontmatter)

    patch: dict[str, Any] = {
        "git_sha": new_sha,
        "version": meta["version"] or register.get("version"),
        "description": meta["description"] or register.get("description") or "",
        "icon": meta["icon"] or register.get("icon"),
        "last_updated": _now(),
    }
    registry_mod.write_register_json(safe, patch)
    return {"ok": True, "name": safe, "git_sha": new_sha}


def check_update(name: str) -> dict[str, Any]:
    """Detect whether an upstream update is available for a github-sourced skill.

    Returns ``update_available`` (bool) — name matches the frontend contract in
    ``skills-detail.jsx`` (POST /api/skills/<name>/check-update consumer reads
    ``data.update_available``). Boolean derives from SHA diff against the
    pinned ``git_ref``; ``latest_version`` is parsed from the remote
    ``SKILL.md`` frontmatter so the UI can show "v1.2 → v1.3" alongside the
    SHA when upstream uses a versioned manifest. ``latest_version`` falls
    back to ``None`` if the remote SKILL.md can't be parsed (we still trust
    the SHA-diff signal).
    """
    safe = registry_mod.safe_skill_name(name)
    register = registry_mod.read_register_json(safe)
    source = register.get("source")
    target = registry_mod.skill_dir(safe)

    if not target.is_dir():
        raise FileNotFoundError(f"skill not in registry: {safe}")
    if source != "github":
        return {"update_available": False, "name": safe, "reason": "no upstream"}

    git_dir = target / ".git"
    if not git_dir.is_dir():
        return {"update_available": False, "name": safe, "reason": "git repo missing"}

    ref = register.get("git_ref") or "main"
    try:
        _run_git(["fetch", "--depth", "1", "origin", ref], cwd=target, timeout=GIT_FETCH_TIMEOUT_S)
    except RuntimeError as exc:
        return {"update_available": False, "name": safe, "error": str(exc)}

    current = _resolve_git_sha(target) or ""
    try:
        latest_out = _run_git(["rev-parse", f"origin/{ref}"], cwd=target, timeout=10)
    except RuntimeError as exc:
        return {"update_available": False, "name": safe, "error": str(exc)}
    latest = latest_out.strip()

    latest_version = _remote_skill_version(target, ref, register.get("plugin_root"))

    return {
        "update_available": bool(current and latest and current != latest),
        "name": safe,
        "current": current[:7] if current else None,
        "latest": latest[:7] if latest else None,
        "current_version": register.get("version"),
        "latest_version": latest_version,
    }


def _remote_skill_version(repo_dir: Path, ref: str, plugin_root: str | None) -> str | None:
    """Parse ``version`` from the remote SKILL.md frontmatter on ``origin/<ref>``.

    Uses ``git show origin/<ref>:<plugin_root>/SKILL.md`` so we don't have to
    actually merge upstream into the working tree. Returns ``None`` on any
    failure (missing file, parse error, no version field) — the SHA-diff
    signal in :func:`check_update` is the source of truth for "is there an
    update"; this is a UI affordance for showing a version label alongside.
    """
    rel = (plugin_root or ".").strip("/") or "."
    path_in_tree = "SKILL.md" if rel in (".", "") else f"{rel}/SKILL.md"
    try:
        content = _run_git(
            ["show", f"origin/{ref}:{path_in_tree}"],
            cwd=repo_dir, timeout=10,
        )
    except RuntimeError:
        return None
    if not content:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(content)
        tmp_path = Path(tf.name)
    try:
        parsed = registry_mod.parse_skill_md(tmp_path)
    except (FileNotFoundError, RuntimeError):
        return None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    fm = parsed.get("frontmatter") or {}
    if not isinstance(fm, dict):
        return None
    version = fm.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


# ── AI-generated skill (from a free-form description) ───────────────

# 300s: skill gen is the heaviest one-shot — the model authors a full SKILL.md
# (frontmatter + body) on a cold-spawned tmux slot, often with the default
# (non-haiku) model. 150s timed out under the interactive path; 300s gives the
# agentic write room. Returns cleanly on timeout regardless (no hang).
SKILL_GENERATION_TIMEOUT_S: float = 300.0
_SUBPROCESS_BUFFER_LIMIT: int = 2 * 1024 * 1024
_MAX_DESCRIPTION_CHARS = 4000

_SKILL_META_PROMPT = """You are scaffolding a Claude Code skill from a user description.

Output EXACTLY this format. No prose, no JSON envelope blocks, no code fences around the whole reply. Plain text in this order:

NAME: <kebab-case lowercase a-z 0-9 hyphens, ≤64 chars, descriptive of what the skill does>
ICON: <single emoji that fits the skill>
---
<the full SKILL.md content here, starting with `---` for YAML frontmatter; total ≤200 lines>

Rules for the SKILL.md content:
- Frontmatter `name` field must match NAME above; include a one-sentence `description` field with trigger keywords (Polish + English if user's description was Polish).
- Body sections: `## What this is`, `## How to invoke`, `## Constraints`, optional `## Examples`.
- Helper scripts (if any) live at `~/.orchestrator/skills-registry/<name>/scripts/`. If no scripts are needed, the skill is just markdown instructions.
- Be specific. Match terminology and language register from the user's description.

User description:
"""


async def generate_skill_from_description(description: str) -> dict[str, Any]:
    """Generate SKILL.md via claude-cli, then register it as a custom skill.

    Mirrors :mod:`agent_identity_generator`'s spawn pattern (one-shot ``claude -p``,
    forced session-id with JSONL cleanup, ``--permission-mode auto`` + ``--add-dir
    ~/.claude``). Returns the same envelope as :func:`install_from_custom`.
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("description is empty")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        raise ValueError(f"description too long (≤{_MAX_DESCRIPTION_CHARS} chars)")

    # Route by skill_runner_mode: "interactive" (default) → tmux pool
    # (subscription, raw extraction so the NAME:/ICON:/--- layout survives —
    # the block-fencing extractor would inject ``` lines and break the parser);
    # programmatic → the legacy `claude -p` path below (credit pool) as rollback.
    try:
        from . import orchestrator_settings as _settings
        mode = _settings.resolve_runner_mode("skill_runner_mode")
    except Exception:  # noqa: BLE001 — settings unavailable → subscription default
        mode = "interactive"
    if mode == "interactive":
        from . import orchestrator_oneshot as oneshot_mod
        res = await oneshot_mod.run_oneshot(
            _SKILL_META_PROMPT + description, cwd=Path.home(), raw=True,
            timeout_s=SKILL_GENERATION_TIMEOUT_S, label="skill-gen",
        )
        if not res["ok"]:
            raise RuntimeError(res["error"] or "skill generation produced no output")
        parsed = _parse_skill_meta_response(res["text"])
        return install_from_custom(
            name=parsed["name"],
            skill_md_content=parsed["skill_md"],
            icon=parsed.get("icon"),
        )

    # ── programmatic rollback (claude -p) ──
    bootstrap_sid = str(uuid.uuid4())
    cwd_path = Path.home()
    args = [
        _resolve_claude_bin(),
        "-p", _SKILL_META_PROMPT + description,
        "--session-id", bootstrap_sid,
        "--output-format", "text",
        "--permission-mode", "auto",
        "--add-dir", str(Path.home() / ".claude"),
    ]
    env = env_mod.scrubbed_env({"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude")})
    env_mod.log_billing_path("skill-gen", interactive=False)

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd_path),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_SUBPROCESS_BUFFER_LIMIT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("claude binary not found on PATH") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=SKILL_GENERATION_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            raise RuntimeError(
                f"skill generation timed out after {SKILL_GENERATION_TIMEOUT_S}s"
            ) from exc

        if proc.returncode != 0:
            tail = (stderr_b or b"").decode("utf-8", errors="replace").strip()[-500:]
            raise RuntimeError(f"claude exited {proc.returncode}: {tail}")

        raw = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        parsed = _parse_skill_meta_response(raw)
    finally:
        _delete_bootstrap_jsonl(bootstrap_sid, cwd_path)

    return install_from_custom(
        name=parsed["name"],
        skill_md_content=parsed["skill_md"],
        icon=parsed.get("icon"),
    )


def _resolve_claude_bin() -> str:
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"


def _delete_bootstrap_jsonl(session_id: str, cwd: Path) -> None:
    # Programmatic rollback path; delegate to the canonical helper (claude's
    # real slug rule: '/', '_', '.' -> '-').
    from . import orchestrator_oneshot as _oneshot
    _oneshot.delete_bootstrap_jsonl(session_id, cwd)


def _parse_skill_meta_response(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    name: str | None = None
    icon: str | None = None
    body_start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("NAME:") and name is None:
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("ICON:") and icon is None:
            icon = stripped.split(":", 1)[1].strip()
        elif stripped == "---" and name is not None and body_start is None:
            body_start = i + 1
            break
    if not name:
        raise RuntimeError(f"could not parse NAME from response (head: {text[:300]!r})")
    if body_start is None:
        raise RuntimeError("response missing the `---` divider after NAME/ICON")
    skill_md = "\n".join(lines[body_start:]).strip()
    if not skill_md:
        raise RuntimeError("response had empty SKILL.md body")
    if not skill_md.startswith("---"):
        raise RuntimeError(
            f"SKILL.md missing YAML frontmatter (head: {skill_md[:200]!r})"
        )
    return {"name": name, "icon": (icon or None), "skill_md": skill_md}
