"""Passive read-aloud (TTS) watcher — auto-speak assistant turns over tmux.

When the user types straight into the ttyd terminal (the default surface) NO
dashboard runner is dispatched, so the usual ``assistant_message`` SSE / React
auto-speak path never fires. This module fills that gap: a per-session
background task tails the session JSONL and, on each completed assistant turn,
publishes a ``speak`` event on the :class:`SessionEventHub` (process-lifetime,
independent of the turn runner). The browser's ``useReadAloud`` hook plays it.

Design:

* **Trigger** — reuses :func:`orchestrator_jsonl_tail.tail_until_turn_end`, so
  the end-of-turn signal is the deterministic ``stop_hook_summary`` marker
  (correct even with the user's gating Stop hooks, which a raw ``end_turn``
  check would mis-fire on). Poll is 0.05 s.
* **Anchoring + resume-following** — a freshly-armed watcher starts at the
  CURRENT end of the JSONL (no history replay), and re-resolves the path each
  loop so a session resumed mid-watch under a different cwd-slug (which writes a
  fresh ``<sid>.jsonl``) is followed instead of silently abandoned; a
  truncation/replacement (``size < offset``) re-anchors at the new EOF.
* **Ref-counting** — one task per session, kept alive while ≥1 client SSE is
  connected (``arm``/``disarm`` from the ``/read-aloud`` route's connect/finally).
* **Gating** — server flag ``read_aloud_tmux_enabled``; the device-scoped
  ``voiceOutput`` mode decides client-side whether the frame is actually spoken.

Event-loop safety: the ref-count + task dicts are mutated only from ``arm`` /
``disarm``, both invoked from async route handlers on the single uvicorn loop —
serial execution, no locks needed. Path resolution (a small directory scan +
stat) is off-loaded to a worker via ``asyncio.to_thread`` so it never blocks the
loop; the tail itself is small sync reads + an ``await asyncio.sleep`` per poll.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, NamedTuple

from . import orchestrator_events as events_mod
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_jsonl_tail as tail_mod
from . import orchestrator_settings as settings_mod

logger = logging.getLogger(__name__)

# Match the turn-tail poll; a fresh assistant turn becomes audible ~one poll
# after the stop-hook marker flushes.
POLL_INTERVAL_S: float = 0.05
# Single-tail cap: a watcher just keeps re-tailing, so a turn that never ends
# (or a quiet session) only costs one re-loop per timeout. Kept modest so the
# loop re-checks the flag + re-resolves the path (following a mid-watch cwd
# resume) at least this often even when the session is silent.
TAIL_TIMEOUT_S: float = 60.0

# A re-arm within this window of a disarm is treated as a reconnect (flap) and
# resumes the watcher from its saved byte offset, so a turn that completed while
# a mobile/car EventSource was between connections is re-read and re-published.
# Past this, a re-arm is a cold open → anchor at EOF (no history replay).
RESUME_GRACE_S: float = 90.0

_FLAG = "read_aloud_tmux_enabled"


class SpokenTurn(NamedTuple):
    """A completed assistant turn to read aloud.

    ``key`` (the assistant event uuid) de-dups replays on the client. Named so a
    text/key transposition across the extract → publish → JSON → JS boundary
    can't type-check cleanly.
    """

    text: str
    key: str


def is_enabled() -> bool:
    """True when the server flag is on. Indirected so callers + tests share it."""
    return settings_mod.get_flag(_FLAG) is True


def is_forwardable_frame(frame: bytes) -> bool:
    """True if a frame from the shared per-session bus belongs on this channel.

    The hub fans EVERY session event (artifact_created/open AND speak) to every
    subscriber; the read-aloud channel forwards only ``speak`` events plus
    keepalive comments (``: ping``), dropping artifact frames. Extracted +
    named so the one guarantee that artifact frames never leak here is unit-
    testable without an HTTP client.
    """
    return frame.startswith(b":") or b"event: speak\n" in frame


def _resolve_jsonl_path(session_id: str) -> Path:
    """Resolve the session's JSONL path (scans the claude project tree).

    Wrapped so tests can patch the resolution without touching ``jsonl_mod``.
    """
    return jsonl_mod.jsonl_path(session_id)


def _resolve_and_size(session_id: str) -> tuple[Path, int | None]:
    """Resolve the JSONL path AND its current byte size in one shot.

    Runs in a worker thread (``asyncio.to_thread``) because path resolution does
    a directory scan + per-dir stat — an fs walk that must not block the loop.

    ``size`` is ``None`` when the file is missing / un-stattable, so the caller
    can DISTINGUISH a transient stat failure from a genuine 0-byte file. Treating
    a failed stat as size 0 would make the re-anchor guard reset the offset to 0
    and replay (re-speak) the whole transcript.
    """
    path = _resolve_jsonl_path(session_id)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return path, size


def _read_from(path: Path, offset: int) -> bytes:
    """Read all bytes of ``path`` from ``offset`` to EOF (sync; run via to_thread).

    Returns ``b""`` on a transient OS error or when nothing new has been written.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:
        return b""


def _key_for(obj: dict[str, Any], text: str) -> str:
    """Stable de-dup key for a spoken turn.

    Prefers the per-event ``uuid`` (always present in real claude JSONL), then
    the API ``message.id``, finally a content hash so a malformed line without
    either still de-dups against itself on a reconnect replay.
    """
    uid = obj.get("uuid")
    if isinstance(uid, str) and uid:
        return uid
    mid = (obj.get("message") or {}).get("id")
    if isinstance(mid, str) and mid:
        return mid
    return "sha:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _assistant_line_text(obj: dict[str, Any]) -> SpokenTurn | None:
    """Return ``(text, key)`` for ONE assistant JSONL line carrying prose, else None.

    Concatenates the line's ``text`` content blocks (skips tool_use / thinking).
    Used for block-by-block reading — each completed assistant text block is read
    aloud as it flushes (intermediate progress + the final answer).
    """
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    parts = [
        block.get("text")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    text = "".join(p for p in parts if p).strip()
    if not text:
        return None
    return SpokenTurn(text, _key_for(obj, text))


def _extract_last_assistant(lines: list[dict[str, Any]]) -> SpokenTurn | None:
    """Return the LAST assistant line carrying prose, or ``None``."""
    for obj in reversed(lines):
        r = _assistant_line_text(obj)
        if r is not None:
            return r
    return None


class ReadAloudManager:
    """Ref-counted per-session JSONL watchers that publish ``speak`` events."""

    def __init__(self) -> None:
        self._refs: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Warm-resume across an SSE flap. The watcher stamps the byte position
        # of its last fully-consumed line here each poll; disarm records WHEN it
        # stopped. On a re-arm within RESUME_GRACE_S, the new watcher resumes
        # from that offset (re-reading the gap) instead of anchoring at EOF —
        # otherwise an assistant turn that completes while a mobile/car
        # EventSource is briefly between connections is PERMANENTLY lost (the
        # cancelled watcher never published it, and a fresh watcher anchors past
        # it). Validated 2026-06-13: a turn during a simulated flap vanished.
        # Guards (cur_path == saved_path AND saved_offset <= cur_size) keep a
        # /compact-driven file swap or truncation from re-speaking stale turns
        # eyes-free; the client's per-uuid de-dup (spokenKeysRef) is the
        # belt-and-suspenders against any overlap re-publish.
        self._offsets: dict[str, tuple[int, str]] = {}  # sid → (offset, path_str)
        self._disarmed_at: dict[str, float] = {}        # sid → monotonic time

    def arm(self, session_id: str) -> None:
        """Increment the ref-count; start the watcher on the 0→1 transition.

        No-op when the feature flag is off or ``session_id`` is empty. Starts a
        fresh watcher only if none is currently LIVE (a stranded done task is
        replaced rather than blocking the session forever). Call from the loop.
        """
        if not session_id or not is_enabled():
            return
        self._refs[session_id] = self._refs.get(session_id, 0) + 1
        if self._refs[session_id] != 1:
            return
        existing = self._tasks.get(session_id)
        if existing is not None and not existing.done():
            return
        # Warm resume if this session disarmed recently (a flap, not a cold open).
        resume_hint: tuple[int, str] | None = None
        saved = self._offsets.get(session_id)
        disarmed = self._disarmed_at.get(session_id, 0.0)
        if saved is not None and (time.monotonic() - disarmed) < RESUME_GRACE_S:
            resume_hint = saved
        self._tasks[session_id] = asyncio.create_task(self._watch(session_id, resume_hint))
        logger.info(
            "[read-aloud] armed %s (watcher started%s)",
            session_id, " RESUME@%d" % resume_hint[0] if resume_hint else "",
        )

    def disarm(self, session_id: str) -> None:
        """Decrement the ref-count; cancel the watcher on the 1→0 transition.

        Safe to call for a session that was never armed (no-op). The saved
        offset is KEPT (not cleared) so a reconnect within the grace window can
        resume from it.
        """
        cur = self._refs.get(session_id, 0)
        if cur <= 0:
            return
        if cur > 1:
            self._refs[session_id] = cur - 1
            return
        self._refs.pop(session_id, None)
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        self._disarmed_at[session_id] = time.monotonic()  # arm grace-resume
        logger.info("[read-aloud] disarmed %s (offset %s saved for resume)",
                    session_id, self._offsets.get(session_id, ("-",))[0])

    def refcount(self, session_id: str) -> int:
        """Live subscriber count for a session (for connect/disconnect logs)."""
        return self._refs.get(session_id, 0)

    async def _watch(self, session_id: str, resume_hint: tuple[int, str] | None = None) -> None:
        """Tail the JSONL forever; publish a ``speak`` event per assistant TEXT
        block AS IT FLUSHES (intermediate progress prose + the final answer),
        plus an ``end_flow`` marker on each turn's ``stop_hook_summary`` so the
        client can finalise its queued TTS. The client reads each block the
        moment it arrives and QUEUES the next — no cut-off.

        This is block-granular (not token-granular: the JSONL has no partial
        line). Each assistant content block is one complete JSONL line written
        when that block finishes, so a long tool-using turn reads its progress
        narration as it happens instead of going silent until the end.

        ``resume_hint`` ``(offset, path_str)`` — a warm reconnect within
        ``RESUME_GRACE_S``: start at the saved offset (re-reading any turn that
        flushed during the flap) instead of EOF, but ONLY if the resolved path
        is unchanged and the offset still fits the file (else a /compact swap or
        truncation would re-speak stale turns). Cold open → anchor at EOF.

        Crash-proof: the entire loop body is guarded; only ``CancelledError``
        (disarm) ends the watcher. ``seen`` de-dups by block key so a re-resolve
        never re-reads a block.
        """
        hub = events_mod.get_hub()
        path: Path | None = None
        offset = 0
        buffer = b""
        seen: set[str] = set()
        try:
            while True:
                try:
                    # Runtime kill-switch: stop tailing/publishing the instant the
                    # flag is flipped off (the client also closes the SSE; don't
                    # depend on it).
                    if not is_enabled():
                        await asyncio.sleep(1.0)
                        continue
                    cur_path, cur_size = await asyncio.to_thread(_resolve_and_size, session_id)
                    if path is None:
                        # First resolve. Warm resume if the hint is still valid
                        # (same file, offset within bounds), else anchor at EOF.
                        anchor = cur_size or 0
                        if (resume_hint is not None and cur_size is not None
                                and resume_hint[1] == str(cur_path)
                                and 0 <= resume_hint[0] <= cur_size):
                            anchor = resume_hint[0]
                            logger.info(
                                "[read-aloud] %s warm-resume from %d (gap %d bytes)",
                                session_id, anchor, cur_size - anchor,
                            )
                        path, offset, buffer = cur_path, anchor, b""
                    elif cur_path != path or (cur_size is not None and cur_size < offset):
                        # Resume under a new cwd-slug / genuine truncation → re-anchor
                        # at the new EOF (a transient stat failure, cur_size None, is
                        # NOT treated as truncation, so offset can't reset to 0).
                        path, offset, buffer = cur_path, (cur_size or 0), b""
                    chunk = await asyncio.to_thread(_read_from, path, offset)
                    if not chunk:
                        await asyncio.sleep(POLL_INTERVAL_S)
                        continue
                    offset += len(chunk)
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if not raw.strip():
                            continue
                        try:
                            obj = json.loads(raw.decode("utf-8", "replace"))
                        except Exception:  # noqa: BLE001 — a split flush can look corrupt mid-write
                            continue
                        if not isinstance(obj, dict):
                            continue
                        otype = obj.get("type")
                        if otype == "assistant":
                            turn = _assistant_line_text(obj)
                            if turn is None or turn.key in seen:
                                continue
                            seen.add(turn.key)
                            logger.info(
                                "[read-aloud] speak block %s (key=%s, %d chars)",
                                session_id, turn.key, len(turn.text),
                            )
                            # buffer=False: speak frames are ephemeral and must not
                            # evict must-deliver artifact frames from the shared buffer.
                            hub.publish(
                                session_id, "speak",
                                {"text": turn.text, "key": turn.key}, buffer=False,
                            )
                        elif otype == "system" and obj.get("subtype") == "stop_hook_summary":
                            # Turn boundary → tell the client to finalise its TTS
                            # queue so 'speaking' clears once the queue drains.
                            hub.publish(
                                session_id, "speak",
                                {"text": "", "key": "end", "end_flow": True}, buffer=False,
                            )
                    # Stamp the position of the last fully-consumed line (offset
                    # minus the still-unparsed trailing partial) so a disarm then
                    # re-arm within the grace window resumes exactly here — the
                    # gap a car/mobile EventSource missed gets re-read.
                    if path is not None:
                        self._offsets[session_id] = (offset - len(buffer), str(path))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — a watcher must never crash
                    logger.exception("[read-aloud] watcher error for %s", session_id)
                    await asyncio.sleep(1.0)
                    continue
        except asyncio.CancelledError:
            return


_manager: ReadAloudManager | None = None


def get_manager() -> ReadAloudManager:
    """Lazy module-level singleton (mirrors ``orchestrator_events.get_hub``)."""
    global _manager
    if _manager is None:
        _manager = ReadAloudManager()
    return _manager
