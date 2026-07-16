"""Interactive-mode runner: drives a long-lived ``claude`` via the tmux pool.

Drop-in replacement for :class:`ClaudeRunner` (the legacy ``claude -p`` per-turn
runner). The orchestrator dispatch at ``_post_message_handler`` instantiates one
of these instead when ``runner_mode == "interactive"`` (global flag) or when the
POST body sets ``interactive_mode: true`` (per-request override).

Same public contract as ClaudeRunner:

* ``subscribe(last_event_id=None) → asyncio.Queue[bytes | None]``
* ``start_turn(user_text) → None`` (long-running)
* ``cancel() → None``
* ``status_snapshot() → dict``
* ``_done: asyncio.Event``
* ``subscribers: list[asyncio.Queue]``
* ``_buffered_events: deque[bytes]``
* ``_seq: int``

Routing differences:

* Bills against the user's interactive subscription (via tmux ``-e`` env
  scrub of ANTHROPIC_API_KEY) — NOT the post-2026-06-15 programmatic credit
  pool.
* End-of-turn detection comes from JSONL ``stop_hook_summary`` rather than
  the stdout result event, so token streaming is replaced by a single
  "thinking…" placeholder + the final assistant markdown.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from pathlib import Path
from typing import Any

from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_jsonl_tail as tail_mod
from . import orchestrator_prompts as prompts_mod
from . import orchestrator_runner as legacy_runner
from . import orchestrator_tmux as tmux_mod

# Re-export so tests can monkeypatch via this module rather than reaching
# into orchestrator_jsonl_tail directly.
tail_until_turn_end = tail_mod.tail_until_turn_end

HOME: Path = Path(os.environ.get("HOME", str(Path.home())))
PROMPT_PATH: Path = prompts_mod.SYSTEM_PROMPT_PATH
MAX_BUFFERED_EVENTS: int = legacy_runner.MAX_BUFFERED_EVENTS
TOOL_RESULT_TRUNCATE_BYTES: int = legacy_runner.TOOL_RESULT_TRUNCATE_BYTES
# Mirror the legacy reap grace window so late SSE reconnects can still replay
# the buffered tail. Patched in tests to 0.0 so the deferred _reap fires on the
# next event loop tick.
REAP_GRACE_S: float = legacy_runner.REAP_GRACE_S

# Default turn timeout for the JSONL tail. Generous — claude tool-using turns
# can take a couple of minutes for multi-step agentic work. The legacy
# `claude -p` runner caps via the subprocess; we cap via the tail.
TURN_TAIL_TIMEOUT_S: float = 600.0


class TmuxClaudeRunner:
    """One in-flight turn driving a long-lived tmux+claude session."""

    def __init__(
        self,
        session_id: str,
        *,
        pool: tmux_mod.TmuxPool,
        cwd: Path | None = None,
        append_system_prompt_paths: list[Path] | None = None,
        # Accepted for interface parity with ClaudeRunner even though
        # interactive mode doesn't change runtime behavior for these yet
        # (model selection is a session-start thing, not a per-turn flag).
        has_run_before: bool = False,
        model: str | None = None,
        extra_prompt_path: Path | None = None,
        agent_skills_dir: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.has_run_before = has_run_before
        self.model = model.strip() if isinstance(model, str) and model.strip() else None
        self._pool = pool
        self._cwd = Path(cwd) if cwd is not None else HOME
        # Per-spawn env (scope `.env` secrets) forwarded to build_spawn_cmd as
        # `-e K=V`. Honored ONLY on a fresh spawn — see TmuxPool.acquire.
        self._env_extra = dict(env_extra) if env_extra else None
        self._append_paths: list[Path] = (
            [Path(p) for p in append_system_prompt_paths] if append_system_prompt_paths else []
        )
        self._extra_prompt_path = Path(extra_prompt_path) if extra_prompt_path else None
        self._agent_skills_dir = Path(agent_skills_dir) if agent_skills_dir else None

        # SSE plumbing — mirrors ClaudeRunner so the stream handler doesn't
        # need to know which class it's talking to.
        self.subscribers: list[asyncio.Queue[bytes | None]] = []
        self._done: asyncio.Event = asyncio.Event()
        self._buffered_events: collections.deque[bytes] = collections.deque(
            maxlen=MAX_BUFFERED_EVENTS
        )
        self._seq: int = 0
        self._started_at_ms: int = int(time.time() * 1000)
        self._cancelled: bool = False
        self._turn_idx: int = 0
        # Terminal ("done"/"error") event captured in _broadcast, consumed by
        # _finalize to emit the persistent turn_done/turn_error lifecycle event
        # (shared helper in legacy_runner). Mirrors ClaudeRunner.
        self._final_event: tuple[str, dict[str, Any]] | None = None
        # Track our position in the JSONL across turns so re-acquires of the
        # same tmux slot read only new lines.
        self._jsonl_offset: int = 0
        # Set in start_turn so cancel() can break the tail without waiting
        # for the next poll iteration. Cleared on natural completion.
        self._tail_task: asyncio.Task[Any] | None = None
        # Set in start_turn before awaiting `pool.acquire`. Cancelling it
        # propagates through `wait_until_ready`'s long poll so a user-issued
        # cancel during cold start exits in <1 s instead of waiting out the
        # 60 s readiness timeout (Phase 2A.1).
        self._acquire_task: asyncio.Task[Any] | None = None

    # ── subscribe ──────────────────────────────────────────────────

    def subscribe(self, last_event_id: int | None = None) -> asyncio.Queue[bytes | None]:
        """Register a subscriber + replay buffered events.

        Identical contract to :meth:`ClaudeRunner.subscribe` so the SSE
        stream handler keeps working unchanged.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        if last_event_id is None:
            for evt in self._buffered_events:
                queue.put_nowait(evt)
        else:
            for evt in self._buffered_events:
                head, _, _ = evt.partition(b"\n")
                if head.startswith(b"id: "):
                    try:
                        ev_seq = int(head[4:])
                    except ValueError:
                        continue
                    if ev_seq > last_event_id:
                        queue.put_nowait(evt)
        if self._done.is_set():
            queue.put_nowait(None)
        self.subscribers.append(queue)
        return queue

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "in_flight": not self._done.is_set(),
            "started_at_ms": self._started_at_ms,
            "last_seq": self._seq,
        }

    # ── broadcast helpers ──────────────────────────────────────────

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        self._seq += 1
        formatted = legacy_runner._format_sse(event, data, seq=self._seq)
        self._buffered_events.append(formatted)
        if event in ("done", "error"):
            self._final_event = (event, data)
        for q in self.subscribers:
            try:
                q.put_nowait(formatted)
            except asyncio.QueueFull:
                pass

    def _close_subscribers(self) -> None:
        for q in self.subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ── main loop ──────────────────────────────────────────────────

    async def start_turn(self, user_text: str) -> None:
        """Acquire a pool slot, send the prompt, tail JSONL → broadcast SSE."""
        # Early-cancel guard: a user cancel POST can land between the
        # dispatcher's `asyncio.create_task(start_turn(...))` and this task
        # actually running. In that window `_acquire_task` is still None, so
        # `cancel()` skipped cancelling it and only set `_done`. Without this
        # check, we'd happily spawn a slot and fire pipe_prompt for a turn
        # the user already abandoned (R3 finding on PR #40).
        if self._cancelled or self._done.is_set():
            return
        # Forward the per-agent prompt stack + skills dir into the pool so
        # they reach build_spawn_cmd on a fresh spawn. Reused slots ignore
        # these — see TmuxPool.acquire docstring for the rationale.
        add_dirs = [self._agent_skills_dir] if self._agent_skills_dir is not None else None
        # Cold-start UX: peek at the pool BEFORE the (potentially long)
        # acquire. If we'd have to spawn from scratch — claude boots in
        # 10-20 s on Mac — emit a `spawning` event immediately so the
        # frontend can render a placeholder with a timer. Skipped for
        # warm reuse where acquire returns in milliseconds.
        if not self._pool.has_warm_slot(self.session_id):
            self._broadcast("spawning", {
                "session_id": self.session_id,
                "started_at_ms": int(time.time() * 1000),
            })
        # If a JSONL already exists for this session_id + cwd (e.g. the user
        # is returning to a previously-used session after a pool restart or
        # cooldown expiry), spawning with bare `--session-id` would make
        # claude exit with "Session ID is already in use". Use `--resume`
        # instead so claude reopens the existing transcript. The detection
        # uses our slug helper, which matches claude's actual file layout.
        existing_jsonl = tail_mod.jsonl_path_for(self._cwd, self.session_id)
        wants_resume = existing_jsonl.is_file()
        # Wrap acquire in a Task so `cancel()` can break the in-flight
        # readiness wait. Without this, a user-issued cancel during cold
        # start waits out the full 60 s wait_until_ready timeout before
        # surfacing a misleading "failed to acquire" error.
        self._acquire_task = asyncio.create_task(
            self._pool.acquire(
                session_id=self.session_id,
                cwd=self._cwd,
                append_system_prompt_paths=self._append_paths,
                add_dirs=add_dirs,
                resume=wants_resume,
                model=self.model,
                env_extra=self._env_extra,
            )
        )
        try:
            slot = await self._acquire_task
        except asyncio.CancelledError:
            # cancel() already broadcast "error: cancelled" and finalized.
            # The pool's acquire tore down the half-spawned tmux session in
            # its own except-handler before re-raising. Just exit cleanly.
            if self._done.is_set():
                return
            self._broadcast("error", {"message": "cancelled"})
            self._finalize()
            return
        except Exception as exc:  # noqa: BLE001 — surface to client, never crash
            self._broadcast("error", {"message": f"failed to acquire tmux slot: {exc}"})
            self._finalize()
            return

        # Re-check after acquire: a cancel that landed AFTER the task was
        # created but BEFORE wait_until_ready started yielding would have
        # marked us cancelled without firing CancelledError through the
        # awaiting task path. Bail out before pipe_prompt sends user text
        # that they already cancelled.
        if self._cancelled or self._done.is_set():
            return

        self._broadcast("init", {
            "model": self.model,
            "session_id": self.session_id,
            "cwd": str(slot.cwd),
            "mode": "interactive",
        })
        # No token streaming under interactive mode — emit a placeholder so
        # the frontend can render a "thinking…" spinner immediately instead
        # of leaving the bubble empty during the wait.
        self._broadcast("thinking", {"turn_idx": self._turn_idx})

        # Anchor the tail at the CURRENT end of the JSONL BEFORE we send the
        # prompt. Each turn instantiates a fresh runner with _jsonl_offset=0;
        # without this anchor, the tail re-reads from byte 0 and returns
        # the very first user/assistant/stop_hook trio in the file — broadcasting
        # the FIRST turn's response as if it were the current turn's. UI then
        # shows "Cześć! W czym mogę pomóc?" for every subsequent prompt even
        # though claude is generating different content (observed UAT
        # 2026-05-15: 3 different prompts → same stale response in 3 bubbles).
        jsonl_path = tail_mod.jsonl_path_for(self._cwd, self.session_id)
        try:
            self._jsonl_offset = jsonl_path.stat().st_size if jsonl_path.exists() else 0
        except OSError:
            self._jsonl_offset = 0

        try:
            await self._pool.pipe_prompt(self.session_id, user_text)
        except Exception as exc:  # noqa: BLE001
            self._broadcast("error", {"message": f"failed to send prompt: {exc}"})
            self._finalize()
            return

        # Wrap the tail in a task so cancel() can interrupt the sleep loop
        # without waiting for the next poll iteration to notice _cancelled.
        self._tail_task = asyncio.create_task(
            tail_until_turn_end(
                jsonl_path,
                since_byte=self._jsonl_offset,
                timeout=TURN_TAIL_TIMEOUT_S,
            )
        )
        try:
            lines, end_offset = await self._tail_task
        except asyncio.CancelledError:
            if self._done.is_set():
                # cancel() already broadcast + finalized; just return cleanly.
                return
            self._broadcast("error", {"message": "cancelled"})
            self._finalize()
            return
        except TimeoutError:
            self._broadcast("error", {"message": "turn timed out waiting for JSONL flush"})
            self._finalize()
            return
        except Exception as exc:  # noqa: BLE001
            self._broadcast("error", {"message": f"tail error: {exc}"})
            self._finalize()
            return

        if self._cancelled:
            self._finalize()
            return

        self._jsonl_offset = end_offset

        # Map JSONL lines → SSE events. The order matters: tool_use blocks
        # surface BEFORE the matching tool_result; the final text envelope is
        # rendered AFTER all tool roundtrips.
        for obj in lines:
            etype = obj.get("type")
            if etype == "assistant":
                self._handle_assistant(obj)
            elif etype == "user":
                self._handle_user(obj)
            # system / stop_hook_summary intentionally not surfaced — the
            # `done` event below is the closer.

        self._broadcast("done", {"reason": "turn complete"})
        self._finalize()

    # ── event mapping ──────────────────────────────────────────────

    def _handle_assistant(self, evt: dict[str, Any]) -> None:
        """Translate one assistant JSONL line → SSE events.

        Mirrors the relevant branches of :meth:`ClaudeRunner._handle_assistant`:
        thinking + tool_use blocks ride on ``assistant_message``; user-facing
        text is emitted as plain markdown on ``assistant_message`` too (the
        envelope pipeline was removed — Claude writes prose, not JSON).
        """
        msg = evt.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            return

        text_parts: list[str] = []
        thinking_blocks: list[dict[str, Any]] = []
        tool_use_blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if text:
                    text_parts.append(text)
            elif btype == "thinking":
                thought = block.get("thinking") or block.get("text") or ""
                if thought:
                    thinking_blocks.append({"kind": "thinking", "text": thought})
            elif btype == "tool_use":
                tool_use_blocks.append({
                    "kind": "tool_use",
                    "tool_use_id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "input": block.get("input") or {},
                })

        full_text = "".join(text_parts)

        if thinking_blocks or tool_use_blocks:
            self._broadcast("assistant_message", {
                "turn_idx": self._turn_idx,
                "blocks": thinking_blocks + tool_use_blocks,
            })

        if full_text:
            self._broadcast("assistant_message", {
                "turn_idx": self._turn_idx,
                "blocks": [{"kind": "markdown", "text": full_text}],
            })

        if not (thinking_blocks or tool_use_blocks or full_text):
            return
        self._turn_idx += 1

    def _handle_user(self, evt: dict[str, Any]) -> None:
        """Translate a tool_result-bearing user line → ``tool_result`` SSE."""
        msg = evt.get("message") or {}
        content = msg.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            if isinstance(raw, list):
                output = "\n".join(
                    b.get("text", "") for b in raw
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            elif isinstance(raw, str):
                output = raw
            else:
                output = ""
            self._broadcast("tool_result", {
                "tool_use_id": block.get("tool_use_id") or "",
                "stdout": legacy_runner._truncate_text(output),
                "is_error": bool(block.get("is_error", False)),
                "ms": block.get("duration_ms") or 0,
            })

    # ── cancel ─────────────────────────────────────────────────────

    async def cancel(self) -> None:
        """Mark the runner cancelled and break in-flight async work.

        Cancellation is wired through *two* tasks so it propagates whether
        the runner is mid-spawn (acquire) or mid-response (tail):

        * ``_acquire_task`` — set in :meth:`start_turn` before awaiting
          ``pool.acquire``. Cancelling it raises ``CancelledError`` through
          ``wait_until_ready``'s poll loop, which the pool catches in a
          try/finally to teardown the half-spawned tmux session before
          re-raising.
        * ``_tail_task`` — set right before awaiting the JSONL tail. Cancelling
          it unblocks the tail's poll/sleep loop and surfaces the cancel
          back to ``start_turn``.

        **Slot release policy**: cancel does NOT call ``pool.release()``.
        Rationale:

        * Cold-starting claude (spawn + ``wait_until_ready``) is ~9–20 s
          empirically. Tearing the slot down on cancel means the user's next
          turn on the same session eats the full cold start.
        * Leaving the slot alive means the next turn is "instant" (warm
          slot, sub-second). The pool's idle-evictor (10 min TTL default)
          reclaims slots the user doesn't actually return to.
        * The RAM cost of a warm claude (~1.3 GB on Mac) is bounded by
          ``POOL_SIZE`` (default 4) — worst case ~5 GB, well within the
          Hetzner box's headroom.

        If the cancel was issued because claude looped or jammed (rather
        than because the user changed their mind), the next user input
        will either steer claude out of it or hit the same problem again
        — same outcome whether we released the slot or not. Manually
        evicting via the dashboard's DELETE
        ``/api/orchestrator/sessions/{id}`` endpoint tears the slot down
        deterministically (the handler calls ``pool.release(session_id)``
        after cancelling).
        """
        if self._done.is_set():
            return
        self._cancelled = True
        self._broadcast("error", {"message": "cancelled"})
        # Cancel both in-flight tasks so start_turn wakes up immediately,
        # whichever phase it's in.
        if self._acquire_task is not None and not self._acquire_task.done():
            self._acquire_task.cancel()
        if self._tail_task is not None and not self._tail_task.done():
            self._tail_task.cancel()
        self._finalize()

    # ── finalize ───────────────────────────────────────────────────

    def _finalize(self) -> None:
        """Mark done, close subscribers, defer registry pop + buffer release.

        Mirrors :meth:`ClaudeRunner._finalize`'s reap-grace window so a
        late-reconnecting client (e.g. tab reopened seconds after the turn
        finished) can still subscribe + replay the buffered tail before the
        runner is dropped from `_active_runs` and its event deque cleared.
        """
        if self._done.is_set():
            return
        self._done.set()
        try:
            jsonl_mod.invalidate_cache(self.session_id)
        except Exception:
            pass
        # Emit the persistent turn_done/turn_error lifecycle event (shared
        # mapping helper). After invalidate_cache so the transcript idx re-reads
        # the final turn. The tail timeout/cancel paths all funnel through here,
        # so an external orchestrator's /wait can never hang forever.
        legacy_runner.emit_turn_lifecycle(
            self.session_id,
            final_event=self._final_event,
            cancelled=self._cancelled,
        )
        self._close_subscribers()
        self.subscribers.clear()
        try:
            asyncio.get_running_loop().call_later(REAP_GRACE_S, self._reap)
        except RuntimeError:
            # No running loop (synchronous test teardown, etc.) — reap inline.
            self._reap()

    def _reap(self) -> None:
        """Drop runner from `_active_runs` + release the event buffer.

        Only pops the registry entry if it still points at this exact runner —
        a newer turn for the same session may have replaced us in the meantime.
        """
        if legacy_runner._active_runs.get(self.session_id) is self:
            legacy_runner._active_runs.pop(self.session_id, None)
        self._buffered_events.clear()
