"""Seed repo-bundled skills into the orchestrator registry on startup.

The repo ships canonical "local" skills under ``<repo>/skills/<name>/`` (each a
``SKILL.md`` + optional ``register.json`` + ``scripts/``). They install into the
dashboard's runtime registry at ``~/.orchestrator/skills-registry/<name>/`` — the
same place ``scripts/migrate_skills_to_registry.py`` writes — so they deploy WITH
the code (git pull + restart) instead of a manual rsync.

Idempotent + non-destructive: a bundled skill is copied in ONLY when its registry
dir is absent (``seed-if-missing``), then registered (register.json) and
globally enabled. An existing registry entry — including one the user edited or
intentionally removed-then-rebuilt — is left untouched. Mirrors the seed-on-boot
pattern already used for agent prompts, the watchdog job, and tasks-reminders.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import skills_registry as registry_mod

# <repo>/skills/ — package is <repo>/src/orbit/, so parents[2] is the
# repo root. The bundled skills ship inside the deployed tree, so this resolves
# on the box too.
BUNDLED_SKILLS_DIR: Path = Path(__file__).resolve().parents[2] / "skills"


def _register_payload(skill_md: Path) -> dict[str, Any]:
    """Build a register.json dict from a skill's SKILL.md frontmatter."""
    parsed = registry_mod.parse_skill_md(skill_md)
    fm = parsed.get("frontmatter") or {}
    description = fm.get("description")
    version = fm.get("version")
    icon = fm.get("icon") or fm.get("emoji")
    meta = fm.get("metadata")
    if not icon and isinstance(meta, dict):
        # Tolerate `metadata: {emoji: …}` / `metadata: {clawdbot: {emoji: …}}`.
        icon = meta.get("emoji") or (
            meta.get("clawdbot", {}).get("emoji") if isinstance(meta.get("clawdbot"), dict) else None
        )
    now = time.time()
    return {
        "source": "local",
        "description": str(description).strip() if isinstance(description, str) else "",
        "version": str(version).strip() if isinstance(version, str) and str(version).strip() else None,
        "icon": str(icon).strip() if isinstance(icon, str) and str(icon).strip() else None,
        "git_origin": None, "git_ref": None, "git_sha": None,
        "zip_hash": None, "plugin_root": None,
        "installed_at": now, "last_updated": now,
    }


def seed_bundled_skills() -> None:
    """Copy any missing repo-bundled skill into the registry + enable globally.

    Best-effort: a single bad skill dir is logged and skipped, never blocks boot.
    """
    if not BUNDLED_SKILLS_DIR.is_dir():
        return
    try:
        registry_mod.bootstrap()
    except Exception as exc:  # noqa: BLE001
        print(f"[bundled_skills] registry bootstrap failed: {exc}", file=sys.stderr)
        return

    for entry in sorted(BUNDLED_SKILLS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if not (entry / registry_mod.SKILL_MD_FILENAME).is_file():
            continue
        try:
            name = registry_mod.safe_skill_name(entry.name)
        except ValueError as exc:
            print(f"[bundled_skills] skip {entry.name!r}: {exc}", file=sys.stderr)
            continue
        target = registry_mod.skill_dir(name)
        try:
            if target.exists():
                # Already installed → refresh the repo-OWNED code so bundled-skill
                # fixes propagate on deploy (the seed-if-missing alone left stale
                # scripts in the registry). Overwrite SKILL.md + scripts/ from the
                # repo; PRESERVE register.json / config.json / global-enable
                # (install + user-editable runtime state). User-managed skills
                # (source != local, i.e. github/zip-installed) are never touched
                # because they don't live in the repo skills/ dir.
                _refresh_code(entry, target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(entry), str(target), symlinks=False)
            # Prefer a committed register.json; synthesize from frontmatter only
            # when the skill didn't ship one (e.g. generate-image).
            if not (target / registry_mod.REGISTER_JSON_FILENAME).is_file():
                registry_mod.write_register_json(name, _register_payload(target / registry_mod.SKILL_MD_FILENAME))
            registry_mod.add_to_global(name)
            print(f"[bundled_skills] installed {name} → registry (global)")
        except Exception as exc:  # noqa: BLE001
            print(f"[bundled_skills] failed to install/refresh {name}: {exc}", file=sys.stderr)


# Repo-owned files refreshed in-place on boot for an already-installed bundled
# skill. register.json (enable/install metadata) and config.json (runtime
# config like dashboard_url) are intentionally excluded — they're state, not code.
_REPO_OWNED = ("SKILL.md", "TOOL.md", "scripts")


def _refresh_code(src_dir: Path, dst_dir: Path) -> None:
    """Overwrite the repo-owned code files of an installed bundled skill."""
    changed = False
    for item in _REPO_OWNED:
        src = src_dir / item
        if not src.exists():
            continue
        dst = dst_dir / item
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(str(src), str(dst), symlinks=False)
        else:
            shutil.copy2(str(src), str(dst))
        changed = True
    if changed:
        try:
            registry_mod._invalidate_skill_cache(dst_dir.name)
        except Exception:  # noqa: BLE001 — cache is best-effort
            pass
        print(f"[bundled_skills] refreshed code for {dst_dir.name}")
