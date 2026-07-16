"""Watchdog: push notifications + dedicated 'Cron alerts' Global session.

Two channels fire on a failed run:

1. ``orchestrator_notifications.send_to_all`` PWA push (best-effort;
   pruned on dead endpoints automatically). Desktop browsers / installed
   PWA clients receive this.
2. A structured envelope appended to the long-lived Global "Cron alerts"
   session — the user can later open ``/chat/<sid>`` and ask the agent
   ("co dziś padło?") to summarize. Session id persisted in the cron
   sidecar (``alerts_meta.json``); created on first failure.

Mobile push (Telegram) is NOT fired from here — it goes through
``cron_scheduler._maybe_notify_run`` which is the single source of truth
for run-status-driven mobile pushes (failure-by-default for jobs without
an explicit ``notify`` field, or per the user's ``notify.on`` allowlist).
Routing both paths to ``_maybe_notify_run`` avoids the double-ping that
showed up when both layers fired independently.
"""
from __future__ import annotations
import json
import sys
import time
from typing import Any

from . import cron_store as store
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from . import orchestrator_notifications as notifs


_ALERTS_TITLE: str = "Cron alerts"


def _warn(msg: str) -> None:
    print(f"[cron_alerts] {msg}", file=sys.stderr)


async def report_failure(job: dict, run: dict) -> None:
    """Fire the watchdog channels for a failed run. Never raises.

    Mobile (Telegram) push is intentionally NOT fired here — see the
    module docstring for why; ``cron_scheduler._maybe_notify_run`` owns
    that path so configurations don't double-fire.
    """
    try:
        await _send_push(job, run)
    except Exception as exc:
        _warn(f"push failed: {exc}")
    try:
        sid = await _ensure_alerts_session()
    except Exception as exc:
        _warn(f"_ensure_alerts_session failed: {exc}")
        return
    try:
        await _inject_alert(sid, job, run)
    except Exception as exc:
        _warn(f"_inject_alert failed: {exc}")


# ── push ────────────────────────────────────────────────────────


async def _send_push(job: dict, run: dict) -> None:
    name = job.get("name") or job.get("id") or "(unknown)"
    err = run.get("error") or f"exit_code={run.get('exit_code')}"
    body = f"{name}: {err}"[:500]
    data = {
        "type": "cron_failure",
        "job_id": job.get("id"),
        "run_id": run.get("run_id"),
    }
    await notifs.send_to_all("Cron job failed", body, data)


# ── alerts session ──────────────────────────────────────────────


async def _ensure_alerts_session() -> str:
    """Return the Global 'Cron alerts' session id; create on first call.

    Persists the id in ``~/.orchestrator/cron/alerts_meta.json`` so we
    don't keep creating new sessions on every failure. Validates the
    persisted sid still has either a JSONL or a sidecar entry — if the
    user manually deleted the session we transparently create a new one.
    """
    cached = store.get_alerts_session_id()
    if cached and _session_alive(cached):
        return cached

    from . import orchestrator as orch_mod  # local import: avoid circular

    payload = {"title": _ALERTS_TITLE}
    result = await orch_mod._create_session_handler(payload)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"create_session refused: {result!r}")
    sid = result.get("id")
    if not isinstance(sid, str) or not sid:
        raise RuntimeError("create_session returned no id")
    store.set_alerts_session_id(sid)
    return sid


def _session_alive(session_id: str) -> bool:
    try:
        if jsonl_mod.jsonl_path(session_id).exists():
            return True
    except Exception:
        pass
    try:
        meta = meta_mod.get_meta(session_id)
    except Exception:
        return False
    return bool(meta) and any(meta.get(k) for k in ("title", "cwd", "lib_id"))


# ── envelope formatting ─────────────────────────────────────────


def _format_failure_envelope(job: dict, run: dict) -> str:
    """Format a failure as the orchestrator JSON envelope.

    Two blocks: a markdown summary + a fenced JSON block carrying the raw
    run record so a future agent turn can parse and reason about it.
    """
    name = job.get("name") or job.get("id") or "(unknown)"
    job_id = job.get("id")
    run_id = run.get("run_id")
    started_iso = _iso(run.get("started_at"))
    finished_iso = _iso(run.get("finished_at"))
    duration_ms = run.get("duration_ms")
    exit_code = run.get("exit_code")
    error = run.get("error") or "(no error message)"
    stderr_tail = run.get("stderr_tail") or ""

    md_lines: list[str] = [
        f"## Cron failure · {name}",
        "",
        f"- **job_id**: `{job_id}`",
        f"- **run_id**: `{run_id}`",
        f"- **started**: {started_iso}",
        f"- **finished**: {finished_iso} ({duration_ms} ms)",
        f"- **exit_code**: `{exit_code}`",
        f"- **error**: {error}",
    ]
    if stderr_tail.strip():
        md_lines.extend(["", "**stderr (tail)**:", "```", stderr_tail, "```"])

    raw_block = json.dumps(
        {"job_id": job_id, "run_id": run_id, "run": run},
        ensure_ascii=False,
        indent=2,
    )
    envelope: dict[str, Any] = {
        "blocks": [
            {"type": "markdown", "text": "\n".join(md_lines)},
            {"type": "code", "language": "json", "text": raw_block},
        ]
    }
    return json.dumps(envelope, ensure_ascii=False)


def _iso(ts: Any) -> str:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v))


async def _inject_alert(session_id: str, job: dict, run: dict) -> None:
    """Append a turn into the alerts session via post_message_handler."""
    from . import orchestrator as orch_mod  # local import: avoid circular

    name = job.get("name") or job.get("id") or "(unknown)"
    error = run.get("error") or f"exit_code={run.get('exit_code')}"
    body = (
        f"Cron job **{name}** failed at {_iso(run.get('finished_at'))}: {error}\n\n"
        f"run_id: `{run.get('run_id')}`"
    )
    if run.get("stderr_tail"):
        body += f"\n\n```\n{run['stderr_tail']}\n```"
    await orch_mod._post_message_handler(session_id, {"text": body})
