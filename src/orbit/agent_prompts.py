"""Per-agent prompt-stack resolver.

Layout on disk:

    ~/.orchestrator/agent-prompts/general.md       ← every agent inherits
    ~/.orchestrator/agent-prompts/orchestrator.md  ← Global agent only
    ~/.orchestrator/agents/
        areas/<lib_name>/{identity.md,custom.md,icon.txt}
        projects/<lib_name>/{identity.md,custom.md,icon.txt}
        resources/<lib_name>/{identity.md,custom.md,icon.txt}

The four-layer stack is composed at session-start time by
``prompts_for_session(cwd, lib_id)``: ``[general, orchestrator?, identity?,
custom?]``. The orchestrator layer is dropped for non-Global agents; identity
and custom layers are dropped if the file doesn't exist on disk so build_args
stays tolerant on a freshly-created agent that hasn't run identity gen yet.

Bootstrap is idempotent: missing baseline files are seeded from
``agent_prompts_defaults``; existing files are NEVER overwritten so user edits
in /settings persist across server restarts.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Iterable

from . import agent_prompts_defaults as _defaults

_AGENT_HEADER_RE: re.Pattern[str] = re.compile(r"<!--\s*agent-prompt:v(\d+)\s*-->")

# ── filesystem layout ─────────────────────────────────────────────

HOME: Path = Path(os.environ.get("HOME", str(Path.home())))
ORCHESTRATOR_DIR: Path = HOME / ".orchestrator"
AGENT_PROMPTS_DIR: Path = ORCHESTRATOR_DIR / "agent-prompts"
AGENTS_DIR: Path = ORCHESTRATOR_DIR / "agents"

GENERAL_FILENAME: str = "general.md"
ORCHESTRATOR_FILENAME: str = "orchestrator.md"
# User-editable custom layer for the Global agent — the analogue of a per-agent
# ``custom.md`` for global sessions. Kept SEPARATE from ``orchestrator.md``
# (which bootstrap()/_seed_or_migrate version-migrates and would clobber) so the
# user's edits survive prompt-version bumps. Appended after orchestrator.md for
# global sessions; never migrated.
GLOBAL_CUSTOM_FILENAME: str = "orchestrator-custom.md"
IDENTITY_FILENAME: str = "identity.md"
CUSTOM_FILENAME: str = "custom.md"
ICON_FILENAME: str = "icon.txt"

# Path-traversal guard: lib_id is "<kind>/<rest>" where kind is one of these
# and rest must not contain ``..`` segments. Mirrors library._safe_*_path
# style; we duplicate here to keep agent_prompts free of import cycles.
_VALID_KINDS: frozenset[str] = frozenset({"areas", "projects", "resources"})

# Singular ↔ plural normalisation. Callers in library.py / library_files.py
# use the singular form ("area" / "project" / "resource") in route signatures
# and helpers; agent_prompts canonicalises on the plural folder names. Accept
# both at the boundary so we don't have to thread a normalisation through
# every caller.
_KIND_ALIASES: dict[str, str] = {
    "area": "areas", "project": "projects", "resource": "resources",
    "areas": "areas", "projects": "projects", "resources": "resources",
}


def _normalize_kind(kind: str) -> str:
    """Map singular library kinds onto the plural folder name; passes through plural unchanged."""
    return _KIND_ALIASES.get(kind, kind)

# Single-emoji cap on icon.txt. ZWJ sequences (e.g. 👨‍💻) can be up to 11 code
# points — 32 chars covers the worst-case grapheme cluster comfortably while
# still rejecting an accidental novel.
_ICON_MAX_LEN: int = 32

_bootstrapped: bool = False


# ── path helpers ──────────────────────────────────────────────────


def general_prompt_path() -> Path:
    """Absolute path to the shared general.md (every agent)."""
    return AGENT_PROMPTS_DIR / GENERAL_FILENAME


def orchestrator_prompt_path() -> Path:
    """Absolute path to orchestrator.md (Global agent only)."""
    return AGENT_PROMPTS_DIR / ORCHESTRATOR_FILENAME


def global_custom_prompt_path() -> Path:
    """Absolute path to the Global agent's user-editable custom layer."""
    return AGENT_PROMPTS_DIR / GLOBAL_CUSTOM_FILENAME


def read_global_custom() -> str:
    """Return the Global agent's custom prompt text, or '' if unset."""
    try:
        return global_custom_prompt_path().read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return ""


def write_global_custom(text: str) -> None:
    """Persist (or clear, when blank) the Global agent's custom prompt layer."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    path = global_custom_prompt_path()
    if not text.strip():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _validate_kind_lib(kind: str, lib_id: str) -> tuple[str, str]:
    """Refuse path-traversal; return (kind, normalized_lib_id).

    ``lib_id`` is the path-rest after the ``<kind>/`` prefix — e.g. for
    ``"projects/my-project"`` the caller has already split off ``projects``
    and passes ``my-project``. Empty / ``..`` segments raise ``ValueError``.
    """
    canonical_kind = _normalize_kind(kind)
    if canonical_kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind: {kind!r} (allowed: {sorted(_VALID_KINDS)})")
    if not isinstance(lib_id, str) or not lib_id.strip():
        raise ValueError("lib_id must be a non-empty string")
    cleaned = lib_id.strip().strip("/")
    parts = cleaned.split("/")
    if not parts or any(seg in ("", "..", ".") for seg in parts):
        raise ValueError(f"lib_id contains illegal segment: {lib_id!r}")
    return canonical_kind, "/".join(parts)


def agent_dir(kind: str, lib_id: str) -> Path:
    """Return the per-agent directory: ``~/.orchestrator/agents/<kind>/<lib_id>/``.

    Does NOT create the directory — readers should tolerate absence and
    writers ``mkdir(parents=True, exist_ok=True)`` themselves.
    """
    valid_kind, valid_lib = _validate_kind_lib(kind, lib_id)
    return AGENTS_DIR / valid_kind / valid_lib


def agent_identity_path(kind: str, lib_id: str) -> Path:
    return agent_dir(kind, lib_id) / IDENTITY_FILENAME


def agent_custom_path(kind: str, lib_id: str) -> Path:
    return agent_dir(kind, lib_id) / CUSTOM_FILENAME


def agent_icon_path(kind: str, lib_id: str) -> Path:
    return agent_dir(kind, lib_id) / ICON_FILENAME


# ── icon read / write ─────────────────────────────────────────────


def read_icon(kind: str, lib_id: str) -> str | None:
    """Return the agent's icon (single emoji / grapheme cluster) or ``None``.

    Tolerates missing dir, missing file, IO errors, and over-long contents
    (treated as missing rather than raising — icons are cosmetic).
    """
    try:
        path = agent_icon_path(kind, lib_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text or len(text) > _ICON_MAX_LEN:
        return None
    return text


def write_icon(kind: str, lib_id: str, emoji: str | None) -> None:
    """Persist (or clear) the agent's icon.

    ``None`` / empty string deletes the file. Otherwise the value is stripped
    and written. Long values raise ``ValueError`` so the route layer can
    surface a 400 — we don't want to silently truncate user input.
    """
    path = agent_icon_path(kind, lib_id)
    if emoji is None or (isinstance(emoji, str) and not emoji.strip()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if not isinstance(emoji, str):
        raise ValueError("emoji must be a string or None")
    cleaned = emoji.strip()
    if len(cleaned) > _ICON_MAX_LEN:
        raise ValueError(f"emoji too long (>{_ICON_MAX_LEN} chars)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned, encoding="utf-8")


# ── session-time resolution ───────────────────────────────────────


def _is_global_session(cwd: str | None) -> bool:
    """Global = no cwd override OR cwd resolves to ``$HOME`` exactly.

    Per-agent sessions always carry an explicit cwd pointing at their PARA
    item; absence of cwd OR cwd == HOME both mean "run from the user's home,
    which is what the legacy single-prompt behaviour did".
    """
    if cwd is None or not isinstance(cwd, str) or not cwd.strip():
        return True
    try:
        resolved = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        home_resolved = HOME.resolve()
    except (OSError, RuntimeError):
        home_resolved = HOME
    return resolved == home_resolved


def _split_lib_id(lib_id: str | None) -> tuple[str, str] | None:
    """Split ``"<kind>/<rest>"`` into ``(kind, rest)``; ``None`` on bad input.

    Garbage (empty, missing slash, traversal segments, unknown kind) returns
    ``None`` so callers fall back to "no per-agent overlay" gracefully — same
    failure mode as a missing identity.md.
    """
    if not isinstance(lib_id, str):
        return None
    cleaned = lib_id.strip().strip("/")
    if "/" not in cleaned:
        return None
    kind, rest = cleaned.split("/", 1)
    if kind not in _VALID_KINDS:
        return None
    if not rest or any(seg in ("", "..", ".") for seg in rest.split("/")):
        return None
    return kind, rest


def prompts_for_session(cwd: str | None, lib_id: str | None) -> list[Path]:
    """Resolve the ordered list of ``--append-system-prompt-file`` targets.

    Order (filtered by existence on disk so build_args stays tolerant):

        1. general.md             — always, when present
        2. orchestrator.md        — only when cwd is None or ``$HOME``
        3. orchestrator-custom.md — global only, user-editable, when non-empty
        4. <agent>/identity.md    — only when lib_id parses + file exists
        5. <agent>/custom.md      — only when lib_id parses + file exists

    Bootstrap is implicitly invoked here so the very first turn after a fresh
    install still gets general.md / orchestrator.md.
    """
    bootstrap()

    paths: list[Path] = []
    general = general_prompt_path()
    if general.is_file():
        paths.append(general)

    if _is_global_session(cwd):
        orch = orchestrator_prompt_path()
        if orch.is_file():
            paths.append(orch)
        gcustom = global_custom_prompt_path()
        if gcustom.is_file() and gcustom.stat().st_size > 0:
            paths.append(gcustom)

    split = _split_lib_id(lib_id)
    if split is not None:
        kind, rest = split
        try:
            identity = agent_identity_path(kind, rest)
            custom = agent_custom_path(kind, rest)
        except ValueError:
            return paths
        if identity.is_file() and identity.stat().st_size > 0:
            paths.append(identity)
        if custom.is_file() and custom.stat().st_size > 0:
            paths.append(custom)

    return paths


def existing_paths(paths: Iterable[Path]) -> list[Path]:
    """Filter helper: drop any path that doesn't currently exist on disk."""
    return [p for p in paths if Path(p).is_file()]


# ── bootstrap ─────────────────────────────────────────────────────


def bootstrap() -> None:
    """Seed ``general.md`` + ``orchestrator.md``, migrating stale versions. Idempotent.

    Called once on app startup AND defensively on the first
    ``prompts_for_session`` to cover test contexts that bypass ``create_app``.
    A current-version file is left strictly alone (user edits persist); an
    older / unversioned baseline (e.g. the pre-cutover v1 envelope prompt) is
    backed up to ``<path>.v<old>.bak`` and rewritten so the cutover actually
    reaches existing installs. ``identity.md`` / ``custom.md`` are never touched.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        AGENT_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Don't crash app start over a permissions glitch — log and bail.
        # Subsequent prompts_for_session calls will still try mkdir again.
        print(f"[agent_prompts] bootstrap mkdir failed: {exc}")
        return

    _seed_or_migrate(general_prompt_path(), _defaults.GENERAL_PROMPT_DEFAULT)
    _seed_or_migrate(orchestrator_prompt_path(), _defaults.ORCHESTRATOR_PROMPT_DEFAULT)
    _bootstrapped = True


def _agent_version_int() -> int:
    return int(_defaults.AGENT_PROMPT_VERSION[1:])


def _read_agent_version(path: Path) -> int | None:
    """Extract ``<!-- agent-prompt:vN -->`` integer from the file head, or None."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:200]
    except OSError:
        return None
    match = _AGENT_HEADER_RE.search(head)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (ValueError, TypeError):
        return None


def _seed_or_migrate(path: Path, content: str) -> None:
    """Write ``content`` when missing; back up + rewrite when stale; leave current.

    Mirrors ``orchestrator_prompts._render_with_migration``: a file whose header
    version is >= current is left alone (user edits to a current default
    persist); a missing/older/unparseable header is backed up to
    ``<path>.v<old>.bak`` (or ``.vunknown.bak``) before rewriting.
    """
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return
        on_disk = _read_agent_version(path)
        if on_disk is not None and on_disk >= _agent_version_int():
            return
        suffix = f".v{on_disk}.bak" if on_disk is not None else ".vunknown.bak"
        backup = path.with_name(path.name + suffix)
        n = 1
        while backup.exists():
            backup = path.with_name(path.name + suffix + f".{n}")
            n += 1
        path.rename(backup)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        # Same rationale as bootstrap: don't crash, just log. The runner
        # tolerates missing append-system-prompt-file targets silently.
        print(f"[agent_prompts] failed to seed/migrate {path}: {exc}")


def reset_bootstrap_for_tests() -> None:
    """Force ``bootstrap`` to re-run on next call; for unit tests that swap HOME."""
    global _bootstrapped
    _bootstrapped = False
