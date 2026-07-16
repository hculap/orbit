"""Subscription-billed one-shot Claude turns via the interactive tmux pool.

Phase 1 of the subscription-only migration. :func:`run_oneshot` is the headless
analogue of a chat turn: it mints a throwaway session-id, spawns a pool slot
(INTERACTIVE → Max subscription, never ``claude -p`` / credit pool), pipes the
prompt, tails the native JSONL to turn completion, extracts the assistant text,
then releases the slot and deletes the throwaway JSONL so it never pollutes the
session list.

Backs the four ``-p`` one-shot callers (cron LLM fire, auto-titles, agent
identity, skill metadata) when their ``*_runner_mode`` flag is ``"interactive"``
(the default). The legacy ``-p`` path stays behind ``"programmatic"`` as a
documented rollback.

Output extraction has two modes (the runner exposes structured SSE blocks, not
raw stdout):
  * ``raw=False`` (default) — prose; ``code`` blocks are re-fenced ```` ```lang ````.
  * ``raw=True`` — verbatim text/markdown/code CONTENT joined without fences, for
    callers that parse a strict line layout (skills ``NAME:``/``ICON:``/``---``,
    identity ``ICON:``) where injected fence lines would break parsing.
"""
from __future__ import annotations

import asyncio
import json as _json
import uuid
from pathlib import Path
from typing import Any

from . import orchestrator_env as env_mod

# Inner JSONL-tail cap inside TmuxClaudeRunner is 600s; keep the default here
# at/under that so the caller's timeout governs (callers pass their own).
DEFAULT_TIMEOUT_S: float = 600.0
# Hard cap on the post-timeout teardown (cancel + pool.release) so run_oneshot
# always returns within ~timeout_s + this, even under a contended box where the
# tmux /exit + grace + kill stalls.
_TEARDOWN_TIMEOUT_S: float = 15.0

# Prepended to a one-shot prompt so claude knows it has no human to ask — a
# clarifying AskUserQuestion in a headless run would otherwise hang until the
# turn timeout. Kept short; callers' prompts already expect a direct answer.
_HEADLESS_PREAMBLE: str = (
    "[Non-interactive headless run: there is no human to answer questions. "
    "Never ask a clarifying question — make a best-effort decision and "
    "produce the requested output directly.]\n\n"
)


async def run_oneshot(
    prompt: str,
    *,
    cwd: Path | None = None,
    model: str | None = None,
    append_system_prompt_paths: list[Path] | None = None,
    agent_skills_dir: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    raw: bool = False,
    headless_hint: bool = True,
    require_text: bool = True,
    label: str = "oneshot",
) -> dict[str, Any]:
    """Run one ephemeral, subscription-billed Claude turn through the tmux pool.

    Returns ``{"ok": bool, "text": str, "error": str | None}``. Never raises for
    a model/runtime failure — the failure is reported in ``error``. NEVER falls
    back to ``claude -p`` (that would hit the credit pool); the caller decides
    whether to retry or surface the error.

    ``require_text`` (default ``True``): when ``True`` an empty assistant reply
    is itself a failure (the title/identity/skill callers NEED the text). Pass
    ``False`` for cron, where a turn that did its work via tools and produced no
    prose is still a SUCCESS — keying ``ok`` on ``bool(text)`` there would flag
    a working scheduled job as FAILED.

    Scope secrets: any ``<cwd>/.env`` is parsed and layered into the spawned
    claude's env (scrubbed of billing keys) so the interactive path reaches the
    SAME dashboard-managed secrets the legacy ``-p`` path injected.
    """
    # Imported lazily to avoid an import cycle (orchestrator imports this module
    # transitively via the routes that call the migrated one-shots).
    from . import orchestrator as _orch
    from . import orchestrator_runner_tmux as _runner_tmux

    bootstrap_sid = str(uuid.uuid4())  # fresh → never resumes, isolated context
    cwd_path = Path(cwd) if cwd is not None else Path.home()
    full_prompt = (_HEADLESS_PREAMBLE + prompt) if headless_hint else prompt
    env_extra = env_mod.scope_env_values(cwd_path) or None

    pool = _orch._get_tmux_pool()
    runner = _runner_tmux.TmuxClaudeRunner(
        session_id=bootstrap_sid,
        pool=pool,
        cwd=cwd_path,
        append_system_prompt_paths=append_system_prompt_paths,
        agent_skills_dir=agent_skills_dir,
        model=(model.strip() if isinstance(model, str) and model.strip() else None),
        env_extra=env_extra,
    )

    env_mod.log_billing_path(f"{label} sid={bootstrap_sid[:8]}", interactive=True)
    text, error = "", None
    # IMPORTANT: do NOT `asyncio.wait_for(start_turn, …)`. start_turn catches
    # CancelledError (it absorbs the cancel wait_for injects) while its inner
    # _acquire_task/_tail_task keep running — so wait_for's cancel-and-await
    # tangles and the call hung >130s past its timeout under load (UAT). Use
    # asyncio.wait (never cancels the task) + the runner's DESIGNED cancel()
    # (which cancels the inner tasks → start_turn wakes promptly), all bounded.
    turn = asyncio.create_task(runner.start_turn(full_prompt))
    try:
        done, _pending = await asyncio.wait({turn}, timeout=timeout_s)
        if turn in done:
            exc = turn.exception()
            if exc is not None:
                error = f"interactive runner raised: {exc}"
            else:
                text = extract_runner_text(runner, raw=raw)
        else:
            error = f"oneshot timed out after {timeout_s}s"
            try:
                await asyncio.wait_for(runner.cancel(), timeout=_TEARDOWN_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                pass
            # Bounded-wait for start_turn to unwind (asyncio.wait never raises
            # on timeout; it does NOT cancel the task — cancel() above did).
            await asyncio.wait({turn}, timeout=_TEARDOWN_TIMEOUT_S)
    finally:
        if not turn.done():
            turn.cancel()
        # BOUND the slot teardown — pool.release() (tmux /exit + grace + kill)
        # can stall under a contended box; leak-then-reap beats hanging.
        try:
            await asyncio.wait_for(pool.release(bootstrap_sid), timeout=_TEARDOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass
        delete_bootstrap_jsonl(bootstrap_sid, cwd_path)

    if error is None and not text:
        # Distinguish a real runner error (surface it) from a legitimately
        # text-less turn. Only treat empty output as a failure when the caller
        # actually needs text (require_text); for cron a tool-only turn is a
        # success and must not be stamped FAILED.
        runner_err = runner_error_message(runner)
        if runner_err:
            error = runner_err
        elif require_text:
            error = "interactive runner produced no output"
    return {"ok": error is None, "text": text, "error": error}


# ── SSE-buffer extraction (shared with cron) ─────────────────────────


def decode_sse_frame(evt: bytes) -> tuple[str | None, dict[str, Any] | None]:
    """Decode one buffered SSE frame → ``(event_name, payload_dict)``.

    A frame is ``b"id: N\\nevent: <name>\\ndata: <json>\\n\\n"``. Returns
    ``(None, None)`` on any malformation (missing header, non-JSON, non-dict
    payload). The single decoder shared by :func:`_iter_block_events`,
    :func:`runner_error_message`, and ``orchestrator_runner._notification_title_body``
    so the SSE envelope shape lives in exactly one place.
    """
    _, _, rest = evt.partition(b"\n")
    event_line, _, rest = rest.partition(b"\n")
    if not event_line.startswith(b"event: "):
        return None, None
    name = event_line[len(b"event: "):].decode("ascii", errors="replace")
    data_line, _, _ = rest.partition(b"\n")
    if not data_line.startswith(b"data: "):
        return None, None
    try:
        payload = _json.loads(data_line[len(b"data: "):])
    except (_json.JSONDecodeError, ValueError):
        return None, None
    return name, (payload if isinstance(payload, dict) else None)


def _iter_block_events(runner) -> list[dict[str, Any]]:
    """Decode the runner's buffered SSE frames → list of structured payloads
    (most recent first), filtered to block-carrying events."""
    out: list[dict[str, Any]] = []
    for evt in reversed(getattr(runner, "_buffered_events", []) or []):
        name, payload = decode_sse_frame(evt)
        if payload is not None and name in ("structured_blocks", "assistant_message"):
            out.append(payload)
    return out


def _block_text_parts(payload: dict[str, Any], *, raw: bool) -> list[str]:
    """Pull usable text out of one block-carrying payload.

    ``raw=False`` re-fences ``code`` blocks and surfaces an end-of-turn
    ``choice``/``ask`` prompt (prose display, e.g. cron). ``raw=True`` joins
    text/markdown/code CONTENT verbatim and DROPS ``choice``/``ask`` — a
    clarifying question must never reach a strict-layout parser (skill
    ``NAME:``/``ICON:``/``---``, identity ``ICON:``) as if it were the answer.
    """
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return []
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        kind = b.get("kind") or b.get("type")
        if kind in ("markdown", "text"):
            t = b.get("content") or b.get("text") or ""
            if isinstance(t, str) and t.strip():
                parts.append(t)
        elif kind == "code":
            t = b.get("content") or ""
            if isinstance(t, str) and t.strip():
                if raw:
                    parts.append(t)
                else:
                    lang = b.get("lang") if isinstance(b.get("lang"), str) else ""
                    parts.append(f"```{lang}\n{t}\n```")
        elif kind in ("choice", "ask") and not raw:
            prompt_text = b.get("prompt") or ""
            if isinstance(prompt_text, str) and prompt_text.strip():
                parts.append(prompt_text)
    return parts


def extract_runner_text(runner, *, raw: bool = False) -> str:
    """Assistant text from the runner's buffered SSE blocks.

    ``raw=False`` (prose, e.g. cron) returns the MOST-RECENT block event's text,
    re-fencing ``code`` blocks — mirrors ``claude -p --output-format text``'s
    final-answer semantics.

    ``raw=True`` (strict-layout parsers: identity ``ICON:``, skill
    ``NAME:``/``ICON:``/``---``) concatenates the text of ALL block events of the
    one-shot turn in chronological order. An agentic turn can emit the
    strict-format answer and THEN a closing remark ("Done, created it.") in a
    later assistant_message; returning only the most-recent event would hand the
    parser the remark and drop the answer. The parsers scan for their markers,
    so a benign preamble/remark around the answer is tolerated — losing the
    answer is not.
    """
    events = _iter_block_events(runner)  # most-recent first
    if raw:
        parts: list[str] = []
        for payload in reversed(events):  # chronological
            parts.extend(_block_text_parts(payload, raw=True))
        return "\n\n".join(parts).strip()
    for payload in events:
        parts = _block_text_parts(payload, raw=False)
        if parts:
            return "\n\n".join(parts).strip()
    return ""


def runner_error_message(runner) -> str | None:
    """Message from the runner's most recent ``error`` SSE event, or None."""
    for evt in reversed(getattr(runner, "_buffered_events", []) or []):
        name, payload = decode_sse_frame(evt)
        if name != "error" or payload is None:
            continue
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg
    return None


def delete_bootstrap_jsonl(session_id: str, cwd: Path) -> None:
    """Wipe the ephemeral one-shot transcript so it never pollutes the session
    list. Uses the CANONICAL ``slug_for_cwd`` (via ``jsonl_path_for``) — claude
    slugs ``/``, ``_`` AND ``.`` to ``-``, so a hand-rolled ``/``-only replace
    misses the file for any cwd containing ``_``/``.`` (e.g. real PARA dirs)."""
    from . import orchestrator_jsonl_tail as tail_mod
    targets = {
        tail_mod.jsonl_path_for(cwd, session_id),
        tail_mod.jsonl_path_for(Path.home(), session_id),
    }
    for target in targets:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
