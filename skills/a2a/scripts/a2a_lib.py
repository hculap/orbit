#!/usr/bin/env python3
"""
a2a_lib.py — importable, stdlib-only API for the `a2a` (agent-to-agent) CLI.

A2A is a same-host mailbox bus between Claude Code agents running under the
orbit. Each agent owns a maildir under ``~/.orchestrator/a2a/<key>/``
(inbox/ tmp/ cur/); the dashboard SERVER is the only writer of inbox envelopes
(a /send routes PURELY into the target's inbox/ and returns — no revive, no
push; the target's human drains it later). This client lib only:

  - resolves env / config (dashboard URL, token, this agent's lib_id / session),
  - reads + drains THIS agent's own inbox (move inbox/<id>.json → cur/<id>.json),
  - POSTs a /send and GETs the /agents + /whois directories over HTTP.

This module is import-safe: importing it has NO side effects (no I/O, no env
reads at import time). All discovery happens lazily inside functions. The
``agent_key`` / id / atomic-write helpers are kept byte-for-byte in step with the
dashboard-side Python contract so both sides resolve the SAME maildir.

Config resolution for the dashboard URL (first match wins):
    1. $HD_NOTIFY_URL
    2. <skill_dir>/config.json  "dashboard_url"
    3. http://localhost:8766

Auth token (the SAME token the artifacts skill uses):
    HD_ARTIFACT_TOKEN_FILE → ~/.orchestrator/artifact_token
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent

# Maildir root — IDENTICAL to the dashboard-side A2A_ROOT.
A2A_ROOT = Path.home() / ".orchestrator" / "a2a"
MAILDIR_SUBDIRS = ("inbox", "tmp", "cur")

GLOBAL_KEY = "__global__"

# A valid PARA lib_id: projects/… | areas/… | resources/… (no ".." segment).
LIB_ID_RE = re.compile(r"^(projects|areas|resources)/[A-Za-z0-9._/-]+$")

# Message id: a2a-<utc compact>-<6 lowercase hex> (mirrors artifacts ID_RE).
ID_RE = re.compile(r"^a2a-[0-9TZ]+-[0-9a-f]{6}$")

# Orchestrator session id: uuid4 (the SAME shape uploads_module.SESSION_ID_RE
# validates dashboard-side). Used to resolve a per-session sub-maildir.
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

MESSAGE_TYPES = ("message", "reply", "system")
TEXT_MAX_BYTES = 16384

SEND_TIMEOUT_S = 10
AGENTS_TIMEOUT_S = 10

DEFAULT_DASHBOARD_URL = "http://localhost:8766"
SEND_PATH = "/api/orchestrator/a2a/send"
AGENTS_PATH = "/api/orchestrator/a2a/agents"
WHOIS_PATH = "/api/orchestrator/a2a/whois"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class A2AError(Exception):
    """Raised for any user-facing A2A failure (bad input, missing target…)."""


# ---------------------------------------------------------------------------
# Time / id helpers (mirror artifacts_lib)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with explicit offset."""
    return datetime.now(timezone.utc).isoformat()


def _utc_compact() -> str:
    """Current UTC time as a compact YYYYMMDDTHHMMSS stamp (no separators/zone)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def new_id() -> str:
    """Generate a fresh message id: a2a-<utc compact>-<6 lowercase hex>."""
    return f"a2a-{_utc_compact()}-{secrets.token_hex(3)}"


def validate_id(msg_id: str) -> str:
    """Validate a message id against the strict regex before any FS op.

    Rejects path traversal (../), absolute paths and anything that is not the
    exact ``a2a-<digits/T/Z>-<6 hex>`` shape. Returns the id on success.
    """
    if not isinstance(msg_id, str) or not ID_RE.match(msg_id):
        raise A2AError(f"invalid message id {msg_id!r}: must match {ID_RE.pattern}")
    return msg_id


# ---------------------------------------------------------------------------
# agent_key / maildir (IDENTICAL to the dashboard-side contract)
# ---------------------------------------------------------------------------


def agent_key(lib_id: str | None) -> str:
    """Map a PARA lib_id to its on-disk maildir key.

    - falsy / empty / whitespace lib_id           → ``__global__``
    - literal "global" / "__global__"             → ``__global__``
    - ``(projects|areas|resources)/<path>``        → ``lib_id.replace("/", "__")``
      (must match LIB_ID_RE and contain NO ".." segment)
    - anything else                               → ValueError
    """
    if lib_id is None or not str(lib_id).strip():
        return GLOBAL_KEY
    val = str(lib_id).strip()
    if val in ("global", GLOBAL_KEY):
        return GLOBAL_KEY
    if ".." in val.split("/"):
        raise A2AError(f"invalid lib_id {lib_id!r}: '..' segment not allowed")
    if not LIB_ID_RE.match(val):
        raise A2AError(
            f"invalid lib_id {lib_id!r}: must be 'global' or match {LIB_ID_RE.pattern}"
        )
    return val.replace("/", "__")


def maildir_for(lib_id: str | None) -> Path:
    """Resolve (and create) the maildir for an agent: A2A_ROOT/<key>/{inbox,tmp,cur}.

    Final guard: the resolved dir must stay inside A2A_ROOT, else ValueError.
    """
    key = agent_key(lib_id)
    target = A2A_ROOT / key
    try:
        root_resolved = A2A_ROOT.resolve()
    except OSError:
        root_resolved = A2A_ROOT
    # Validate the key resolves under the root before any mkdir (defence in depth;
    # agent_key already rejects traversal, but the resolve guard is the contract).
    candidate = (A2A_ROOT / key)
    if not str(candidate).startswith(str(A2A_ROOT)):
        raise A2AError(f"maildir for {lib_id!r} escapes the a2a root")
    try:
        for sub in MAILDIR_SUBDIRS:
            (target / sub).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise A2AError(f"could not create maildir {target}: {exc}") from exc
    try:
        if not target.resolve().is_relative_to(root_resolved):
            raise A2AError(f"maildir for {lib_id!r} escapes the a2a root")
    except (OSError, AttributeError):
        # is_relative_to is 3.9+; the prefix check above already guards older pys.
        pass
    return target


def session_maildir(lib_id: str | None, session_id: str) -> Path:
    """Resolve (and create) the per-session sub-maildir for an agent.

    Layout (IDENTICAL to the dashboard-side contract):
        A2A_ROOT/<key>/sessions/<session_id>/{inbox,cur}

    ``session_id`` must match :data:`SESSION_ID_RE` (uuid4) — this both rejects
    path traversal (``../``, slashes) and pins the same validation the
    dashboard applies. The atomic-write ``tmp`` for this inbox is the
    AGENT-level ``<key>/tmp`` (same filesystem → os.replace stays atomic), so
    no per-session tmp is created. Final guard: the resolved session dir must
    stay inside A2A_ROOT, else A2AError.
    """
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise A2AError(
            f"invalid session id {session_id!r}: must match {SESSION_ID_RE.pattern}"
        )
    agent = maildir_for(lib_id)  # creates the agent tree (incl. its tmp/)
    target = agent / "sessions" / session_id
    try:
        root_resolved = A2A_ROOT.resolve()
    except OSError:
        root_resolved = A2A_ROOT
    if not str(target).startswith(str(A2A_ROOT)):
        raise A2AError(f"session maildir for {session_id!r} escapes the a2a root")
    try:
        for sub in ("inbox", "cur"):
            (target / sub).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise A2AError(f"could not create session maildir {target}: {exc}") from exc
    try:
        if not target.resolve().is_relative_to(root_resolved):
            raise A2AError(f"session maildir for {session_id!r} escapes the a2a root")
    except (OSError, AttributeError):
        pass
    return target


# ---------------------------------------------------------------------------
# Config / env discovery (mirror artifacts_lib)
# ---------------------------------------------------------------------------


def _load_skill_config() -> dict[str, Any]:
    """Read <skill_dir>/config.json if present. Never raises — returns {}."""
    path = SKILL_DIR / "config.json"
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except (OSError, ValueError):
        pass
    return {}


def notify_url() -> str:
    """Dashboard base URL: HD_NOTIFY_URL → config.json → localhost:8766."""
    return (
        os.environ.get("HD_NOTIFY_URL")
        or _load_skill_config().get("dashboard_url")
        or DEFAULT_DASHBOARD_URL
    ).rstrip("/")


def read_token() -> str | None:
    """Read the artifact auth token, if any (the SAME token A2A /send requires).

    HD_ARTIFACT_TOKEN_FILE → ~/.orchestrator/artifact_token. Returns the file
    contents (stripped) or None when no token file exists / is readable.
    """
    candidates = [
        os.environ.get("HD_ARTIFACT_TOKEN_FILE"),
        str(Path.home() / ".orchestrator" / "artifact_token"),
    ]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand).expanduser()
        try:
            if path.is_file():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    return token
        except OSError:
            continue
    return None


def resolve_lib_id() -> str | None:
    """PARA library id for this agent. Empty / unset HD_LIB_ID → global (None)."""
    val = os.environ.get("HD_LIB_ID", "").strip()
    return val or None


def resolve_session_id() -> str:
    """Resolve the orchestrator session id.

    Order: HD_SESSION_ID → ORCHESTRATOR_SESSION_ID → tmux session name with the
    leading "hd-" stripped → "" (unknown). The tmux probe is best-effort and
    swallows any failure (no tmux, not in a session, binary missing).
    """
    for key in ("HD_SESSION_ID", "ORCHESTRATOR_SESSION_ID"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            name = result.stdout.strip()
            if name:
                return name[3:] if name.startswith("hd-") else name
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def this_agent_label() -> str:
    """Human label for THIS agent: its lib_id, or 'global' when unset."""
    return resolve_lib_id() or "global"


# ---------------------------------------------------------------------------
# Local inbox read / drain (THIS agent only)
# ---------------------------------------------------------------------------


def _read_envelope(path: Path) -> dict[str, Any] | None:
    """Read + parse one envelope. Returns None on any read / parse error."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _candidate_ids(inbox: Path) -> list[str]:
    """Sorted (oldest-first) list of valid message ids waiting in ``inbox``.

    The id embeds a compact UTC stamp, so a lexical sort is chronological.
    Non-id / non-json names are skipped. Never raises — a missing/unreadable
    dir yields an empty list.
    """
    try:
        names = sorted(p.name for p in inbox.glob("*.json"))
    except OSError:
        return []
    ids: list[str] = []
    for name in names:
        msg_id = name[: -len(".json")]
        if ID_RE.match(msg_id):
            ids.append(msg_id)
    return ids


def _claim_and_read(maildir: Path, msg_id: str) -> dict[str, Any] | None:
    """ATOMIC-CLAIM one message: move inbox/<id>.json → cur/<id>.json, then read.

    The ``os.replace`` is the claim: it succeeds for exactly ONE caller across
    concurrent drains (the file exists once). If it raises FileNotFoundError,
    ANOTHER session already claimed the file → return None (skip; do not read,
    do not include). On a successful claim we read from cur/<id>.json and return
    the parsed envelope (augmented with private ``_path`` / ``_id`` keys). A
    malformed-but-claimed file returns None (it now lives in cur/, won't redrain).
    """
    src = maildir / "inbox" / f"{msg_id}.json"
    dst = maildir / "cur" / f"{msg_id}.json"
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        # Lost the race — another concurrent session claimed it first.
        return None
    except OSError as exc:
        raise A2AError(f"could not claim {msg_id}: {exc}") from exc
    env = _read_envelope(dst)
    if env is None:
        return None
    return {**env, "_path": str(dst), "_id": msg_id}


def _resolve_inboxes(lib_id: str | None, session_id: str | None) -> list[Path]:
    """The maildirs to drain/watch: the agent maildir AND this session's, if any.

    Ordered agent-first; both are created (mkdir -p) so a watcher/drain never
    errors on a missing dir. ``session_id`` defaults to the resolved orchestrator
    session id — an empty / invalid one is silently dropped (agent inbox only).
    """
    if lib_id is None:
        lib_id = resolve_lib_id()
    dirs = [maildir_for(lib_id)]
    if session_id is None:
        session_id = resolve_session_id()
    if session_id and SESSION_ID_RE.match(session_id):
        try:
            dirs.append(session_maildir(lib_id, session_id))
        except A2AError:
            pass
    return dirs


def list_inbox(
    lib_id: str | None = None, session_id: str | None = None
) -> list[dict[str, Any]]:
    """List waiting envelopes across the agent inbox AND this session's inbox.

    Oldest-first by id (merged across both maildirs). This is a NON-CLAIMING
    peek (read without moving), used for the plain `inbox` listing; the drain
    path uses :func:`drain_inbox` which claims. Each entry carries private
    ``_path`` / ``_id`` keys. Malformed / non-id files are skipped.
    """
    out: list[dict[str, Any]] = []
    for maildir in _resolve_inboxes(lib_id, session_id):
        inbox = maildir / "inbox"
        for msg_id in _candidate_ids(inbox):
            env = _read_envelope(inbox / f"{msg_id}.json")
            if env is None:
                continue
            out.append({**env, "_path": str(inbox / f"{msg_id}.json"), "_id": msg_id})
    # Sort by id (id embeds a UTC compact stamp → chronological), tie-break name.
    out.sort(key=lambda e: e.get("_id", ""))
    return out


def drain_inbox(
    lib_id: str | None = None, session_id: str | None = None
) -> list[dict[str, Any]]:
    """Claim + read every waiting message across the agent AND session inboxes.

    CLAIM-then-READ (see :func:`_claim_and_read`): for each candidate id we TRY
    to move it inbox→cur; only successfully-claimed files are read + returned.
    This guarantees exactly-one consumer across concurrent drains (zero dup)
    even when an agent has multiple live sessions watching the SAME agent inbox.
    Merged oldest-first by id across both maildirs.
    """
    drained: list[dict[str, Any]] = []
    for maildir in _resolve_inboxes(lib_id, session_id):
        inbox = maildir / "inbox"
        for msg_id in _candidate_ids(inbox):
            env = _claim_and_read(maildir, msg_id)
            if env is not None:
                drained.append(env)
    drained.sort(key=lambda e: e.get("_id", ""))
    return drained


def read_message(
    msg_id: str, lib_id: str | None = None, session_id: str | None = None
) -> dict[str, Any]:
    """Read one message by id across THIS agent's + this session's inbox/cur.

    If found waiting in an inbox/, it is CLAIMED (atomic inbox→cur move) before
    reading — losing the claim race (another session won) falls through to a
    cur/ lookup. Already-drained messages are read straight from cur/. Raises
    A2AError if the id is nowhere to be found. Checks the agent maildir first,
    then this session's.
    """
    validate_id(msg_id)
    maildirs = _resolve_inboxes(lib_id, session_id)
    name = f"{msg_id}.json"
    # First pass: try to CLAIM it out of any inbox (atomic, race-safe).
    for maildir in maildirs:
        if (maildir / "inbox" / name).is_file():
            env = _claim_and_read(maildir, msg_id)
            if env is not None:
                return env
            # Either lost the race or malformed — fall through to cur/ below.
    # Second pass: already drained → read from cur/ (read-only).
    for maildir in maildirs:
        cur_path = maildir / "cur" / name
        if cur_path.is_file():
            env = _read_envelope(cur_path)
            if env is None:
                raise A2AError(f"message {msg_id} is unreadable / malformed")
            return {**env, "_path": str(cur_path), "_id": msg_id}
    raise A2AError(f"message {msg_id} not found in inbox/ or cur/")


# ---------------------------------------------------------------------------
# HTTP — send / agents (talk to the dashboard server)
# ---------------------------------------------------------------------------


def _http_json(
    path: str,
    *,
    method: str,
    body: dict[str, Any] | None,
    timeout: int,
    with_token: bool,
) -> tuple[int, dict[str, Any]]:
    """Issue an HTTP request to the dashboard, returning (status, parsed-json).

    Raises A2AError on a connection failure / timeout (the dashboard is offline
    or unreachable). A non-2xx with a JSON body returns (code, body) so callers
    can surface the server's error message (e.g. "a2a disabled").
    """
    url = notify_url() + path
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if with_token:
        token = read_token()
        if token:
            headers["X-A2A-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_resp_body(resp.read())
    except urllib.error.HTTPError as exc:
        # Server replied with a non-2xx; try to surface its JSON body.
        try:
            payload = exc.read()
        except OSError:
            payload = b""
        return exc.code, _parse_resp_body(payload)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise A2AError(f"could not reach the dashboard at {url}: {exc}") from exc


def _parse_resp_body(raw: bytes) -> dict[str, Any]:
    """Parse a response body as a JSON object; tolerate empties / non-objects."""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _detail_of(body: dict[str, Any], status: int) -> str:
    """Best human message out of a server error body."""
    for key in ("detail", "error", "message"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"HTTP {status}"


def http_get_agents() -> list[dict[str, Any]]:
    """GET /agents → the agent roster. Raises A2AError on failure / non-2xx.

    Each agent entry carries: ``lib_id``, ``name``, ``warm``, ``description``
    (first paragraph of its identity.md), ``dir`` (its absolute PARA directory),
    ``session_id`` / ``last_active`` (representative), and ``sessions`` — every
    LIVE session as ``{session_id, last_active, title, transcript}`` (the
    session's display title + absolute ``.jsonl`` transcript path).
    """
    status, body = _http_json(
        AGENTS_PATH, method="GET", body=None,
        timeout=AGENTS_TIMEOUT_S, with_token=False,
    )
    if not (200 <= status < 300) or not body.get("ok", True if status < 300 else False):
        raise A2AError(f"agents query failed: {_detail_of(body, status)}")
    agents = body.get("agents")
    return agents if isinstance(agents, list) else []


def http_get_whois(lib_id: str) -> dict[str, Any]:
    """GET /whois?lib_id=<lib_id> → one agent's full directory record.

    Returns ``{lib_id, name, dir, identity, warm, sessions}`` where ``identity``
    is the FULL identity.md and ``sessions`` lists EVERY session (live + cold),
    each ``{session_id, title, last_active, live, transcript}``. The ``lib_id``
    is sent as a query param (matching the server route) so a slash in
    ``areas/Dom`` needs no special handling. Raises A2AError on failure / non-2xx.
    """
    from urllib.parse import urlencode

    path = f"{WHOIS_PATH}?{urlencode({'lib_id': lib_id or 'global'})}"
    status, body = _http_json(
        path, method="GET", body=None,
        timeout=AGENTS_TIMEOUT_S, with_token=False,
    )
    if not (200 <= status < 300) or not body.get("ok", True if status < 300 else False):
        raise A2AError(f"whois query failed: {_detail_of(body, status)}")
    agent = body.get("agent")
    return agent if isinstance(agent, dict) else {}


def http_post_send(
    *,
    to: str,
    text: str,
    msg_type: str = "message",
    correlation_id: str | None = None,
    reply_to: str | None = None,
    session_id: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """POST /send. The SERVER sets `from`, mints id/ts — we only carry the body.

    ``session_id`` is the CALLER's own session (so the server can read its
    sidecar lib_id for `from`); ``session`` is the OPTIONAL target session id —
    when set, the server routes the envelope into that session's sub-maildir
    rather than the agent-level inbox. Either way this is a PURE ENQUEUE: the
    server writes into the target maildir and returns (no revive, no push).

    Returns the server's response body (expects ``{ok, id, to, delivery}`` with
    ``delivery == "enqueued"``). Raises A2AError on a connection failure or a
    non-2xx (403 → disabled/unauth).
    """
    if not text or not text.strip():
        raise A2AError("message text is required and must be non-empty")
    if len(text.encode("utf-8")) > TEXT_MAX_BYTES:
        raise A2AError(f"message text exceeds {TEXT_MAX_BYTES} bytes")
    if msg_type not in MESSAGE_TYPES:
        raise A2AError(f"type must be one of {MESSAGE_TYPES} (got {msg_type!r})")
    if session_id is None:
        session_id = resolve_session_id()
    if session is not None:
        session = session.strip()
        if not session:
            session = None
        elif not SESSION_ID_RE.match(session):
            raise A2AError(
                f"invalid --session {session!r}: must match {SESSION_ID_RE.pattern}"
            )

    body: dict[str, Any] = {
        "to": to,
        "type": msg_type,
        "text": text,
        "session_id": session_id or None,
    }
    if correlation_id:
        body["correlation_id"] = correlation_id
    if reply_to:
        body["reply_to"] = reply_to
    if session:
        body["session"] = session

    status, resp = _http_json(
        SEND_PATH, method="POST", body=body,
        timeout=SEND_TIMEOUT_S, with_token=True,
    )
    if status == 403:
        raise A2AError(
            "A2A disabled or unauthorized "
            f"({_detail_of(resp, status)}) — flip a2a_enabled / check the token"
        )
    if not (200 <= status < 300):
        raise A2AError(f"send failed: {_detail_of(resp, status)}")
    return resp


