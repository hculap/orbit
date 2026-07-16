"""Orchestrator JSONL scanner — read-only parser over Claude Code session files.

Claude Code 2.x writes one transcript per session at
`~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl`. With cwd=/home/user
the slug is `-home-user`. Each line is a JSON object: `user`, `assistant`,
`system`, `last-prompt`, or `file-history-snapshot`. We expose a fast list
view (5 s TTL) and a per-file mtime cache for full parses.
"""
from __future__ import annotations
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Claude Code maps a cwd to a project dir by replacing every non-alphanumeric
# char with '-' (so /home/alice → -home-alice). Derive the home slug at runtime
# instead of hardcoding one operator's home.
_HOME_SLUG = re.sub(r"[^a-zA-Z0-9]", "-", str(Path.home()))
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects" / _HOME_SLUG
# Root for ALL claude project slugs. Each cwd Claude is invoked from gets its
# own subdirectory keyed by slugified cwd (`/` → `-`). Per-area / per-project
# agent sessions live under their own slug, not the home one — list_sessions
# and jsonl_path enumerate this whole tree.
_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

_LIST_TTL = 5.0
_PREVIEW_LIMIT = 120
_SKIP_TYPES = {"system", "last-prompt", "file-history-snapshot"}
_CHOICE_FENCE = re.compile(
    r"```orchestrator-choice\s*\n(?P<json>\{.*?\})\s*\n```", re.DOTALL,
)
# Echo bookkeeping messages the frontend POSTs after a pill click / ask submit.
# We must NOT use them as session previews — they're "[choice:id=opt]" strings,
# not real user content.
_CONTROL_FULL_RE = re.compile(r"^\s*\[(choice|ask):[^\]=]+=[^\]]*\]\s*$")
# A "user" message that is really a harness artifact, not user prose: a
# slash-command caveat/echo (<local-command-caveat> / <command-name> / …) or a
# `!`-run bash command + its output (<bash-input> / <bash-stdout> / …). These
# must NOT become the session title (first_user_preview) nor pollute the search
# corpus — skip them and fall back to the first REAL user message. Issue #85.
_LOCAL_CMD_RE = re.compile(
    r"^\s*<(local-command-caveat|local-command-stdout|command-name|command-message"
    r"|command-args|bash-input|bash-stdout|bash-stderr)\b",
    re.IGNORECASE,
)
# Trailing <attached>…</attached> block injected by the post-message handler so
# the runner sees uploads as part of the user turn. Hydrating peels it back off.
_ATTACHED_RE = re.compile(
    r"\n*<attached>\s*\n([^\n]+(?:\n[^\n]+)*)\n</attached>\s*$", re.DOTALL,
)
# Leading <reply-to turn_idx="N"/> tag injected by the post-message handler when
# the user is replying to a specific assistant message. Hydrating splits it off
# into a sibling block so the frontend can render the back-reference inline.
_REPLY_TO_RE = re.compile(
    r"^\s*<reply-to turn_idx=\"(\d+)\"/>\s*\n*", re.DOTALL,
)
# Leading <compacted-from session_id="<uuid>"/> tag injected by the compact
# handler when seeding a new session from an older one. Mirrors _REPLY_TO_RE.
_COMPACTED_FROM_RE = re.compile(
    r"^\s*<compacted-from session_id=\"([0-9a-f-]{36})\"/>\s*\n*", re.DOTALL,
)

_list_cache: tuple[float, list[dict]] | None = None
_session_cache: dict[str, tuple[float, dict]] = {}
_summary_cache: dict[str, tuple[float, dict]] = {}
# list_sessions now runs in asyncio.to_thread workers (session list + corpora +
# tmux snapshot), so its check-scan-write on _list_cache races with
# invalidate_cache (called on the event loop by write handlers). The lock keeps
# the check/write fast-and-consistent; _list_gen is bumped on every invalidate
# so a worker that scanned across an invalidate won't write its now-stale result
# back (which would resurrect a just-deleted session for up to the TTL).
_list_cache_lock = threading.Lock()
_list_gen = 0


def _warn(msg: str) -> None:
    print(f"[orchestrator_jsonl] {msg}", file=sys.stderr)


def _parse_ts(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _PREVIEW_LIMIT else text[: _PREVIEW_LIMIT - 3] + "..."


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b["text"] for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ).strip()


def _is_tool_result_only(content: object) -> bool:
    return (isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and content[0].get("type") == "tool_result")


def _iter_lines(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError as e:
                _warn(f"{path.name}:{line_no} malformed line: {e}")


def jsonl_path(session_id: str) -> Path:
    """Return the on-disk path for ``session_id`` across ALL project slugs.

    Searches every ``~/.claude/projects/<slug>/`` subdir for
    ``<session_id>.jsonl``. When the same id exists in multiple slug dirs
    (claude can write a stub under one cwd's slug and a real transcript
    under another's — e.g. when a session was originally created with
    cwd=$HOME and later resumed under cwd=~/Projects/X) the most-recently-
    modified file wins. Mirrors the tie-break in ``list_sessions``.

    Falls back to the home-slug path when the file doesn't exist anywhere
    — preserves the prior semantics for callers that use ``.exists()`` to
    detect "session has no transcript yet".
    """
    best: Path | None = None
    best_mtime: float = -1.0
    if _PROJECTS_ROOT.is_dir():
        try:
            for slug_dir in _PROJECTS_ROOT.iterdir():
                if not slug_dir.is_dir():
                    continue
                candidate = slug_dir / f"{session_id}.jsonl"
                try:
                    if not candidate.is_file():
                        continue
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best, best_mtime = candidate, mtime
        except OSError:
            pass
    if best is not None:
        return best
    return CLAUDE_PROJECTS_DIR / f"{session_id}.jsonl"


# Per-session corpus is shipped to the browser for client-side fuzzy search
# (MiniSearch). Cap at 80 KB so the full session-list payload stays well under
# 10 MB even with 100+ sessions.
_CORPUS_LIMIT = 80 * 1024


def _build_summary(path: Path) -> dict:
    created_at = 0.0
    msg_count = 0
    last_user = first_user = last_role = ""
    total_in = total_out = 0
    # Track the LATEST assistant turn's prompt size — this approximates current
    # context-window occupancy because each turn re-sees the entire conversation
    # as input (cache_read covers what was already cached, cache_create the new
    # delta, input_tokens any uncached residual). Updated on every assistant
    # event so we land on the last one.
    last_context_tokens = 0
    last_model = ""
    first = True
    corpus_parts: list[str] = []
    corpus_len = 0
    ai_title = ""
    for evt in _iter_lines(path):
        etype = evt.get("type")
        if etype == "ai-title":
            # Native Claude Code session title — record {"type":"ai-title",
            # "aiTitle":"…"}, the SAME string shown in /resume, re-appended every
            # prompt so the latest wins. Has no timestamp/message, so capture +
            # skip before the created_at / msg-count bookkeeping below.
            t = evt.get("aiTitle")
            if isinstance(t, str) and t.strip():
                ai_title = t.strip()
            continue
        if first:
            created_at = _parse_ts(evt.get("timestamp"))
            first = False
        if etype in _SKIP_TYPES or etype not in ("user", "assistant"):
            continue
        msg = evt.get("message") or {}
        content = msg.get("content")
        if etype == "user" and _is_tool_result_only(content):
            continue
        msg_count += 1
        last_role = etype
        text = _extract_text(content)
        if (etype == "user" and text and not _CONTROL_FULL_RE.match(text)
                and not _LOCAL_CMD_RE.match(text)):
            # Skip echo "[choice:id=opt]" / "[ask:id=value]" AND local-command /
            # bash artifacts — those aren't real user content; fall back to the
            # next real user message (so the title isn't a "<local-command-…>").
            last_user = _truncate(text)
            if not first_user:
                first_user = _truncate(text)
        if etype == "assistant":
            usage = msg.get("usage") or {}
            input_t = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_create = int(usage.get("cache_creation_input_tokens") or 0)
            total_in += input_t
            total_out += int(usage.get("output_tokens") or 0)
            prompt_t = input_t + cache_read + cache_create
            if prompt_t > 0:
                last_context_tokens = prompt_t
                model_id = msg.get("model")
                if isinstance(model_id, str) and model_id:
                    last_model = model_id
        if text and corpus_len < _CORPUS_LIMIT:
            # Strip control echoes ([choice:..] / [ask:..]), leading
            # <reply-to> / <compacted-from> tags, and trailing <attached>
            # upload-path blocks so search isn't polluted by routing tokens
            # or internal paths. Local-command / bash artifacts are dropped
            # whole (not real conversation content).
            if not _CONTROL_FULL_RE.match(text) and not _LOCAL_CMD_RE.match(text):
                clean = _COMPACTED_FROM_RE.sub("", text)
                clean = _REPLY_TO_RE.sub("", clean)
                clean = _ATTACHED_RE.sub("", clean).strip()
                if clean:
                    remaining = _CORPUS_LIMIT - corpus_len
                    snippet = clean[:remaining]
                    corpus_parts.append(snippet)
                    corpus_len += len(snippet) + 1  # +1 for the join separator
    try:
        updated_at = path.stat().st_mtime
    except OSError:
        updated_at = 0.0
    return {
        "id": path.stem,
        "created_at": created_at or updated_at,
        "updated_at": updated_at,
        "msg_count": msg_count,
        "last_user_preview": last_user,
        "last_role": last_role,
        "first_user_preview": first_user,
        "ai_title": ai_title,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "last_context_tokens": last_context_tokens,
        "last_model": last_model,
        "corpus": "\n".join(corpus_parts),
    }


def _summary_for(path: Path) -> dict:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _build_summary(path)
    cached = _summary_cache.get(path.stem)
    if cached and cached[0] == mtime:
        return cached[1]
    summary = _build_summary(path)
    _summary_cache[path.stem] = (mtime, summary)
    return summary


def list_sessions() -> list[dict]:
    """Scan ALL project slugs, return per-session summary, sorted by updated_at desc.

    Walks every ``~/.claude/projects/<slug>/*.jsonl`` so per-area / per-project
    agent sessions (which live under per-cwd slugs) are aggregated alongside
    the global home-slug ones. If a duplicate session id appears in two slug
    dirs (shouldn't happen in practice — claude assigns unique uuids) the
    most-recently-modified file wins.
    """
    now = time.time()
    with _list_cache_lock:
        if _list_cache and (now - _list_cache[0]) < _LIST_TTL:
            return list(_list_cache[1])
        gen_at_start = _list_gen

    def _store(result: list[dict]) -> None:
        # Cache the freshly-scanned result UNLESS an invalidate_cache landed
        # while we were scanning (gen bumped) — otherwise a concurrent worker
        # could resurrect a just-deleted session until the TTL lapses.
        global _list_cache
        with _list_cache_lock:
            if _list_gen == gen_at_start:
                _list_cache = (now, result)

    if not _PROJECTS_ROOT.is_dir():
        _store([])
        return []
    by_id: dict[str, Path] = {}
    try:
        for slug_dir in _PROJECTS_ROOT.iterdir():
            if not slug_dir.is_dir():
                continue
            try:
                for path in slug_dir.glob("*.jsonl"):
                    sid = path.stem
                    prev = by_id.get(sid)
                    if prev is None:
                        by_id[sid] = path
                    else:
                        # Tie-break by mtime (most recent wins). Defensive —
                        # collisions between slugs should be vanishingly rare.
                        try:
                            if path.stat().st_mtime > prev.stat().st_mtime:
                                by_id[sid] = path
                        except OSError:
                            pass
            except OSError as e:
                _warn(f"cannot list {slug_dir}: {e}")
    except OSError as e:
        _warn(f"cannot list {_PROJECTS_ROOT}: {e}")
        _store([])
        return []
    summaries: list[dict] = []
    for path in by_id.values():
        try:
            summaries.append(_summary_for(path))
        except OSError as e:
            _warn(f"skip {path.name}: {e}")
    summaries.sort(key=lambda s: s["updated_at"], reverse=True)
    _store(summaries)
    return list(summaries)


def _content_to_blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"kind": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or ""):
            out.append({"kind": "text", "text": b.get("text") or ""})
        elif bt == "thinking":
            text = b.get("text") or b.get("thinking") or ""
            if text:
                out.append({"kind": "thinking", "text": text})
        elif bt == "tool_use":
            out.append({"kind": "tool_use", "tool_use_id": b.get("id") or "",
                        "name": b.get("name") or "", "input": b.get("input") or {}})
        elif bt == "tool_result":
            raw = b.get("content")
            if isinstance(raw, list):
                output = "\n".join(
                    x.get("text", "") for x in raw
                    if isinstance(x, dict) and x.get("type") == "text"
                )
            else:
                output = raw if isinstance(raw, str) else ""
            out.append({"kind": "tool_result", "tool_use_id": b.get("tool_use_id") or "",
                        "output": output, "is_error": bool(b.get("is_error", False))})
    return out


def _try_parse_envelope(text: str) -> list[dict] | None:
    """Best-effort HISTORICAL envelope → transcript blocks.

    The envelope pipeline was removed in the artifacts cutover — new sessions
    write plain markdown. But OLD JSONL transcripts stored assistant replies as
    a ``{"blocks":[…]}`` JSON envelope; without this shim those would render as
    raw JSON on reload. Strict ``json.loads`` only (no json-repair: the JSONL
    was already validated when written); anything that isn't a ``blocks`` dict
    returns None so plain-markdown replies render verbatim.

    Remaps ``content`` → ``text`` for markdown/code, and degrades the
    now-removed ``choice``/``ask`` block kinds to plain markdown so legacy
    transcripts stay readable after the frontend dropped those renderers.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if not text.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw_blocks = parsed.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        return None
    out: list[dict] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type") or block.get("kind")
        if not kind:
            continue
        entry: dict = {k: v for k, v in block.items() if k not in ("type", "kind")}
        if kind == "choice":
            lines = [str(entry.get("prompt") or "")]
            for opt in (entry.get("options") or []):
                label = (opt.get("label") if isinstance(opt, dict) else opt) or ""
                if label:
                    lines.append(f"- {label}")
            out.append({"kind": "markdown", "text": "\n".join(s for s in lines if s)})
            continue
        if kind == "ask":
            out.append({"kind": "markdown", "text": str(entry.get("prompt") or "")})
            continue
        entry["kind"] = kind
        if kind in ("markdown", "code") and "content" in entry:
            entry["text"] = entry.pop("content")
        out.append(entry)
    return out or None


def _expand_envelope_text(blocks: list[dict]) -> list[dict]:
    """Replace any `text`-kind assistant block whose body is a v2 JSON
    envelope with the parsed structured blocks. Non-envelope text blocks are
    dropped only when they appear *between* tool calls — those are stray
    prose emissions ("Now restarting…", "Validation:") that the runner has
    no business surfacing as user-facing bubbles.

    The trailing text block (after the last tool_use/thinking) is preserved
    even if it isn't wrapped in an envelope — it's the user-facing reply
    when the model regresses on the prompt and writes prose directly.
    Belt-and-suspenders for v1 sessions and post-regression turns.
    """
    last_tool_idx = -1
    for i, b in enumerate(blocks):
        if b.get("kind") in ("tool_use", "thinking"):
            last_tool_idx = i
    out: list[dict] = []
    for i, block in enumerate(blocks):
        if block.get("kind") != "text":
            out.append(block)
            continue
        text = block.get("text") or ""
        envelope_blocks = _try_parse_envelope(text)
        if envelope_blocks is not None:
            out.extend(envelope_blocks)
            continue
        # Drop only prose sandwiched between tool calls; keep the trailing
        # text block (the user-facing reply if envelope wrapping was missed).
        if last_tool_idx >= 0 and i < last_tool_idx:
            continue
        out.append(block)
    return out


def _scan_choice_fence(blocks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for block in blocks:
        if block.get("kind") != "text":
            out.append(block)
            continue
        text = block.get("text") or ""
        match = _CHOICE_FENCE.search(text)
        if not match:
            out.append(block)
            continue
        try:
            payload = json.loads(match.group("json"))
            stripped = (text[: match.start()] + text[match.end():]).strip()
            if stripped:
                out.append({"kind": "text", "text": stripped})
            out.append({"kind": "choice", "id": payload.get("id") or "",
                        "prompt": payload.get("prompt") or "",
                        "options": payload.get("options") or []})
        except (json.JSONDecodeError, ValueError) as e:
            out.append(block)
            out.append({"kind": "error",
                        "text": f"Malformed orchestrator-choice JSON: {e}"})
    return out


def _split_compacted_from(blocks: list[dict]) -> list[dict]:
    """Peel a leading <compacted-from session_id="..."/> tag off user text into
    a dedicated `compacted_from` block (emitted BEFORE the cleaned text).
    Non-text blocks pass through unchanged.
    """
    out: list[dict] = []
    for block in blocks:
        if block.get("kind") != "text":
            out.append(block)
            continue
        text = block.get("text") or ""
        match = _COMPACTED_FROM_RE.match(text)
        if not match:
            out.append(block)
            continue
        session_id = match.group(1)
        out.append({"kind": "compacted_from", "session_id": session_id})
        remaining = text[match.end():].lstrip("\n")
        if remaining:
            out.append({"kind": "text", "text": remaining})
    return out


def _split_reply_to(blocks: list[dict]) -> list[dict]:
    """Peel a leading <reply-to turn_idx="N"/> tag off user text into a
    dedicated `reply_to` block (emitted BEFORE the cleaned text). Non-text
    blocks pass through unchanged.
    """
    out: list[dict] = []
    for block in blocks:
        if block.get("kind") != "text":
            out.append(block)
            continue
        text = block.get("text") or ""
        match = _REPLY_TO_RE.match(text)
        if not match:
            out.append(block)
            continue
        try:
            turn_idx = int(match.group(1))
        except (ValueError, TypeError):
            out.append(block)
            continue
        out.append({"kind": "reply_to", "turn_idx": turn_idx})
        remaining = text[match.end():].lstrip("\n")
        if remaining:
            out.append({"kind": "text", "text": remaining})
    return out


def _split_attached(blocks: list[dict]) -> list[dict]:
    """Peel a trailing <attached>…</attached> block off user text into a
    dedicated `attachments` block. Non-text blocks pass through unchanged.
    """
    out: list[dict] = []
    for block in blocks:
        if block.get("kind") != "text":
            out.append(block)
            continue
        text = block.get("text") or ""
        match = _ATTACHED_RE.search(text)
        if not match:
            out.append(block)
            continue
        paths_block = match.group(1)
        paths = [
            line.strip().lstrip("- ").strip()
            for line in paths_block.splitlines()
            if line.strip().startswith("-")
        ]
        remaining = text[: match.start()].rstrip()
        if remaining:
            out.append({"kind": "text", "text": remaining})
        if paths:
            out.append({"kind": "attachments", "paths": paths})
    return out


def _build_messages(path: Path) -> dict:
    messages: list[dict] = []
    turn_idx = -1
    for evt in _iter_lines(path):
        etype = evt.get("type")
        if etype in _SKIP_TYPES or etype not in ("user", "assistant"):
            continue
        msg = evt.get("message") or {}
        content = msg.get("content")
        ts = _parse_ts(evt.get("timestamp"))
        if etype == "user" and _is_tool_result_only(content):
            blocks = _content_to_blocks(content)
            if blocks:
                messages.append({"role": "tool_result", "ts": ts,
                                 "turn_idx": max(turn_idx, 0), "blocks": [blocks[0]]})
            continue
        turn_idx += 1
        blocks = _content_to_blocks(content)
        if etype == "assistant":
            blocks = _expand_envelope_text(blocks)
            blocks = _scan_choice_fence(blocks)
        elif etype == "user":
            blocks = _split_compacted_from(blocks)
            blocks = _split_reply_to(blocks)
            blocks = _split_attached(blocks)
        messages.append({"role": etype, "ts": ts, "turn_idx": turn_idx, "blocks": blocks})
    return {"ok": True, "messages": messages, "cost_usd": 0.0, "session_id": path.stem}


def _all_jsonl_paths(session_id: str) -> list[Path]:
    """Every JSONL on disk for this session id, across all project slugs.

    claude-cli writes per-cwd: if the same session id is resumed (or
    written to via cron / external scripts) under a different cwd, a
    second file shows up under that slug's dir. Both belong to the same
    logical conversation and should appear together in the UI even
    though claude itself only sees one of them on resume.
    """
    found: list[Path] = []
    if not _PROJECTS_ROOT.is_dir():
        return found
    try:
        for slug_dir in _PROJECTS_ROOT.iterdir():
            if not slug_dir.is_dir():
                continue
            candidate = slug_dir / f"{session_id}.jsonl"
            if candidate.is_file():
                found.append(candidate)
    except OSError:
        pass
    return found


def read_session(session_id: str) -> dict:
    """Parse jsonl(s) for ``session_id``; merged + de-duped + cached.

    Multiple JSONLs under different project slugs are unioned so the UI
    sees every turn — including out-of-band injections (cron tools that
    POST a turn under a different cwd than the user's normal slug). Cache
    key is the max(mtime) across the merged file set.
    """
    paths = _all_jsonl_paths(session_id)
    if not paths:
        return {"ok": False, "error": "session not found"}
    try:
        mtimes = [p.stat().st_mtime for p in paths]
    except OSError as e:
        return {"ok": False, "error": f"stat failed: {e}"}
    mtime_key = (tuple(sorted(str(p) for p in paths)), max(mtimes))
    cached = _session_cache.get(session_id)
    if cached and cached[0] == mtime_key:
        return cached[1]
    try:
        if len(paths) == 1:
            result = _build_messages(paths[0])
        else:
            # Multi-file: parse each and merge by ts. Claude's internal
            # turn_idx is per-file so we re-number the merged sequence.
            merged: list[dict] = []
            for p in paths:
                part = _build_messages(p)
                for m in (part.get("messages") or []):
                    merged.append(m)
            merged.sort(key=lambda m: (m.get("ts") or 0))
            for i, m in enumerate(merged):
                m["turn_idx"] = i
            result = {"ok": True, "messages": merged, "cost_usd": 0.0, "session_id": session_id}
    except OSError as e:
        return {"ok": False, "error": f"read failed: {e}"}
    _session_cache[session_id] = (mtime_key, result)
    return result


def delete_session(session_id: str) -> bool:
    """Delete every JSONL on disk for this session id (across all slugs).

    Multi-slug duplicates exist when the same session was resumed under
    different cwds (claude writes per-cwd-derived slug). ``read_session``
    merges them into one logical conversation, so ``delete_session`` must
    do the same — otherwise ``jsonl_path()`` only returns the mtime
    winner, the older slug's file persists, and the session reappears in
    the next list / read. Returns True if at least one file was deleted.
    """
    paths = _all_jsonl_paths(session_id)
    invalidate_cache(session_id)
    if not paths:
        return False
    deleted_any = False
    for p in paths:
        try:
            p.unlink()
            deleted_any = True
        except FileNotFoundError:
            # Race: file already gone. Treat as success for that path.
            deleted_any = True
        except OSError as e:
            _warn(f"delete {p.name} ({p.parent.name}): {e}")
    return deleted_any


def invalidate_cache(session_id: str | None = None) -> None:
    """Drop per-file cache entry (or all if None)."""
    global _list_cache, _list_gen
    # Bump the generation under the lock so an in-flight list_sessions scan
    # (running in a worker thread) won't write its now-stale result back.
    with _list_cache_lock:
        _list_gen += 1
        _list_cache = None
    if session_id is None:
        _session_cache.clear()
        _summary_cache.clear()
        return
    _session_cache.pop(session_id, None)
    _summary_cache.pop(session_id, None)
