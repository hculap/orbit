"""Persistent, server-initiated SSE hub — decoupled from the turn runner.

The turn :class:`ClaudeRunner` (``orchestrator_runner``) reaps its broadcast
channel ``REAP_GRACE_S`` (60s) after a turn finishes, so it cannot deliver
events that arrive *between* turns — e.g. an ``artifact open`` the agent
triggers while the user is just watching the terminal. This hub lives for the
whole process and keeps a per-session fan-out of EventSource queues, so artifact
toasts/modals fire regardless of turn state.

Mirrors ``orchestrator_runner``'s ``_format_sse`` / ``_broadcast`` / ``subscribe``
/ keepalive, but standalone: no subprocess, no turn lifecycle, no dependency on
``_active_runs``. The HTTP route (``GET /sessions/{sid}/events``) is a closure in
``orchestrator.register_routes`` mirroring ``_stream_handler``; this module stays
free of FastAPI so it's trivially unit-testable.
"""
from __future__ import annotations
import asyncio
import collections
import json
from typing import Any

KEEPALIVE_INTERVAL_S: float = 15.0
# Sparse control events (artifact created/open), not token deltas — a small
# replay buffer is plenty for Last-Event-ID resume across a brief reconnect.
MAX_BUFFERED_EVENTS: int = 200


def _format_sse(event: str, data: dict[str, Any], seq: int | None = None) -> bytes:
    """Encode one SSE frame: optional ``id:`` + ``event:`` + ``data:`` + blank line."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    head = f"id: {seq}\n" if seq is not None else ""
    return (head + f"event: {event}\ndata: {payload}\n\n").encode("utf-8")


class SessionEventHub:
    """Per-session pub/sub of SSE byte-frames, alive for the whole process."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[bytes | None]]] = {}
        self._buffers: dict[str, collections.deque[bytes]] = {}
        self._seq: dict[str, int] = {}
        self._keepalive_task: asyncio.Task[None] | None = None

    # ── publish / subscribe ───────────────────────────────────────

    def publish(
        self, session_id: str, event: str, data: dict[str, Any], *, buffer: bool = True
    ) -> None:
        """Fan an event out to every subscriber of ``session_id``.

        ``buffer=False`` skips the per-session replay deque (still fans out live).
        Use it for high-frequency, ephemeral events (e.g. read-aloud ``speak``
        frames) so they don't evict must-deliver, low-frequency events
        (``artifact_open``) from the shared 200-slot buffer before a Last-Event-ID
        resume can replay them.
        """
        if not session_id:
            return
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        formatted = _format_sse(event, data, seq=seq)
        if buffer:
            buf = self._buffers.setdefault(
                session_id, collections.deque(maxlen=MAX_BUFFERED_EVENTS)
            )
            buf.append(formatted)
        for q in self._subscribers.get(session_id, []):
            try:
                q.put_nowait(formatted)
            except asyncio.QueueFull:
                pass  # unbounded queue — defensive only

    def subscribe(
        self, session_id: str, last_event_id: int | None = None
    ) -> asyncio.Queue[bytes | None]:
        """Register a subscriber queue.

        Replay only happens on RESUME (a ``Last-Event-ID`` was supplied) so a
        reconnect after a blip doesn't miss an ``artifact_open``. A FRESH
        connect (``last_event_id is None``) replays NOTHING — these are
        notifications, not a transcript: a new page load must not get a storm
        of every past toast/modal in the buffer.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        buf = self._buffers.get(session_id)
        if buf and last_event_id is not None:
            for evt in buf:
                head, _, _ = evt.partition(b"\n")
                if head.startswith(b"id: "):
                    try:
                        ev_seq = int(head[4:])
                    except ValueError:
                        continue
                    if ev_seq > last_event_id:
                        queue.put_nowait(evt)
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[bytes | None]) -> None:
        """Drop a subscriber; free per-session state once the last one leaves.

        On full disconnect we drop the subscriber list AND the replay buffer
        (the bulk of the memory — a maxlen deque per session) so the dicts
        can't grow unbounded across many short-lived panel opens. We KEEP
        ``_seq`` (a tiny monotonic int): resetting it would let a reconnecting
        client's ``Last-Event-ID`` suppress fresh events (``ev_seq > last_id``
        would be false). Losing replay across a *full* disconnect is fine for a
        notification channel — a fresh connect replays nothing anyway.
        """
        subs = self._subscribers.get(session_id)
        if not subs:
            return
        try:
            subs.remove(queue)
        except ValueError:
            pass
        if not subs:
            self._subscribers.pop(session_id, None)
            self._buffers.pop(session_id, None)

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def shutdown(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._keepalive_task = None
        for subs in list(self._subscribers.values()):
            for q in subs:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        self._subscribers.clear()

    async def _keepalive_loop(self) -> None:
        """Ping every subscriber across all sessions so proxies don't reap idle SSE."""
        ping = b": ping\n\n"
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                for subs in list(self._subscribers.values()):
                    for q in subs:
                        try:
                            q.put_nowait(ping)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            return


_hub: SessionEventHub | None = None


def get_hub() -> SessionEventHub:
    """Lazy module-level singleton (mirrors ``orchestrator._get_tmux_pool``)."""
    global _hub
    if _hub is None:
        _hub = SessionEventHub()
    return _hub
