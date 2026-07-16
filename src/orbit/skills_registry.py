"""Canonical skills registry — `~/.orchestrator/skills-registry/<name>/`.

A skill folder always contains ``SKILL.md`` (YAML frontmatter + markdown body)
and ``register.json`` (our metadata: source, version, origin, hashes). Optional
``scripts/``, ``references/``, and ``.git/`` subdirs follow upstream skill
conventions; we never inspect them — only the SKILL.md / register.json pair.

Per-agent gating happens via :mod:`skills_per_agent` — this module is the
content-addressed store + globally-enabled set, no per-agent state.

Atomic writes via ``tempfile.mkstemp`` + ``os.replace`` (mirrors
``library.write_sidecar``). All paths derived from ``Path.home()`` — never
hardcode ``/home/user/`` so unit tests can swap ``HOME``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

HOME: Path = Path(os.environ.get("HOME", str(Path.home())))
ORCHESTRATOR_DIR: Path = HOME / ".orchestrator"
SKILLS_REGISTRY_DIR: Path = ORCHESTRATOR_DIR / "skills-registry"
GLOBAL_ENABLED_PATH: Path = SKILLS_REGISTRY_DIR / ".global-enabled.json"

SKILL_MD_FILENAME: str = "SKILL.md"
REGISTER_JSON_FILENAME: str = "register.json"

_SKILL_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONTMATTER_RE: re.Pattern[str] = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*\n(?P<rest>.*)\Z",
    re.DOTALL,
)


def safe_skill_name(name: str) -> str:
    """Validate + normalise a skill name; raise ``ValueError`` on bad input.

    Allowed: lowercase ascii letters/digits + hyphens, 1–64 chars, must start
    with letter/digit. Mirrors ``library._validate_name`` in spirit but stricter
    (we reserve uppercase for human display only — the on-disk dir name is the
    canonical key everywhere else).
    """
    if not isinstance(name, str):
        raise ValueError("skill name must be a string")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("skill name required")
    if not _SKILL_NAME_RE.match(cleaned):
        raise ValueError(
            f"skill name has invalid chars: {name!r} "
            "(allowed: a-z 0-9 -, ≤64 chars, must start with letter/digit)"
        )
    return cleaned


def skill_dir(name: str) -> Path:
    """Absolute path to ``<registry>/<name>/`` — does NOT assert existence."""
    return SKILLS_REGISTRY_DIR / safe_skill_name(name)


def parse_skill_md(path: Path) -> dict:
    """Read a SKILL.md and return ``{frontmatter: dict, body: str}``.

    Tolerant of missing frontmatter (returns ``frontmatter={}``) and malformed
    YAML (returns ``frontmatter={"_error": "..."}`` so callers can render the
    body anyway). Raises ``FileNotFoundError`` if the file itself is missing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"SKILL.md not found: {path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"failed to read {path}: {exc}") from exc

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {"frontmatter": {}, "body": text}

    raw = match.group("body")
    body = match.group("rest")
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        fallback = _lenient_frontmatter(raw)
        if fallback:
            fallback["_warning"] = f"YAML strict parse failed; used line-based fallback: {exc}"
            return {"frontmatter": fallback, "body": body}
        return {"frontmatter": {"_error": f"YAML parse error: {exc}"}, "body": body}

    if not isinstance(parsed, dict):
        return {"frontmatter": {"_error": "frontmatter is not a mapping"}, "body": body}

    return {"frontmatter": parsed, "body": body}


_TOPLEVEL_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s?(?P<value>.*)$")


def _lenient_frontmatter(raw: str) -> dict:
    """Line-based fallback for SKILL.md frontmatter when YAML strict parse fails.

    Real-world SKILL.md files in the wild often embed unquoted colons inside
    description values (e.g. ``Returns abs path via "MEDIA: <path>"``) which
    PyYAML interprets as nested mappings and rejects. This recovers the simple
    ``key: value`` pairs without trying to handle nested structures, lists, or
    multiline scalars — those callers fall back to ``_error``.
    """
    fm: dict = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            continue
        m = _TOPLEVEL_KEY_RE.match(line)
        if not m:
            continue
        key = m.group("key")
        value = m.group("value").strip()
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        fm[key] = value
    return fm


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` as pretty JSON to ``path`` via tmpfile + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".register.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_register_json(name: str) -> dict:
    """Return the raw ``register.json`` for a skill; ``{}`` if absent."""
    path = skill_dir(name) / REGISTER_JSON_FILENAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_register_json(name: str, patch: dict) -> None:
    """Deep-merge ``patch`` into existing register.json; atomic write.

    Top-level keys in ``patch`` overwrite existing values verbatim (no nested
    merge — keep the schema flat). Pass an empty dict to no-op.
    """
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    target_dir = skill_dir(name)
    if not target_dir.is_dir():
        raise FileNotFoundError(f"skill not in registry: {name}")
    current = read_register_json(name)
    merged = {**current, **patch}
    _atomic_write_json(target_dir / REGISTER_JSON_FILENAME, merged)
    _invalidate_skill_cache(name)


# Per-skill cache keyed on (SKILL.md mtime, register.json mtime). Pure
# in-memory; busted on disk write via ``write_register_json`` and
# ``delete_skill``. Avoids re-parsing YAML frontmatter on every /api/skills
# poll (the list endpoint walks all ~30 skills).
#
# Why mtime rather than TTL (cf. CLAUDE.md "in-memory caches with TTL"):
# a TTL window would force re-parsing every N seconds even when nothing on
# disk changed. We instead stat() the source files on every access (a
# cheap single syscall pair) and reuse the cached parse only when both
# mtimes match — so the cache can never serve stale content, and steady-
# state reads pay just two stat() calls. The TTL rule's intent ("don't
# do heavy work on every request") is preserved; the validation method
# is strictly tighter than TTL.
_READ_CACHE: dict[str, tuple[float, float, dict]] = {}


def _stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _invalidate_skill_cache(name: str) -> None:
    """Drop cache entry for ``name`` — called from writers (install, delete,
    register-patch). Cheap; tolerates missing key."""
    _READ_CACHE.pop(name, None)


def read_skill(name: str) -> dict | None:
    """Combined SkillInfo: frontmatter + register + on-disk presence flag.

    Returns ``None`` when the skill dir doesn't exist OR has no SKILL.md (the
    presence of ``register.json`` alone is not enough — a skill without
    SKILL.md is effectively broken).

    Cached by ``(skill_md_mtime, register_json_mtime)`` to skip YAML parsing
    when neither file changed since the previous read.
    """
    try:
        target = skill_dir(name)
    except ValueError:
        return None
    if not target.is_dir():
        return None
    skill_md = target / SKILL_MD_FILENAME
    if not skill_md.is_file():
        return None
    register_path = target / REGISTER_JSON_FILENAME
    md_mtime = _stat_mtime(skill_md)
    reg_mtime = _stat_mtime(register_path)
    cached = _READ_CACHE.get(name)
    if cached is not None and cached[0] == md_mtime and cached[1] == reg_mtime:
        return cached[2]
    parsed = parse_skill_md(skill_md)
    register = read_register_json(name)
    info = {
        "name": name,
        "frontmatter": parsed.get("frontmatter") or {},
        "body": parsed.get("body") or "",
        "register": register,
        "exists": True,
    }
    _READ_CACHE[name] = (md_mtime, reg_mtime, info)
    return info


def list_skills() -> list[dict]:
    """All skills currently in the registry, sorted by name.

    Skips dirs starting with ``.`` (hidden / metadata) and any dir without a
    valid name. Broken skills (no SKILL.md) are omitted from the list — call
    ``read_skill`` directly with a known name if you need the raw dir state.
    """
    if not SKILLS_REGISTRY_DIR.is_dir():
        return []
    entries: list[dict] = []
    for entry in sorted(SKILLS_REGISTRY_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            safe_skill_name(entry.name)
        except ValueError:
            continue
        info = read_skill(entry.name)
        if info is not None:
            entries.append(info)
    return entries


def list_existing_skill_names() -> set[str]:
    """Names of every valid skill dir on disk — cheaper than ``list_skills``
    when the caller only needs to test membership (e.g. filter union sets in
    :func:`skills_per_agent.enabled_for_all_agents`).
    """
    if not SKILLS_REGISTRY_DIR.is_dir():
        return set()
    out: set[str] = set()
    for entry in SKILLS_REGISTRY_DIR.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            out.add(safe_skill_name(entry.name))
        except ValueError:
            continue
    return out


def delete_skill(name: str) -> None:
    """Remove ``<registry>/<name>/`` + purge from global + per-agent farms.

    Raises ``FileNotFoundError`` if the skill dir doesn't exist. Any per-agent
    cleanup failure is best-effort — the registry dir removal is the primary
    operation; orphan symlinks are harmless.
    """
    target = skill_dir(name)
    if not target.is_dir():
        raise FileNotFoundError(f"skill not in registry: {name}")
    shutil.rmtree(target)
    _invalidate_skill_cache(name)
    try:
        remove_from_global(name)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"[skills_registry] global cleanup failed for {name}: {exc}")
    try:
        from . import skills_per_agent as _per_agent
        _per_agent.cleanup_orphan_symlinks_globally(name)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"[skills_registry] per-agent cleanup failed for {name}: {exc}")


def read_global_enabled() -> set[str]:
    """Return the set of globally-auto-enabled skill names; ``set()`` if file missing."""
    if not GLOBAL_ENABLED_PATH.is_file():
        return set()
    try:
        raw = json.loads(GLOBAL_ENABLED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(s) for s in raw if isinstance(s, str) and s.strip()}


def write_global_enabled(skills: set[str]) -> None:
    """Persist the global-enabled set; atomic write."""
    if not isinstance(skills, (set, frozenset, list, tuple)):
        raise ValueError("skills must be a collection of strings")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in skills:
        if not isinstance(raw, str):
            continue
        try:
            name = safe_skill_name(raw)
        except ValueError:
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    cleaned.sort()
    SKILLS_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".global-enabled.",
        suffix=".tmp",
        dir=str(SKILLS_REGISTRY_DIR),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
        os.replace(tmp, GLOBAL_ENABLED_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def add_to_global(name: str) -> None:
    """Idempotent: add ``name`` to the global-enabled set."""
    safe = safe_skill_name(name)
    current = read_global_enabled()
    if safe in current:
        return
    write_global_enabled(current | {safe})


def remove_from_global(name: str) -> None:
    """Idempotent: drop ``name`` from the global-enabled set."""
    safe = safe_skill_name(name)
    current = read_global_enabled()
    if safe not in current:
        return
    write_global_enabled(current - {safe})


def bootstrap() -> None:
    """Idempotent: ensure ``<registry>/`` exists + ``.global-enabled.json`` is at least ``[]``.

    Safe to call repeatedly; never overwrites an existing ``.global-enabled.json``.
    """
    try:
        SKILLS_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[skills_registry] bootstrap mkdir failed: {exc}")
        return
    if not GLOBAL_ENABLED_PATH.exists():
        try:
            write_global_enabled(set())
        except OSError as exc:
            print(f"[skills_registry] bootstrap seed failed: {exc}")


def default_register_dict(
    *,
    source: str,
    description: str = "",
    icon: str | None = None,
    version: str | None = None,
    git_origin: str | None = None,
    git_ref: str | None = None,
    git_sha: str | None = None,
    zip_hash: str | None = None,
    plugin_root: str | None = None,
) -> dict[str, Any]:
    """Construct a fresh register.json payload with sensible defaults.

    ``installed_at`` / ``last_updated`` are filled by the caller when writing —
    we leave them out so unit tests have deterministic fixtures.
    """
    return {
        "source": source,
        "description": description,
        "icon": icon,
        "version": version,
        "git_origin": git_origin,
        "git_ref": git_ref,
        "git_sha": git_sha,
        "zip_hash": zip_hash,
        "plugin_root": plugin_root,
    }
