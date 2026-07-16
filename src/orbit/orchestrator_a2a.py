"""Agent-to-agent (A2A) message bus — per-agent maildirs + JSON envelopes.

Each Claude Code agent runs its OWN inbox listener (via the Claude Code
``Monitor`` tool); the dashboard is a SUPERVISOR/ROUTER that drops envelopes
into per-agent maildirs and, for an offline target, spawns its session so the
new mail gets drained (auto-revive). No tmux pane injection of message bodies.
Same-host only; loop/cost is best-effort (no hard guardrails).

This module is the dashboard-side writer/reader + maildir resolver — the
counterpart of :mod:`orchestrator_artifacts` for the bus. The on-disk maildir
layout + envelope shape is the ONLY coupling with the ``a2a`` CLI skill
(``skills/a2a/`` → ``~/.orchestrator/skills-registry/a2a/``); the ``agent_key``,
id format, atomic-write, and traversal guards are deliberately IDENTICAL in
both the Python module and the CLI lib so a message minted on one side resolves
on the other.

Reuses the ``_atomic_write_json`` + ``.resolve()``/``is_relative_to`` traversal
idioms from :mod:`orchestrator_artifacts` (copied locally, like that module
copies them from :mod:`orchestrator_uploads`). Import-safe: no I/O at import.
"""
from __future__ import annotations
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from .discovery import HOME

# Maildir root — one subdir per agent (keyed by agent_key(lib_id)).
A2A_ROOT = HOME / ".orchestrator" / "a2a"

# Matches the id the CLI mints: a2a-<utc compact>-<6 hex>. The compact UTC
# stamp is digits + 'T' (+ defensive 'Z'); reject anything else BEFORE any
# filesystem touch (defeats ``../`` / absolute-path traversal in id args).
A2A_ID_RE = re.compile(r"^a2a-[0-9TZ]+-[0-9a-f]{6}$")

# Envelope types the bus accepts.
ALLOWED_TYPES = frozenset({"message", "reply", "system"})

# Hard cap on payload.text (bytes, UTF-8 encoded). Keeps a single envelope
# small enough to drain cheaply and bounds maildir growth.
TEXT_MAX_BYTES = 16384

# Default time-to-live (seconds) stamped on a fresh envelope. Advisory only —
# the GC tick (owned elsewhere) sweeps stale mail; this module never expires.
DEFAULT_TTL = 86400

SCHEMA_VERSION = 1

# Sentinel agent_key for the global (no-lib_id) agent.
GLOBAL_KEY = "__global__"
# The literal lib_ids that map onto the global agent.
_GLOBAL_LIB_IDS = frozenset({"global", GLOBAL_KEY})

# A lib_id is "kind/name" under one of the PARA roots; same shape the maildir
# contract pins. No ".." segment, restricted name charset.
_LIB_ID_RE = re.compile(r"^(projects|areas|resources)/[A-Za-z0-9._/-]+$")

# A live session id is a uuid4 — the SAME shape orchestrator validates with
# (uploads_module.SESSION_ID_RE). Pinned here byte-identical so a `to_session`
# stamp / session-maildir path is rejected BEFORE any filesystem touch.
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Maildir subdirs (mirror the classic Maildir layout the CLI expects).
_MAILDIR_SUBDIRS = ("inbox", "tmp", "cur")

# Per-session maildir subdirs (no tmp — atomic writes use the agent-level tmp,
# which is on the same filesystem so os.replace stays atomic).
_SESSION_SUBDIRS = ("inbox", "cur")


def _warn(msg: str) -> None:
    print(f"[orchestrator_a2a] {msg}", file=sys.stderr)


# ── id + envelope io ──────────────────────────────────────────────


def new_id() -> str:
    """Mint a fresh message id matching the CLI format (``a2a-<utc>-<6hex>``)."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"a2a-{stamp}-{os.urandom(3).hex()}"


def _validate_id(msg_id: str) -> str:
    if not A2A_ID_RE.fullmatch(msg_id or ""):
        raise ValueError("invalid a2a id")
    return msg_id


def _atomic_write_json(path: Path, payload: object) -> None:
    """Atomic JSON write (tmp in the same dir + ``os.replace``).

    Copied from :func:`orchestrator_artifacts._atomic_write_json` so the bus
    has no cross-module write coupling.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        json.dump(payload, tmp, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_path, path)
    except Exception:
        try: tmp.close()
        except Exception: pass
        if tmp_path.exists():
            try: tmp_path.unlink()
            except OSError: pass
        raise


# ── agent_key + maildir resolution ────────────────────────────────


def agent_key(lib_id: str | None) -> str:
    """Map a lib_id to a filesystem-safe maildir key (per the A2A contract).

    - falsy / empty / whitespace-only lib_id → ``"__global__"``.
    - the literal ``"global"`` / ``"__global__"`` → ``"__global__"``.
    - otherwise require ``^(projects|areas|resources)/<name>$`` with NO ``..``
      segment, then key = ``lib_id.replace("/", "__")``.
    - anything else → :class:`ValueError`.
    """
    if lib_id is None:
        return GLOBAL_KEY
    raw = str(lib_id).strip()
    if not raw or raw in _GLOBAL_LIB_IDS:
        return GLOBAL_KEY
    if not _LIB_ID_RE.fullmatch(raw):
        raise ValueError(f"invalid lib_id: {lib_id!r}")
    # Belt-and-suspenders: the charset class above already excludes a literal
    # ".." path segment (a "." run between slashes / ends is fine, ".." is not).
    if any(seg == ".." for seg in raw.split("/")):
        raise ValueError(f"invalid lib_id (contains '..'): {lib_id!r}")
    return raw.replace("/", "__")


def maildir_for(lib_id: str | None) -> dict[str, Path]:
    """Resolve (and create) the maildir for an agent; return its subdir paths.

    Returns ``{"root", "inbox", "tmp", "cur"}`` Paths, all under
    ``A2A_ROOT/<agent_key>``. Creates the tree (mkdir -p). Final guard: the
    resolved root must stay inside ``A2A_ROOT`` (defeats a key that somehow
    escapes — it never should, but the resolve() check is cheap insurance).
    """
    key = agent_key(lib_id)
    root = A2A_ROOT / key
    # Resolve against the (resolved) root so a symlinked A2A_ROOT still anchors
    # the containment check; create the parent first so resolve() is stable.
    A2A_ROOT.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    if not resolved.is_relative_to(A2A_ROOT.resolve()):
        raise ValueError("a2a maildir escapes root")
    paths: dict[str, Path] = {"root": root}
    for sub in _MAILDIR_SUBDIRS:
        p = root / sub
        p.mkdir(parents=True, exist_ok=True)
        paths[sub] = p
    return paths


def _validate_session_id(session_id: str | None) -> str:
    """Validate a session id (uuid4 shape) before any path is built.

    Mirrors ``uploads_module.SESSION_ID_RE`` on the dashboard side / the CLI's
    own uuid regex. Raises :class:`ValueError` on anything that is not a bare
    8-4-4-4-12 lowercase-hex uuid (defeats ``../`` / absolute-path traversal in
    a ``session`` arg).
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid session id")
    return session_id


def session_maildir(lib_id: str | None, session_id: str) -> dict[str, Path]:
    """Resolve (and create) a per-session maildir under an agent's maildir.

    Returns ``{"root", "inbox", "cur", "tmp"}`` Paths. The session inbox/cur
    live at ``A2A_ROOT/<agent_key>/sessions/<session_id>/{inbox,cur}``; ``tmp``
    is the AGENT-level ``<agent_key>/tmp`` (same filesystem as the session
    inbox, so ``os.replace`` from tmp→inbox stays atomic). Validates the session
    id (uuid shape) and traversal-guards the resolved session root under
    ``A2A_ROOT`` before any mkdir.
    """
    _validate_session_id(session_id)
    agent = maildir_for(lib_id)  # creates the agent tree + guards the key
    sessions_root = agent["root"] / "sessions"
    root = sessions_root / session_id
    resolved = root.resolve()
    if not resolved.is_relative_to(A2A_ROOT.resolve()):
        raise ValueError("a2a session maildir escapes root")
    # Extra belt: the session dir must stay under THIS agent's sessions/ dir.
    if not resolved.is_relative_to(sessions_root.resolve()):
        raise ValueError("a2a session maildir escapes agent sessions dir")
    paths: dict[str, Path] = {"root": root, "tmp": agent["tmp"]}
    for sub in _SESSION_SUBDIRS:
        p = root / sub
        p.mkdir(parents=True, exist_ok=True)
        paths[sub] = p
    return paths


def _normalize_target(lib_id: str | None) -> str:
    """Normalize a from/to value to a canonical lib_id or ``"global"``.

    Validates via :func:`agent_key` (raises on a bad lib_id); the global agent
    is represented as the literal ``"global"`` in an envelope's from/to.
    """
    return "global" if agent_key(lib_id) == GLOBAL_KEY else str(lib_id).strip()


# ── envelope build + validate ─────────────────────────────────────


def _coerce_text(text: object) -> str:
    """Validate payload text: non-empty str within the byte cap."""
    if not isinstance(text, str):
        raise ValueError("payload.text must be a string")
    if not text.strip():
        raise ValueError("payload.text must be non-empty")
    if len(text.encode("utf-8")) > TEXT_MAX_BYTES:
        raise ValueError(f"payload.text exceeds {TEXT_MAX_BYTES} bytes")
    return text


def build_envelope(
    *,
    from_lib: str | None,
    to_lib: str | None,
    type: str = "message",
    text: str,
    correlation_id: str | None = None,
    reply_to: str | None = None,
    to_session: str | None = None,
) -> dict:
    """Mint a fresh, validated envelope dict.

    ``from_lib`` is supplied by the SERVER (derived from the caller session's
    sidecar lib_id) — never trusted from the client. Normalizes from/to to a
    canonical lib_id or ``"global"``, validates ``type`` + ``text``, and stamps
    ``id`` / ``ts`` / ``hops`` / ``schema_version`` / ``ttl``. ``to_session`` (a
    uuid-shaped session id) is optional — when set the message targets a SPECIFIC
    live session of ``to_lib``; ``None`` (default) is agent-level delivery.
    """
    if type not in ALLOWED_TYPES:
        raise ValueError(f"invalid type: {type!r}")
    body = _coerce_text(text)
    reply_to_norm = (
        _normalize_target(reply_to)
        if reply_to is not None and str(reply_to).strip()
        else None
    )
    corr = correlation_id.strip() if isinstance(correlation_id, str) and correlation_id.strip() else None
    if corr is not None and not A2A_ID_RE.fullmatch(corr):
        raise ValueError("invalid correlation_id")
    to_session_norm = (
        _validate_session_id(to_session)
        if to_session is not None and str(to_session).strip()
        else None
    )
    envelope = {
        "id": new_id(),
        "from": _normalize_target(from_lib),
        "to": _normalize_target(to_lib),
        "to_session": to_session_norm,
        "type": type,
        "correlation_id": corr,
        "reply_to": reply_to_norm,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": DEFAULT_TTL,
        "schema_version": SCHEMA_VERSION,
        "hops": 0,
        "payload": {"text": body, "meta": {}},
    }
    # Defensive round-trip through the validator so build + read share one
    # definition of "valid" (and a future build bug surfaces here, not on read).
    return validate_envelope(envelope)


def validate_envelope(d: object) -> dict:
    """Validate a loaded/built envelope; return a normalized copy or raise.

    Enforces: ``id`` matches the id regex; ``type`` in the allowed set;
    ``from``/``to`` are valid lib_ids or ``"global"``; ``payload.text`` is a
    non-empty str within the byte cap. Other fields are coerced to safe shapes
    (ints clamped, missing optionals → ``None``/defaults).
    """
    if not isinstance(d, dict):
        raise ValueError("envelope must be an object")
    msg_id = d.get("id")
    if not isinstance(msg_id, str) or not A2A_ID_RE.fullmatch(msg_id):
        raise ValueError("invalid envelope id")
    mtype = d.get("type")
    if mtype not in ALLOWED_TYPES:
        raise ValueError(f"invalid envelope type: {mtype!r}")
    # from/to: normalize_target raises on a bad lib_id, accepts 'global'.
    from_norm = _normalize_target(d.get("from"))
    to_norm = _normalize_target(d.get("to"))
    # to_session: null (agent-level) or a uuid-shaped session id.
    to_session = d.get("to_session")
    to_session_norm: str | None = None
    if to_session is not None and str(to_session).strip():
        to_session_norm = _validate_session_id(to_session)
    payload = d.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("envelope payload must be an object")
    text = _coerce_text(payload.get("text"))
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    corr = d.get("correlation_id")
    if corr is not None:
        if not isinstance(corr, str) or not A2A_ID_RE.fullmatch(corr):
            raise ValueError("invalid correlation_id")
    reply_to = d.get("reply_to")
    reply_to_norm: str | None = None
    if reply_to is not None and str(reply_to).strip():
        reply_to_norm = _normalize_target(reply_to)
    ts = d.get("ts")
    if not isinstance(ts, str) or not ts.strip():
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ttl_raw = d.get("ttl")
    try:
        ttl = int(ttl_raw)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    if ttl < 0:
        ttl = DEFAULT_TTL
    hops_raw = d.get("hops")
    try:
        hops = int(hops_raw)
    except (TypeError, ValueError):
        hops = 0
    if hops < 0:
        hops = 0
    return {
        "id": msg_id,
        "from": from_norm,
        "to": to_norm,
        "to_session": to_session_norm,
        "type": mtype,
        "correlation_id": corr,
        "reply_to": reply_to_norm,
        "ts": ts,
        "ttl": ttl,
        "schema_version": SCHEMA_VERSION,
        "hops": hops,
        "payload": {"text": text, "meta": meta},
    }


# ── enqueue + listing ─────────────────────────────────────────────


def enqueue(envelope: dict, *, session: str | None = None) -> Path:
    """Validate + atomically write an envelope into the target's ``inbox/``.

    Routes by the envelope's ``to`` (a lib_id or ``"global"``). When ``session``
    is given (or the envelope carries a ``to_session``, which is preferred),
    the mail lands in that session's per-session inbox
    (``<key>/sessions/<session>/inbox/<id>.json``, tmp = the agent-level
    ``<key>/tmp``); otherwise the agent-level inbox. Returns the written
    ``inbox/<id>.json`` path. Re-validates first so a hand-built or tampered
    dict can't land malformed mail.
    """
    valid = validate_envelope(envelope)
    # The envelope's own to_session wins (it's already validated); fall back to
    # the explicit kwarg. Either way the value is uuid-validated downstream.
    target_session = valid.get("to_session") or session
    if target_session is not None and str(target_session).strip():
        dirs = session_maildir(valid["to"], str(target_session))
    else:
        dirs = maildir_for(valid["to"])
    inbox = dirs["inbox"]
    target = (inbox / f"{valid['id']}.json").resolve()
    if not target.is_relative_to(inbox.resolve()):
        raise ValueError("a2a inbox path escapes maildir")
    # Atomic-write via the agent-level tmp (same filesystem → os.replace atomic);
    # _atomic_write_json already uses a tmp in target.parent, which for a session
    # inbox is the session inbox itself — also same fs, so the contract holds.
    _atomic_write_json(target, valid)
    return target


def inbox_has(lib_id: str | None, msg_id: str) -> bool:
    """True if ``<id>.json`` already sits in the agent's ``inbox/`` or ``cur/``.

    Used by the send route to dedup a re-delivery (the in-mem LRU is the fast
    path; this catches a restart-spanning resend). Traversal-guarded via
    :func:`_validate_id` before any path is built.
    """
    _validate_id(msg_id)
    try:
        dirs = maildir_for(lib_id)
    except ValueError:
        return False
    name = f"{msg_id}.json"
    for sub in ("inbox", "cur"):
        if (dirs[sub] / name).is_file():
            return True
    return False


def list_inbox(lib_id: str | None) -> list[Path]:
    """List the agent's ``inbox/*.json`` paths, oldest-first by id.

    The id's leading compact-UTC stamp makes a lexical sort chronological, so
    a plain ``sorted`` on the filename yields oldest-first (the drain order).
    Skips names that don't match the id regex (defensive against junk files).
    """
    try:
        dirs = maildir_for(lib_id)
    except ValueError:
        return []
    inbox = dirs["inbox"]
    if not inbox.is_dir():
        return []
    out: list[Path] = []
    try:
        scanner = os.scandir(inbox)
    except OSError as e:
        _warn(f"scandir failed for {inbox}: {e}")
        return []
    with scanner as it:
        for entry in it:
            if not entry.name.endswith(".json") or not entry.is_file():
                continue
            if not A2A_ID_RE.fullmatch(entry.name[:-5]):
                continue
            out.append(Path(entry.path))
    out.sort(key=lambda p: p.name)
    return out


def read_message(lib_id: str | None, msg_id: str) -> dict | None:
    """Read + validate one envelope by id, CLAIM-then-READ for dup-free drain.

    Mirrors the CLI's concurrent-safe path: FIRST try to CLAIM the message by
    ``os.replace(inbox/<id>.json → cur/<id>.json)`` — if it succeeds THIS caller
    won the race and reads from ``cur/``; if it raises ``FileNotFoundError`` the
    message was never in ``inbox/`` (already claimed by another drainer, or only
    ever in ``cur/``), so we fall through to read the existing ``cur/`` copy.
    Returns the normalized envelope, or ``None`` if absent/corrupt.
    """
    _validate_id(msg_id)
    try:
        dirs = maildir_for(lib_id)
    except ValueError:
        return None
    name = f"{msg_id}.json"
    cur_path = dirs["cur"] / name
    src = dirs["inbox"] / name
    dst = cur_path.resolve()
    if not dst.is_relative_to(dirs["cur"].resolve()):
        raise ValueError("a2a cur path escapes maildir")
    # CLAIM: move inbox→cur first. Success means we own the read; a missing
    # source means another drainer claimed it (or it was only ever in cur/).
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        pass
    except OSError as e:
        _warn(f"claim failed for {src} → {dst}: {e}")
        # Fall through — the cur/ copy may still be readable.
    if not cur_path.is_file():
        return None
    try:
        with cur_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        _warn(f"unreadable {cur_path}: {e}")
        return None
    try:
        return validate_envelope(raw)
    except ValueError as e:
        _warn(f"invalid envelope {cur_path}: {e}")
        return None


def mark_read(lib_id: str | None, msg_id: str) -> bool:
    """CLAIM ``inbox/<id>.json`` → ``cur/<id>.json`` (drain). Idempotent.

    Returns True if THIS caller claimed the message (the move happened); False
    if it wasn't in ``inbox/`` (already claimed/drained, or never delivered).
    The CLAIM is a single ``os.replace`` — the atomic primitive that guarantees
    exactly-one consumer across concurrent drains (the CLI uses the same).
    """
    _validate_id(msg_id)
    try:
        dirs = maildir_for(lib_id)
    except ValueError:
        return False
    name = f"{msg_id}.json"
    src = dirs["inbox"] / name
    dst = (dirs["cur"] / name).resolve()
    if not dst.is_relative_to(dirs["cur"].resolve()):
        raise ValueError("a2a cur path escapes maildir")
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        return False
    except OSError as e:
        _warn(f"failed to drain {src} → {dst}: {e}")
        return False
    return True


# ── agent directory ───────────────────────────────────────────────


def _humanize_slug(slug: str) -> str:
    """kebab/snake/camel → 'Title Case' — mirrors orchestrator._humanize_slug.

    Kept self-contained so this module stays import-safe (importing
    orchestrator.py would be a circular import and pulls in FastAPI).
    """
    s = re.sub(r"[-_]+", " ", slug or "")
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return " ".join(w[:1].upper() + w[1:] for w in s.split() if w)


def _agent_name_for(lib_id: str | None) -> str:
    """Human agent label from a lib_id — mirrors orchestrator._agent_name_for.

    ``areas/Work`` → "Work"; ``projects/my-project`` → "My Project";
    global / unresolvable → "Global".
    """
    if not lib_id:
        return "Global"
    m = re.match(r"^(areas|projects|resources)/(.+)$", lib_id)
    raw = m.group(2).split("/")[-1] if m else lib_id
    return _humanize_slug(raw) or "Global"


# lib_id kinds are lowercase but the on-disk PARA roots are Capitalized. A naive
# ``HOME/kind/rest`` join yields the WRONG path — map the kind explicitly.
_PARA_ROOTS = {"areas": "Areas", "projects": "Projects", "resources": "Resources"}


def _para_dir_for(lib_id: str | None) -> str:
    """Absolute PARA directory an agent lives in — for cross-agent fs reads.

    ``areas/Home`` → ``~/Areas/Home``; ``projects/my-project`` →
    ``~/Projects/my-project``; ``resources/x`` → ``~/Resources/x``. The
    ``global`` agent (and any ``None`` / unparseable lib_id) is home-rooted →
    ``str(HOME)`` (i.e. ``~/``). Import-safe: no orchestrator.py import.
    """
    if not lib_id:
        return str(HOME)
    m = re.match(r"^(areas|projects|resources)/(.+)$", str(lib_id).strip())
    if not m:
        return str(HOME)
    return str(HOME / _PARA_ROOTS[m.group(1)] / m.group(2))


def _identity_for(lib_id: str | None, *, full: bool = False) -> str:
    """Read an agent's ``identity.md`` summary from its on-disk agent dir.

    Path (inlined to stay import-cycle-free; mirrors
    ``agent_prompts.agent_identity_path``):
    ``HOME/.orchestrator/agents/<kind>/<rest>/identity.md``. When ``full`` is
    False, return the FIRST prose paragraph — a leading ``# heading`` line is
    stripped, then text is accumulated up to the first blank line. When ``full``
    is True, return the whole file verbatim. Returns ``""`` for a missing /
    unreadable file, a bad / global lib_id.
    """
    if not lib_id:
        return ""
    m = re.match(r"^(areas|projects|resources)/(.+)$", str(lib_id).strip())
    if not m:
        return ""
    path = HOME / ".orchestrator" / "agents" / m.group(1) / m.group(2) / "identity.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    if full:
        return content
    # First paragraph: skip leading blanks + a leading markdown heading, then
    # accumulate non-blank lines until the first blank line.
    para: list[str] = []
    started = False
    for line in content.splitlines():
        stripped = line.strip()
        if not started:
            if not stripped or stripped.startswith("#"):
                continue
            started = True
            para.append(stripped)
        else:
            if not stripped:
                break
            para.append(stripped)
    return " ".join(para).strip()


def list_agents(live_session_ids: set[str]) -> list[dict]:
    """Directory of every known agent, reconciled against live sessions.

    SYNC + import-safe + unit-testable: the live-session set is a PARAMETER
    (the route computes it from the async pool), so this never touches global
    pool state. Groups :func:`jsonl_mod.list_sessions` by sidecar lib_id (the
    same grouping the /agents directory uses), annotates each group with:

      - ``lib_id``    — the agent key (``"global"`` for the no-lib_id agent),
      - ``name``      — human label (``_agent_name_for``),
      - ``description``— first paragraph of the agent's ``identity.md`` (``""``
        when none / global),
      - ``dir``       — the agent's absolute PARA directory (``~/Areas/<x>`` …;
        ``~/`` for global) so a peer can read its files directly,
      - ``warm``      — any session in the group is live,
      - ``session_id``— the most-recently-active LIVE session in the group
        (falls back to the most-recently-active session of any state when the
        agent is cold),
      - ``last_active``— that session's ``updated_at`` (float epoch seconds),
      - ``sessions``  — ALL LIVE sessions for the agent, each
        ``{"session_id": str, "last_active": float, "title": str,
        "transcript": str}`` (the session's display title + absolute ``.jsonl``
        transcript path), sorted by ``last_active`` descending (so a human / the
        CLI can pick one for ``--session`` or read its transcript). Empty list
        for a cold agent.

    The global agent is ALWAYS included (even with zero sessions) so a sender
    can address ``global`` before it has ever run. Sorted by ``last_active``
    descending, with the global agent kept in its natural position.
    """
    try:
        summaries = jsonl_mod.list_sessions()
    except Exception as exc:  # noqa: BLE001 — directory never breaks on a read error
        _warn(f"list_sessions failed: {exc}")
        summaries = []
    try:
        overlay = meta_mod.all_meta()
    except Exception as exc:  # noqa: BLE001
        _warn(f"all_meta failed: {exc}")
        overlay = {}

    # group_key → accumulator. Seed the global agent so it always appears.
    # ``_live`` is a private staging list of {session_id,last_active} for every
    # LIVE session in the group; finalized into the public ``sessions`` field
    # (sorted desc) after accumulation.
    groups: dict[str, dict] = {
        "global": {
            "lib_id": "global",
            "name": "Global",
            "description": _identity_for("global"),
            "dir": _para_dir_for("global"),
            "warm": False,
            "session_id": None,
            "last_active": 0.0,
            "_live": [],
        }
    }

    # sid → its summary, so the finalize loop can resolve per-session titles.
    summ_by_sid: dict[str, dict] = {}

    for summ in summaries:
        sid = summ.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        summ_by_sid[sid] = summ
        meta = overlay.get(sid) if isinstance(overlay, dict) else None
        raw_lib = meta.get("lib_id") if isinstance(meta, dict) else None
        lib_id = raw_lib if isinstance(raw_lib, str) and raw_lib.strip() else None
        # Normalize to the canonical group key: a valid lib_id stays itself,
        # everything else collapses to the global agent.
        try:
            key = "global" if agent_key(lib_id) == GLOBAL_KEY else lib_id
        except ValueError:
            key = "global"
        try:
            updated = float(summ.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated = 0.0
        is_live = sid in live_session_ids

        group = groups.get(key)
        if group is None:
            group = {
                "lib_id": key,
                "name": _agent_name_for(None if key == "global" else key),
                "description": _identity_for(key),
                "dir": _para_dir_for(key),
                "warm": False,
                "session_id": None,
                "last_active": 0.0,
                "_live": [],
            }
            groups[key] = group
        if is_live:
            group["warm"] = True
            group["_live"].append({"session_id": sid, "last_active": updated})
        # Most-recently-active session represents the agent (any state — kept for
        # back-compat; the per-session targeting list below is LIVE-only).
        if group["session_id"] is None or updated > group["last_active"]:
            group["session_id"] = sid
            group["last_active"] = updated

    # Finalize: replace the private staging list with the public, sorted
    # ``sessions`` field (all LIVE sessions, most-recent first), enriching each
    # with its display title + absolute transcript path so a peer can read it.
    ov = overlay if isinstance(overlay, dict) else {}
    for group in groups.values():
        live_sessions = group.pop("_live", [])
        live_sessions.sort(key=lambda s: s.get("last_active") or 0.0, reverse=True)
        for s in live_sessions:
            sid = s["session_id"]
            s["title"] = meta_mod.resolve_title(ov.get(sid), summ_by_sid.get(sid))
            s["transcript"] = str(jsonl_mod.jsonl_path(sid))
        group["sessions"] = live_sessions

    agents = list(groups.values())
    agents.sort(key=lambda a: a.get("last_active") or 0.0, reverse=True)
    return agents


def whois(lib_id: str | None, live_session_ids: set[str]) -> dict:
    """Full directory record for a SINGLE agent (the ``whois`` endpoint).

    Unlike :func:`list_agents` (whose ``sessions`` is LIVE-only for
    back-compat), ``whois`` returns EVERY session of the agent — live and cold —
    each tagged with a ``live`` flag, its display title, ``last_active`` and its
    absolute ``.jsonl`` transcript path, sorted most-recent first. Also returns
    the FULL ``identity.md`` (not just the first paragraph) + the agent's PARA
    ``dir``. An unknown-but-valid lib_id still yields a well-formed record (its
    ``dir`` + an empty ``sessions`` list) so a sender can address a cold agent.
    """
    # Normalize to the canonical group key. A bad lib_id (unknown kind) still
    # returns a best-effort not-found record rather than raising into the route.
    try:
        normalized = "global" if agent_key(lib_id) == GLOBAL_KEY else str(lib_id).strip()
    except ValueError:
        raw = str(lib_id).strip() if lib_id else "global"
        return {
            "lib_id": raw,
            "name": _agent_name_for(None),
            "dir": _para_dir_for(raw),
            "identity": "",
            "warm": False,
            "sessions": [],
        }

    try:
        summaries = jsonl_mod.list_sessions()
    except Exception as exc:  # noqa: BLE001 — never break on a read error
        _warn(f"list_sessions failed: {exc}")
        summaries = []
    try:
        overlay = meta_mod.all_meta()
    except Exception as exc:  # noqa: BLE001
        _warn(f"all_meta failed: {exc}")
        overlay = {}
    ov = overlay if isinstance(overlay, dict) else {}

    sessions: list[dict] = []
    warm = False
    for summ in summaries:
        sid = summ.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        meta = ov.get(sid)
        raw_lib = meta.get("lib_id") if isinstance(meta, dict) else None
        s_lib = raw_lib if isinstance(raw_lib, str) and raw_lib.strip() else None
        try:
            s_key = "global" if agent_key(s_lib) == GLOBAL_KEY else s_lib
        except ValueError:
            s_key = "global"
        if s_key != normalized:
            continue
        try:
            updated = float(summ.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated = 0.0
        live = sid in live_session_ids
        if live:
            warm = True
        sessions.append({
            "session_id": sid,
            "title": meta_mod.resolve_title(meta, summ),
            "last_active": updated,
            "live": live,
            "transcript": str(jsonl_mod.jsonl_path(sid)),
        })
    sessions.sort(key=lambda s: s.get("last_active") or 0.0, reverse=True)

    dir_key = None if normalized == "global" else normalized
    return {
        "lib_id": normalized,
        "name": _agent_name_for(dir_key),
        "dir": _para_dir_for(dir_key),
        "identity": _identity_for(dir_key, full=True),
        "warm": warm,
        "sessions": sessions,
    }
