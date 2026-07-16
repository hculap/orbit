"""Auto-titles — fire-and-forget Haiku call after every turn.

Triggers a one-shot ``claude -p --model haiku --no-session-persistence`` to
summarise a session into ≤50 chars at specific milestone user-message counts:

    1   → first impression after the opening exchange
    5   → topic settles after a few rounds
    25  → mid-session refresh — direction may have shifted
    100 → long-session checkpoint; rarely fires

Token budget per call is bounded by ``_EXCERPT_BYTE_LIMIT`` (~6 KB ≈ 1.5 KB
text post-stripping) so the cost stays well under a cent on Haiku 4.5. The
runner schedules this via ``asyncio.create_task`` so a slow Haiku call (or a
crash) never blocks user-facing flow.

Flags honoured by the user:
- ``meta.title_manual = True`` — set by the rename UI; we never overwrite.
- Existing entries with a non-empty ``title`` but no ``title_manual`` field
  are treated as manual (legacy preservation).
"""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from . import orchestrator_env as env_mod
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from . import orchestrator_settings as settings_mod

# Milestones — user-message counts at which we (re)generate the title. Keep
# the set small + sparse: each call costs a Haiku roundtrip, and titles
# stabilise quickly. ``25`` lets a refactor mid-conversation update the
# label; ``100`` is a long-session checkpoint.
TITLE_THRESHOLDS: frozenset[int] = frozenset({1, 5, 25, 100})

# Excerpt cap. Shipped to Haiku as the user message; we stay well under the
# 200k Haiku context window but also under 2k tokens to keep the call cheap.
_EXCERPT_BYTE_LIMIT = 6 * 1024
# Per-message text cap inside the excerpt — keeps any single bombastic message
# from blowing the budget by itself.
_PER_MSG_BYTE_LIMIT = 800

# Hard upper bound on the title we accept from Haiku; anything longer is
# truncated at the boundary. The UI also clips visually, but we keep the
# stored value sensible for tooltips / search corpora.
_TITLE_MAX_CHARS = 80

# Per-session lock to avoid double-firing if two consecutive turns both
# cross a threshold (race after compact, etc.).
_in_flight: set[str] = set()
_lock = asyncio.Lock()

CLAUDE_BIN_DEFAULT = "/usr/bin/claude"


def _resolve_claude_bin() -> str:
    if Path(CLAUDE_BIN_DEFAULT).exists():
        return CLAUDE_BIN_DEFAULT
    found = shutil.which("claude")
    return found or CLAUDE_BIN_DEFAULT


def _truncate_msg(text: str, limit: int = _PER_MSG_BYTE_LIMIT) -> str:
    if not isinstance(text, str):
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="replace") + " […]"


def _count_user_messages(session_id: str) -> int:
    """Count user-authored entries in the JSONL, skipping echo control msgs."""
    path = jsonl_mod.jsonl_path(session_id)
    if not path.exists():
        return 0
    n = 0
    for evt in jsonl_mod._iter_lines(path):
        if evt.get("type") != "user":
            continue
        msg = evt.get("message") or {}
        content = msg.get("content")
        if jsonl_mod._is_tool_result_only(content):
            continue
        text = jsonl_mod._extract_text(content)
        if text and jsonl_mod._CONTROL_FULL_RE.match(text):
            # Echo "[choice:id=opt]" / "[ask:id=value]" — not real user content.
            continue
        n += 1
    return n


def _build_excerpt(session_id: str) -> str:
    """Compose the conversation excerpt sent to Haiku.

    Always includes the FIRST user message (defines what the session is
    about) plus the LAST few rounds (shows current direction). Bounded by
    ``_EXCERPT_BYTE_LIMIT``.
    """
    path = jsonl_mod.jsonl_path(session_id)
    if not path.exists():
        return ""
    rows: list[tuple[str, str]] = []
    for evt in jsonl_mod._iter_lines(path):
        etype = evt.get("type")
        if etype not in ("user", "assistant"):
            continue
        msg = evt.get("message") or {}
        content = msg.get("content")
        if etype == "user" and jsonl_mod._is_tool_result_only(content):
            continue
        text = jsonl_mod._extract_text(content)
        if not text:
            continue
        if etype == "user" and jsonl_mod._CONTROL_FULL_RE.match(text):
            continue
        rows.append((etype, text))
    if not rows:
        return ""
    first_user = next((row for row in rows if row[0] == "user"), None)
    # Last 6 rows ≈ 3 round-trips. Skip the first if it'd be doubled.
    tail = rows[-6:]
    selected: list[tuple[str, str]] = []
    if first_user and (not tail or first_user not in tail):
        selected.append(first_user)
    selected.extend(tail)
    parts: list[str] = []
    used = 0
    for role, text in selected:
        snippet = _truncate_msg(text)
        line = f"[{role}] {snippet}".strip()
        encoded = (line + "\n").encode("utf-8", errors="replace")
        if used + len(encoded) > _EXCERPT_BYTE_LIMIT and parts:
            break
        parts.append(line)
        used += len(encoded)
    return "\n".join(parts)


def _build_prompt(excerpt: str) -> str:
    return (
        "Write a SHORT title (max 50 chars) for the conversation below.\n"
        "Rules:\n"
        "- Use the same language as the user (Polish for Polish, English for English).\n"
        "- No quotes, no leading/trailing punctuation, no Markdown.\n"
        "- One line only — output the title and NOTHING else.\n"
        "- Be concrete: name the actual topic / artefact, not 'Pytanie o X'.\n"
        "\n"
        "Conversation:\n"
        f"{excerpt}\n"
    )


_NEWLINE_RE = re.compile(r"[\r\n]+")


def _clean_title(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    # Take the first non-empty line; strip surrounding quotes/punct that the
    # model sometimes adds despite instructions.
    for line in _NEWLINE_RE.split(raw):
        candidate = line.strip().strip('"').strip("'").strip("«»“”").strip()
        if candidate:
            # Drop a trailing period / exclamation that adds no info.
            while candidate.endswith((".", "!", "?", "…")):
                candidate = candidate[:-1].rstrip()
            if not candidate:
                continue
            if len(candidate) > _TITLE_MAX_CHARS:
                candidate = candidate[: _TITLE_MAX_CHARS - 1].rstrip() + "…"
            return candidate
    return ""


async def _run_haiku(prompt: str, timeout_s: float = 90.0) -> str | None:
    """One haiku turn for title generation. 90s (was 45s) because the
    interactive path COLD-spawns a tmux slot (~10-25s) before the haiku turn;
    45s was too tight under load and timed out. Routes by ``titles_runner_mode``:
    ``interactive`` (default) → subscription via the tmux pool; ``programmatic``
    → the legacy ``claude -p`` path (credit pool) kept as rollback. Returns the
    model's text reply or ``None`` on any failure."""
    mode = settings_mod.resolve_runner_mode("titles_runner_mode")
    if mode == "interactive":
        from . import orchestrator_oneshot as oneshot_mod
        res = await oneshot_mod.run_oneshot(
            prompt, model="haiku", timeout_s=timeout_s, label="titles-haiku",
        )
        return (res["text"] or None) if res["ok"] else None
    return await _run_haiku_programmatic(prompt, timeout_s)


async def _run_haiku_programmatic(prompt: str, timeout_s: float = 90.0) -> str | None:
    """Legacy ``claude -p --model haiku --no-session-persistence`` (credit pool).

    Rollback path behind ``titles_runner_mode=programmatic``. Env is scrubbed of
    ANTHROPIC_API_KEY so it can't be forced onto raw API billing.
    """
    binary = _resolve_claude_bin()
    args = [
        binary,
        "-p",
        prompt,
        "--model",
        "haiku",
        "--no-session-persistence",
        "--output-format",
        "text",
        "--permission-mode",
        "default",
    ]
    # This file had NO env= (inherited the full parent env) — the largest
    # leak surface. scrubbed_env() strips ANTHROPIC_API_KEY/AUTH_TOKEN.
    env_mod.log_billing_path("titles-haiku", interactive=False)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env_mod.scrubbed_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"[orchestrator_titles] spawn failed: {exc}")
        return None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        print(f"[orchestrator_titles] haiku timed out after {timeout_s}s")
        return None
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-500:]
        print(f"[orchestrator_titles] haiku exit={proc.returncode}: {tail}")
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


async def maybe_generate_title(session_id: str) -> None:
    """Threshold-gated background entrypoint. Safe to call after every turn.

    No-ops if:
    - Session has no JSONL yet.
    - User-message count is not a milestone in ``TITLE_THRESHOLDS``.
    - Sidecar marks the title manual (user renamed via UI).
    - Another regen is already in flight for this session.
    """
    if not isinstance(session_id, str) or not session_id:
        return
    # Server-side flag — flipped via settings UI. Default true; off skips
    # everything (including the JSONL read) so haiku is never spawned.
    if not settings_mod.get_flag("auto_titles"):
        return
    if not jsonl_mod.jsonl_path(session_id).exists():
        return
    user_msgs = _count_user_messages(session_id)
    if user_msgs not in TITLE_THRESHOLDS:
        return
    meta = meta_mod.get_meta(session_id)
    if meta.get("title_manual"):
        # Either the user renamed via UI (explicit True), or the load layer
        # treated a legacy entry with a pre-existing title as manual. Either
        # way, hands off.
        return
    async with _lock:
        if session_id in _in_flight:
            return
        _in_flight.add(session_id)
    try:
        excerpt = _build_excerpt(session_id)
        if not excerpt.strip():
            return
        prompt = _build_prompt(excerpt)
        raw = await _run_haiku(prompt)
        if raw is None:
            return
        title = _clean_title(raw)
        if not title:
            return
        await meta_mod.set_meta(session_id, title=title, title_manual=False)
        # Drop list cache so the new title appears on next /sessions GET.
        jsonl_mod.invalidate_cache()
    except Exception as exc:  # noqa: BLE001 — background task must not raise
        print(f"[orchestrator_titles] {session_id} regen failed: {exc}")
    finally:
        async with _lock:
            _in_flight.discard(session_id)
