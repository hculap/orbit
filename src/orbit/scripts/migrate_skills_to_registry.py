"""One-shot migration: ``~/.claude/skills/*`` → ``~/.orchestrator/skills-registry/*``.

Usage:
    python -m orbit.scripts.migrate_skills_to_registry [--dry-run]

Idempotent: safe to re-run. After a successful migration ``~/.claude/skills/``
is renamed to ``~/.claude/skills.legacy-<unix-ts>/`` so we never destroy data.

Symlinks (e.g. ``~/.claude/skills/generate-image`` → ``~/Projects/.../skills/generate-image``)
are dereferenced before the copy so the registry holds a real tree, not a link.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from orbit import skills_per_agent as per_agent_mod
from orbit import skills_registry as registry_mod

LEGACY_SKILLS_DIR: Path = registry_mod.HOME / ".claude" / "skills"


def _resolve_real_dir(entry: Path) -> Path:
    """Resolve a (possibly symlinked) skill dir to its real on-disk location."""
    if entry.is_symlink():
        target = Path(os.readlink(entry))
        if not target.is_absolute():
            target = (entry.parent / target).resolve()
        else:
            target = target.resolve()
        return target
    return entry.resolve()


def _is_skill_dir(entry: Path) -> bool:
    """True iff ``entry`` resolves to a dir containing SKILL.md."""
    real = _resolve_real_dir(entry)
    return real.is_dir() and (real / registry_mod.SKILL_MD_FILENAME).is_file()


def _build_register_payload(skill_md: Path) -> dict[str, Any]:
    """Construct register.json for a migrated local skill."""
    parsed = registry_mod.parse_skill_md(skill_md)
    fm = parsed.get("frontmatter") or {}
    description = fm.get("description")
    version = fm.get("version")
    icon = fm.get("icon") or fm.get("emoji")
    now = time.time()
    return {
        "source": "local",
        "description": str(description).strip() if isinstance(description, str) else "",
        "version": str(version).strip() if isinstance(version, str) and str(version).strip() else None,
        "icon": str(icon).strip() if isinstance(icon, str) and str(icon).strip() else None,
        "git_origin": None,
        "git_ref": None,
        "git_sha": None,
        "zip_hash": None,
        "plugin_root": None,
        "installed_at": now,
        "last_updated": now,
    }


def _migrate_single(name: str, source_dir: Path, *, dry_run: bool) -> str:
    """Migrate one skill dir; return status token: NEW / EXISTS / SKIPPED."""
    target = registry_mod.skill_dir(name)
    if target.exists():
        return "EXISTS"

    real_source = _resolve_real_dir(source_dir)
    if not real_source.is_dir():
        return "SKIPPED:not-a-dir"
    if not (real_source / registry_mod.SKILL_MD_FILENAME).is_file():
        return "SKIPPED:no-skill-md"

    if dry_run:
        return "NEW(dry-run)"

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(real_source), str(target), symlinks=False)

    skill_md = target / registry_mod.SKILL_MD_FILENAME
    payload = _build_register_payload(skill_md)
    registry_mod.write_register_json(name, payload)
    registry_mod.add_to_global(name)
    return "NEW"


def _move_legacy_dir(*, dry_run: bool) -> str:
    """Rename ``~/.claude/skills`` → ``~/.claude/skills.legacy-<ts>``; return status."""
    if not LEGACY_SKILLS_DIR.exists():
        return "absent"
    timestamp = int(time.time())
    legacy = LEGACY_SKILLS_DIR.parent / f"skills.legacy-{timestamp}"
    if dry_run:
        return f"would-rename → {legacy.name}"
    try:
        os.rename(str(LEGACY_SKILLS_DIR), str(legacy))
        return f"renamed → {legacy.name}"
    except OSError as exc:
        return f"rename-failed: {exc}"


def _print_summary(rows: list[tuple[str, str]]) -> None:
    """Render a 2-column table summarising skill statuses."""
    if not rows:
        print("(no skills found)")
        return
    width = max(len(name) for name, _ in rows)
    for name, status in sorted(rows):
        print(f"  {name:<{width}}   {status}")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (0 = success)."""
    parser = argparse.ArgumentParser(description="Migrate ~/.claude/skills/* into the orchestrator skills registry")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what WOULD happen without writing anything",
    )
    args = parser.parse_args(argv)

    print(f"[migrate] LEGACY_SKILLS_DIR  = {LEGACY_SKILLS_DIR}")
    print(f"[migrate] REGISTRY_DIR       = {registry_mod.SKILLS_REGISTRY_DIR}")
    print(f"[migrate] dry_run            = {args.dry_run}")

    if not args.dry_run:
        registry_mod.bootstrap()

    rows: list[tuple[str, str]] = []
    if LEGACY_SKILLS_DIR.is_dir():
        for entry in sorted(LEGACY_SKILLS_DIR.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                name = registry_mod.safe_skill_name(entry.name)
            except ValueError as exc:
                rows.append((entry.name, f"SKIPPED:invalid-name ({exc})"))
                continue
            if not _is_skill_dir(entry):
                rows.append((name, "SKIPPED:not-a-skill"))
                continue
            try:
                status = _migrate_single(name, entry, dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001
                status = f"ERROR: {exc}"
            rows.append((name, status))
    else:
        print("[migrate] no legacy skills dir — nothing to migrate")

    print("\n[migrate] per-skill results:")
    _print_summary(rows)

    print("\n[migrate] post-steps:")
    print(f"  legacy-rename            {_move_legacy_dir(dry_run=args.dry_run)}")

    if not args.dry_run:
        try:
            per_agent_mod.migrate_sidecar_skills_allowlist()
            print("  sidecar-migration         done")
        except Exception as exc:  # noqa: BLE001
            print(f"  sidecar-migration         FAILED: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
