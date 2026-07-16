"""Long-poll primitive: block until the next assistant turn of a session lands.

This is the request/response keystone for an external orchestrator (an MCP
client): it POSTs a message, then GETs ``/wait`` and blocks — bounded — until
the turn it started either produces new assistant messages (``done``), fails
(``error``), or the window elapses (``timeout``). It rides the persistent
:class:`SessionEventHub` lifecycle events (``turn_done`` / ``turn_error``)
published by both runners' ``_finalize``.

Design (each point closes a concrete failure mode an unattended client hits):

* **Subscribe FIRST, then read the transcript.** A terminal event firing in
  the gap between a read-then-subscribe would be lost and the caller would hang
  the full window. Subscribing first guarantees the frame lands in our queue;
  the transcript pre-check then just short-circuits when already advanced.
* **One monotonic deadline across the whole loop.** Re-arming a fresh
  ``wait_for(timeout)`` every time we skip a keepalive frame would let a client
  receiving a 15 s ping never time out — pinning the worker forever. We compute
  the deadline once and pass the shrinking remainder to each ``wait_for``.
* **Only ``turn_done`` / ``turn_error`` resolve.** Keepalive comments,
  ``turn_started``, ``artifact_*`` and ``speak`` frames on the same per-session
  bus are ignored so a mid-turn artifact toast can't resolve ``/wait`` early
  with empty ``new_messages``.
* **Unsubscribe in ``finally``** (covering client-disconnect ``CancelledError``)
  so the hub's per-session subscriber list can't leak.
* **A process-wide concurrency cap** (`await_turn_guarded`) returns a
  ``WaitSaturated`` signal (→ HTTP 429) so a client can't open hundreds of
  blocking waiters and exhaust the worker.

FastAPI-free on purpose — the route closure in ``orchestrator.register_routes``
wraps :func:`await_turn_guarded` with the session-id validator, mirroring how
``orchestrator_events`` stays import-light and trivially unit-testable.

Contract note: ``since_turn`` is the transcript-global, positional ``turn_idx``
(see ``orchestrator_runner.transcript_turn_idx``). It is NOT a stable id — an
out-of-band turn injected under a different cwd slug renumbers the merged
transcript, so a client must treat it as a cursor it re-reads, not a key.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

WAIT_TIMEOUT_MAX_S: float = 60.0
WAIT_TIMEOUT_DEFAULT_S: float = 25.0
# One MCP client needs one waiter per session it's driving; 32 is generous
# headroom while still bounding worker/queue growth under a runaway client.
WAIT_MAX_CONCURRENT: int = 32

_wait_inflight: int = 0


class WaitSaturated(Exception):
    """Raised by :func:`await_turn_guarded` when the concurrency cap is hit."""


def _assistant_after(messages: list[dict[str, Any]], since_turn: int) -> list[dict[str, Any]]:
    """Assistant messages whose transcript ``turn_idx`` is strictly past the cursor."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        try:
            if int(m.get("turn_idx", -1)) > since_turn:
                out.append(m)
        except (TypeError, ValueError):
            continue
    return out


def _latest_turn_idx(messages: list[dict[str, Any]]) -> int:
    return max((int(m.get("turn_idx", -1)) for m in messages
                if isinstance(m.get("turn_idx"), int)), default=-1)


def _result(status: str, since_turn: int, messages: list[dict[str, Any]],
            new: list[dict[str, Any]], cost_usd: Any, error: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "since_turn": since_turn,
        "latest_turn_idx": _latest_turn_idx(messages),
        "new_messages": new,
        "cost_usd": cost_usd,
        "error": error,
    }


def _parse_terminal_frame(raw: bytes) -> tuple[str, dict[str, Any]] | None:
    """Decode an SSE byte-frame → ``(event, data)`` iff it's a terminal frame.

    Returns ``None`` for keepalive comments (``: ping``), non-terminal events
    (``turn_started`` / ``artifact_*`` / ``speak``), or undecodable frames — the
    wait loop treats ``None`` as "keep waiting".
    """
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    event: str | None = None
    data_line: str | None = None
    for line in text.split("\n"):
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_line = line[len("data:"):].strip()
    if event not in ("turn_done", "turn_error"):
        return None
    try:
        data = json.loads(data_line) if data_line else {}
    except Exception:  # noqa: BLE001 — malformed payload still signals terminal
        data = {}
    if not isinstance(data, dict):
        data = {}
    return (event, data)


async def await_turn(
    session_id: str,
    since_turn: int,
    timeout_s: float,
    *,
    hub: Any,
    read_session: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Block until an assistant turn past ``since_turn`` lands, or ``timeout_s``.

    ``hub`` is a :class:`SessionEventHub`; ``read_session`` is
    ``orchestrator_jsonl.read_session`` (injected for testability). Returns a
    dict with ``status`` in ``{"done","error","timeout"}``.
    """
    timeout_s = max(0.001, min(float(timeout_s), WAIT_TIMEOUT_MAX_S))
    try:
        since_turn = max(-1, int(since_turn))
    except (TypeError, ValueError):
        since_turn = -1

    # Subscribe-first: fresh connect (last_event_id=None) replays nothing, so we
    # only catch frames published from here on — the transcript pre-check covers
    # a turn that already finished.
    queue = hub.subscribe(session_id, None)
    try:
        snap = await asyncio.to_thread(read_session, session_id)
        messages = snap.get("messages") or []
        new = _assistant_after(messages, since_turn)
        if new:
            return _result("done", since_turn, messages, new, None, None)

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _result("timeout", since_turn, messages, [], None, None)
            try:
                raw = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                return _result("timeout", since_turn, messages, [], None, None)
            if raw is None:  # hub shutdown sentinel
                return _result("timeout", since_turn, messages, [], None, None)
            parsed = _parse_terminal_frame(raw)
            if parsed is None:
                continue  # keepalive / turn_started / artifact / speak → keep waiting
            event, data = parsed
            snap = await asyncio.to_thread(read_session, session_id)
            messages = snap.get("messages") or []
            new = _assistant_after(messages, since_turn)
            if event == "turn_error":
                return _result("error", since_turn, messages, new,
                               data.get("cost_usd"), data.get("message") or "error")
            return _result("done", since_turn, messages, new, data.get("cost_usd"), None)
    finally:
        hub.unsubscribe(session_id, queue)


async def await_turn_guarded(
    session_id: str,
    since_turn: int,
    timeout_s: float,
    *,
    hub: Any,
    read_session: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """:func:`await_turn` behind a process-wide concurrency cap.

    Single-threaded asyncio means the counter check + increment can't race
    (no await between them), so no lock is needed. Raises :class:`WaitSaturated`
    (→ HTTP 429) when the cap is reached.
    """
    global _wait_inflight
    if _wait_inflight >= WAIT_MAX_CONCURRENT:
        raise WaitSaturated()
    _wait_inflight += 1
    try:
        return await await_turn(session_id, since_turn, timeout_s,
                                hub=hub, read_session=read_session)
    finally:
        _wait_inflight -= 1
