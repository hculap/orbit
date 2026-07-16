"""Per-agent skill enablement + symlink farm builder.

Each agent (Global, Areas, Projects, Resources) has a directory under
``~/.orchestrator/agents/<kind>/<lib_id>/`` carrying:

    skills_allowlist.json               — set of skill names the user enabled
    skills/.claude/skills/<name>        — per-spawn symlink farm (rebuilt fresh)

The symlink farm is what claude-cli actually loads at session start: caller
passes ``<agent-dir>/skills/`` to ``--add-dir`` and the cli auto-discovers
``.claude/skills/`` underneath it. The farm is wiped + rebuilt before every
spawn so a stale entry can't leak across sessions.

``kind="global"`` + ``lib_id="global"`` is the synthesised path for the Global
(orchestrator) agent. Otherwise ``kind`` ∈ ``{"areas","projects","resources"}``
mirrors :mod:`agent_prompts`.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from . import skills_registry as registry_mod

HOME: Path = registry_mod.HOME
ORCHESTRATOR_DIR: Path = registry_mod.ORCHESTRATOR_DIR
AGENTS_DIR: Path = ORCHESTRATOR_DIR / "agents"

ALLOWLIST_FILENAME: str = "skills_allowlist.json"
SKILLS_SUBDIR: str = "skills"
CLAUDE_SUBDIR: str = ".claude"

VALID_KINDS: frozenset[str] = frozenset({"areas", "projects", "resources", "global"})

_KIND_ALIASES: dict[str, str] = {
    "area": "areas",
    "project": "projects",
    "resource": "resources",
    "global": "global",
    "areas": "areas",
    "projects": "projects",
    "resources": "resources",
}


def safe_kind(kind: str) -> str:
    """Normalise singular kinds → plural; raise on unknown."""
    if not isinstance(kind, str):
        raise ValueError("kind must be a string")
    canonical = _KIND_ALIASES.get(kind.strip())
    if canonical not in VALID_KINDS:
        raise ValueError(f"unknown kind: {kind!r} (allowed: {sorted(VALID_KINDS)})")
    return canonical


def safe_lib_id(lib_id: str, kind: str) -> str:
    """Validate ``lib_id`` against ``kind``; raise on traversal / empty.

    For ``kind="global"`` the only legal ``lib_id`` is ``"global"`` (the synth
    sentinel). For the other kinds we accept any non-empty path that doesn't
    contain ``..`` or ``.`` segments — matches ``agent_prompts._validate_kind_lib``.
    """
    canonical_kind = safe_kind(kind)
    if not isinstance(lib_id, str):
        raise ValueError("lib_id must be a string")
    cleaned = lib_id.strip().strip("/")
    if not cleaned:
        raise ValueError("lib_id required")

    if canonical_kind == "global":
        if cleaned != "global":
            raise ValueError("lib_id must be 'global' when kind='global'")
        return "global"

    parts = cleaned.split("/")
    if any(seg in ("", "..", ".") for seg in parts):
        raise ValueError(f"lib_id contains illegal segment: {lib_id!r}")
    return "/".join(parts)


def agent_dir(kind: str, lib_id: str) -> Path:
    """Return ``~/.orchestrator/agents/<kind>/<lib_id>/`` — does NOT mkdir."""
    canonical_kind = safe_kind(kind)
    canonical_lib = safe_lib_id(lib_id, canonical_kind)
    return AGENTS_DIR / canonical_kind / canonical_lib


def allowlist_path(kind: str, lib_id: str) -> Path:
    """Return ``<agent-dir>/skills_allowlist.json``."""
    return agent_dir(kind, lib_id) / ALLOWLIST_FILENAME


def read_allowlist(kind: str, lib_id: str) -> set[str]:
    """Per-agent enabled set; ``set()`` if file missing or malformed."""
    try:
        path = allowlist_path(kind, lib_id)
    except ValueError:
        return set()
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(s) for s in raw if isinstance(s, str) and s.strip()}


def write_allowlist(kind: str, lib_id: str, skills: set[str]) -> None:
    """Persist the per-agent enabled set; atomic write."""
    target = allowlist_path(kind, lib_id)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in skills or set():
        if not isinstance(raw, str):
            continue
        try:
            name = registry_mod.safe_skill_name(raw)
        except ValueError:
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    cleaned.sort()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".allowlist.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def add_to_allowlist(kind: str, lib_id: str, name: str) -> None:
    """Idempotent: add ``name`` to ``<kind>/<lib_id>``'s allowlist."""
    safe = registry_mod.safe_skill_name(name)
    current = read_allowlist(kind, lib_id)
    if safe in current:
        return
    write_allowlist(kind, lib_id, current | {safe})


def remove_from_allowlist(kind: str, lib_id: str, name: str) -> None:
    """Idempotent: drop ``name`` from ``<kind>/<lib_id>``'s allowlist."""
    safe = registry_mod.safe_skill_name(name)
    current = read_allowlist(kind, lib_id)
    if safe not in current:
        return
    write_allowlist(kind, lib_id, current - {safe})


def enabled_for_agent(kind: str, lib_id: str) -> set[str]:
    """Effective enabled set: global ∪ per-agent allowlist, filtered to existing skills.

    A skill name in either set that's not present in the registry (deleted but
    not yet purged from the allowlist) is silently dropped — that way callers
    don't have to worry about stale entries breaking symlink-farm builds.
    """
    union = registry_mod.read_global_enabled() | read_allowlist(kind, lib_id)
    if not union:
        return set()
    return {name for name in union if registry_mod.skill_dir(name).is_dir()}


def enabled_for_all_agents(
    agent_keys: list[dict],
) -> dict[tuple[str, str], set[str]]:
    """Batched version of ``enabled_for_agent`` for many agents in one pass.

    Used by ``GET /api/skills`` to render the agents-enabled matrix without
    re-reading the global-enabled file or filesystem-stat'ing every skill
    once per (skill, agent) pair. Reads:
      - ``.global-enabled.json`` once (via ``read_global_enabled``).
      - Each agent's ``skills_allowlist.json`` once.
      - The set of existing skill names once (via ``list_existing_skill_names``).

    Returns ``{(kind, lib_id): set_of_effective_skill_names}``.
    """
    global_set = registry_mod.read_global_enabled()
    existing = registry_mod.list_existing_skill_names()
    out: dict[tuple[str, str], set[str]] = {}
    for entry in agent_keys:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        lib_id = entry.get("lib_id")
        if not isinstance(kind, str) or not isinstance(lib_id, str):
            continue
        try:
            allowlist = read_allowlist(kind, lib_id)
        except Exception:
            allowlist = set()
        union = global_set | allowlist
        out[(kind, lib_id)] = (union & existing) if union else set()
    return out


def _farm_target_dir(kind: str, lib_id: str) -> Path:
    """``<agent-dir>/skills/.claude/skills/`` — symlink target dir."""
    return agent_dir(kind, lib_id) / SKILLS_SUBDIR / CLAUDE_SUBDIR / "skills"


def _wipe_symlinks_only(target: Path) -> None:
    """Remove symlinks (and broken symlinks) under ``target``; leave other files alone.

    ``target`` itself is created if missing. We're conservative here: if a
    user accidentally drops a real file into the farm, we don't want to nuke
    it — just clear our own symlinks.
    """
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        return
    if not target.is_dir():
        raise RuntimeError(f"farm target is not a directory: {target}")
    for entry in target.iterdir():
        if entry.is_symlink():
            try:
                entry.unlink()
            except OSError as exc:
                print(f"[skills_per_agent] failed to unlink {entry}: {exc}")


def build_symlink_farm(kind: str, lib_id: str) -> Path:
    """Wipe + rebuild ``<agent-dir>/skills/.claude/skills/`` and return ``<agent-dir>/skills/``.

    Return value is what the caller should pass to claude-cli's ``--add-dir``;
    the cli auto-loads ``.claude/skills/`` underneath it.

    Idempotent — safe to call before every spawn even when the agent has zero
    enabled skills (returns the parent dir with an empty farm).
    """
    parent = agent_dir(kind, lib_id) / SKILLS_SUBDIR
    target = _farm_target_dir(kind, lib_id)
    parent.mkdir(parents=True, exist_ok=True)
    _wipe_symlinks_only(target)

    enabled = enabled_for_agent(kind, lib_id)
    for name in sorted(enabled):
        try:
            source = registry_mod.skill_dir(name)
        except ValueError:
            continue
        if not source.is_dir():
            continue
        link = target / name
        try:
            os.symlink(str(source), str(link))
        except FileExistsError:
            # Race: another spawn rebuilt concurrently. Replace to be safe.
            try:
                link.unlink()
                os.symlink(str(source), str(link))
            except OSError as exc:
                print(f"[skills_per_agent] symlink replace failed for {name}: {exc}")
        except OSError as exc:
            print(f"[skills_per_agent] symlink create failed for {name}: {exc}")
    return parent


def cleanup_orphan_symlinks_globally(skill_name: str) -> None:
    """Walk every agent dir and remove symlinks + allowlist entries for ``skill_name``.

    Cleans both:
      - per-agent symlink farms (``<agent>/skills/.claude/skills/<name>``)
      - per-agent allowlist files (``<agent>/skills_allowlist.json``)
      - registry-wide ``.global-enabled.json``

    Best-effort: errors are logged and swallowed. Used by ``delete_skill`` to
    keep per-agent state in sync with the registry on uninstall.
    """
    # Registry-wide global-enabled set: drop the orphan name.
    try:
        current = registry_mod.read_global_enabled()
        if skill_name in current:
            registry_mod.write_global_enabled(current - {skill_name})
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"[skills_per_agent] cleanup global-enabled failed: {exc}")

    if not AGENTS_DIR.is_dir():
        return
    try:
        target_source = registry_mod.skill_dir(skill_name).resolve(strict=False)
    except ValueError:
        return

    for kind_dir in AGENTS_DIR.iterdir():
        if not kind_dir.is_dir() or kind_dir.name not in VALID_KINDS:
            continue
        for entry in _walk_agent_lib_dirs(kind_dir):
            # 1. Symlink farm.
            farm = entry / SKILLS_SUBDIR / CLAUDE_SUBDIR / "skills"
            if farm.is_dir():
                for link in farm.iterdir():
                    if not link.is_symlink() or link.name != skill_name:
                        continue
                    try:
                        resolved = Path(os.readlink(link))
                        if not resolved.is_absolute():
                            resolved = (link.parent / resolved).resolve(strict=False)
                    except OSError:
                        continue
                    if resolved == target_source or link.name == skill_name:
                        try:
                            link.unlink()
                        except OSError as exc:
                            print(f"[skills_per_agent] orphan unlink failed for {link}: {exc}")

            # 2. Per-agent allowlist file.
            allowlist_file = entry / ALLOWLIST_FILENAME
            if not allowlist_file.is_file():
                continue
            try:
                data = json.loads(allowlist_file.read_text(encoding="utf-8"))
                if not isinstance(data, list) or skill_name not in data:
                    continue
                cleaned = sorted(set(data) - {skill_name})
                # Atomic write — mirrors write_allowlist's tmpfile + os.replace.
                fd, tmp = tempfile.mkstemp(
                    prefix=".allowlist.", suffix=".tmp",
                    dir=str(allowlist_file.parent),
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(cleaned, f, indent=2, ensure_ascii=False)
                    os.replace(tmp, allowlist_file)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                    raise
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                print(f"[skills_per_agent] allowlist cleanup failed for {allowlist_file}: {exc}")


def _walk_agent_lib_dirs(kind_dir: Path) -> list[Path]:
    """Yield every ``<lib_id>`` dir under a ``<kind>`` dir; tolerates two-level groups.

    Returns concrete agent dirs that contain at least the canonical layout
    markers (allowlist / skills subdir / icon.txt etc). For a ``projects/``
    kind dir we accept either ``projects/<name>/`` (top-level) or
    ``projects/<group>/<name>/`` (grouped) — same shape as ``_safe_project_path``.
    """
    if not kind_dir.is_dir():
        return []
    out: list[Path] = []
    for entry in kind_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if _looks_like_agent_dir(entry):
            out.append(entry)
            continue
        for sub in entry.iterdir():
            if sub.is_dir() and not sub.name.startswith(".") and _looks_like_agent_dir(sub):
                out.append(sub)
    return out


def _looks_like_agent_dir(path: Path) -> bool:
    """Heuristic: a real agent dir has at least one of the well-known artefacts.

    Used by walkers so we don't accidentally treat a stray ``projects/Foo/src/``
    as an agent dir during orphan cleanup.
    """
    markers = (
        ALLOWLIST_FILENAME,
        SKILLS_SUBDIR,
        "icon.txt",
        "identity.md",
        "custom.md",
    )
    return any((path / m).exists() for m in markers)


def list_all_agent_keys(discovered_data: dict | None = None) -> list[dict]:
    """Return ``[{kind, lib_id, label, icon}]`` for every known agent.

    Includes the synthetic Global entry plus the union of:
      - every ``~/.orchestrator/agents/<kind>/<lib_id>/`` dir on disk (i.e.
        agents that already had identity.md generated), and
      - every PARA library entity from :mod:`discovery` (areas, projects,
        resources) — even without a per-agent dir, so the skill-detail page
        can show toggles for newly-created libraries before identity-gen.

    Used by route handlers to render the agents-matrix on the skill detail
    page and to compute ``agents_enabled_count`` in the list view.

    ``discovered_data`` (optional): a snapshot from ``app._get_data()`` —
    if supplied, the PARA section is read from there instead of triggering
    three fresh filesystem walks. Lets route handlers reuse the 30 s
    discovery cache.
    """
    out: list[dict] = [{
        "kind": "global", "lib_id": "global",
        "label": "Global", "icon": "🤖",
    }]
    seen: set[tuple[str, str]] = {("global", "global")}

    # 1. Configured agent dirs — already have identity.md / skills_allowlist.json.
    if AGENTS_DIR.is_dir():
        for kind in ("areas", "projects", "resources"):
            kind_dir = AGENTS_DIR / kind
            if not kind_dir.is_dir():
                continue
            for entry in sorted(_walk_agent_lib_dirs(kind_dir), key=lambda p: str(p)):
                try:
                    rel = str(entry.relative_to(kind_dir))
                except ValueError:
                    continue
                key = (kind, rel)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"kind": kind, "lib_id": rel, "label": rel, "icon": None})

    # 2. PARA library entities — covers libraries that haven't had identity
    #    generated yet so user can still flip skill toggles for them.
    library_sources: tuple[tuple[str, str, callable], ...]
    if discovered_data is not None:
        # Cache-fed path: lift areas/projects/resources off the discovery snapshot.
        def _take(key: str):
            def _getter() -> list[dict]:
                val = discovered_data.get(key)
                return val if isinstance(val, list) else []
            return _getter
        library_sources = (
            ("areas", "📂", _take("areas")),
            ("projects", "📦", _take("projects")),
            ("resources", "📚", _take("resources")),
        )
    else:
        # Fallback: trigger fresh PARA walks (legacy callers / bootstrap).
        try:
            from . import discovery as _disc
        except Exception as exc:  # noqa: BLE001 — startup tolerance
            print(f"[skills_per_agent] discovery import failed: {exc}")
            return out
        library_sources = (
            ("areas", "📂", _disc.discover_areas),
            ("projects", "📦", lambda: _disc.discover_projects({})),
            ("resources", "📚", _disc.discover_resources),
        )
    for kind, default_icon, getter in library_sources:
        try:
            entries = getter() or []
        except Exception as exc:  # noqa: BLE001 — never break the matrix
            print(f"[skills_per_agent] discover {kind} failed: {exc}")
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            lib_id = item.get("lib_id") or item.get("name")
            if not lib_id:
                continue
            key = (kind, str(lib_id))
            if key in seen:
                # Patch label/icon from discovery if the configured-dir entry
                # only had a placeholder.
                for row in out:
                    if row["kind"] == kind and row["lib_id"] == lib_id:
                        if not row.get("icon"):
                            row["icon"] = item.get("icon") or default_icon
                        if row.get("label") == lib_id:
                            row["label"] = item.get("label") or lib_id
                        break
                continue
            seen.add(key)
            out.append({
                "kind": kind,
                "lib_id": str(lib_id),
                "label": item.get("label") or item.get("name") or str(lib_id),
                "icon": item.get("icon") or default_icon,
            })
    return out


def migrate_sidecar_skills_allowlist() -> None:
    """One-shot migration: copy ``agent.skills_allowlist`` from PARA sidecars.

    Walks every area/project/resource sidecar (``.library.json``); if
    ``agent.skills_allowlist`` is a non-null list AND the per-agent
    ``skills_allowlist.json`` does NOT yet exist, copies the list to the new
    location and clears the sidecar field. Safe to call repeatedly — the
    "skills_allowlist.json exists" guard short-circuits later runs.
    """
    try:
        from . import discovery as _disc
        from . import library as _lib
    except Exception as exc:  # noqa: BLE001 — startup-time tolerance
        print(f"[skills_per_agent] migrate import failed: {exc}")
        return

    pairs: list[tuple[str, Path, str]] = []
    if _disc.AREAS.is_dir():
        for area in _disc.AREAS.iterdir():
            if area.is_dir() and not area.name.startswith((".", "_")):
                pairs.append(("areas", area, area.name))
    if _disc.PROJECTS.is_dir():
        for top in _disc.PROJECTS.iterdir():
            if not top.is_dir() or top.name.startswith("."):
                continue
            is_proj = (top / ".git").exists() or (top / "pyproject.toml").exists() or (top / "package.json").exists()
            if is_proj:
                pairs.append(("projects", top, top.name))
            else:
                for sub in top.iterdir():
                    if sub.is_dir() and not sub.name.startswith("."):
                        pairs.append(("projects", sub, f"{top.name}/{sub.name}"))
    if _disc.RESOURCES.is_dir():
        for res in _disc.RESOURCES.iterdir():
            if res.is_dir() and not res.name.startswith((".", "_")):
                pairs.append(("resources", res, res.name))

    for kind, item_path, lib_id in pairs:
        try:
            sidecar = _lib.read_sidecar(item_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[skills_per_agent] sidecar read failed for {item_path}: {exc}")
            continue
        agent_block = sidecar.get("agent")
        if not isinstance(agent_block, dict):
            continue
        legacy = agent_block.get("skills_allowlist")
        if not isinstance(legacy, list) or not legacy:
            continue
        try:
            target = allowlist_path(kind, lib_id)
        except ValueError:
            continue
        if target.is_file():
            continue

        try:
            cleaned = {s for s in legacy if isinstance(s, str) and s.strip()}
            write_allowlist(kind, lib_id, cleaned)
        except Exception as exc:  # noqa: BLE001
            print(f"[skills_per_agent] allowlist write failed for {kind}/{lib_id}: {exc}")
            continue

        try:
            new_block = {**agent_block, "skills_allowlist": None}
            _lib.write_sidecar(item_path, {"agent": new_block})
        except Exception as exc:  # noqa: BLE001
            print(f"[skills_per_agent] sidecar clear failed for {kind}/{lib_id}: {exc}")


def resolve_lib_id_from_session(
    cwd: str | None,
    lib_id_hint: str | None,
) -> tuple[str, str]:
    """Resolve a session's ``(kind, lib_id)`` for symlink-farm purposes.

    Mirrors :func:`agent_prompts.prompts_for_session` semantics:
      - cwd missing or equals ``$HOME`` → Global agent → ``("global", "global")``
      - ``lib_id_hint`` shaped ``"<kind>/<rest>"`` → split on first ``/``
      - anything else (orphaned cwd, unknown kind, traversal) → Global fallback

    Always returns a valid pair; never raises.
    """
    if not _is_per_agent_session(cwd):
        return ("global", "global")
    if not isinstance(lib_id_hint, str):
        return ("global", "global")
    cleaned = lib_id_hint.strip().strip("/")
    if "/" not in cleaned:
        return ("global", "global")
    kind, rest = cleaned.split("/", 1)
    if kind not in ("areas", "projects", "resources"):
        return ("global", "global")
    if not rest or any(seg in ("", "..", ".") for seg in rest.split("/")):
        return ("global", "global")
    try:
        safe_lib_id(rest, kind)
    except ValueError:
        return ("global", "global")
    return (kind, rest)


def _is_per_agent_session(cwd: str | None) -> bool:
    """True when cwd points at a non-HOME location — i.e. a PARA agent session."""
    if cwd is None or not isinstance(cwd, str) or not cwd.strip():
        return False
    try:
        resolved = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        home_resolved = HOME.resolve()
    except (OSError, RuntimeError):
        home_resolved = HOME
    return resolved != home_resolved


def cleanup_all_farms() -> None:
    """Wipe every per-agent symlink farm. Used by tests / explicit reset."""
    if not AGENTS_DIR.is_dir():
        return
    for kind in VALID_KINDS:
        kind_dir = AGENTS_DIR / kind
        if not kind_dir.is_dir():
            continue
        for entry in _walk_agent_lib_dirs(kind_dir):
            farm_root = entry / SKILLS_SUBDIR
            if farm_root.is_dir():
                try:
                    shutil.rmtree(farm_root)
                except OSError as exc:
                    print(f"[skills_per_agent] cleanup_all_farms failed for {farm_root}: {exc}")
