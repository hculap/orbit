"""Orchestrator session compaction — summarize an old session, seed a new one.

The "Compact" button collapses a long-running session into a fresh one that
starts from a tight summary. The flow:

1. Read the OLD session JSONL → walk backwards for the latest TodoWrite
   tool_use so active todos survive the jump.
2. Spawn a `claude --resume <old_id>` turn against the OLD session asking for
   a markdown summary of the conversation. Wait for the runner's
   ``structured_blocks`` SSE event by subscribing as a frontend client would.
3. Generate a fresh UUID; spawn `claude --session-id <new_uuid>` with a seed
   message wrapping the summary + active todos. The new session's JSONL is
   created by the subprocess.
4. Stamp sidecars: old.compacted_to = new_id, new.compacted_from = old_id,
   new.title = "Compact: <old title or first user preview>".

Plans referenced in the summary live as files under ``~/.claude/plans/``
(cwd-scoped); they're accessible from any session, so we don't transfer them
explicitly — the seed message just refers to them by path and the new session
reads them via the Read tool when needed.
"""
from __future__ import annotations
import asyncio
import json
import re
import uuid
from pathlib import Path

from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from . import orchestrator_runner as runner_mod


def _make_compact_runner(session_id: str, *, has_run_before: bool):
    """Construct the compaction turn runner per the ``runner_mode`` flag.

    ``interactive`` (default) → ``TmuxClaudeRunner`` (Max subscription);
    ``programmatic`` → legacy ``ClaudeRunner`` (``claude -p`` credit pool) as
    rollback. Both expose the same SSE contract (``structured_blocks`` /
    ``assistant_message`` / ``done`` / ``error``) so the queue-drain logic below
    is identical. For the interactive case we resolve the session's
    cwd/model/prompt-stack from its sidecar so ``--resume`` lands under the
    correct project slug (TmuxClaudeRunner keys the JSONL path on cwd).
    """
    try:
        from . import orchestrator_settings as _settings
        mode = _settings.resolve_runner_mode("runner_mode")
    except Exception:  # noqa: BLE001 — settings unavailable → subscription default
        mode = "interactive"
    if mode != "interactive":
        return runner_mod.ClaudeRunner(session_id, has_run_before=has_run_before)
    from . import agent_prompts as _prompts_mod
    from . import orchestrator as _orch
    from . import orchestrator_runner_tmux as _runner_tmux
    from .discovery import HOME as _HOME
    sidecar = meta_mod.get_meta(session_id) or {}
    cwd_str = sidecar.get("cwd") if isinstance(sidecar, dict) else None
    lib_id = (
        sidecar.get("lib_id")
        if isinstance(sidecar, dict) and isinstance(sidecar.get("lib_id"), str)
        else None
    )
    model = sidecar.get("model") if isinstance(sidecar, dict) else None
    cwd_path = Path(cwd_str) if isinstance(cwd_str, str) and cwd_str else _HOME
    try:
        append_paths = _prompts_mod.prompts_for_session(cwd_str, lib_id)
    except Exception:  # noqa: BLE001
        append_paths = None
    return _runner_tmux.TmuxClaudeRunner(
        session_id,
        pool=_orch._get_tmux_pool(),
        cwd=cwd_path,
        has_run_before=has_run_before,
        model=(model or None),
        append_system_prompt_paths=append_paths,
    )

# UUID validation for old_id; mirrors the pattern claude uses.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Target summary length suggested to the model (chars of markdown).
_SUMMARY_TARGET_CHARS = 2500

# Hard cap on how long we wait for a single SSE event from ClaudeRunner's
# broadcast queue. claude subprocess can hang on network/API freezes; without
# a timeout the compact endpoint blocks the FastAPI worker indefinitely.
# 5 min generously covers the largest historical sessions (≤90s observed).
_TURN_QUEUE_TIMEOUT_S = 300.0


def _extract_latest_task_list(session_id: str) -> list[dict]:
    """Walk session backwards for the most recent task snapshot.

    The orchestrator's claude harness uses the ``TodoWrite`` tool (single
    batch-replace API: each call carries the FULL todo list as
    ``input.todos``). The latest TodoWrite call's input is therefore the
    current task state — no need to look at tool_result.

    Returns only ``pending`` / ``in_progress`` tasks. Empty list when no
    TodoWrite ever ran.
    """
    parsed = jsonl_mod.read_session(session_id)
    if not parsed.get("ok"):
        return []
    messages = parsed.get("messages") or []
    # Walk backwards for the most recent TodoWrite tool_use.
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("blocks") or []:
            if block.get("kind") != "tool_use":
                continue
            if block.get("name") != "TodoWrite":
                continue
            todos = (block.get("input") or {}).get("todos") or []
            if not isinstance(todos, list):
                continue
            out: list[dict] = []
            for t in todos:
                if not isinstance(t, dict):
                    continue
                status = t.get("status")
                if status not in ("pending", "in_progress"):
                    continue
                subject = (t.get("content") or "").strip()
                if not subject:
                    continue
                out.append({
                    "subject": subject,
                    "status": status,
                    "description": (t.get("activeForm") or "").strip(),
                })
            return out
    return []


def _build_compact_prompt(active_tasks: list[dict]) -> str:
    """Compose the user turn we send to the OLD session asking for a summary."""
    parts: list[str] = [
        "We're about to compact this session into a fresh one. Produce a"
        " tight markdown summary of the conversation so far — do NOT include"
        " the original conversation verbatim, only a high-signal recap.",
        "",
        "Cover, in this order:",
        "1. The user's main goals for this session.",
        "2. Decisions made and why (link to alternatives considered if relevant).",
        "3. File paths / modules / services touched (full paths).",
        "4. Plan files referenced — full path under `~/.claude/plans/<name>.md`.",
        "5. Current state — exactly where we left off.",
        "6. Open questions or blockers.",
        "",
        f"Target length: ≤{_SUMMARY_TARGET_CHARS} chars of markdown content."
        " Use markdown headings, bullet lists, inline code for paths."
        " Emit a single markdown block — the dashboard will lift its content"
        " verbatim into the new session's seed message.",
    ]
    if active_tasks:
        # The orchestrator injects the authoritative todo list (read from
        # your most recent TodoWrite call) into the seed message itself —
        # no need to repeat it in the summary. Just nudge the model not to
        # spend prose on enumerating todos.
        parts.extend([
            "",
            "Note: active todos are forwarded to the forked session by the"
            " orchestrator separately — DON'T list them in the summary.",
        ])
    return "\n".join(parts)


def _format_seed_message(summary: str, tasks: list[dict], old_id: str) -> str:
    """Compose the user turn that seeds the new session.

    Layout: a single self-closing ``<compacted-from session_id=".."/>`` marker
    (peeled into a frontend pill by ``orchestrator_jsonl._split_compacted_from``)
    followed by clean markdown — the summary as a heading + body, and an
    "Aktywne zadania" section listing tasks the model is supposed to re-create
    via TodoWrite. No inner XML tags; the user sees natural prose, the model
    parses semantically per the ``## Compaction`` section in the system
    prompt (current ``PROMPT_VERSION = v11``).
    """
    parts = [
        f'<compacted-from session_id="{old_id}"/>',
        "",
        "## Sesja po kompakcji — kontekst",
        "",
        summary.strip(),
    ]
    if tasks:
        parts.extend([
            "",
            "### Aktywne zadania (do re-utworzenia przez TodoWrite na początku odpowiedzi)",
            "",
        ])
        for t in tasks:
            line = f"- **[{t['status']}]** {t['subject']}"
            if t.get("description"):
                line += f" — {t['description']}"
            parts.append(line)
    return "\n".join(parts)


# ── runner-driven helpers ─────────────────────────────────────────────


def _decode_sse_event(raw: bytes) -> tuple[str | None, dict | None]:
    """Extract (event_name, payload) from one SSE-framed bytes blob.

    The runner emits events as ``id: N\\nevent: <name>\\ndata: <json>\\n\\n``.
    Keepalive comments (``: ping``) and malformed frames return (None, None).
    """
    try:
        text = raw.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        return (None, None)
    event_name: str | None = None
    data_line: str | None = None
    for line in text.split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_line = line[len("data: "):]
    if event_name is None or data_line is None:
        return (None, None)
    try:
        payload = json.loads(data_line)
    except (json.JSONDecodeError, ValueError):
        return (event_name, None)
    if not isinstance(payload, dict):
        return (event_name, None)
    return (event_name, payload)


def _summary_from_blocks(blocks: list[dict]) -> str:
    """Concatenate markdown content from a structured_blocks payload."""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type") or block.get("kind")
        if kind == "markdown":
            text = block.get("content") or block.get("text") or ""
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


async def _run_summary_turn(old_id: str, prompt: str) -> str:
    """Drive a summary turn against the OLD session and return the summary text.

    We instantiate a ``ClaudeRunner`` directly (skipping the post-message
    validation path) and subscribe to its broadcast queue exactly as a frontend
    client would. ``ClaudeRunner._handle_assistant`` (orchestrator_runner.py:439)
    parses the envelope and broadcasts a ``structured_blocks`` event whenever a
    valid envelope arrives; we collect the latest such payload before the
    runner finalises (closes the queue with ``None``).

    Raises ``RuntimeError`` if no envelope was emitted (turn errored or
    layer-3 repair also failed).
    """
    # Match the handler-level check: a finished runner can still be in
    # _active_runs during the 60s reap grace window for resilient SSE
    # reconnects. Only refuse if it's actually still in-flight.
    existing = runner_mod._active_runs.get(old_id)
    if existing is not None and not existing._done.is_set():
        raise RuntimeError(f"session {old_id} has an in-flight turn; cancel first")
    # Explicitly evict the stale finished entry before overwriting so its
    # eventual `_reap` no-ops cleanly (it identity-checks `is self`) and any
    # broadcast machinery hanging off it stops being addressable through the
    # registry. Avoids orphaning subscribers added to the prior runner.
    if existing is not None:
        runner_mod._active_runs.pop(old_id, None)
    active = _make_compact_runner(old_id, has_run_before=True)
    runner_mod._active_runs[old_id] = active
    queue = active.subscribe()
    turn_task = asyncio.create_task(active.start_turn(prompt))

    latest_blocks: list[dict] | None = None
    fallback_text: str | None = None
    try:
        while True:
            # Hard cap so a hung subprocess (claude API freeze, network drop)
            # surfaces as a RuntimeError → 500 rather than a permanent block
            # on the FastAPI worker. 5 min covers the largest sessions we've
            # seen by 5×.
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=_TURN_QUEUE_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"summary turn timed out after {_TURN_QUEUE_TIMEOUT_S}s"
                    f" (no SSE events from claude subprocess)"
                ) from exc
            if evt is None:
                break
            name, payload = _decode_sse_event(evt)
            if payload is None:
                continue
            if name == "structured_blocks":
                blocks = payload.get("blocks")
                if isinstance(blocks, list):
                    latest_blocks = blocks
            elif name == "assistant_message":
                # Layer-3 fallback: raw text rescued from a broken envelope.
                # Only used if no structured_blocks ever arrive.
                blocks = payload.get("blocks")
                if isinstance(blocks, list):
                    text_parts = [
                        b.get("text") or b.get("content") or ""
                        for b in blocks
                        if isinstance(b, dict) and b.get("kind") in ("text", "markdown")
                    ]
                    joined = "\n\n".join(p for p in text_parts if isinstance(p, str) and p.strip())
                    if joined.strip():
                        fallback_text = joined.strip()
    finally:
        try:
            active.subscribers.remove(queue)
        except ValueError:
            pass
        # Cancel a still-running turn task (timeout path) before exiting so we
        # don't leak a zombie claude subprocess.
        if not turn_task.done():
            turn_task.cancel()
        try:
            await turn_task
        except (Exception, asyncio.CancelledError):
            pass

    if latest_blocks:
        summary = _summary_from_blocks(latest_blocks)
        if summary.strip():
            return summary
    if fallback_text:
        return fallback_text
    raise RuntimeError("summary turn produced no envelope")


async def _seed_new_session(seed_text: str, *, new_id: str | None = None) -> str:
    """Spawn a fresh ``claude --session-id <new>`` to write the new JSONL.

    We don't care about the model's reply content — the goal is just to make
    claude write the session file with the seed turn appended.

    ``new_id`` (kwarg-only): when provided, used as the session UUID instead
    of minting a fresh one. Lets callers (e.g. the demo-session script)
    pre-stage uploads against a known session id BEFORE the seed turn fires.
    """
    if new_id is None:
        new_id = str(uuid.uuid4())
    active = _make_compact_runner(new_id, has_run_before=False)
    runner_mod._active_runs[new_id] = active
    queue = active.subscribe()
    turn_task = asyncio.create_task(active.start_turn(seed_text))
    try:
        # Drain until done; we only care about completion, not content.
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=_TURN_QUEUE_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"seed turn timed out after {_TURN_QUEUE_TIMEOUT_S}s"
                    f" (no SSE events from claude subprocess)"
                ) from exc
            if evt is None:
                break
    finally:
        try:
            active.subscribers.remove(queue)
        except ValueError:
            pass
        if not turn_task.done():
            turn_task.cancel()
        try:
            await turn_task
        except (Exception, asyncio.CancelledError):
            pass
    return new_id


# ── public entrypoint ─────────────────────────────────────────────────


async def compact_session(old_id: str) -> dict:
    """Run summary turn → seed new session → update sidecars.

    Returns ``{"ok": True, "new_session_id": str, "n_tasks": int,
    "summary_chars": int}``.

    Raises:
        ValueError: ``old_id`` is not a valid UUID or has no JSONL.
        RuntimeError: summary turn failed or new session JSONL never appeared.
    """
    if not isinstance(old_id, str) or not _UUID_RE.match(old_id):
        raise ValueError(f"invalid session id: {old_id!r}")
    if not jsonl_mod.jsonl_path(old_id).exists():
        raise ValueError(f"session JSONL not found for {old_id}")

    active_tasks = _extract_latest_task_list(old_id)
    prompt = _build_compact_prompt(active_tasks)
    summary = await _run_summary_turn(old_id, prompt)
    seed_text = _format_seed_message(summary, active_tasks, old_id)
    new_id = await _seed_new_session(seed_text)

    # Verify the new JSONL was actually created — otherwise the seed turn
    # silently failed and we'd leave a dangling sidecar entry.
    if not jsonl_mod.jsonl_path(new_id).exists():
        raise RuntimeError(f"seed turn for {new_id} produced no JSONL")

    # Stamp sidecars. Old session's title is reused for the new session as
    # "Compact: <title>" so the user can find it in the list. Shared precedence
    # (issue #85): manual rename → native Claude Code ai-title → stored title →
    # first-message preview; id stub only when there's no title signal at all.
    old_meta = meta_mod.get_meta(old_id)
    summary_data = jsonl_mod._summary_for(jsonl_mod.jsonl_path(old_id))
    old_title = meta_mod.resolve_title(old_meta, summary_data) or old_id[:8]
    new_title = f"Compact: {old_title}"

    await meta_mod.set_meta(old_id, compacted_to=new_id)
    await meta_mod.set_meta(new_id, title=new_title, compacted_from=old_id)
    # Inherit the parent's --model override so a forked Opus chat keeps using
    # Opus for subsequent user-driven turns. ``""`` clears (CLI default) which
    # matches a parent with no override.
    inherited_model = old_meta.get("model")
    await meta_mod.set_meta(new_id, model=inherited_model if isinstance(inherited_model, str) else "")

    # Drop list cache so the new session shows up immediately.
    jsonl_mod.invalidate_cache()

    return {
        "ok": True,
        "new_session_id": new_id,
        "n_tasks": len(active_tasks),
        "summary_chars": len(summary),
    }
