"""Async incremental JSONL tail reader for the tmux-driven interactive runner.

The PoC `_tail_until_turn_end` (`scripts/poc_tmux_claude.py`) proved the
end-of-turn signal: a ``system / stop_hook_summary`` line after the latest
``user`` line in the tailed range. This module is the production version
of that algorithm, used by :class:`TmuxClaudeRunner` to bridge JSONL → SSE
events when claude-cli runs interactively under tmux.

Why this is its own module (rather than methods on the runner):

* The poll loop is pure I/O + buffer state — easy to unit-test in isolation
  by writing a temp JSONL from a background thread, no tmux/claude needed.
* Multiple callers in the future (e.g. cron migration in Phase 4) will want
  the same tail semantics but a different SSE bridge.

Key contract: ``tail_until_turn_end`` returns ``(lines, end_offset)`` so the
caller can chain consecutive turns by passing ``since_byte=end_offset`` into
the next call. Each successful return guarantees the JSONL has flushed at
least one complete turn since ``since_byte``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

# Tuneables — overridable per-call so tests can drive faster polls without
# patching module-level globals.
DEFAULT_TIMEOUT_S: float = 120.0
DEFAULT_POLL_INTERVAL_S: float = 0.1
DEFAULT_QUIET_SECONDS: float = 3.0

# Root of claude-cli's per-cwd project tree. Overridable via the `_CLAUDE_HOME`
# module attribute so tests can point at a tmp directory without monkey-patching
# `Path.home()` globally.
_CLAUDE_HOME: Path = Path.home() / ".claude"


# ── path helpers ───────────────────────────────────────────────────


def slug_for_cwd(cwd: str | os.PathLike[str]) -> str:
    """Reproduce claude-cli's per-cwd directory slug.

    Empirically (PoC commit 37e0c26, May 2026) the slug substitutes ALL of
    ``/``, ``_``, ``.`` with ``-`` — the plan's original assumption ("only
    ``/`` → ``-``") was wrong, and using the looser rule would point the
    tail at a path that never exists for cwds containing ``_`` or ``.``.
    """
    real = os.path.realpath(str(cwd))
    return "".join("-" if ch in "/_." else ch for ch in real)


def jsonl_path_for(cwd: str | os.PathLike[str], session_id: str) -> Path:
    """Resolve ``~/.claude/projects/<slug>/<sid>.jsonl`` for a given cwd."""
    return _CLAUDE_HOME / "projects" / slug_for_cwd(cwd) / f"{session_id}.jsonl"


# ── tail loop ──────────────────────────────────────────────────────


async def tail_until_turn_end(
    path: Path,
    *,
    since_byte: int = 0,
    timeout: float = DEFAULT_TIMEOUT_S,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
) -> tuple[list[dict[str, Any]], int]:
    """Read ``path`` from ``since_byte`` until the current turn completes.

    Returns ``(lines, end_offset)`` where ``lines`` is the parsed JSON objects
    seen since ``since_byte`` and ``end_offset`` is the absolute byte position
    of the end of the last consumed line.

    End-of-turn signals (first one wins):

    1. ``{"type": "system", "subtype": "stop_hook_summary"}`` AFTER a ``user``
       line in the tailed range. This is the primary, deterministic marker
       claude-cli emits at the end of each turn (PoC confirmed flush ordering).
    2. Last assistant line has ``message.stop_reason == "end_turn"`` AND the
       JSONL mtime hasn't advanced for ``quiet_seconds``. Used when the hook
       didn't fire (older claude versions, edge cases).

    Raises :class:`TimeoutError` if neither signal fires within ``timeout``.
    Malformed lines are silently skipped — claude occasionally emits split
    writes that may transiently look corrupt mid-flush.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    lines: list[dict[str, Any]] = []
    offset = since_byte
    user_seen = False
    buffer = b""
    last_change = loop.time()

    while loop.time() < deadline:
        chunk = b""
        if path.exists():
            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError:
                chunk = b""

        if chunk:
            offset += len(chunk)
            last_change = loop.time()
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                lines.append(obj)
                if obj.get("type") == "user":
                    user_seen = True
                elif (
                    user_seen
                    and obj.get("type") == "system"
                    and obj.get("subtype") == "stop_hook_summary"
                ):
                    return lines, offset
        else:
            quiet = loop.time() - last_change
            if user_seen and quiet >= quiet_seconds:
                for prev in reversed(lines):
                    if prev.get("type") != "assistant":
                        continue
                    stop_reason = (prev.get("message") or {}).get("stop_reason")
                    if stop_reason == "end_turn":
                        return lines, offset
                    break
        # Yield unconditionally at the end of every poll iteration. The
        # data-rich branch above used to skip this, which turned the loop
        # into a non-cooperative reader during high-rate JSONL flushes —
        # SSE writes to the connected client would back up until the file
        # went quiet. Moving the sleep here costs at most one extra
        # poll_interval per turn-end (acceptable) and guarantees the loop
        # always cooperates with the event loop.
        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"turn never completed within {timeout}s at {path}")
