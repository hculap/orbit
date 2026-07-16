"""Session teleport — export/import a Claude Code session as a portable file.

Issue #91. "Teleport" moves a session as a self-contained **file**:

* EXPORT (GET, download) → a JSON envelope bundling the full transcript plus
  provenance (source id, cwd, lib_id, model, title).
* IMPORT (POST, upload) → "plug" that envelope into a chosen agent: mint a
  fresh uuid, **rewrite the absolute paths** (every line's ``cwd`` → the target
  agent's dir, ``sessionId`` → the new uuid) and write a resumable ``.jsonl``
  under the target agent's cwd-slug directory.

The new session is **unsaved by default** (no manual title unless one is
passed) and the target **agent must be specified** at import time — both per
the issue. The endpoint is skill-driven: the bundled ``teleport`` skill does
the GET then the token-gated POST.

JSONL stays the single source of truth; the only sidecar touch is a cosmetic
``teleported_from`` provenance stamp (mirrors compact's ``compacted_from``).
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import time
import uuid as uuid_mod
from pathlib import Path

from . import orchestrator_artifacts as artifacts_mod
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from .discovery import HOME

ENVELOPE_VERSION = 1
ENVELOPE_KIND = "hetzner-session-teleport"

# Permissive id guard: real ids are uuid4, but we only need to forbid path
# traversal / separators since the id becomes a filename + a slug-dir lookup.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# Claude Code derives a project's slug by replacing EVERY non-alphanumeric
# character with ``-`` (not just ``/``): ``/home/user/.orchestrator`` becomes
# ``-home-user--orchestrator`` (note the doubled dash from ``/.``). We MUST
# match it exactly — the dashboard finds the file by scanning all slug dirs, but
# ``claude --resume`` recomputes the slug from the cwd, so a teleported session
# only resumes if it was written to the dir claude will look in.
_SLUG_NONALNUM = re.compile(r"[^a-zA-Z0-9]")


def cwd_to_slug(cwd: str | Path) -> str:
    """Encode an absolute cwd to its ``~/.claude/projects`` slug, matching Claude.

    Every non-alphanumeric char → ``-`` (case preserved): ``/home/user`` →
    ``-home-user``; ``/home/user/my.proj`` → ``-home-user-my-proj``.
    """
    return _SLUG_NONALNUM.sub("-", str(cwd))


def _safe_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _ID_RE.match(session_id):
        raise ValueError("invalid session id")
    return session_id


def _slug_files(session_id: str) -> list[Path]:
    """Every ``<slug>/<session_id>.jsonl`` across all project slugs.

    Largest first so the canonical transcript's ordering wins the merge and a
    tiny stub under a second cwd-slug only contributes unique stray lines.
    """
    root = jsonl_mod._PROJECTS_ROOT
    out: list[Path] = []
    if root.is_dir():
        for slug_dir in root.iterdir():
            if not slug_dir.is_dir():
                continue
            cand = slug_dir / f"{session_id}.jsonl"
            try:
                if cand.is_file():
                    out.append(cand)
            except OSError:
                continue
    # Largest first; break size ties by dir name so the merge is deterministic
    # across filesystems (iterdir order is arbitrary).
    out.sort(key=lambda p: (-p.stat().st_size, p.parent.name))
    return out


def collect_raw_lines(session_id: str) -> list[dict]:
    """Parse + merge + de-dupe the session's JSONL lines, ordered.

    Lines with a ``uuid`` de-dupe on it (keep first seen); header-ish lines
    without one (e.g. ``{"type":"mode"}``) de-dupe on their serialized form.
    Raises ``FileNotFoundError`` when the session has no transcript anywhere.
    """
    _safe_id(session_id)
    files = _slug_files(session_id)
    if not files:
        raise FileNotFoundError(session_id)
    seen_uuid: set[str] = set()
    seen_meta: set[str] = set()
    lines: list[dict] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            u = obj.get("uuid")
            if isinstance(u, str) and u:
                if u in seen_uuid:
                    continue
                seen_uuid.add(u)
            else:
                key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
                if key in seen_meta:
                    continue
                seen_meta.add(key)
            lines.append(obj)
    if not lines:
        raise FileNotFoundError(session_id)
    return lines


def _cwd_from_lines(lines: list[dict]) -> str | None:
    for obj in lines:
        cwd = obj.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _branch_from_lines(lines: list[dict]) -> str | None:
    for obj in lines:
        br = obj.get("gitBranch")
        if isinstance(br, str) and br:
            return br
    return None


def export_session(session_id: str) -> dict:
    """Build a self-contained teleport envelope for ``session_id``.

    Raises ``ValueError`` for an unsafe id and ``FileNotFoundError`` when the
    session doesn't exist (the route maps these to 400 / 404).
    """
    lines = collect_raw_lines(session_id)
    meta = meta_mod.get_meta(session_id)
    src_cwd = meta.get("cwd") or _cwd_from_lines(lines) or str(HOME)
    return {
        "version": ENVELOPE_VERSION,
        "kind": ENVELOPE_KIND,
        "source_session_id": session_id,
        "source_cwd": src_cwd,
        "source_lib_id": meta.get("lib_id"),
        "model": meta.get("model"),
        "title": meta.get("title"),
        "git_branch": _branch_from_lines(lines),
        "exported_at": time.time(),
        "msg_count": len(lines),
        "transcript": lines,
    }


def _validate_envelope(env: object) -> None:
    if not isinstance(env, dict):
        raise ValueError("envelope must be an object")
    if env.get("version") != ENVELOPE_VERSION:
        raise ValueError(f"unsupported envelope version: {env.get('version')!r}")
    if env.get("kind") != ENVELOPE_KIND:
        raise ValueError("not a teleport envelope")
    transcript = env.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("envelope transcript must be a non-empty list")
    if not all(isinstance(x, dict) for x in transcript):
        raise ValueError("transcript lines must be objects")


def _validate_model(model: str | None) -> str | None:
    if model is None or model == "":
        return None
    if model not in meta_mod.ALLOWED_MODELS:
        raise ValueError(f"invalid model: {model!r}")
    return model


def _resolve_target_cwd(lib_id: str | None) -> Path:
    """Target agent cwd. Empty/None lib_id → global ($HOME)."""
    if not lib_id or not str(lib_id).strip():
        return HOME
    cwd = artifacts_mod._lib_id_to_cwd(lib_id)
    if cwd is None:
        raise ValueError(f"unknown agent lib_id: {lib_id!r}")
    return cwd


def _rewrite_line(obj: dict, *, new_id: str, cwd: str) -> dict:
    """Immutable copy with the session id + cwd substituted (paths plugged in)."""
    out = dict(obj)
    if "sessionId" in out:
        out["sessionId"] = new_id
    if "cwd" in out:
        out["cwd"] = cwd
    return out


def write_imported(transcript: list[dict], *, target_cwd: Path, new_id: str) -> tuple[Path, int]:
    """Write the path-substituted transcript as a resumable JSONL. Sync."""
    slug = cwd_to_slug(target_cwd)
    dest_dir = jsonl_mod._PROJECTS_ROOT / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{new_id}.jsonl"
    cwd_str = str(target_cwd)
    with dest.open("w", encoding="utf-8") as fh:
        for obj in transcript:
            line = _rewrite_line(obj, new_id=new_id, cwd=cwd_str)
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return dest, len(transcript)


async def import_session(
    envelope: object,
    *,
    lib_id: str | None,
    title: str | None = None,
    model: str | None = None,
    new_id: str | None = None,
) -> dict:
    """Plug a teleport envelope into ``lib_id``, minting a fresh resumable session.

    Raises ``ValueError`` (→ 400) for a malformed envelope / unknown agent /
    bad model.
    """
    _validate_envelope(envelope)
    assert isinstance(envelope, dict)  # narrowed by _validate_envelope
    norm_model = _validate_model(model)
    target_cwd = _resolve_target_cwd(lib_id)
    nid = _safe_id(new_id) if new_id else str(uuid_mod.uuid4())

    dest, n_lines = await asyncio.to_thread(
        write_imported, envelope["transcript"], target_cwd=target_cwd, new_id=nid
    )

    source_id = envelope.get("source_session_id") or ""
    meta_kwargs: dict = {
        "lib_id": lib_id or "",
        "cwd": str(target_cwd),
        "model": norm_model or "",
        "teleported_from": source_id or "",
    }
    if title:
        meta_kwargs["title"] = title
        meta_kwargs["title_manual"] = True
    await meta_mod.set_meta(nid, **meta_kwargs)
    jsonl_mod.invalidate_cache()

    return {
        "ok": True,
        "new_session_id": nid,
        "cwd": str(target_cwd),
        "lib_id": lib_id or None,
        "n_lines": n_lines,
        "teleported_from": source_id or None,
        "path": str(dest),
    }


# ── skill distribution (install on a LOCAL agent) ──────────────────
#
# The teleport skill is also useful from a *remote* machine (a laptop's local
# Claude Code) to pull/push sessions over the tailnet. We serve the skill as a
# tarball + a markdown install doc so a local agent can self-install it.

# Repo-bundled skill dir (same root bundled_skills.py seeds from on boot).
SKILL_NAME = "hetzner-teleport"
SKILL_SRC_DIR = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME


def build_skill_tarball() -> bytes:
    """Package the bundled teleport skill as a gzipped tar.

    Top-level member is ``teleport/`` so ``tar xz -C ~/.claude/skills/`` lands
    it at ``~/.claude/skills/teleport/``. Excludes the live ``config.json`` (it
    may hold a token / runtime state) — ships ``config.json.example`` only.
    """
    if not SKILL_SRC_DIR.is_dir():
        raise FileNotFoundError("teleport skill source dir missing")
    buf = io.BytesIO()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        base = Path(info.name).name
        if base == "config.json" or base.startswith("."):
            return None
        if "__pycache__" in parts or base.endswith(".pyc"):
            return None
        return info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(SKILL_SRC_DIR), arcname=SKILL_NAME, filter=_filter)
    return buf.getvalue()


def install_prompt(base_url: str) -> str:
    """The short, paste-to-agent instruction Settings offers (acts, not reads)."""
    base = base_url.rstrip("/")
    return (
        f"Przeczytaj i zainstaluj skill `hetzner-teleport` opisany pod "
        f"{base}/api/orchestrator/teleport/install — wykonaj kroki instalacji. "
        f"Jeśli skill jest już zainstalowany, zaktualizuj go (nadpisz pliki nową wersją)."
    )


def install_doc(base_url: str) -> str:
    """Markdown a local agent FETCHES (via the install prompt) to self-install."""
    base = base_url.rstrip("/")
    return f"""# hetzner-teleport — zainstaluj/zaktualizuj skill (działaj, nie tylko czytaj)

Jesteś agentem, który ma **zainstalować lub zaktualizować** ten skill na TYM
komputerze. Wykonaj poniższe kroki teraz.

Skill przenosi sesje Claude Code między dashboardem `{base}` a lokalnym Claude.

## 1. Pobierz / zaktualizuj pliki skilla

```bash
mkdir -p ~/.claude/skills
# nadpisuje istniejącą instalację (= update):
curl -fsSL {base}/api/orchestrator/teleport/skill.tar.gz | tar xz -C ~/.claude/skills/
```

## 2. Skonfiguruj adres + token (raz)

Token: **Settings → Serwer → Teleport** na dashboardzie (osobne pole *Kopiuj*).
Zachowaj istniejący `config.json` jeśli już jest poprawny; w przeciwnym razie:

```bash
cat > ~/.claude/skills/hetzner-teleport/config.json <<'EOF'
{{"dashboard_url": "{base}", "artifact_token": "WKLEJ_TOKEN_Z_SETTINGS"}}
EOF
```

## 3. Jak używać (komenda `/hetzner-teleport`)

Kierunek wykrywasz z argumentu — **nie pytaj zbędnie**:

```bash
CLI=~/.claude/skills/hetzner-teleport/scripts/teleport_cli.py

# IMPORT (server → tu): URL lub UUID sesji → zapis do BIEŻĄCEGO projektu lokalnego
python3 "$CLI" import {base}/chat/<UUID> --title "opcjonalna nazwa"

# EXPORT (tu → server): nazwa agenta/projektu → wypchnij BIEŻĄCĄ lokalną sesję
python3 "$CLI" export projects/<nazwa> --title "opcjonalna nazwa"
python3 "$CLI" export areas/Home
python3 "$CLI" export global
```

> IMPORT pobiera (GET, bez tokenu) i tworzy lokalną wznawialną sesję
> (`claude --resume <id>`). EXPORT wgrywa (POST, wymaga tokenu z config.json).
> Jeśli masz starą wersję skilla pod `~/.claude/skills/teleport/` — usuń ją,
> ta zastępuje ją jako `hetzner-teleport`.
"""
